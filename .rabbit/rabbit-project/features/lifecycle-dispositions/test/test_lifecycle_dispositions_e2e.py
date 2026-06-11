#!/usr/bin/env python3
"""End-to-end conformance tests for lifecycle-dispositions.

Every behaviour in docs/spec.md has an e2e test here. The feature provides:

  1. Disposition  — closed set RUNNING|IDLE|STOPPED|ABORTED|RESTART_NEEDED,
     persisted via a durable on-disk marker (read/write helpers, injectable
     runtime path).
  2. Single-writer mutex — a lock marker with stale-marker detection: a live
     holder refuses a second acquire; a dead holder's stale lock is reclaimable.
  3. GUARD state — run(TickContext) -> StateResult (fsm-contracts contract):
     reads disposition; STOPPED/ABORTED -> HALT_REQUESTED; RESTART_NEEDED ->
     RESTART_REQUIRED; otherwise acquires the mutex (reclaiming stale), sets
     disposition RUNNING, emits OK.
  4. EXIT state — run(TickContext) -> StateResult: reads the tick outcome slot,
     selects the next disposition and emits refire/idle/break/halt, releases
     the mutex.
  5. Host-agnostic resumption — behaviour is identical fresh/headless or
     warm/in-session; the on-disk markers are the only source of truth.

The feature CONSUMES fsm-contracts (TickContext, StateResult, StateManifest,
apply_result, SignalVocabulary) and does NOT import durable-state.

Owner: changyu87
"""

import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Consume the already-implemented fsm-contracts module via sys.path, exactly
# as the tick-orchestrator tests do. Do NOT edit/fork fsm-contracts.
_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _runtime():
    """A fresh temp runtime dir (the injectable marker location)."""
    return tempfile.mkdtemp(prefix="lifecycle-rt-")


def _exit_context(outcome):
    """A TickContext carrying the tick outcome slot EXIT reads."""
    ctx = fc.TickContext()
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.write("tick_outcome", outcome)
    return ctx


# ==========================================================================
# Behaviour 1 — Disposition: closed set + durable read/write helpers.
# ==========================================================================

def test_disposition_is_a_closed_set():
    members = set(ld.Disposition.members())
    assert members == {
        "RUNNING", "IDLE", "STOPPED", "ABORTED", "RESTART_NEEDED"
    }


def test_disposition_write_then_read_roundtrips():
    rt = _runtime()
    ld.write_disposition(rt, "STOPPED")
    assert ld.read_disposition(rt) == "STOPPED"


def test_disposition_default_when_unset():
    """A fresh runtime with no marker reads the default IDLE disposition."""
    rt = _runtime()
    assert ld.read_disposition(rt) == "IDLE"


def test_write_rejects_unknown_disposition():
    rt = _runtime()
    raised = False
    try:
        ld.write_disposition(rt, "BOGUS")
    except ld.LifecycleError:
        raised = True
    assert raised, "writing a non-member disposition must raise"


# ==========================================================================
# Behaviour 2 — Single-writer mutex with stale-marker detection.
# ==========================================================================

def test_mutex_second_live_acquire_is_refused():
    rt = _runtime()
    assert ld.acquire_lock(rt) is True
    # A second acquire while the live lock is held is refused.
    assert ld.acquire_lock(rt) is False


def test_mutex_stale_lock_is_reclaimable():
    rt = _runtime()
    # Stamp a lock owned by a definitely-dead PID (stale holder).
    ld.write_stale_lock_for_test(rt, pid=ld.dead_pid())
    # A dead holder's stale lock is reclaimable.
    assert ld.acquire_lock(rt) is True
    # And now we own it: held by the current process.
    assert ld.lock_is_held(rt) is True


def test_mutex_release_frees_the_lock():
    rt = _runtime()
    assert ld.acquire_lock(rt) is True
    ld.release_lock(rt)
    assert ld.lock_is_held(rt) is False
    # After release a fresh acquire succeeds.
    assert ld.acquire_lock(rt) is True


# ==========================================================================
# Behaviour 3 — GUARD state (fsm-contracts run(ctx) -> StateResult).
# ==========================================================================

def test_guard_clean_start_acquires_mutex_sets_running_emits_ok():
    rt = _runtime()
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    assert isinstance(result, fc.StateResult)
    assert result.signal == "OK"
    # GUARD acquired the mutex and latched RUNNING on disk.
    assert ld.lock_is_held(rt) is True
    assert ld.read_disposition(rt) == "RUNNING"


def test_guard_stopped_emits_halt_requested_and_does_not_acquire():
    rt = _runtime()
    ld.write_disposition(rt, "STOPPED")
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    assert result.signal == "HALT_REQUESTED"
    # Latched STOPPED must NOT proceed: no mutex acquired, disposition intact.
    assert ld.lock_is_held(rt) is False
    assert ld.read_disposition(rt) == "STOPPED"


def test_guard_aborted_emits_halt_requested_and_does_not_acquire():
    rt = _runtime()
    ld.write_disposition(rt, "ABORTED")
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    assert result.signal == "HALT_REQUESTED"
    assert ld.lock_is_held(rt) is False
    assert ld.read_disposition(rt) == "ABORTED"


def test_guard_restart_needed_emits_restart_required():
    rt = _runtime()
    ld.write_disposition(rt, "RESTART_NEEDED")
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    assert result.signal == "RESTART_REQUIRED"
    # RESTART_NEEDED auto-resumes after a restart; GUARD does not acquire here.
    assert ld.lock_is_held(rt) is False


