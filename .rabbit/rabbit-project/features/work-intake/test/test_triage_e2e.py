#!/usr/bin/env python3
"""End-to-end + unit tests for the work-intake slice-2 TRIAGE validity gate.

TRIAGE reads the `work_items` slot, applies a DETERMINISTIC validity gate to
each WorkItem (well-formed = has a title; in-scope = state == "open"; not-stale =
`updated_at` within a hardcoded window of an injectable reference time), maps
each ACCEPTED item 1:1 to a `WorkOrder(decision="accepted")`, writes the
`work_orders` slot, and emits OK if any accepted else EMPTY.

Determinism: the only time-dependent edge — staleness — is driven by an
INJECTABLE reference timestamp (Triage(now=...)), so tests pin staleness exactly
with no wall-clock dependence (spec-rules §1). No network, no AI anywhere.

The e2e tests drive TRIAGE exactly as tick-orchestrator will — building a real
fsm-contracts TickContext, registering the `work_items` + `work_orders` slots,
seeding `work_items`, running the state, and committing its StateResult through
`fc.apply_result` under the manifest + signal vocabulary (bounded-scope).

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


# --------------------------------------------------------------------------
# Fixtures — a deterministic reference time and a builder for WorkItems.
# --------------------------------------------------------------------------

# A fixed reference "now" so staleness is fully deterministic.
REF_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _item(number=1, title="Fix the thing", state="open",
          updated_at=None, labels=None):
    """Build a WorkItem with sane defaults (a valid, open, fresh item)."""
    if updated_at is None:
        updated_at = _iso(REF_NOW - timedelta(days=10))
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title=title,
        body="body text",
        url=f"https://github.com/acme/widget/issues/{number}",
        state=state,
        labels=list(labels or []),
        author="octocat",
        created_at=_iso(REF_NOW - timedelta(days=20)),
        updated_at=updated_at,
    )


def _fresh_ctx(work_items=None):
    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"],
        wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"],
    )
    ctx.register_slot(
        wi.WORK_ORDERS_SLOT["name"],
        wi.WORK_ORDERS_SLOT["schema"],
        version=wi.WORK_ORDERS_SLOT["version"],
    )
    ctx.register_slot(
        wi.CROSS_CUTTING_RISK_SLOT["name"],
        wi.CROSS_CUTTING_RISK_SLOT["schema"],
        version=wi.CROSS_CUTTING_RISK_SLOT["version"],
    )
    if work_items is not None:
        ctx.write("work_items", [it.to_dict() for it in work_items])
    return ctx


# ==========================================================================
# Behaviour: WorkOrder slot schema — typed, machine-first, versioned roundtrip
# ==========================================================================

def test_workorder_roundtrips_through_dict():
    order = wi.WorkOrder(
        id="wo-acme/widget#7",
        work_item_id="acme/widget#7",
        title="Crash on empty config",
        body="Steps ...",
        url="https://github.com/acme/widget/issues/7",
        labels=["bug", "p1"],
        decision="accepted",
        reason="",
        created_at="2026-05-01T10:00:00Z",
    )
    d = order.to_dict()
    assert wi.WorkOrder.from_dict(d) == order


def test_workorder_dict_carries_schema_version():
    order = wi.WorkOrder(
        id="wo-1", work_item_id="acme/widget#1", title="t", body="b",
        url="u", labels=[], decision="accepted", reason="",
        created_at="2026-05-01T10:00:00Z",
    )
    d = order.to_dict()
    assert d["schema_version"] == wi.WORK_ORDER_SCHEMA_VERSION
    assert wi.WORK_ORDER_SCHEMA_VERSION  # non-empty


# ==========================================================================
# Behaviour: per-state manifest is {reads: [work_items], writes: [work_orders],
# emits: [OK, EMPTY]} and conforms to the fsm-contracts manifest shape.
# ==========================================================================

def test_triage_manifest_declares_reads_writes_emits():
    m = wi.TRIAGE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("work_items",)
    # TRIAGE writes work_orders AND the cross_cutting_risk slot (DESIGN §3.5.9).
    assert m.writes == ("work_orders", "cross_cutting_risk")
    assert set(m.emits) == {"OK", "EMPTY"}


def test_triage_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("EMPTY")
    assert not vocab.is_member("MAYBE")


# ==========================================================================
# E2E Behaviour: TRIAGE over a mix (valid open, malformed/no-title, stale) ->
# work_orders contains ONLY the accepted, correctly mapped; signal OK.
# ==========================================================================

def test_triage_e2e_mixed_accepts_only_valid_and_emits_ok():
    valid = _item(number=7, title="Crash on empty config", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=5)),
                  labels=["bug"])
    malformed = _item(number=8, title="", state="open",
                      updated_at=_iso(REF_NOW - timedelta(days=5)))
    stale = _item(number=9, title="Old request", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=400)))

    ctx = _fresh_ctx(work_items=[valid, malformed, stale])
    state = wi.Triage(now=REF_NOW)

    result = state.run(ctx)

    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    written = ctx.read("work_orders")
    assert isinstance(written, list)
    # Only the one valid item is forwarded.
    assert len(written) == 1
    order = written[0]
    assert order["decision"] == "accepted"
    # work_item_id linkage points back at the source WorkItem id.
    assert order["work_item_id"] == "acme/widget#7"
    assert order["title"] == "Crash on empty config"
    assert order["url"] == "https://github.com/acme/widget/issues/7"
    assert order["labels"] == ["bug"]
    assert order["schema_version"] == wi.WORK_ORDER_SCHEMA_VERSION
    # The rejected items (malformed, stale) are NOT forwarded.
    forwarded_ids = {o["work_item_id"] for o in written}
    assert "acme/widget#8" not in forwarded_ids
    assert "acme/widget#9" not in forwarded_ids


# ==========================================================================
# E2E Behaviour: all-rejected -> work_orders empty, signal EMPTY.
# ==========================================================================

def test_triage_e2e_all_rejected_emits_empty():
    malformed = _item(number=8, title="", state="open")
    closed = _item(number=10, title="Done already", state="closed")
    stale = _item(number=9, title="Old", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=400)))

    ctx = _fresh_ctx(work_items=[malformed, closed, stale])
    state = wi.Triage(now=REF_NOW)

    result = state.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    assert ctx.read("work_orders") == []


def test_triage_e2e_empty_work_items_emits_empty():
    ctx = _fresh_ctx(work_items=[])
    state = wi.Triage(now=REF_NOW)

    result = state.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)

    assert ctx.read("work_orders") == []


# ==========================================================================
# Behaviour: staleness uses the INJECTED reference time, deterministically.
# A stale item is rejected; the SAME item with a fresh updated_at is accepted —
# proving the gate keys off the injected `now`, not the wall clock.
# ==========================================================================

def test_triage_staleness_keys_off_injected_reference_time():
    fresh = _item(number=11, title="Fresh", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=1)))
    stale = _item(number=12, title="Stale", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=500)))

    ctx = _fresh_ctx(work_items=[fresh, stale])
    result = wi.Triage(now=REF_NOW).run(ctx)

    vocab = fc.SignalVocabulary(wi.TRIAGE_SIGNALS)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result, vocab)
    written = ctx.read("work_orders")

    forwarded_ids = {o["work_item_id"] for o in written}
    assert "acme/widget#11" in forwarded_ids       # fresh -> accepted
    assert "acme/widget#12" not in forwarded_ids    # stale -> rejected


def test_triage_staleness_boundary_moves_with_reference_time():
    """The SAME item is stale relative to one reference time and fresh relative
    to an earlier one — proving the window is measured from the injected now."""
    item = _item(number=13, title="Boundary", state="open",
                 updated_at=_iso(datetime(2026, 1, 1, tzinfo=timezone.utc)))

    # Reference far in the future: item is stale -> rejected.
    ctx_late = _fresh_ctx(work_items=[item])
    res_late = wi.Triage(
        now=datetime(2027, 12, 1, tzinfo=timezone.utc)).run(ctx_late)
    assert res_late.signal == "EMPTY"

    # Reference shortly after update: item is fresh -> accepted.
    ctx_early = _fresh_ctx(work_items=[item])
    res_early = wi.Triage(
        now=datetime(2026, 1, 15, tzinfo=timezone.utc)).run(ctx_early)
    assert res_early.signal == "OK"


# ==========================================================================
# Behaviour: rejection reasons are deterministic per the gate rules.
# ==========================================================================

def test_triage_rejection_reasons_are_deterministic():
    """Triage exposes a pure classifier so the gate's verdict + reason are
    inspectable and deterministic, independent of slot plumbing."""
    valid = _item(number=1, title="ok", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=1)))
    no_title = _item(number=2, title="", state="open",
                     updated_at=_iso(REF_NOW - timedelta(days=1)))
    not_open = _item(number=3, title="ok", state="closed",
                     updated_at=_iso(REF_NOW - timedelta(days=1)))
    stale = _item(number=4, title="ok", state="open",
                  updated_at=_iso(REF_NOW - timedelta(days=400)))

    triage = wi.Triage(now=REF_NOW)
    assert triage.classify(valid) == ("accepted", "")
    assert triage.classify(no_title) == ("rejected", "malformed: no title")
    assert triage.classify(not_open) == ("rejected", "not open")
    decision, reason = triage.classify(stale)
    assert decision == "rejected"
    assert "stale" in reason.lower()


# ==========================================================================
# Behaviour: TRIAGE consumes work_items written by PULL (slice 1 -> slice 2).
# A full e2e: run PULL, then run TRIAGE over its output through the same ctx.
# ==========================================================================

def test_pull_then_triage_pipeline_e2e():
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

    # PULL fixture: one fresh open issue, updated just before REF_NOW so it
    # survives the staleness gate.
    fresh = _iso(REF_NOW - timedelta(days=2))
    gh_json = (
        '[{"number": 7, "title": "Crash on empty config", "body": "x",'
        ' "url": "https://github.com/acme/widget/issues/7", "state": "open",'
        ' "labels": [{"name": "bug"}], "author": {"login": "octocat"},'
        ' "createdAt": "2026-05-01T10:00:00Z", "updatedAt": "%s"}]' % fresh)

    def pull_source(repo=None):
        return wi.parse_gh_issues(gh_json)

    pull = wi.Pull(source=pull_source)
    pull_res = pull.run(ctx)
    fc.apply_result(ctx, wi.PULL_MANIFEST, pull_res,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    assert pull_res.signal == "OK"

    triage = wi.Triage(now=REF_NOW)
    triage_res = triage.run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, triage_res,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))

    assert triage_res.signal == "OK"
    orders = ctx.read("work_orders")
    assert len(orders) == 1
    assert orders[0]["work_item_id"] == "acme/widget#7"
    assert orders[0]["decision"] == "accepted"
