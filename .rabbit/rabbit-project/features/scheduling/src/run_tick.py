#!/usr/bin/env python3
"""run_tick — the deterministic single-tick runner for the maintainer loop.

One invocation = one tick. This is the script-tier core of the scheduling
feature (spec-rules §1). Slice 3 makes the route DATA: instead of hardcoding the
state graph, run_tick defines the shipped DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP and
hands them to adapter-wiring's build_loop, which loads a project-local override
(else the default), resolves each routed port to its built-in adapter via the
factory convention, validates the wiring at LOAD, and returns (route, states)
ready for tick_orchestrator.run.

The shipped DEFAULT_ROUTE is the read-and-idle spine

    GUARD -> DRAIN -> PULL -> PERSIST -> EXIT

over the already-implemented anchors:

  - GUARD / EXIT come from lifecycle-dispositions (entry gate + single-writer
    mutex; terminal disposition selection + mutex release);
  - DRAIN / PERSIST come from durable-state (crash-recovery replay; durable
    flush);
  - PULL / TRIAGE come from work-intake (PULL writes work_items; TRIAGE gates
    them into work_orders). TRIAGE is in DEFAULT_ADAPTER_MAP — resolvable — even
    though the default route omits it, so inserting it is a pure route.json edit.

Route-as-data override: a project-local
``${project_dir}/.auto-maintainer/route.json`` (and optional
``adapter-map.json``) overrides the defaults. Inserting TRIAGE between PULL and
PERSIST is a DATA edit; adapter-wiring resolves it from the map and validates it
at load. A broken override (e.g. TRIAGE before PULL, or an unknown port) raises a
locatable WiringError BEFORE any tick body runs.

Read-and-idle (spec slice 2): with only read stages and no act stage yet, the
tick seeds the EXIT outcome to "empty" so EXIT selects IDLE rather than refire.
A pure read produces nothing to act on, so refiring would busy-loop; the
heartbeat re-pulls on the next interval instead. EXIT's refire/idle becomes
work-driven again once an act stage lands.

PULL's issue source is the live `gh` CLI in production but is INJECTABLE so tests
pass a stub over fixture issues (no network) — the determinism seam.

Runtime paths (durable state, journal, disposition + lock markers) are injected
so tests use a temp dir and the on-disk files are the only source of truth.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state,
lifecycle-dispositions, work-intake, and adapter-wiring UNCHANGED; it never edits
or forks them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the tick interval becomes
  config-driven and this slice's hardcoding is removed.
"""

import os
import sys

# Consume the sibling features via sys.path, exactly as the other feature
# sources/tests do. Resolve them relative to this file's feature dir so the
# runner works both in the worktree and from the shipped plugin layout. In the
# installed plugin `lib/` the modules are flat siblings of this file (already on
# the path once _SRC is inserted).
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402
import adapter_wiring as aw  # noqa: E402


# The production PULL issue source: work-intake's live `gh` CLI adapter. Tests
# inject a stub instead so the suite touches no network; the shipped run_tick
# (no injected source) pulls real open issues.
DEFAULT_PULL_SOURCE = wi.gh_issue_source

# The durable-state document keys under which the last tick's pulled work_items
# (and triaged work_orders, when the active route produced them) are persisted,
# so status.py can report the counts without re-running the loop.
WORK_ITEMS_KEY = "work_items"
WORK_ORDERS_KEY = "work_orders"

# DONE is the true terminal: tick-orchestrator HALTS the moment it reaches a
# terminal state and NEVER run()s it, so EXIT must be a NON-terminal state that
# the loop actually runs (selecting the disposition + releasing the mutex). Its
# disposition signals (refire/idle/break/halt) all route to DONE. HALTED is the
# short-circuit terminal for GUARD's latch/restart path.
_DONE = "DONE"
_HALTED = "HALTED"


# --------------------------------------------------------------------------
# Built-in adapter factories (the adapter-wiring factory convention).
#
# Each factory(runtime) -> (StateManifest, run_callable) WRAPS the existing
# sibling adapter UNCHANGED. `runtime` is a dict carrying the resolved
# runtime_dir (for GUARD/EXIT marker I/O), the injectable PULL source, and the
# TRIAGE reference time. adapter-wiring resolves a routed port to its factory by
# the "run_tick:make_<port>" address in DEFAULT_ADAPTER_MAP.
# --------------------------------------------------------------------------

def make_guard(runtime):
    """GUARD anchor (lifecycle-dispositions): entry gate + single-writer mutex,
    bound to the runtime marker dir."""
    guard = ld.Guard(runtime["runtime_dir"])
    return guard.manifest, guard.run


def make_exit(runtime):
    """EXIT anchor (lifecycle-dispositions): tick-outcome -> disposition +
    signal, releasing the mutex, bound to the runtime marker dir."""
    exit_state = ld.Exit(runtime["runtime_dir"])
    return exit_state.manifest, exit_state.run


