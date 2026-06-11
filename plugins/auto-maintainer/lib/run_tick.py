#!/usr/bin/env python3
"""run_tick — the deterministic single-tick runner for the maintainer loop.

One invocation = one tick. This is the script-tier core of the scheduling
feature (spec-rules §1): it assembles the real lifecycle-core route

    GUARD -> DRAIN -> PULL -> PERSIST -> EXIT

over the already-implemented anchors and runs it through tick-orchestrator:

  - GUARD / EXIT come from lifecycle-dispositions (entry gate + single-writer
    mutex; terminal disposition selection + mutex release);
  - DRAIN / PERSIST come from durable-state (crash-recovery replay; durable
    flush);
  - PULL comes from work-intake (the GitHub-Issues adapter): each tick fetches
    the repo's OPEN issues into the `work_items` slot. This is the first real
    maintainer work, retiring the slice-1 DEMO_WORK stub.

Read-and-idle (spec slice 2): with only a read stage and no act stage yet, the
tick seeds the EXIT outcome to "empty" so EXIT selects IDLE rather than refire.
A pure read produces nothing to act on, so refiring would busy-loop re-pulling
the same issues; the heartbeat re-pulls on the next interval instead. EXIT's
refire/idle becomes work-driven again once an act stage lands.

PULL's issue source is the live `gh` CLI in production but is INJECTABLE so tests
pass a stub over fixture issues (no network) — the determinism seam.

The loop MECHANICS are real (mutex, journal, durable persisted state,
disposition transitions, DRAIN crash-recovery). A pure read records no mutation
intent, so DRAIN has nothing to reconcile and is a no-op for PULL — expected.

Runtime paths (durable state, journal, disposition + lock markers) are injected
so tests use a temp dir and the on-disk files are the only source of truth.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state,
lifecycle-dispositions, and work-intake UNCHANGED; it never edits or forks them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the tick interval/route become
  config-driven and this slice's hardcoding is removed.
"""

import os
import sys

# Consume the sibling features via sys.path, exactly as the other feature
# sources/tests do. Resolve them relative to this file's feature dir so the
# runner works both in the worktree and from the shipped plugin layout.
_SRC = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402


# The production PULL issue source: work-intake's live `gh` CLI adapter. Tests
# inject a stub instead so the suite touches no network; the shipped run_tick
# (no injected source) pulls real open issues.
DEFAULT_PULL_SOURCE = wi.gh_issue_source

# The durable-state document key under which the last pull's work_items snapshot
# is persisted, so status.py can report the pulled count without re-pulling.
WORK_ITEMS_KEY = "work_items"


# --------------------------------------------------------------------------
# The route: GUARD -> DRAIN -> PULL -> PERSIST -> EXIT (+ HALTED terminal).
# Routing is DATA (fsm-contracts / tick-orchestrator); no state names a
# successor. GUARD's latch/restart signals short-circuit to the HALTED terminal
# so a STOPPED/ABORTED/RESTART_NEEDED tick ends WITHOUT running PULL and WITHOUT
# EXIT clobbering the latched disposition.
# --------------------------------------------------------------------------

# DONE is the true terminal: tick-orchestrator HALTS the moment it reaches a
# terminal state and NEVER run()s it, so EXIT must be a NON-terminal state that
# the loop actually runs (selecting the disposition + releasing the mutex). Its
# disposition signals (refire/idle/break) all route to DONE. HALTED is the
# short-circuit terminal for GUARD's latch/restart path.
_DONE = "DONE"
_HALTED = "HALTED"

ROUTE = {
    "states": ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT", _DONE, _HALTED],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": _HALTED},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": _HALTED},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "PERSIST"},
        {"state": "PULL", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": _DONE},
        {"state": "EXIT", "signal": "idle", "next": _DONE},
        {"state": "EXIT", "signal": "break", "next": _DONE},
        {"state": "EXIT", "signal": "halt", "next": _DONE},
    ],
    "terminal": [_DONE, _HALTED],
}

# The closed signal vocabulary spanning every state in the route.
_VOCAB = fc.SignalVocabulary([
    "OK", "EMPTY", "HALT_REQUESTED", "RESTART_REQUIRED",
    "refire", "idle", "break", "halt",
])


def _build_states(runtime_dir, source):
    """Map each route state -> (manifest, run_callable). GUARD/EXIT bind the
    injected runtime_dir; DRAIN/PERSIST read injected slots; PULL binds the
    injectable issue source (default = work-intake's live gh source)."""
    guard = ld.Guard(runtime_dir)
    exit_state = ld.Exit(runtime_dir)
    pull = wi.Pull(source=source)
    return {
        "GUARD": (guard.manifest, guard.run),
        "DRAIN": (ds.DRAIN_MANIFEST, ds.drain_run),
        "PULL": (wi.PULL_MANIFEST, pull.run),
        "PERSIST": (ds.PERSIST_MANIFEST, ds.persist_run),
        "EXIT": (exit_state.manifest, exit_state.run),
    }


