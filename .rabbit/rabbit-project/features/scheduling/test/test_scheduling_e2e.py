#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling (slice 1).

scheduling is the INTEGRATION cycle: it composes the already-implemented
lifecycle-core anchors (GUARD/EXIT from lifecycle-dispositions, DRAIN/PERSIST
from durable-state) plus a local DEMO_WORK state into a single self-restarting
tick loop driven through tick-orchestrator. Every behaviour in docs/spec.md has
an e2e test here:

  1. DEMO_WORK state — run(TickContext) -> StateResult: reads counter, writes
     counter+1, journals the increment intent (record-before-act via
     durable-state's Journal), emits OK while counter < THRESHOLD else EMPTY.
  2. run_tick — one invocation = one tick: assembles the route
     GUARD->DRAIN->DEMO_WORK->PERSIST->EXIT and the states map, seeds a
     TickContext from durable state, runs tick_orchestrator.run(...), and
     persists/returns the EXIT disposition signal.
  3. HEADLINE multi-tick run — invoke run_tick repeatedly; the persisted counter
     advances 0->1->2->...; GUARD acquires / EXIT releases the mutex each tick;
     dispositions transition RUNNING (refire while counter < THRESHOLD) -> idle
     at THRESHOLD.
  4. HEADLINE crash-safety across the full route — truncate a tick after the
     journal records the increment intent but before PERSIST; the next tick's
     DRAIN finishes the owed increment EXACTLY ONCE (no double-count) and the
     loop continues correctly.
  5. HEADLINE STOPPED latches — after stop, GUARD halts and the tick does not
     advance the counter.
  6. Control scripts (#29/#30) — status.py reads the REAL disposition + counter
     (no slice-1 stub) and reports a sane "not started" state when no runtime
     dir exists; stop.py latches STOPPED via the lifecycle-dispositions API
     using run_tick's runtime-path resolution; both import self-contained from
     the flat shipped lib/ layout. The shipped start/stop/status skills invoke
     ${CLAUDE_PLUGIN_ROOT}/lib/{run_tick,stop,status}.py and hand-roll NO Python.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state, and
lifecycle-dispositions UNCHANGED (imported via sys.path). It does NOT edit or
fork them.

Owner: changyu87
"""

import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Consume the already-implemented sibling features via sys.path, exactly as the
# other feature tests do. Do NOT edit/fork any of them.
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import run_tick as rt  # noqa: E402
import status as st  # noqa: E402
import stop as sp  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _paths():
    """A fresh temp dir with injectable runtime/state/journal paths."""
    root = tempfile.mkdtemp(prefix="scheduling-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _demo_ctx(state_path, journal_path, counter):
    """A TickContext carrying the slots DEMO_WORK reads/writes."""
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    ctx.write("counter", counter)
    return ctx


# --------------------------------------------------------------------------
# Behaviour 1 — DEMO_WORK state contract.
# --------------------------------------------------------------------------

def test_demo_work_reads_counter_writes_increment_and_emits_ok_below_threshold():
    _, state_path, journal_path = _paths()
    ctx = _demo_ctx(state_path, journal_path, counter=0)
    result = rt.demo_work_run(ctx)
    assert isinstance(result, fc.StateResult), result
    assert result.signal == "OK", result.signal
    assert result.writes["counter"] == 1, result.writes
    # work-remains while below THRESHOLD -> EXIT will refire.
    assert result.writes["tick_outcome"] == "work-remains", result.writes


def test_demo_work_emits_empty_at_threshold():
    _, state_path, journal_path = _paths()
    ctx = _demo_ctx(state_path, journal_path, counter=rt.THRESHOLD)
    result = rt.demo_work_run(ctx)
    assert result.signal == "EMPTY", result.signal
    assert result.writes["tick_outcome"] == "empty", result.writes
    # At/above THRESHOLD the queue is empty: DEMO_WORK does NOT advance the
    # counter and journals nothing.
    assert result.writes["counter"] == rt.THRESHOLD, result.writes
    assert ds.Journal(journal_path).entries() == []


def test_demo_work_journals_increment_intent_record_before_act():
    """record-before-act: the increment intent (target = counter+1) is durably
    journaled with a stable dedup_key BEFORE PERSIST commits it."""
    _, state_path, journal_path = _paths()
    ctx = _demo_ctx(state_path, journal_path, counter=3)
    rt.demo_work_run(ctx)
    journal = ds.Journal(journal_path)
    entries = journal.entries()
    assert len(entries) == 1, entries
    assert entries[0]["target_counter"] == 4, entries
    assert "dedup_key" in entries[0], entries
    # The intent is recorded but not yet confirmed (PERSIST/DRAIN confirm it).
    assert journal.unconfirmed() == [entries[0]["dedup_key"]], journal.unconfirmed()


def test_demo_work_manifest_conforms_to_route_enforcement():
    """DEMO_WORK exposes an fsm-contracts manifest whose writes/emits cover
    what it actually writes/emits, so apply_result accepts its StateResult."""
    manifest = rt.DEMO_WORK_MANIFEST
    ctx = _demo_ctx(_paths()[1], _paths()[2], counter=0)
    result = rt.demo_work_run(ctx)
    vocab = fc.SignalVocabulary(["OK", "EMPTY"])
    # apply_result raises if writes escape manifest.writes or signal escapes
    # manifest.emits / the vocabulary; a clean return proves conformance.
    fc.apply_result(ctx, manifest, result, vocab)
    assert ctx.read("counter") == 1


# --------------------------------------------------------------------------
# Behaviour 2 — run_tick: one invocation = one tick over the full route.
# --------------------------------------------------------------------------

def test_run_tick_single_tick_advances_counter_and_returns_refire():
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path)
    # Counter 0 -> 1, work remains -> refire.
    assert ds.DurableState(state_path).load()["counter"] == 1
    assert signal == "refire", signal
    assert ld.read_disposition(runtime_dir) == ld.Disposition.RUNNING


def test_run_tick_releases_mutex_after_exit():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path)
    # EXIT released the single-writer mutex: no live holder remains.
    assert not ld.lock_is_held(runtime_dir)


def test_run_tick_traverses_full_route():
    """The orchestrator runs GUARD->DRAIN->DEMO_WORK->PERSIST->EXIT (EXIT runs
    and selects the disposition; the run halts at the DONE terminal)."""
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, return_run_result=True)
    assert result.path[:5] == ["GUARD", "DRAIN", "DEMO_WORK", "PERSIST",
                               "EXIT"], result.path
    # EXIT actually runs (it is not terminal); the run halts at the DONE
    # terminal after EXIT selects the disposition + releases the mutex.
    assert result.final_state == "DONE", result.path


# --------------------------------------------------------------------------
# Behaviour 3 — HEADLINE multi-tick run: counter advances across ticks,
# dispositions transition RUNNING (refire) -> idle at THRESHOLD.
# --------------------------------------------------------------------------

def test_multi_tick_run_advances_counter_and_idles_at_threshold():
    runtime_dir, state_path, journal_path = _paths()

    signals = []
    # Ticks 1..THRESHOLD each advance the persisted counter by exactly 1
    # (0->1->2->...->THRESHOLD); each refires because the counter was below
    # THRESHOLD at the start of the tick.
    for expected in range(1, rt.THRESHOLD + 1):
        sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                          journal_path=journal_path)
        signals.append(sig)
        assert ds.DurableState(state_path).load()["counter"] == expected, \
            (expected, ds.DurableState(state_path).load())
        # The mutex is released at the end of every tick.
        assert not ld.lock_is_held(runtime_dir)
        assert sig == "refire", (expected, sig)

    # The next tick sees counter == THRESHOLD: the queue is empty, so it idles
    # and the counter does not advance.
    idle_sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                           journal_path=journal_path)
    assert idle_sig == "idle", idle_sig
    assert ds.DurableState(state_path).load()["counter"] == rt.THRESHOLD
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE


# --------------------------------------------------------------------------
# Behaviour 4 — HEADLINE crash-safety across the full route: truncate after the
# journal records the increment but before PERSIST; the next tick's DRAIN
# finishes the owed increment EXACTLY ONCE.
# --------------------------------------------------------------------------

def test_crash_safety_truncate_after_journal_before_persist_then_resume():
    runtime_dir, state_path, journal_path = _paths()

    # --- Tick 1 (TRUNCATED): record-before-act journals the move 0 -> 1, then
    # the process dies BEFORE PERSIST commits it. Durable counter is still 0;
    # the journal owes the move to 1. We simulate the truncated tick by running
    # only DEMO_WORK's journaling against a fresh durable baseline. ---
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = _demo_ctx(state_path, journal_path, counter=0)
    rt.demo_work_run(ctx)  # journals intent target=1; crash before PERSIST
    assert ds.DurableState(state_path).load()["counter"] == 0
    owed = ds.Journal(journal_path).unconfirmed()
    assert len(owed) == 1, owed

    # --- Tick 2: a full run_tick. DRAIN finishes the owed increment EXACTLY
    # ONCE before DEMO_WORK does new work. After DRAIN reconciles 0 -> 1, the
    # loop continues. The persisted counter must NOT double-count the owed
    # move. ---
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path)
    after = ds.DurableState(state_path).load()["counter"]
    # DRAIN drains the owed move to 1; then DEMO_WORK does one more increment to
    # 2. No double-count: had DRAIN re-incremented instead of reconciling to
    # target, the counter would overshoot.
    assert after == 2, after
    # The owed intent from tick 1 (target_counter == 1) is now confirmed.
    confirmed = ds.Journal(journal_path).confirmed_keys()
    owed_key = owed[0]
    assert owed_key in confirmed, (owed_key, confirmed)


def test_crash_safety_drain_does_not_double_count_on_repeated_resume():
    """A second resume after the owed work is drained must not re-apply it."""
    runtime_dir, state_path, journal_path = _paths()
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = _demo_ctx(state_path, journal_path, counter=0)
    rt.demo_work_run(ctx)  # owed move to 1, truncated

    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path)
    first = ds.DurableState(state_path).load()["counter"]
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path)
    second = ds.DurableState(state_path).load()["counter"]
    # Each genuine tick advances by exactly 1; the drained intent is never
    # re-counted.
    assert second == first + 1, (first, second)


# --------------------------------------------------------------------------
# Behaviour 5 — HEADLINE STOPPED latches: after stop, GUARD halts and the tick
# does not advance the counter.
# --------------------------------------------------------------------------

def test_stopped_disposition_latches_guard_halts_counter_frozen():
    runtime_dir, state_path, journal_path = _paths()

    # One clean tick advances the counter to 1.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path)
    assert ds.DurableState(state_path).load()["counter"] == 1

    # A human stop latches STOPPED.
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)

    # The next tick: GUARD halts; the counter does NOT advance.
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, return_run_result=True)
    assert ds.DurableState(state_path).load()["counter"] == 1, \
        "STOPPED must freeze the counter (GUARD halts before DEMO_WORK)"
    # GUARD short-circuited to the halt terminal; DEMO_WORK never ran.
    assert "DEMO_WORK" not in result.path, result.path
    # STOPPED stays latched (a halt does not clear it).
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


def test_run_tick_returns_halt_signal_when_stopped():
    runtime_dir, state_path, journal_path = _paths()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path)
    assert signal == "halt", signal


# --------------------------------------------------------------------------
# Shipped control skills exist with the documented command names.
# --------------------------------------------------------------------------

def _ship_skill(name):
    return os.path.join(_FEATURE_DIR, "ship", "skills", name, "SKILL.md")


def test_start_and_stop_skills_are_shipped():
    start = _ship_skill("start")
    stop = _ship_skill("stop")
    assert os.path.isfile(start), start
    assert os.path.isfile(stop), stop
    start_body = open(start).read()
    stop_body = open(stop).read()
    # The control skills name the documented commands and the hardcoded ~1-min
    # heartbeat interval.
    assert "auto-maintainer:start" in start_body, start_body[:200]
    assert "auto-maintainer:stop" in stop_body, stop_body[:200]
    # start runs the tick-runner and schedules the recurring heartbeat.
    assert "run_tick.py" in start_body, start_body[:400]
    # stop latches STOPPED + cancels the heartbeat.
    assert "STOPPED" in stop_body, stop_body[:400]


# --------------------------------------------------------------------------
# Regression (auto-maintainer-framework#24) — the shipped /start and /stop
# skills must invoke run_tick at its INSTALLED path. In an installed plugin
# run_tick lands at lib/run_tick.py and skills run with cwd = the user's
# project, so a bare `src/run_tick.py` does not resolve and /start fails. Both
# skills must reference ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py and carry NO bare
# src/run_tick.py.
# --------------------------------------------------------------------------

_INSTALL_PATH = "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py"


def test_start_skill_references_install_correct_run_tick_path():
    body = open(_ship_skill("start")).read()
    assert _INSTALL_PATH in body, body
    # No bare src/run_tick.py — that path does not exist in an installed plugin.
    assert "src/run_tick.py" not in body, body


def test_stop_skill_has_no_bare_src_run_tick_path():
    body = open(_ship_skill("stop")).read()
    # stop need not run the tick-runner, but if it names run_tick at all it must
    # use the install-correct path, never the bare src/ path.
    assert "src/run_tick.py" not in body, body
    if "run_tick.py" in body:
        assert _INSTALL_PATH in body, body


# --------------------------------------------------------------------------
# Regression (auto-maintainer-framework#24) — run_tick, invoked with NO
# injected paths (the installed case), must default its durable-state / journal
# / disposition-marker location to a writable per-project runtime dir:
# ${CLAUDE_PROJECT_DIR}/.auto-maintainer/ when that env var is set, else
# .auto-maintainer/ under cwd. Injected paths still win.
# --------------------------------------------------------------------------

def test_resolve_runtime_paths_prefers_claude_project_dir():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        runtime_dir, state_path, journal_path = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    expected_dir = os.path.join(project_dir, ".auto-maintainer")
    assert runtime_dir == expected_dir, runtime_dir
    assert state_path.startswith(expected_dir + os.sep), state_path
    assert journal_path.startswith(expected_dir + os.sep), journal_path


def test_resolve_runtime_paths_falls_back_to_cwd_when_no_project_dir():
    cwd = tempfile.mkdtemp(prefix="scheduling-cwd-")
    old_env = os.environ.pop("CLAUDE_PROJECT_DIR", None)
    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = old_env
    # cwd fallback: <cwd>/.auto-maintainer (realpath-normalized for macos /tmp).
    assert runtime_dir == os.path.join(os.path.realpath(cwd), ".auto-maintainer") \
        or runtime_dir == os.path.join(cwd, ".auto-maintainer"), runtime_dir


def test_run_tick_runs_end_to_end_with_default_project_dir():
    """The installed case: no injected path, only CLAUDE_PROJECT_DIR. run_tick
    must create/use <that>/.auto-maintainer/ and actually advance the counter
    end-to-end with the default alone."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        signal = rt.run_tick()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    assert os.path.isdir(am_dir), am_dir
    state_path = os.path.join(am_dir, "durable-state.json")
    assert ds.DurableState(state_path).load()["counter"] == 1
    assert signal == "refire", signal


def test_run_tick_injected_paths_still_win_over_default():
    """Injected paths take precedence over the env/cwd default — the temp-path
    injection capability that the rest of the suite relies on is preserved."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path)
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # The injected state advanced; the default .auto-maintainer dir was NOT used.
    assert ds.DurableState(state_path).load()["counter"] == 1
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer"))


# --------------------------------------------------------------------------
# Control script: stop.py (#30) — deterministic STOPPED latch, no hand-rolled
# Python, no non-existent `runtime_dir` import. stop.py owns the state write via
# the lifecycle-dispositions API and resolves the runtime dir exactly as
# run_tick does (reusing resolve_runtime_paths).
# --------------------------------------------------------------------------

def test_stop_latches_stopped_readable_by_lifecycle_api():
    runtime_dir, state_path, journal_path = _paths()
    # The control scripts take no injected paths in production; they resolve via
    # CLAUDE_PROJECT_DIR. Point that at our temp project and assert the marker
    # the lifecycle API reads back is STOPPED.
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sp.stop()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    assert ld.read_disposition(am_dir) == ld.Disposition.STOPPED


def test_stop_resolves_runtime_dir_the_same_way_as_run_tick():
    """stop.py must NOT duplicate path logic: it reuses run_tick's
    resolve_runtime_paths so the marker it writes is the one a tick reads."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sp.stop()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


def test_stop_then_tick_freezes_counter_end_to_end():
    """End-to-end: stop.py latches STOPPED, and a subsequent tick (same runtime
    dir) halts in GUARD without advancing the counter."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sp.stop()
        signal = rt.run_tick()
        runtime_dir, state_path, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert signal == "halt", signal
    assert ds.DurableState(state_path).load()["counter"] == 0
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


# --------------------------------------------------------------------------
# Control script: status.py (#29) — deterministic, reads the REAL disposition +
# counter (no hardcoded slice-1 stub). Resolves the runtime dir the same way as
# run_tick.
# --------------------------------------------------------------------------

def test_status_reports_real_disposition_and_counter_after_ticks():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick()  # counter 0 -> 1, disposition RUNNING
        rt.run_tick()  # counter 1 -> 2
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # The status line reports the REAL counter (2) and disposition (RUNNING),
    # not a stub.
    assert "2" in line, line
    assert ld.Disposition.RUNNING in line, line
    assert "no loop configured" not in line, line


def test_status_reports_not_started_when_no_runtime_dir():
    """When the loop was never started (no runtime dir), status reports a sane
    'not started' state: default IDLE disposition + counter 0, no crash."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # No runtime dir was created just by asking for status.
    assert not os.path.isdir(runtime_dir), runtime_dir
    # Default unset disposition is IDLE; counter defaults to 0.
    assert ld.Disposition.IDLE in line, line
    assert "0" in line, line


def test_status_reflects_stopped_after_stop():
    """status.py reads the marker stop.py wrote: STOPPED is reported."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick()
        sp.stop()
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert ld.Disposition.STOPPED in line, line


def test_status_line_includes_runtime_dir():
    """The status line names the runtime dir it read from, for operator clarity."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert runtime_dir in line, (runtime_dir, line)


# --------------------------------------------------------------------------
# Self-contained import (#29/#30) — status.py and stop.py must resolve their
# imports from the shipped `lib/` layout alone, where run_tick.py and the
# consumed sibling modules (lifecycle_dispositions, durable_state, fsm_contracts,
# tick_orchestrator) are FLAT SIBLINGS, NOT under a feature `<dep>/src/` tree.
# This is the installed-plugin layout; if the scripts only resolved via the
# worktree's feature dirs they would ImportError in production (the #30 root
# cause class).
# --------------------------------------------------------------------------

def _materialize_plugin_lib():
    """Copy the scheduling scripts + every consumed sibling module into a single
    flat dir, mirroring the installed plugin `lib/` layout."""
    import shutil
    lib = tempfile.mkdtemp(prefix="scheduling-lib-")
    # scheduling's own scripts.
    for fn in ("run_tick.py", "status.py", "stop.py"):
        shutil.copy(os.path.join(_SRC, fn), os.path.join(lib, fn))
    # consumed sibling modules, flattened next to them.
    for dep, mod in (("fsm-contracts", "fsm_contracts.py"),
                     ("tick-orchestrator", "tick_orchestrator.py"),
                     ("durable-state", "durable_state.py"),
                     ("lifecycle-dispositions", "lifecycle_dispositions.py")):
        shutil.copy(os.path.join(_FEATURES, dep, "src", mod),
                    os.path.join(lib, mod))
    return lib


def _import_from_lib(lib, modname):
    import importlib.util
    path = os.path.join(lib, modname + ".py")
    spec = importlib.util.spec_from_file_location("libtest_" + modname, path)
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    sys.path.insert(0, lib)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def test_status_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "status")
        assert hasattr(mod, "status_line")
    finally:
        # Restore module table so the flat-lib copies don't shadow the
        # worktree-resolved modules for the rest of the suite.
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


def test_stop_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "stop")
        assert hasattr(mod, "stop")
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