def make_drain(runtime):  # noqa: ARG001 - DRAIN reads its paths from ctx slots
    """DRAIN anchor (durable-state): crash-recovery replay. Reads its file paths
    from the seeded TickContext slots, so the runtime carries no extra config."""
    return ds.DRAIN_MANIFEST, ds.drain_run


def make_persist(runtime):  # noqa: ARG001 - PERSIST reads its paths from ctx
    """PERSIST anchor (durable-state): durable flush. Reads its file paths from
    the seeded TickContext slots."""
    return ds.PERSIST_MANIFEST, ds.persist_run


def make_pull(runtime):
    """PULL adapter (work-intake): fetch OPEN issues into the work_items slot.
    Binds the injectable issue source (default = work-intake's live gh source)."""
    source = runtime.get("source") or DEFAULT_PULL_SOURCE
    pull = wi.Pull(source=source)
    return wi.PULL_MANIFEST, pull.run


def make_triage(runtime):
    """TRIAGE adapter (work-intake): the deterministic validity gate mapping
    work_items -> work_orders. Binds the injectable reference time (default =
    now), so staleness is deterministic under test."""
    triage = wi.Triage(now=runtime.get("now"))
    return wi.TRIAGE_MANIFEST, triage.run


# --------------------------------------------------------------------------
# The shipped DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP (route-as-data).
#
# DEFAULT_ROUTE is the read-and-idle spine GUARD->DRAIN->PULL->PERSIST->EXIT
# plus the DONE/HALTED terminals — matching the prior hardcoded behaviour.
# DEFAULT_ADAPTER_MAP maps every KNOWN port (incl. TRIAGE) + the terminals to a
# "run_tick:make_<port>" factory address, so a route.json edit inserting TRIAGE
# wires it with NO code change.
# --------------------------------------------------------------------------

DEFAULT_ROUTE = {
    "schema_version": "1.0.0",
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

# Every known port -> its built-in factory address. TRIAGE is included even
# though DEFAULT_ROUTE omits it (the ports-and-adapters promise: insert by data).
# The terminals are addressed too so adapter-wiring can resolve every state in a
# route (terminals never run(), but their manifests must resolve for validation).
DEFAULT_ADAPTER_MAP = {
    "GUARD": "run_tick:make_guard",
    "DRAIN": "run_tick:make_drain",
    "PULL": "run_tick:make_pull",
    "TRIAGE": "run_tick:make_triage",
    "PERSIST": "run_tick:make_persist",
    "EXIT": "run_tick:make_exit",
    _DONE: "run_tick:make_terminal",
    _HALTED: "run_tick:make_terminal",
}


def make_terminal(runtime):  # noqa: ARG001 - terminals never run()
    """A terminal anchor (DONE/HALTED): resolvable so the validator sees its
    (empty) manifest, but it never run()s — tick-orchestrator halts on reaching
    a terminal state."""
    manifest = fc.StateManifest(reads=[], writes=[], emits=[])

    def _run(ctx):  # pragma: no cover - terminal states never execute
        raise ld.LifecycleError("terminal state must never run()")

    return manifest, _run


# The closed signal vocabulary spanning every state in the route. It is a
# superset covering both the default spine and TRIAGE's OK/EMPTY signals.
_VOCAB = fc.SignalVocabulary([
    "OK", "EMPTY", "HALT_REQUESTED", "RESTART_REQUIRED",
    "refire", "idle", "break", "halt",
])

# Slots seeded into the TickContext BEFORE the route runs (the data-readiness
# `initial` set). PULL writes work_items, TRIAGE writes work_orders — those are
# produced by predecessors, not seeded.
_INITIAL_SLOTS = ["state_path", "journal_path", "counter", "tick_outcome"]


def _seed_context(state_path, journal_path, route):
    """A TickContext seeded from durable state plus the read-and-idle outcome.

    Registers the durable-state plumbing slots (counter/state_path/journal_path)
    that DRAIN/PERSIST read, the work_items slot PULL writes, the tick_outcome
    slot EXIT reads, and — when the active route includes TRIAGE — the
    work_orders slot TRIAGE writes. tick_outcome is seeded "empty" so EXIT
    selects IDLE after the read stages (read-and-idle).
    """
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])
    if "TRIAGE" in route["states"]:
        ctx.register_slot(
            wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
            version=wi.WORK_ORDERS_SLOT["version"])
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    ctx.write("counter", ds.DurableState(state_path).load()["counter"])
    # Read-and-idle: no act stage, so the tick always idles after the reads.
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


