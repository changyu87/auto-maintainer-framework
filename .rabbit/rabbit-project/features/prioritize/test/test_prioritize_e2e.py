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

def _order(oid, decision="accepted", reason="", body="body", labels=None):
    """Build a minimal WorkOrder dict in the shape TRIAGE writes. PRIORITIZE
    consumes `id` + `decision`, and (for same-feature serialization, #214) the
    `body`/`labels` blast-radius declaration; the rest carries sane filler. The
    default `body="body"` carries NO `Component:` line and `labels=[]`, so a
    default-built order has no declarable feature and is never serialized against
    another (each gets its own private bucket)."""
    return {
        "schema_version": "1.0.0",
        "id": oid,
        "work_item_id": oid.replace("wo-", ""),
        "title": f"title for {oid}",
        "body": body,
        "url": f"https://github.com/acme/widget/issues/{oid}",
        "labels": list(labels or []),
        "decision": decision,
        "reason": reason,
        "created_at": "2026-05-01T10:00:00Z",
    }


def _order_in(oid, feature, decision="accepted"):
    """An accepted order whose body declares `feature` as its blast radius via a
    `Component:` line (the maintainer's own issue convention, #214)."""
    return _order(oid, decision=decision,
                  body=f"some description\n\nComponent: {feature}.")


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


# ==========================================================================
# Same-feature serialization (#214): the v1-minimal instance of DESIGN §2.2 /
# §3.8.6. IMPLEMENT fans out one implementer per ordered work_order in parallel;
# two orders touching the SAME feature each bump that feature's shared metadata
# (feature.json/CHANGELOG/contract + the built plugins/ tree) and COLLIDE the
# moment one auto-PR lands. PRIORITIZE serializes them: at most ONE accepted
# order per feature per tick (FIFO-first wins), the rest deferred (absent from
# the plan). Cross-feature orders stay parallel.
# ==========================================================================

def test_serializes_same_feature_keeps_only_the_first():
    # Two scheduling-feature orders + one packaging order, like the live
    # #205/#207 (scheduling) vs #206 (packaging) collision.
    orders = [
        _order_in("wo-205", "scheduling"),
        _order_in("wo-207", "scheduling"),
        _order_in("wo-206", "packaging-config"),
    ]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    # The first scheduling order fans out; the second is DEFERRED. The
    # cross-feature packaging order fans out in parallel.
    assert plan["ordered"] == ["wo-205", "wo-206"]
    # wo-207 is absent from BOTH maps (deferred to a later tick).
    assert "wo-207" not in plan["ordered"]
    assert "wo-207" not in plan["status"]
    assert plan["status"] == {"wo-205": "pending", "wo-206": "pending"}


def test_cross_feature_orders_all_fan_out_in_parallel():
    orders = [
        _order_in("wo-a", "scheduling"),
        _order_in("wo-b", "work-intake"),
        _order_in("wo-c", "verify-integrate"),
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    # Disjoint features -> all kept, FIFO order preserved.
    assert plan["ordered"] == ["wo-a", "wo-b", "wo-c"]


def test_serialization_preserves_fifo_first_wins_per_feature():
    # Interleaved features: each feature's FIRST-seen order wins; later
    # same-feature orders defer, but distinct features all keep their first.
    orders = [
        _order_in("wo-1", "scheduling"),
        _order_in("wo-2", "work-intake"),
        _order_in("wo-3", "scheduling"),   # defer (scheduling taken by wo-1)
        _order_in("wo-4", "work-intake"),  # defer (work-intake taken by wo-2)
        _order_in("wo-5", "implement"),
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-1", "wo-2", "wo-5"]


def test_multi_feature_blast_radius_serializes_against_each():
    # An order whose Component spans TWO features shares with EITHER, so a later
    # order touching just one of them must defer.
    orders = [
        _order("wo-1", body="Component: verify-integrate + scheduling."),
        _order_in("wo-2", "scheduling"),       # shares scheduling -> defer
        _order_in("wo-3", "verify-integrate"),  # shares verify-int -> defer
        _order_in("wo-4", "packaging-config"),  # disjoint -> keep
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-1", "wo-4"]


def test_orders_without_declarable_feature_stay_parallel():
    # No Component line, no labels -> each order is its own private bucket and
    # never serialized against another (the safe default: serialize only on a
    # PROVEN shared feature). This is exactly the existing _order builder.
    orders = [_order("wo-1"), _order("wo-2"), _order("wo-3")]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-1", "wo-2", "wo-3"]


def test_serialization_falls_back_to_labels_when_no_component_line():
    # When the body declares no Component, the order's labels name the feature.
    orders = [
        _order("wo-1", body="no component here", labels=["scheduling"]),
        _order("wo-2", body="also none", labels=["scheduling"]),  # defer
        _order("wo-3", body="none", labels=["work-intake"]),       # keep
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-1", "wo-3"]


def test_component_feature_match_is_case_and_punctuation_insensitive():
    # `scheduling`, `Scheduling.`, and ` SCHEDULING ` are the SAME feature.
    orders = [
        _order("wo-1", body="Component: scheduling."),
        _order("wo-2", body="component: Scheduling"),    # defer
        _order("wo-3", body="COMPONENT:  SCHEDULING  "),  # defer
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-1"]


def test_rejected_orders_do_not_claim_features():
    # A rejected same-feature order is excluded entirely and must NOT consume the
    # feature slot — the accepted same-feature order still fans out.
    orders = [
        _order_in("wo-1", "scheduling", decision="rejected"),
        _order_in("wo-2", "scheduling", decision="accepted"),
    ]
    ctx = _fresh_ctx(work_orders=orders)
    plan = pr.run(ctx).writes["execution_plan"]
    assert plan["ordered"] == ["wo-2"]


def test_serialization_is_deterministic_byte_identical():
    orders = [
        _order_in("wo-1", "scheduling"),
        _order_in("wo-2", "scheduling"),
        _order_in("wo-3", "work-intake"),
    ]
    import json as _json
    plan_a = pr.run(_fresh_ctx(work_orders=orders)).writes["execution_plan"]
    plan_b = pr.run(_fresh_ctx(work_orders=orders)).writes["execution_plan"]
    assert _json.dumps(plan_a, sort_keys=True) == _json.dumps(
        plan_b, sort_keys=True)
