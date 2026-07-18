#!/usr/bin/env python3
"""End-to-end + unit tests for the Phase 2 park guard (convergence).

An issue that has failed to merge too many times must stop being re-worked so
the loop CONVERGES to idle instead of looping/escalating forever. verify-
integrate's INTEGRATE posts the FIXED gate-fail marker
`<!-- auto-maintainer:gate-fail -->` (source of truth
`verify_integrate.GATE_FAIL_MARKER`) on the issue for each failed merge attempt.
Once an issue's (bounded) comments carry >= PARK_THRESHOLD (5) such markers PULL
UNCONDITIONALLY EXCLUDES (parks) it — independent of work_own_filings — so it
never becomes a work_item / work_order and stays OPEN with its gate-fail
comments for a human to resolve on the tracker.

Unlike the loopback guard (which is gated on work_own_filings) this exclusion is
UNCONDITIONAL; like it, it is a PULL exclusion, NOT a TRIAGE reject (a reject
would route to the doer's close path and CLOSE the issue).

These tests are fully deterministic — no network. They cover:
  1. is_parked counts the gate-fail marker across the item's comments' bodies;
     >= 5 -> parked (True), 4 -> not parked (False); markers counted across
     MULTIPLE comments; is_parked is pure (WorkItem or dict).
  2. PULL EXCLUDES parked items — even with work_own_filings=True (unconditional).
  3. A mixed batch drops ONLY the parked items, keeping the rest.

Owner: changyu87
"""

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
import work_intake as wi  # noqa: E402

# The exact fixed marker verify-integrate's INTEGRATE posts. Source of truth:
# verify_integrate.GATE_FAIL_MARKER. Pinned here so the test fails loudly if
# work-intake's copy drifts from the verify-integrate source of truth.
_GATE_FAIL_MARKER = "<!-- auto-maintainer:gate-fail -->"


def _comment(body, author="ci-bot", created_at="2026-05-02T11:30:00Z"):
    return {"author": author, "created_at": created_at, "body": body}


def _item_with_marker_comments(count, number=7, per_comment=1):
    """A WorkItem whose comments carry `count` gate-fail markers total, spread
    `per_comment` markers per comment."""
    comments = []
    remaining = count
    while remaining > 0:
        n = min(per_comment, remaining)
        body = "merge attempt failed\n" + ("\n".join([_GATE_FAIL_MARKER] * n))
        comments.append(_comment(body))
        remaining -= n
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title="Flaky feature keeps failing merge",
        body="Original report ...",
        url=f"https://github.com/acme/widget/issues/{number}",
        state="OPEN",
        labels=["bug"],
        author="octocat",
        created_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-02T11:30:00Z",
        comments=comments,
    )


def _normal_item(number=9):
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title="Add a config flag",
        body="Please add ...",
        url=f"https://github.com/acme/widget/issues/{number}",
        state="OPEN",
        labels=["enhancement"],
        author="octocat",
        created_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-02T11:30:00Z",
        comments=[_comment("looks reasonable")],
    )


def _stub_source(items):
    def source(repo=None):
        return list(items)
    return source


def _fresh_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"],
        wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"],
    )
    return ctx


# ==========================================================================
# Behaviour: the threshold + marker are the fixed source-of-truth values.
# ==========================================================================

def test_park_threshold_is_five():
    assert wi.PARK_THRESHOLD == 5


def test_gate_fail_marker_matches_verify_integrate_source_of_truth():
    # work-intake's marker MUST equal verify_integrate.GATE_FAIL_MARKER exactly.
    assert wi.GATE_FAIL_MARKER == _GATE_FAIL_MARKER


# ==========================================================================
# Behaviour: is_parked counts the marker across the item's comments' bodies.
# >= PARK_THRESHOLD -> parked; below -> not parked. Pure (WorkItem or dict).
# ==========================================================================

def test_is_parked_true_at_threshold():
    item = _item_with_marker_comments(5)
    assert wi.is_parked(item) is True
    assert wi.is_parked(item.to_dict()) is True


def test_is_parked_true_above_threshold():
    item = _item_with_marker_comments(6)
    assert wi.is_parked(item) is True


def test_is_parked_false_below_threshold():
    item = _item_with_marker_comments(4)
    assert wi.is_parked(item) is False
    assert wi.is_parked(item.to_dict()) is False


