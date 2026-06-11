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
    """The orchestrator visits GUARD->DRAIN->DEMO_WORK->PERSIST->EXIT."""
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, return_run_result=True)
    assert result.final_state == "EXIT", result.path
    assert result.path == ["GUARD", "DRAIN", "DEMO_WORK", "PERSIST", "EXIT"], \
        result.path


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
