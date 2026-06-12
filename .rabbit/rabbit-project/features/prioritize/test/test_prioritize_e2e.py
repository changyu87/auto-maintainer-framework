#!/usr/bin/env python3
"""End-to-end + unit tests for the prioritize PRIORITIZE adapter state.

PRIORITIZE reads the `work_orders` slot (the array of decision-carrying
WorkOrder dicts TRIAGE produced), keeps only those with
`decision == "accepted"`, preserves TRIAGE's order (identity / FIFO — there is
no severity/priority key on a WorkOrder), back-fills `status[id] = "pending"`
for every planned order, writes the `execution_plan` slot, and emits OK when the
plan has at least one entry else EMPTY.

PRIORITIZE is DETERMINISTIC and effect-free: it is a pure function of
`work_orders` — no model, no wall-clock, no randomness, no network, no
filesystem. The same input yields a byte-identical plan.

The e2e tests drive PRIORITIZE exactly as tick-orchestrator will — building a
real fsm-contracts TickContext, registering the `work_orders` + `execution_plan`
slots, seeding `work_orders`, running the state, and committing its StateResult
through `fc.apply_result` under the manifest + signal vocabulary
(bounded-scope).

Owner: changyu87
"""

import json
import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import prioritize as pr  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a builder for accepted/rejected WorkOrder dicts and a fresh ctx.
# --------------------------------------------------------------------------

def _order(oid, decision="accepted", reason=""):
    """Build a minimal WorkOrder dict in the shape TRIAGE writes. PRIORITIZE
    only consumes `id` and `decision`, so the other fields carry sane filler."""
    return {
        "schema_version": "1.0.0",
        "id": oid,
        "work_item_id": oid.replace("wo-", ""),
        "title": f"title for {oid}",
        "body": "body",
        "url": f"https://github.com/acme/widget/issues/{oid}",
        "labels": [],
        "decision": decision,
        "reason": reason,
        "created_at": "2026-05-01T10:00:00Z",
    }


def _fresh_ctx(work_orders=None):
    ctx = fc.TickContext()
    # `work_orders` is the upstream slot work-intake's TRIAGE owns; PRIORITIZE
    # only reads it. The test registers it as the array slot tick-orchestrator
    # would, with no cross-feature import (PRIORITIZE consumes only the dict
    # `id` + `decision` fields, not work-intake's WorkOrder type).
    ctx.register_slot("work_orders", {"type": "array"}, version="1.0.0")
    ctx.register_slot(
        pr.EXECUTION_PLAN_SLOT["name"],
        pr.EXECUTION_PLAN_SLOT["schema"],
        version=pr.EXECUTION_PLAN_SLOT["version"],
    )
    if work_orders is not None:
        ctx.write("work_orders", work_orders)
    return ctx


# ==========================================================================
# Behaviour: the ExecutionPlan slot descriptor is typed, machine-first, versioned
# and mirrors work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
# ==========================================================================

