#!/usr/bin/env python3
"""End-to-end + unit tests for the work-intake TRIAGE cross-cutting-risk slot.

DESIGN §3.5.9: TRIAGE is the only state with the whole-batch view, so it flags
when accepted work orders' blast radii may overlap across DIFFERENT features and
writes a machine-first `cross_cutting_risk` slot (affected features + reason) for
VERIFY (§3.7.6) to act on. This is FT-B of the loop redesign.

These tests prove:
  - the versioned CrossCuttingRisk dataclass {risk, features, reason} round-trips
    through to_dict/from_dict and carries a schema_version;
  - the CROSS_CUTTING_RISK_SLOT descriptor mirrors WORK_ORDERS_SLOT;
  - the deterministic normalizer folds a batch annotation {features, reason} into
    a normalized CrossCuttingRisk — risk=true ONLY when >=2 DISTINCT features AND
    a non-empty reason; single-feature / empty / whitespace-only -> risk=false;
  - the validator REJECTS malformed annotation input;
  - TRIAGE ALWAYS writes the cross_cutting_risk slot (default risk=false/empty so
    VERIFY can always read it), and writes a true flag when handed an annotation;
  - the TRIAGE manifest DECLARES the cross_cutting_risk write, and apply_result
    commits both work_orders and cross_cutting_risk under the bounded-scope
    contract.

Fully deterministic — pure rules over in-memory inputs; no network, no AI.

Owner: changyu87
"""

import os
import sys
from datetime import datetime, timedelta, timezone

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import work_intake as wi  # noqa: E402


REF_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(number=1, title="Fix the thing", state="open", updated_at=None):
    if updated_at is None:
        updated_at = _iso(REF_NOW - timedelta(days=5))
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title=title,
        body="body text",
        url=f"https://github.com/acme/widget/issues/{number}",
        state=state,
        labels=[],
        author="octocat",
        created_at=_iso(REF_NOW - timedelta(days=20)),
        updated_at=updated_at,
    )


def _fresh_ctx(work_items=None):
    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])
    ctx.register_slot(
        wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
        version=wi.WORK_ORDERS_SLOT["version"])
    ctx.register_slot(
        wi.CROSS_CUTTING_RISK_SLOT["name"], wi.CROSS_CUTTING_RISK_SLOT["schema"],
        version=wi.CROSS_CUTTING_RISK_SLOT["version"])
    if work_items is not None:
        ctx.write("work_items", [it.to_dict() for it in work_items])
    return ctx


# ==========================================================================
# Behaviour: CrossCuttingRisk schema — typed, machine-first, versioned roundtrip
# ==========================================================================

def test_cross_cutting_risk_roundtrips_through_dict():
    risk = wi.CrossCuttingRisk(
        risk=True,
        features=["scheduling", "work-intake"],
        reason="both touch the run_tick discovery flush path",
    )
    d = risk.to_dict()
    assert wi.CrossCuttingRisk.from_dict(d) == risk


def test_cross_cutting_risk_dict_carries_schema_version():
    risk = wi.CrossCuttingRisk(risk=False, features=[], reason="")
    d = risk.to_dict()
    assert d["schema_version"] == wi.CROSS_CUTTING_RISK_SCHEMA_VERSION
    assert wi.CROSS_CUTTING_RISK_SCHEMA_VERSION  # non-empty


def test_cross_cutting_risk_default_is_no_risk():
    risk = wi.CrossCuttingRisk(risk=False, features=[], reason="")
    d = risk.to_dict()
    assert d["risk"] is False
    assert d["features"] == []
    assert d["reason"] == ""


# ==========================================================================
# Behaviour: CROSS_CUTTING_RISK_SLOT descriptor mirrors WORK_ORDERS_SLOT shape.
# ==========================================================================

def test_cross_cutting_risk_slot_descriptor():
    slot = wi.CROSS_CUTTING_RISK_SLOT
    assert slot["name"] == "cross_cutting_risk"
    assert "schema" in slot
    assert slot["version"] == wi.CROSS_CUTTING_RISK_SCHEMA_VERSION


# ==========================================================================
# Behaviour: the deterministic normalizer — risk=true ONLY when >=2 DISTINCT
# features AND a non-empty reason.
# ==========================================================================

def test_normalizer_two_features_and_reason_is_risk():
    risk = wi.normalize_cross_cutting_risk({
        "features": ["scheduling", "work-intake"],
        "reason": "shared run_tick flush path",
    })
    assert risk.risk is True
    assert set(risk.features) == {"scheduling", "work-intake"}
    assert risk.reason == "shared run_tick flush path"


def test_normalizer_three_features_and_reason_is_risk():
    risk = wi.normalize_cross_cutting_risk({
        "features": ["a", "b", "c"],
        "reason": "overlapping blast radius",
    })
    assert risk.risk is True


def test_normalizer_single_feature_is_no_risk():
    risk = wi.normalize_cross_cutting_risk({
        "features": ["work-intake"],
        "reason": "only one feature touched",
    })
    assert risk.risk is False