def _seed_context(state_path, journal_path):
    """A TickContext seeded from durable state plus the read-and-idle outcome.

    Registers the durable-state plumbing slots (counter/state_path/journal_path)
    that DRAIN/PERSIST read, the work_items slot PULL writes, and the
    tick_outcome slot EXIT reads. tick_outcome is seeded "empty" so EXIT selects
    IDLE after the pull (read-and-idle): a pure read has no act stage, so the
    loop must idle rather than busy-refire. The slot is always written before
    EXIT reads it; the GUARD short-circuit path halts at HALTED without running
    EXIT.
    """
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    ctx.write("counter", ds.DurableState(state_path).load()["counter"])
    # Read-and-idle: no act stage, so the tick always idles after the pull.
    ctx.write("tick_outcome", "empty")
    return ctx


def resolve_runtime_paths():
    """Resolve the default runtime paths for an INVOCATION WITH NO INJECTED
    PATHS — the installed-plugin case where the skill runs run_tick with cwd =
    the user's project and no temp dir is wired in.

    The durable-state / journal / disposition+lock markers default to a writable
    per-project runtime dir: ``${CLAUDE_PROJECT_DIR}/.auto-maintainer/`` when
    that env var is set, else ``.auto-maintainer/`` under cwd. Tests inject a
    temp dir instead, so this default is used only when nothing is injected.
    Returns ``(runtime_dir, state_path, journal_path)``.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return runtime_dir, state_path, journal_path


def persisted_work_items(state_path):
    """The last pull's work_items snapshot persisted in durable state (a list of
    WorkItem dicts), or [] when the loop never ran a pull."""
    doc = ds.DurableState(state_path).load()
    return doc.get(WORK_ITEMS_KEY, [])


def persisted_work_items_count(state_path):
    """The count of work_items pulled by the last tick, from durable state."""
    return len(persisted_work_items(state_path))


def run_tick(runtime_dir=None, state_path=None, journal_path=None,
             source=None, return_run_result=False):
    """Run exactly ONE tick of the real PULL loop and return the EXIT
    disposition signal (or the raw RunResult when return_run_result=True).

    Assembles the route + states map, seeds a TickContext from durable state,
    and runs tick_orchestrator.run(...). On the clean path PULL fetches the
    repo's open issues into work_items, PERSIST flushes durable state, and EXIT
    selects IDLE (read-and-idle) + releases the mutex; "idle" is returned. On
    the GUARD short-circuit path the run halts at HALTED with the latched
    disposition untouched and "halt" is returned (PULL never runs).

    After a clean tick, the pulled work_items are persisted into durable state so
    status.py can report the count without re-pulling.

    Injected paths win: when any of runtime_dir/state_path/journal_path is None
    (the installed case, where the skill invokes run_tick with no wiring) the
    missing ones fall back to resolve_runtime_paths(). `source` is the injectable
    PULL issue source (callable(repo) -> list[WorkItem]); when None it defaults
    to work-intake's live gh source, so the shipped run_tick pulls real issues.

    Prints a one-line tick trace (state path, work_items count, disposition).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal
    if source is None:
        source = DEFAULT_PULL_SOURCE
    ctx = _seed_context(state_path, journal_path)
    states = _build_states(runtime_dir, source)
    result = to.run(ROUTE, states, ctx, _VOCAB, start="GUARD")

    if result.final_state == _DONE:
        # EXIT ran and emitted the disposition-selecting signal last. Persist
        # the pulled work_items snapshot so status can report the count.
        signal = result.signals[-1]
        doc = ds.DurableState(state_path).load()
        doc[WORK_ITEMS_KEY] = ctx.read(wi.WORK_ITEMS_SLOT["name"])
        ds.DurableState(state_path).save(doc)
    else:
        # GUARD short-circuited (STOPPED/ABORTED/RESTART_NEEDED): the tick did
        # no work (PULL never ran); map the halting condition to "halt".
        signal = "halt"

    disposition = ld.read_disposition(runtime_dir)
    work_items_count = persisted_work_items_count(state_path)
    sys.stdout.write(
        f"[tick] path={'->'.join(result.path)} work_items={work_items_count} "
        f"disposition={disposition} signal={signal}\n")

    if return_run_result:
        return result
    return signal


if __name__ == "__main__":
    # Production entrypoint: the scheduling skills invoke this once per tick from
    # the installed plugin with no path wiring and no injected source. run_tick
    # defaults its durable file locations to the writable per-project runtime dir
    # (${CLAUDE_PROJECT_DIR}/.auto-maintainer/ else .auto-maintainer/ under cwd)
    # via resolve_runtime_paths(), and pulls real open issues via the live gh
    # source.
    run_tick()