def test_guard_reclaims_stale_lock_on_clean_start():
    rt = _runtime()
    ld.write_stale_lock_for_test(rt, pid=ld.dead_pid())
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    assert result.signal == "OK"
    assert ld.lock_is_held(rt) is True


def test_guard_result_conforms_to_manifest_via_apply_result():
    """GUARD's StateResult passes the fsm-contracts bounded-scope check."""
    rt = _runtime()
    guard = ld.Guard(rt)
    result = guard.run(fc.TickContext())
    vocab = fc.SignalVocabulary(list(guard.manifest.emits))
    ctx = fc.TickContext()
    # apply_result enforces: signal in manifest.emits + vocabulary, and that
    # every written slot is declared. GUARD writes no slots, so this must not
    # raise.
    fc.apply_result(ctx, guard.manifest, result, vocab)


# ==========================================================================
# Behaviour 4 — EXIT state: tick outcome -> disposition + signal, release.
# ==========================================================================

def test_exit_work_remains_emits_refire():
    rt = _runtime()
    ld.acquire_lock(rt)
    ld.write_disposition(rt, "RUNNING")
    result = ld.Exit(rt).run(_exit_context("work-remains"))
    assert result.signal == "refire"
    # refire: the loop keeps running.
    assert ld.read_disposition(rt) == "RUNNING"
    # EXIT releases the mutex.
    assert ld.lock_is_held(rt) is False


def test_exit_empty_emits_idle():
    rt = _runtime()
    ld.acquire_lock(rt)
    ld.write_disposition(rt, "RUNNING")
    result = ld.Exit(rt).run(_exit_context("empty"))
    assert result.signal == "idle"
    assert ld.read_disposition(rt) == "IDLE"
    assert ld.lock_is_held(rt) is False


def test_exit_fault_emits_halt():
    rt = _runtime()
    ld.acquire_lock(rt)
    ld.write_disposition(rt, "RUNNING")
    result = ld.Exit(rt).run(_exit_context("fault"))
    assert result.signal == "halt"
    assert ld.read_disposition(rt) == "ABORTED"
    assert ld.lock_is_held(rt) is False


def test_exit_restart_emits_break():
    rt = _runtime()
    ld.acquire_lock(rt)
    ld.write_disposition(rt, "RUNNING")
    result = ld.Exit(rt).run(_exit_context("restart"))
    assert result.signal == "break"
    assert ld.read_disposition(rt) == "RESTART_NEEDED"
    assert ld.lock_is_held(rt) is False


def test_exit_result_conforms_to_manifest_via_apply_result():
    rt = _runtime()
    ld.acquire_lock(rt)
    exit_state = ld.Exit(rt)
    result = exit_state.run(_exit_context("work-remains"))
    vocab = fc.SignalVocabulary(list(exit_state.manifest.emits))
    ctx = fc.TickContext()
    fc.apply_result(ctx, exit_state.manifest, result, vocab)


# ==========================================================================
# Behaviour 5 — Host-agnostic resumption: on-disk markers are sole truth.
# A "warm" handle and a "fresh" handle over the SAME runtime dir behave
# identically; a brand-new Guard/Exit object reading the markers picks up the
# state with no in-memory carryover.
# ==========================================================================

def test_resumption_identical_fresh_vs_warm():
    rt = _runtime()
    # "Warm" GUARD runs a clean start: latches RUNNING + holds the mutex.
    ld.Guard(rt).run(fc.TickContext())
    assert ld.read_disposition(rt) == "RUNNING"

    # A latched STOPPED is written to disk (e.g. by a human stop).
    ld.write_disposition(rt, "STOPPED")

    # A FRESH headless GUARD (new object, no shared memory) reads only the
    # on-disk markers and honours the latched STOPPED identically.
    fresh_result = ld.Guard(rt).run(fc.TickContext())
    assert fresh_result.signal == "HALT_REQUESTED"

    # And a SECOND fresh object observes the same on-disk truth.
    fresh_again = ld.Guard(rt).run(fc.TickContext())
    assert fresh_again.signal == "HALT_REQUESTED"


def test_resumption_full_guard_exit_cycle_through_disk():
    """End-to-end: GUARD (entry) -> EXIT (terminal) driven only by markers.

    Mirrors how tick-orchestrator runs GUARD as the route's entry anchor and
    EXIT as its terminal anchor, with all cross-tick state on disk.
    """
    rt = _runtime()
    # Tick N: GUARD admits, work remains, EXIT refires.
    g1 = ld.Guard(rt).run(fc.TickContext())
    assert g1.signal == "OK"
    assert ld.lock_is_held(rt) is True
    e1 = ld.Exit(rt).run(_exit_context("work-remains"))
    assert e1.signal == "refire"
    assert ld.lock_is_held(rt) is False  # mutex released for next tick

    # Tick N+1 (fresh handles): GUARD re-admits (RUNNING is resumable), queue
    # is empty, EXIT idles.
    g2 = ld.Guard(rt).run(fc.TickContext())
    assert g2.signal == "OK"
    e2 = ld.Exit(rt).run(_exit_context("empty"))
    assert e2.signal == "idle"
    assert ld.read_disposition(rt) == "IDLE"

    # Tick N+2: IDLE auto-resumes on the next heartbeat — GUARD admits again.
    g3 = ld.Guard(rt).run(fc.TickContext())
    assert g3.signal == "OK"
    assert ld.read_disposition(rt) == "RUNNING"