def test_normalizer_duplicate_feature_collapses_below_threshold():
    # Two entries naming the SAME feature is one DISTINCT feature -> no risk.
    risk = wi.normalize_cross_cutting_risk({
        "features": ["work-intake", "work-intake"],
        "reason": "same feature twice",
    })
    assert risk.risk is False


def test_normalizer_two_features_but_empty_reason_is_no_risk():
    risk = wi.normalize_cross_cutting_risk({
        "features": ["a", "b"],
        "reason": "",
    })
    assert risk.risk is False


def test_normalizer_two_features_but_whitespace_reason_is_no_risk():
    risk = wi.normalize_cross_cutting_risk({
        "features": ["a", "b"],
        "reason": "   ",
    })
    assert risk.risk is False


def test_normalizer_empty_annotation_is_no_risk():
    risk = wi.normalize_cross_cutting_risk({"features": [], "reason": ""})
    assert risk.risk is False
    assert risk.features == []
    assert risk.reason == ""


def test_normalizer_none_annotation_is_no_risk():
    risk = wi.normalize_cross_cutting_risk(None)
    assert risk.risk is False
    assert risk.features == []


# ==========================================================================
# Behaviour: the validator REJECTS malformed annotation input.
# ==========================================================================

def test_normalizer_rejects_non_mapping():
    for bad in (["a", "b"], "scheduling,work-intake", 42):
        try:
            wi.normalize_cross_cutting_risk(bad)
        except (ValueError, TypeError):
            continue
        raise AssertionError(f"expected rejection for malformed input {bad!r}")


def test_normalizer_rejects_non_list_features():
    try:
        wi.normalize_cross_cutting_risk({"features": "scheduling", "reason": "x"})
    except (ValueError, TypeError):
        return
    raise AssertionError("expected rejection for non-list features")


def test_normalizer_rejects_non_string_feature_entries():
    try:
        wi.normalize_cross_cutting_risk({"features": ["a", 7], "reason": "x"})
    except (ValueError, TypeError):
        return
    raise AssertionError("expected rejection for non-string feature entry")


def test_normalizer_rejects_non_string_reason():
    try:
        wi.normalize_cross_cutting_risk({"features": ["a", "b"], "reason": 5})
    except (ValueError, TypeError):
        return
    raise AssertionError("expected rejection for non-string reason")


# ==========================================================================
# Behaviour: TRIAGE manifest DECLARES the cross_cutting_risk write.
# ==========================================================================

def test_triage_manifest_declares_cross_cutting_risk_write():
    m = wi.TRIAGE_MANIFEST
    assert "work_orders" in m.writes
    assert "cross_cutting_risk" in m.writes


# ==========================================================================
# E2E Behaviour: TRIAGE ALWAYS writes the cross_cutting_risk slot — even with no
# annotation, a default no-risk record is written so VERIFY can always read it.
# ==========================================================================

def test_triage_always_writes_default_no_risk_slot():
    ctx = _fresh_ctx(work_items=[_item(number=7, title="Real task")])
    state = wi.Triage(now=REF_NOW)

    result = state.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert "cross_cutting_risk" in result.writes

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    written = ctx.read("cross_cutting_risk")
    assert written is not None
    assert written["risk"] is False
    assert written["features"] == []
    assert written["reason"] == ""
    assert written["schema_version"] == wi.CROSS_CUTTING_RISK_SCHEMA_VERSION


def test_triage_writes_default_slot_even_when_all_rejected():
    # Empty / all-rejected batch still writes the default no-risk slot.
    ctx = _fresh_ctx(work_items=[])
    state = wi.Triage(now=REF_NOW)

    result = state.run(ctx)
    assert result.signal == "EMPTY"
    assert "cross_cutting_risk" in result.writes

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    written = ctx.read("cross_cutting_risk")
    assert written["risk"] is False


# ==========================================================================
# E2E Behaviour: handed a batch annotation naming >=2 features + a reason, TRIAGE
# writes a risk=true cross_cutting_risk slot alongside the work_orders.
# ==========================================================================

def test_triage_writes_risk_slot_from_annotation():
    ctx = _fresh_ctx(work_items=[_item(number=7, title="Touches A and B")])
    state = wi.Triage(
        now=REF_NOW,
        cross_cutting_annotation={
            "features": ["scheduling", "work-intake"],
            "reason": "both edit run_tick discovery flush",
        },
    )

    result = state.run(ctx)
    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    written = ctx.read("cross_cutting_risk")
    assert written["risk"] is True
    assert set(written["features"]) == {"scheduling", "work-intake"}
    assert written["reason"] == "both edit run_tick discovery flush"

    # work_orders still produced as before.
    orders = ctx.read("work_orders")
    assert len(orders) == 1


def test_triage_single_feature_annotation_yields_no_risk():
    ctx = _fresh_ctx(work_items=[_item(number=7)])
    state = wi.Triage(
        now=REF_NOW,
        cross_cutting_annotation={
            "features": ["work-intake"],
            "reason": "only one feature",
        },
    )
    result = state.run(ctx)
    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    written = ctx.read("cross_cutting_risk")
    assert written["risk"] is False
