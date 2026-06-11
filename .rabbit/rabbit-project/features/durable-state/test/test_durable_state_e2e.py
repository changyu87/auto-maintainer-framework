#!/usr/bin/env python3
"""End-to-end conformance tests for durable-state.

Every behaviour in docs/spec.md has an e2e test here:

  1. DurableState  — versioned JSON doc, atomic temp+rename save, load default.
  2. Journal       — append-only, record-before-act, stable dedup_key.
  3. DRAIN state   — run(TickContext) -> StateResult; finishes owed work
                     idempotently; emits OK.
  4. PERSIST state — run(TickContext) -> StateResult; writes durable state from
                     TickContext slots to disk; emits OK.
  5. Idempotency / dedup convention — re-run / DRAIN replay never double-acts.

The HEADLINE proof is the crash-safety property: a truncate -> resume scenario
where a tick is cut off after journaling an intent but before PERSIST, and the
next tick's DRAIN brings durable state to the intended value EXACTLY ONCE
(no double-count, no lost update).

durable-state CONSUMES fsm-contracts (TickContext, StateResult, StateManifest,
apply_result, SignalVocabulary). It does NOT edit or fork fsm-contracts.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# durable-state consumes the already-implemented fsm-contracts module.
_FSM_SRC = os.path.join(os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import durable_state as ds  # noqa: E402


# A closed signal vocabulary for the DRAIN/PERSIST states. Both emit OK.
SLICE_SIGNALS = ["OK"]


def _tmp_paths():
    d = tempfile.mkdtemp(prefix="durable-state-test-")
    return os.path.join(d, "state.json"), os.path.join(d, "journal.jsonl")


# --------------------------------------------------------------------------
# Behaviour 1 — DurableState: versioned JSON, atomic save, load default.
# --------------------------------------------------------------------------

def test_durable_state_load_default_when_absent():
    state_path, _ = _tmp_paths()
    state = ds.DurableState(state_path)
    doc = state.load()
    assert doc["schema_version"] == ds.SCHEMA_VERSION, doc
    assert doc["counter"] == 0, doc


def test_durable_state_save_then_load_roundtrip():
    state_path, _ = _tmp_paths()
    state = ds.DurableState(state_path)
    state.save({"schema_version": ds.SCHEMA_VERSION, "counter": 7,
                "last_tick": "t-1"})
    reloaded = ds.DurableState(state_path).load()
    assert reloaded["counter"] == 7, reloaded
    assert reloaded["last_tick"] == "t-1", reloaded


def test_durable_state_save_is_atomic_no_temp_left_behind():
    state_path, _ = _tmp_paths()
    state = ds.DurableState(state_path)
    state.save({"schema_version": ds.SCHEMA_VERSION, "counter": 1})
    parent = os.path.dirname(state_path)
    leftovers = [f for f in os.listdir(parent) if f != "state.json"]
    assert leftovers == [], f"temp files left behind: {leftovers}"
    # The on-disk file is the sole source of truth: it is valid JSON.
    with open(state_path) as fh:
        on_disk = json.load(fh)
    assert on_disk["counter"] == 1, on_disk


def test_durable_state_file_is_the_only_source_of_truth():
    # A fresh DurableState over the same path sees committed bytes, not memory.
    state_path, _ = _tmp_paths()
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 42})
    assert ds.DurableState(state_path).load()["counter"] == 42


# --------------------------------------------------------------------------
# Behaviour 2 — Journal: append-only, record-before-act, stable dedup_key.
# --------------------------------------------------------------------------

def test_journal_record_appends_intent_with_dedup_key():
    _, journal_path = _tmp_paths()
    j = ds.Journal(journal_path)
    j.record({"dedup_key": "k1", "target_counter": 1})
    j.record({"dedup_key": "k2", "target_counter": 2})
    entries = j.entries()
    assert [e["dedup_key"] for e in entries] == ["k1", "k2"], entries
    assert entries[1]["target_counter"] == 2, entries


def test_journal_record_before_act_intent_persisted_before_confirm():
    _, journal_path = _tmp_paths()
    j = ds.Journal(journal_path)
    j.record({"dedup_key": "k1", "target_counter": 1})
    # Intent is durable on disk BEFORE any confirmation is written.
    assert j.unconfirmed() == ["k1"], j.unconfirmed()
    j.confirm("k1")
    assert j.unconfirmed() == [], j.unconfirmed()


def test_journal_unconfirmed_survives_reopen():
    _, journal_path = _tmp_paths()
    ds.Journal(journal_path).record({"dedup_key": "k1", "target_counter": 1})
    # A new Journal over the same path replays the on-disk log.
    assert ds.Journal(journal_path).unconfirmed() == ["k1"]


# --------------------------------------------------------------------------
# Behaviour 3+4 — DRAIN / PERSIST conform to the fsm-contracts state contract.
# --------------------------------------------------------------------------

def _fresh_ctx(state_path, journal_path):
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    return ctx


def test_drain_and_persist_implement_run_tickcontext_to_stateresult():
    state_path, journal_path = _tmp_paths()
    ctx = _fresh_ctx(state_path, journal_path)
    ctx.write("counter", ds.DurableState(state_path).load()["counter"])

    drain_result = ds.drain_run(ctx)
    assert isinstance(drain_result, fc.StateResult)
    assert drain_result.signal == "OK", drain_result.signal

    ctx.write("counter", 5)
    persist_result = ds.persist_run(ctx)
    assert isinstance(persist_result, fc.StateResult)
    assert persist_result.signal == "OK", persist_result.signal
    # PERSIST wrote durable state to disk.
    assert ds.DurableState(state_path).load()["counter"] == 5


def test_drain_persist_manifests_conform_to_apply_result():
    # The per-state manifests bound each state's reads/writes/emits, and
    # apply_result enforces that contract against a closed vocabulary.
    state_path, journal_path = _tmp_paths()
    ctx = _fresh_ctx(state_path, journal_path)
    ctx.write("counter", 0)
    vocab = fc.SignalVocabulary(SLICE_SIGNALS)

    drain_result = ds.drain_run(ctx)
    fc.apply_result(ctx, ds.DRAIN_MANIFEST, drain_result, vocab)

    persist_result = ds.persist_run(ctx)
    fc.apply_result(ctx, ds.PERSIST_MANIFEST, persist_result, vocab)


# --------------------------------------------------------------------------
# Behaviour 5 — Idempotency: DRAIN replay never double-acts.
# --------------------------------------------------------------------------

def test_drain_is_idempotent_target_reapplied_never_reincremented():
    state_path, journal_path = _tmp_paths()
    # Durable state already at 3; an intent owes a move to target 4.
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 3})
    ds.Journal(journal_path).record({"dedup_key": "k1", "target_counter": 4})

    # DRAIN twice in a row: the second run must NOT push counter past 4.
    ctx1 = _fresh_ctx(state_path, journal_path)
    ctx1.write("counter", ds.DurableState(state_path).load()["counter"])
    ds.drain_run(ctx1)
    after_first = ds.DurableState(state_path).load()["counter"]

    ctx2 = _fresh_ctx(state_path, journal_path)
    ctx2.write("counter", ds.DurableState(state_path).load()["counter"])
    ds.drain_run(ctx2)
    after_second = ds.DurableState(state_path).load()["counter"]

    assert after_first == 4, after_first
    assert after_second == 4, after_second


# --------------------------------------------------------------------------
# HEADLINE — crash-safety: truncate -> resume reaches intended value EXACTLY
# ONCE. A tick journals an intent (target counter = prior + 1), is truncated
# BEFORE PERSIST, then the next tick's DRAIN brings durable state to the
# intended value with no double-count and no lost update.
# --------------------------------------------------------------------------

def test_crash_safety_truncate_before_persist_then_drain_resumes_exactly_once():
    state_path, journal_path = _tmp_paths()

    # --- Tick 0: a clean baseline. Durable counter starts at 10. ---
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 10})

    # --- Tick 1 (TRUNCATED): record-before-act journals the intent to move
    # the counter from 10 -> 11, then the process dies BEFORE PERSIST runs.
    # Durable state on disk is still 10; the journal owes the move to 11. ---
    Journal_tick1 = ds.Journal(journal_path)
    Journal_tick1.record({"dedup_key": "tick1-inc", "target_counter": 11})
    # (crash here: no PERSIST, no confirm)
    assert ds.DurableState(state_path).load()["counter"] == 10
    assert ds.Journal(journal_path).unconfirmed() == ["tick1-inc"]

    # --- Tick 2: DRAIN replays owed work from the truncated tick BEFORE any
    # new work, idempotently re-applying the target value. ---
    ctx2 = _fresh_ctx(state_path, journal_path)
    ctx2.write("counter", ds.DurableState(state_path).load()["counter"])
    drain_result = ds.drain_run(ctx2)
    assert drain_result.signal == "OK"

    # Durable state reaches the intended value EXACTLY ONCE: counter == 11.
    assert ds.DurableState(state_path).load()["counter"] == 11, \
        "DRAIN must bring durable state to the intended value (no lost update)"

    # The owed intent is now confirmed: a SECOND DRAIN is a no-op (no
    # double-count).
    ctx3 = _fresh_ctx(state_path, journal_path)
    ctx3.write("counter", ds.DurableState(state_path).load()["counter"])
    ds.drain_run(ctx3)
    assert ds.DurableState(state_path).load()["counter"] == 11, \
        "a re-run of DRAIN must NOT double-count the already-drained intent"
    assert ds.Journal(journal_path).unconfirmed() == []