# --------------------------------------------------------------------------
# Shipped control skills are script-backed (#29/#30): start/stop/status invoke
# ${CLAUDE_PLUGIN_ROOT}/lib/{run_tick,stop,status}.py and hand-roll NO Python.
# --------------------------------------------------------------------------

_STATUS_INSTALL = "${CLAUDE_PLUGIN_ROOT}/lib/status.py"
_STOP_INSTALL = "${CLAUDE_PLUGIN_ROOT}/lib/stop.py"


def test_status_skill_is_shipped_and_script_backed():
    status = _ship_skill("status")
    assert os.path.isfile(status), status
    body = open(status).read()
    assert "auto-maintainer:status" in body, body[:300]
    # It invokes the status script at its installed path.
    assert _STATUS_INSTALL in body, body
    # No bare src/ path; no slice-1 stub text.
    assert "src/status.py" not in body, body
    assert "no loop configured" not in body, body


def test_stop_skill_invokes_stop_script_not_handrolled_python():
    body = open(_ship_skill("stop")).read()
    # stop.py latches STOPPED; the skill invokes it at the installed path.
    assert _STOP_INSTALL in body, body
    assert "src/stop.py" not in body, body
    # No hand-rolled Python: no inline heredoc, no direct write_disposition call,
    # no ad-hoc durable_state import (the #30 class of bug).
    assert "python3 - <<" not in body, body
    assert "write_disposition(" not in body, body
    assert "import durable_state" not in body, body
    assert "from durable_state" not in body, body


def test_start_skill_has_no_handrolled_python():
    body = open(_ship_skill("start")).read()
    # start invokes run_tick at the installed path (already covered) and must
    # carry NO hand-rolled Python beyond the script invocation + cron scheduling.
    assert "python3 - <<" not in body, body
    assert "import durable_state" not in body, body
    assert "from durable_state" not in body, body


def test_control_skills_have_no_inline_python_heredoc():
    for name in ("start", "stop", "status"):
        body = open(_ship_skill(name)).read()
        # The #30 root cause: a skill that asks the model to hand-roll Python
        # against an API it guesses at. Forbid the heredoc/inline-eval shape in
        # every control skill.
        assert "python3 - <<" not in body, (name, body)
        assert "<<EOF" not in body, (name, body)
