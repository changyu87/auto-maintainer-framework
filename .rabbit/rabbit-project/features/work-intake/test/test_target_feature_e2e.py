#!/usr/bin/env python3
"""Tests for the authoritative target_feature blast-radius field (issue #258).

TRIAGE — the WorkOrder producer — computes each accepted order's blast-radius
target feature(s) from AUTHORITATIVE signals (feature:/component: prefixed
labels, a Component:/Feature: body line, a conventional title prefix) via the
pure `target_features_for`, and stamps the SORTED result onto the WorkOrder's
`target_feature` field. PRIORITIZE then reads that field instead of re-scraping
labels/body/title (the durable Machine-First fix deferred from #214).

Pure + deterministic: no network, no AI; staleness keys off an injectable now.

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


def _item(number=1, title="Fix the thing", body="body text", labels=None):
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title=title,
        body=body,
        url=f"https://github.com/acme/widget/issues/{number}",
        state="open",
        labels=list(labels or []),
        author="octocat",
        created_at=_iso(REF_NOW - timedelta(days=20)),
        updated_at=_iso(REF_NOW - timedelta(days=5)),
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
# target_features_for — the pure detection over authoritative signals.
# ==========================================================================

def test_detect_from_feature_label_prefix():
    assert wi.target_features_for(labels=["feature:scheduling"]) == \
        ["scheduling"]


def test_detect_from_component_label_prefix():
    assert wi.target_features_for(labels=["component:scheduling"]) == \
        ["scheduling"]


def test_generic_labels_name_no_feature():
    assert wi.target_features_for(
        labels=["bug", "enhancement", "filed-by:autonomous-maintainer"]) == []


def test_detect_from_component_body_line():
    assert wi.target_features_for(body="Some text.\nComponent: scheduling\n") \
        == ["scheduling"]


def test_multi_feature_body_radius_is_sorted():
    # A multi-feature Component: radius claims each feature; result is SORTED.
    assert wi.target_features_for(body="Component: scheduling, prioritize") == \
        ["prioritize", "scheduling"]


def test_and_is_not_a_feature_connector():
    assert wi.target_features_for(body="Component: command-and-control") == \
        ["command-and-control"]


def test_detect_from_bare_name_title_prefix():
    assert wi.target_features_for(title="scheduling: add retry backoff") == \
        ["scheduling"]


def test_detect_from_scoped_conventional_title_prefix():
    assert wi.target_features_for(title="feat(scheduling): add a tick") == \
        ["scheduling"]


def test_bare_conventional_commit_type_names_no_feature():
    assert wi.target_features_for(title="fix: correct a typo") == []


def test_detection_is_case_insensitive():
    assert wi.target_features_for(labels=["feature:Scheduling"]) == \
        ["scheduling"]


def test_no_provable_feature_is_empty():
    assert wi.target_features_for(
        labels=[], body="No signal here.", title="A plain title") == []


def test_signals_union_and_dedup_sorted():
    # A label + body + title naming overlapping/distinct features unions, dedups,
    # and sorts deterministically.
    out = wi.target_features_for(
        labels=["feature:scheduling"],
        body="Component: prioritize",
        title="scheduling: change")
    assert out == ["prioritize", "scheduling"]


# ==========================================================================
# WorkOrder schema — the additive target_feature field roundtrips + versions.
# ==========================================================================

def test_workorder_target_feature_roundtrips():
    order = wi.WorkOrder(
        id="wo-1", work_item_id="x#1", title="t", body="b", url="u",
        decision="accepted", reason="", target_feature=["scheduling"])
    d = order.to_dict()
    assert d["target_feature"] == ["scheduling"]
    assert wi.WorkOrder.from_dict(d) == order


def test_workorder_target_feature_defaults_empty():
    order = wi.WorkOrder(
        id="wo-1", work_item_id="x#1", title="t", body="b", url="u",
        decision="accepted", reason="")
    assert order.target_feature == []
    assert order.to_dict()["target_feature"] == []


def test_workorder_schema_version_bumped_to_1_2_0():
    assert wi.WORK_ORDER_SCHEMA_VERSION == "1.2.0"
    assert wi.WORK_ORDERS_SLOT["version"] == "1.2.0"


def test_from_dict_tolerates_missing_target_feature():
    # An older slot dict without the field deserializes to an empty list.
    d = {
        "id": "wo-1", "work_item_id": "x#1", "title": "t", "body": "b",
        "url": "u", "decision": "accepted", "reason": "",
    }
    assert wi.WorkOrder.from_dict(d).target_feature == []


# ==========================================================================
# TRIAGE stamps target_feature onto each accepted order from the signals.
# ==========================================================================

def test_triage_stamps_target_feature_from_label():
    ctx = _fresh_ctx(work_items=[
        _item(number=1, title="t", labels=["feature:scheduling"])])
    result = wi.Triage(now=REF_NOW).run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    orders = ctx.read("work_orders")
    assert orders[0]["target_feature"] == ["scheduling"]


def test_triage_stamps_target_feature_from_body_line():
    ctx = _fresh_ctx(work_items=[
        _item(number=2, title="t", body="Component: prioritize")])
    result = wi.Triage(now=REF_NOW).run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    assert ctx.read("work_orders")[0]["target_feature"] == ["prioritize"]


def test_triage_stamps_target_feature_from_title_prefix():
    ctx = _fresh_ctx(work_items=[
        _item(number=3, title="scheduling: add backoff", body="no signal")])
    result = wi.Triage(now=REF_NOW).run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    assert ctx.read("work_orders")[0]["target_feature"] == ["scheduling"]


def test_triage_stamps_empty_when_no_provable_feature():
    ctx = _fresh_ctx(work_items=[
        _item(number=4, title="A plain title", body="No signal.")])
    result = wi.Triage(now=REF_NOW).run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    assert ctx.read("work_orders")[0]["target_feature"] == []


def test_triage_target_feature_is_deterministic_and_sorted():
    ctx = _fresh_ctx(work_items=[
        _item(number=5, title="t", body="Component: scheduling, prioritize")])
    result = wi.Triage(now=REF_NOW).run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    assert ctx.read("work_orders")[0]["target_feature"] == \
        ["prioritize", "scheduling"]