def test_execution_plan_slot_descriptor_is_versioned():
    slot = pr.EXECUTION_PLAN_SLOT
    assert slot["name"] == "execution_plan"
    assert slot["schema"] == {"type": "object"}
    assert slot["version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION
    assert pr.EXECUTION_PLAN_SCHEMA_VERSION == "1.0.0"


# ==========================================================================
# Behaviour: per-state manifest is {reads: [work_orders],
# writes: [execution_plan], emits: [OK, EMPTY]} and conforms to the
# fsm-contracts manifest shape.
# ==========================================================================

def test_prioritize_manifest_declares_reads_writes_emits():
    m = pr.PRIORITIZE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("work_orders",)
    assert m.writes == ("execution_plan",)
    assert set(m.emits) == {"OK", "EMPTY"}


def test_prioritize_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("EMPTY")
    assert not vocab.is_member("MAYBE")


# ==========================================================================
# E2E Behaviour: all-accepted orders -> execution_plan.ordered preserves the
# input order; status backfilled "pending" for each; signal OK.
# ==========================================================================

def test_prioritize_e2e_accepted_preserves_order_and_backfills_pending():
    orders = [_order("wo-3"), _order("wo-1"), _order("wo-2")]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)

    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert isinstance(plan, dict)
    # Identity / FIFO ordering: relative order preserved exactly (NOT sorted).
    assert plan["ordered"] == ["wo-3", "wo-1", "wo-2"]
    # Every planned order starts "pending".
    assert plan["status"] == {"wo-3": "pending", "wo-1": "pending",
                              "wo-2": "pending"}
    assert plan["schema_version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION


# ==========================================================================
# E2E Behaviour: mixed accepted/rejected -> only accepted appear, in order;
# rejected excluded from both `ordered` and `status`; signal OK.
# ==========================================================================

def test_prioritize_e2e_mixed_excludes_rejected_preserves_order():
    orders = [
        _order("wo-10", decision="accepted"),
        _order("wo-11", decision="rejected", reason="not open"),
        _order("wo-12", decision="accepted"),
        _order("wo-13", decision="rejected", reason="stale"),
    ]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == ["wo-10", "wo-12"]
    assert plan["status"] == {"wo-10": "pending", "wo-12": "pending"}
    # Rejected orders are absent from both maps.
    assert "wo-11" not in plan["status"]
    assert "wo-13" not in plan["status"]
    assert "wo-11" not in plan["ordered"]
    assert "wo-13" not in plan["ordered"]


# ==========================================================================
# E2E Behaviour: zero accepted (empty slot) -> ordered=[], status={}, EMPTY.
# ==========================================================================

def test_prioritize_e2e_empty_work_orders_emits_empty():
    ctx = _fresh_ctx(work_orders=[])

    result = pr.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == []
    assert plan["status"] == {}
    assert plan["schema_version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION


# ==========================================================================
# E2E Behaviour: all-rejected -> ordered=[], status={}, signal EMPTY.
# ==========================================================================

def test_prioritize_e2e_all_rejected_emits_empty():
    orders = [
        _order("wo-20", decision="rejected", reason="not open"),
        _order("wo-21", decision="rejected", reason="malformed"),
    ]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == []
    assert plan["status"] == {}


# ==========================================================================
# Behaviour: determinism — same input twice yields a byte-identical plan.
# ==========================================================================

def test_prioritize_is_deterministic_byte_identical():
    orders = [_order("wo-3"), _order("wo-1"), _order("wo-2")]

    ctx_a = _fresh_ctx(work_orders=orders)
    ctx_b = _fresh_ctx(work_orders=orders)

    plan_a = pr.run(ctx_a).writes["execution_plan"]
    plan_b = pr.run(ctx_b).writes["execution_plan"]

    assert json.dumps(plan_a, sort_keys=True) == json.dumps(plan_b,
                                                            sort_keys=True)


# ==========================================================================
# Behaviour (scope guard): the v1 execution_plan carries NO `groups` key —
# parallel grouping is deferred to v2 (DESIGN §1.1 [v2]).
# ==========================================================================

def test_prioritize_plan_has_no_groups_key():
    ctx = _fresh_ctx(work_orders=[_order("wo-1")])
    plan = pr.run(ctx).writes["execution_plan"]
    assert "groups" not in plan
    # The v1 plan surface is exactly schema_version + ordered + status.
    assert set(plan.keys()) == {"schema_version", "ordered", "status"}


# ==========================================================================
# Behaviour: the factory follows the adapter-wiring convention —
# factory(runtime) -> (StateManifest, run_callable). The returned callable IS
# the state's run; the returned manifest IS PRIORITIZE_MANIFEST.
# ==========================================================================

def test_factory_returns_manifest_and_run_callable():
    manifest, run = pr.factory({})
    assert manifest is pr.PRIORITIZE_MANIFEST
    assert callable(run)

    ctx = _fresh_ctx(work_orders=[_order("wo-1"), _order("wo-2")])
    result = run(ctx)
    assert result.signal == "OK"
    assert result.writes["execution_plan"]["ordered"] == ["wo-1", "wo-2"]