def test_is_parked_false_for_normal_item():
    assert wi.is_parked(_normal_item()) is False
    assert wi.is_parked(_normal_item().to_dict()) is False


def test_is_parked_counts_markers_across_multiple_comments():
    # Five markers spread one-per-comment across five comments still parks.
    item = _item_with_marker_comments(5, per_comment=1)
    assert len(item.comments) == 5
    assert wi.is_parked(item) is True
    # Four one-per-comment does not.
    item4 = _item_with_marker_comments(4, per_comment=1)
    assert len(item4.comments) == 4
    assert wi.is_parked(item4) is False


def test_is_parked_counts_multiple_markers_within_one_comment():
    # All five markers in a single comment body still parks.
    item = _item_with_marker_comments(5, per_comment=5)
    assert len(item.comments) == 1
    assert wi.is_parked(item) is True


# ==========================================================================
# E2E Behaviour: PULL EXCLUDES parked items — UNCONDITIONALLY (even with the
# default work_own_filings=True). A parked item never reaches work_items.
# ==========================================================================

def test_pull_excludes_parked_item_default_flag():
    ctx = _fresh_ctx()
    items = [_item_with_marker_comments(5, number=7), _normal_item(9)]
    state = wi.Pull(source=_stub_source(items))  # default work_own_filings=True

    result = state.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    written = ctx.read("work_items")
    numbers = {w["number"] for w in written}
    # The parked item (7) is dropped; the normal item (9) survives.
    assert numbers == {9}
    assert all(not wi.is_parked(w) for w in written)


def test_pull_excludes_parked_item_with_work_own_filings_true():
    # Explicit True: the park exclusion is UNCONDITIONAL, not gated on the flag.
    ctx = _fresh_ctx()
    items = [_item_with_marker_comments(5, number=7), _normal_item(9)]
    state = wi.Pull(source=_stub_source(items), work_own_filings=True)

    result = state.run(ctx)
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    numbers = {w["number"] for w in ctx.read("work_items")}
    assert numbers == {9}


def test_pull_includes_below_threshold_item():
    # Four markers is below threshold -> NOT parked -> included.
    ctx = _fresh_ctx()
    items = [_item_with_marker_comments(4, number=7)]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)
    assert {w["number"] for w in ctx.read("work_items")} == {7}


def test_pull_all_parked_emits_empty():
    ctx = _fresh_ctx()
    items = [
        _item_with_marker_comments(5, number=7),
        _item_with_marker_comments(6, number=8),
    ]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "EMPTY"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)
    assert ctx.read("work_items") == []


# ==========================================================================
# E2E Behaviour: a mixed batch drops ONLY the parked items, keeping the rest.
# ==========================================================================

def test_pull_mixed_batch_drops_only_parked():
    ctx = _fresh_ctx()
    items = [
        _normal_item(9),
        _item_with_marker_comments(5, number=7),   # parked
        _normal_item(10),
        _item_with_marker_comments(4, number=8),   # below threshold, kept
    ]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    numbers = {w["number"] for w in ctx.read("work_items")}
    # 7 parked (dropped); 8 (4 markers), 9, 10 kept.
    assert numbers == {8, 9, 10}


# ==========================================================================
# E2E Behaviour: park is a PULL exclusion, NOT a TRIAGE reject. A parked item
# is simply absent from work_items; TRIAGE never sees it, so it is never routed
# to a close path — it stays OPEN on the tracker.
# ==========================================================================

def test_park_is_pull_exclusion_not_triage_reject():
    ctx = _fresh_ctx()
    parked = _item_with_marker_comments(5, number=7)
    state = wi.Pull(source=_stub_source([parked]))

    result = state.run(ctx)
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    # The parked item never enters work_items, so TRIAGE (which reads
    # work_items) produces no work_order for it — no reject, no close path.
    ctx.register_slot(
        wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
        version=wi.WORK_ORDERS_SLOT["version"])
    ctx.register_slot(
        wi.CROSS_CUTTING_RISK_SLOT["name"], wi.CROSS_CUTTING_RISK_SLOT["schema"],
        version=wi.CROSS_CUTTING_RISK_SLOT["version"])
    triage = wi.Triage(now=None)
    tres = triage.run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, tres,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    assert ctx.read("work_orders") == []