def _resolve_project_dir():
    """The project dir adapter-wiring reads the override config from: the
    CLAUDE_PROJECT_DIR env var when set, else cwd (the same anchor
    resolve_runtime_paths uses)."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def persisted_work_items(state_path):
    """The last pull's work_items snapshot persisted in durable state (a list of
    WorkItem dicts), or [] when the loop never ran a pull."""
    doc = ds.DurableState(state_path).load()
    return doc.get(WORK_ITEMS_KEY, [])


def persisted_work_items_count(state_path):
    """The count of work_items pulled by the last tick, from durable state."""
    return len(persisted_work_items(state_path))


def persisted_work_orders(state_path):
    """The last tick's work_orders snapshot persisted in durable state (a list of
    WorkOrder dicts), or [] when the active route produced none (e.g. the default
    route has no TRIAGE)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(WORK_ORDERS_KEY, [])


def persisted_work_orders_count(state_path):
    """The count of work_orders the last tick produced, from durable state."""
    return len(persisted_work_orders(state_path))


def run_tick(runtime_dir=None, state_path=None, journal_path=None,
             project_dir=None, source=None, now=None, return_run_result=False):
    """Run exactly ONE tick of the maintainer loop and return the EXIT
    disposition signal (or the raw RunResult when return_run_result=True).

    Resolves the runtime, hands DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP to
    adapter_wiring.build_loop (which loads a project-local override else the
    default, resolves each routed port to its built-in factory, and validates the
    wiring at LOAD), seeds a TickContext from durable state, and runs
    tick_orchestrator.run(...). A broken override route raises WiringError BEFORE
    any tick body runs.

    On the clean path the route's read stages run (PULL fetches the repo's open
    issues into work_items; TRIAGE, when present, gates them into work_orders),
    PERSIST flushes durable state, and EXIT selects IDLE (read-and-idle) +
    releases the mutex; "idle" is returned. On the GUARD short-circuit path the
    run halts at HALTED with the latched disposition untouched and "halt" is
    returned (no read stage runs).

    After a clean tick the pulled work_items (and work_orders, when the active
    route produced them) are persisted into durable state so status.py can report
    the counts without re-running.

    Injected paths win: when any of runtime_dir/state_path/journal_path is None
    (the installed case) the missing ones fall back to resolve_runtime_paths().
    `project_dir` likewise defaults to the CLAUDE_PROJECT_DIR / cwd anchor.
    `source` is the injectable PULL issue source; `now` the injectable TRIAGE
    reference time.

    Prints a one-line tick trace (state path, work_items/work_orders counts,
    disposition).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal
    if project_dir is None:
        project_dir = _resolve_project_dir()

    # The runtime dict the factory convention binds: GUARD/EXIT read runtime_dir;
    # PULL the injectable source; TRIAGE the reference time. build_loop reads
    # project_dir for the override-config location.
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": runtime_dir,
        "source": source,
        "now": now,
    }

    # Route-as-data: load (override else default) -> resolve -> validate. A bad
    # override raises WiringError here, before any tick body runs.
    route, states = aw.build_loop(
        DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=_INITIAL_SLOTS)

    ctx = _seed_context(state_path, journal_path, route)
    result = to.run(route, states, ctx, _VOCAB, start="GUARD")

    if result.final_state == _DONE:
        # EXIT ran and emitted the disposition-selecting signal last. Persist the
        # pulled work_items (and work_orders, when the active route produced
        # them) so status can report the counts.
        signal = result.signals[-1]
        doc = ds.DurableState(state_path).load()
        doc[WORK_ITEMS_KEY] = ctx.read(wi.WORK_ITEMS_SLOT["name"])
        if "TRIAGE" in route["states"]:
            doc[WORK_ORDERS_KEY] = ctx.read(wi.WORK_ORDERS_SLOT["name"])
        ds.DurableState(state_path).save(doc)
    else:
        # GUARD short-circuited (STOPPED/ABORTED/RESTART_NEEDED): the tick did no
        # work (no read stage ran); map the halting condition to "halt".
        signal = "halt"

    disposition = ld.read_disposition(runtime_dir)
    work_items_count = persisted_work_items_count(state_path)
    work_orders_count = persisted_work_orders_count(state_path)
    sys.stdout.write(
        f"[tick] path={'->'.join(result.path)} work_items={work_items_count} "
        f"work_orders={work_orders_count} disposition={disposition} "
        f"signal={signal}\n")

    if return_run_result:
        return result
    return signal


if __name__ == "__main__":
    # Production entrypoint: the scheduling skills invoke this once per tick from
    # the installed plugin with no path wiring and no injected source. run_tick
    # defaults its durable file locations to the writable per-project runtime dir
    # (${CLAUDE_PROJECT_DIR}/.auto-maintainer/ else .auto-maintainer/ under cwd)
    # via resolve_runtime_paths(), loads the project-local route override (else
    # the default spine), and pulls real open issues via the live gh source.
    run_tick()
