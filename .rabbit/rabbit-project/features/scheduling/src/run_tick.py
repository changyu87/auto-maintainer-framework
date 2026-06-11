#!/usr/bin/env python3
"""run_tick — the deterministic single-tick runner for the maintainer loop.

One invocation = one tick. This is the script-tier core of the scheduling
feature (spec-rules §1): it assembles the real lifecycle-core route

    GUARD -> DRAIN -> DEMO_WORK -> PERSIST -> EXIT

over the already-implemented anchors and runs it through tick-orchestrator:

  - GUARD / EXIT come from lifecycle-dispositions (entry gate + single-writer
    mutex; terminal disposition selection + mutex release);
  - DRAIN / PERSIST come from durable-state (crash-recovery replay; durable
    flush);
  - DEMO_WORK is owned HERE: it reads the persisted counter, journals the
    increment intent (record-before-act, via durable-state's Journal), writes
    counter+1, and emits OK while counter < THRESHOLD else EMPTY.

The loop MECHANICS are real (mutex, journal, durable persisted state,
disposition transitions, DRAIN crash-recovery). Only the WORK is stubbed: the
counter increment stands in for real adapter work (PULL/TRIAGE/IMPLEMENT) that
later features supply.

Runtime paths (durable state, journal, disposition + lock markers) are injected
so tests use a temp dir and the on-disk files are the only source of truth.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state, and
lifecycle-dispositions UNCHANGED; it never edits or forks them.

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
             "lifecycle-dispositions"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402


# The hardcoded demo threshold: DEMO_WORK does work while the counter is below
# it, then reports the queue empty. Hardcoded for slice 1 (configurability is
# deferred to the configuration feature, auto-maintainer-framework#17).
THRESHOLD = 5


# --------------------------------------------------------------------------
# DEMO_WORK — the only locally-owned state. fsm-contracts run(ctx) contract.
# --------------------------------------------------------------------------

# DEMO_WORK reads the counter + injected journal path, and writes the advanced
# counter plus the tick_outcome slot EXIT consumes. Its closed emits are
# OK (work remains) and EMPTY (queue drained).
DEMO_WORK_MANIFEST = fc.StateManifest(
    reads=["counter", "journal_path"],
    writes=["counter", "tick_outcome"],
    emits=["OK", "EMPTY"],
)


def demo_work_run(ctx):
    """DEMO_WORK: the stubbed unit of maintainer work.

    Reads the persisted counter. While counter < THRESHOLD it journals the
    increment intent (record-before-act: the target value is durably recorded
    BEFORE PERSIST commits it, carrying a stable dedup_key), writes counter+1,
    and emits OK with tick_outcome "work-remains". At/above THRESHOLD the queue
    is empty: it advances nothing, journals nothing, and emits EMPTY with
    tick_outcome "empty".
    """
    counter = ctx.read("counter")
    if counter < THRESHOLD:
        target = counter + 1
        journal = ds.Journal(ctx.read("journal_path"))
        # record-before-act: the intent is durable BEFORE the increment is
        # committed by PERSIST. The dedup_key is stable per target value so a
        # replay (DRAIN) reconciles to the target idempotently, never
        # double-incrementing.
        journal.record({"dedup_key": f"demo-inc-to-{target}",
                        "target_counter": target})
        return fc.StateResult(
            signal="OK",
            writes={"counter": target, "tick_outcome": "work-remains"},
            journal=[f"DEMO_WORK: counter {counter} -> {target}"])
    return fc.StateResult(
        signal="EMPTY",
        writes={"counter": counter, "tick_outcome": "empty"},
        journal=["DEMO_WORK: queue empty (counter at THRESHOLD)"])


# --------------------------------------------------------------------------
# The route: GUARD -> DRAIN -> DEMO_WORK -> PERSIST -> EXIT (+ HALTED terminal).
# Routing is DATA (fsm-contracts / tick-orchestrator); no state names a
# successor. GUARD's latch/restart signals short-circuit to the HALTED terminal
# so a STOPPED/ABORTED/RESTART_NEEDED tick ends WITHOUT running DEMO_WORK and
# WITHOUT EXIT clobbering the latched disposition.
# --------------------------------------------------------------------------

# DONE is the true terminal: tick-orchestrator HALTS the moment it reaches a
# terminal state and NEVER run()s it, so EXIT must be a NON-terminal state that
# the loop actually runs (selecting the disposition + releasing the mutex). Its
# disposition signals (refire/idle/break) all route to DONE. HALTED is the
# short-circuit terminal for GUARD's latch/restart path.
_DONE = "DONE"
_HALTED = "HALTED"

ROUTE = {
    "states": ["GUARD", "DRAIN", "DEMO_WORK", "PERSIST", "EXIT", _DONE,
               _HALTED],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": _HALTED},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": _HALTED},
        {"state": "DRAIN", "signal": "OK", "next": "DEMO_WORK"},
        {"state": "DEMO_WORK", "signal": "OK", "next": "PERSIST"},
        {"state": "DEMO_WORK", "signal": "EMPTY", "next": "PERSIST"},
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


def _build_states(runtime_dir):
    """Map each route state -> (manifest, run_callable). GUARD/EXIT bind the
    injected runtime_dir; DRAIN/PERSIST/DEMO_WORK read injected slots."""
    guard = ld.Guard(runtime_dir)
    exit_state = ld.Exit(runtime_dir)
    return {
        "GUARD": (guard.manifest, guard.run),
        "DRAIN": (ds.DRAIN_MANIFEST, ds.drain_run),
        "DEMO_WORK": (DEMO_WORK_MANIFEST, demo_work_run),
        "PERSIST": (ds.PERSIST_MANIFEST, ds.persist_run),
        "EXIT": (exit_state.manifest, exit_state.run),
    }


def _seed_context(state_path, journal_path):
    """A TickContext seeded from durable state: the injected file paths plus
    the counter loaded from disk. The tick_outcome slot is registered but not
    written here — DEMO_WORK writes it before EXIT reads it on the clean path,
    and the GUARD short-circuit path halts at HALTED without ever running
    EXIT, so the slot is always written before any read."""
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    ctx.write("counter", ds.DurableState(state_path).load()["counter"])
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


def run_tick(runtime_dir=None, state_path=None, journal_path=None,
             return_run_result=False):
    """Run exactly ONE tick of the real loop and return the EXIT disposition
    signal (or the raw RunResult when return_run_result=True).

    Assembles the route + states map, seeds a TickContext from durable state,
    and runs tick_orchestrator.run(...). On the clean path EXIT writes the next
    disposition + releases the mutex and the EXIT signal (refire/idle/break) is
    returned. On the GUARD short-circuit path the run halts at HALTED with the
    latched disposition untouched and "halt" is returned.

    Injected paths win: when any of runtime_dir/state_path/journal_path is None
    (the installed case, where the skill invokes run_tick with no wiring) the
    missing ones fall back to resolve_runtime_paths(). Tests still inject a temp
    dir, which takes precedence over the env/cwd default.

    Prints a one-line tick trace (state path, counter, resulting disposition).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal
    ctx = _seed_context(state_path, journal_path)
    states = _build_states(runtime_dir)
    result = to.run(ROUTE, states, ctx, _VOCAB, start="GUARD")

    if result.final_state == _DONE:
        # EXIT ran and emitted the disposition-selecting signal last.
        signal = result.signals[-1]
    else:
        # GUARD short-circuited (STOPPED/ABORTED/RESTART_NEEDED): the tick did
        # no work; map the halting condition to a returned "halt" signal.
        signal = "halt"

    disposition = ld.read_disposition(runtime_dir)
    counter = ds.DurableState(state_path).load()["counter"]
    sys.stdout.write(
        f"[tick] path={'->'.join(result.path)} counter={counter} "
        f"disposition={disposition} signal={signal}\n")

    if return_run_result:
        return result
    return signal


if __name__ == "__main__":
    # Production entrypoint: the scheduling skills invoke this once per tick from
    # the installed plugin with no path wiring. run_tick defaults its durable
    # file locations to the writable per-project runtime dir
    # (${CLAUDE_PROJECT_DIR}/.auto-maintainer/ else .auto-maintainer/ under cwd)
    # via resolve_runtime_paths().
    run_tick()
