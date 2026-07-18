#!/usr/bin/env python3
"""End-to-end + unit tests for the Phase 2 park guard (convergence).

An issue that has failed to merge too many times must stop being re-worked so
the loop CONVERGES to idle instead of looping/escalating forever. verify-
integrate's INTEGRATE posts the FIXED gate-fail marker
`<!-- auto-maintainer:gate-fail -->` (source of truth
`verify_integrate.GATE_FAIL_MARKER`) on the issue for each failed merge attempt.
Each marker comment carries a JSON payload with the failed `pr_ref`.

`is_parked` counts DISTINCT failed PRs, NOT raw marker occurrences: each real
retry is a distinct PR (the implementer supersedes its prior open PR before
opening a new one), so `is_parked` parses each marker comment's JSON payload and
counts the number of DISTINCT `pr_ref` values across the item's comments. Once
that count is >= PARK_THRESHOLD (5) PULL UNCONDITIONALLY EXCLUDES (parks) it —
independent of work_own_filings — so it never becomes a work_item / work_order
and stays OPEN with its gate-fail comments for a human to resolve on the tracker.
This makes park a true RETRY counter rather than a tick-age timer: INTEGRATE
re-posts a gate-fail marker every tick the SAME unchanged PR is re-gated, so
counting raw marker occurrences would park an item after PARK_THRESHOLD *ticks*
regardless of how many times it was actually retried.

Unlike the loopback guard (which is gated on work_own_filings) this exclusion is
UNCONDITIONAL; like it, it is a PULL exclusion, NOT a TRIAGE reject (a reject
would route to the doer's close path and CLOSE the issue).

These tests are fully deterministic — no network. They cover:
  1. is_parked counts DISTINCT pr_ref across the item's marker comments;
     5 distinct PRs -> parked (True), 5 markers for the SAME pr_ref -> NOT parked
     (the bug this cycle fixes); mixed duplicates dedupe; a marker with
     unparseable/missing pr_ref counts as one distinct attempt (keyed by index)
     so malformed markers never silently defeat the guard.
  2. PULL EXCLUDES parked items — even with work_own_filings=True (unconditional).
  3. A mixed batch drops ONLY the parked items, keeping the rest.

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
import work_intake as wi  # noqa: E402

# The exact fixed marker verify-integrate's INTEGRATE posts. Source of truth:
# verify_integrate.GATE_FAIL_MARKER. Pinned here so the test fails loudly if
# work-intake's copy drifts from the verify-integrate source of truth.
_GATE_FAIL_MARKER = "<!-- auto-maintainer:gate-fail -->"


def _gate_fail_body(pr_ref):
    """Build a gate-fail comment body in the EXACT layout verify-integrate's
    gate_fail_comment_body emits: the FIXED marker line, a human sentence, a
    blank line, then a compact sort_keys json.dumps({pr_ref, reason,
    failure_summary}) block, then a trailing newline. Kept byte-identical to the
    verify-integrate source so is_parked parses the real production shape."""
    payload = json.dumps({
        "pr_ref": pr_ref,
        "reason": "gate-failed",
        "failure_summary": "regression suite red",
    }, sort_keys=True)
    return (f"{_GATE_FAIL_MARKER}\n"
            f"Automated regression GATE did not pass for {pr_ref}; "
            f"this PR was NOT merged.\n\n{payload}\n")


def _comment(body, author="ci-bot", created_at="2026-05-02T11:30:00Z"):
    return {"author": author, "created_at": created_at, "body": body}


def _item_with_pr_refs(pr_refs, number=7):
    """A WorkItem whose comments carry one gate-fail marker each, one per
    pr_ref in `pr_refs` (which may contain duplicates to model the same PR being
    re-gated across ticks)."""
    comments = [_comment(_gate_fail_body(ref)) for ref in pr_refs]
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


def _item_with_distinct_prs(count, number=7):
    """A WorkItem with `count` DISTINCT failed PRs (one marker comment each)."""
    return _item_with_pr_refs(
        [f"acme/widget#{100 + i}" for i in range(count)], number=number)


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
# Behaviour: is_parked counts DISTINCT failed PRs (pr_ref), not raw markers.
# >= PARK_THRESHOLD distinct -> parked; below -> not. Pure (WorkItem or dict).
# ==========================================================================

def test_is_parked_true_at_threshold_distinct_prs():
    item = _item_with_distinct_prs(5)
    assert wi.is_parked(item) is True
    assert wi.is_parked(item.to_dict()) is True


def test_is_parked_true_above_threshold_distinct_prs():
    item = _item_with_distinct_prs(6)
    assert wi.is_parked(item) is True


def test_is_parked_false_below_threshold_distinct_prs():
    item = _item_with_distinct_prs(4)
    assert wi.is_parked(item) is False
    assert wi.is_parked(item.to_dict()) is False


def test_is_parked_false_for_normal_item():
    assert wi.is_parked(_normal_item()) is False
    assert wi.is_parked(_normal_item().to_dict()) is False


def test_is_parked_false_when_same_pr_re_gated_five_times():
    """The bug this cycle fixes: INTEGRATE re-posts a gate-fail marker every tick
    the SAME unchanged PR is re-gated. Five markers ALL for one pr_ref is ONE
    distinct failed PR, so the item is NOT parked (a raw-marker count would have
    wrongly parked it after 5 ticks)."""
    item = _item_with_pr_refs(["acme/widget#100"] * 5)
    assert len(item.comments) == 5
    assert wi.is_parked(item) is False
    assert wi.is_parked(item.to_dict()) is False


def test_is_parked_dedupes_mixed_duplicate_pr_refs():
    """A mix of distinct + repeated pr_refs counts only DISTINCT PRs. Here PRs
    100..103 (4 distinct) each appear twice — 8 markers, but only 4 distinct
    failed PRs -> NOT parked."""
    refs = [f"acme/widget#{100 + i}" for i in range(4)] * 2
    item = _item_with_pr_refs(refs)
    assert len(item.comments) == 8
    assert wi.is_parked(item) is False
    # Add a 5th distinct PR -> now 5 distinct -> parked.
    refs5 = refs + ["acme/widget#104"]
    item5 = _item_with_pr_refs(refs5)
    assert wi.is_parked(item5) is True


def test_is_parked_unparseable_marker_counts_as_one_distinct_attempt():
    """A marker comment whose JSON payload is absent/unparseable (or carries no
    pr_ref) must NOT silently defeat the guard: it counts as ONE distinct attempt
    keyed by its comment position. Five such malformed markers -> parked."""
    bad = _GATE_FAIL_MARKER + "\nsomething went wrong (no json payload here)\n"
    item = wi.WorkItem(
        id="acme/widget#7", number=7, title="t", body="b",
        url="https://github.com/acme/widget/issues/7", state="OPEN",
        comments=[_comment(bad) for _ in range(5)])
    assert wi.is_parked(item) is True
    # Four malformed markers -> not parked.
    item4 = wi.WorkItem(
        id="acme/widget#7", number=7, title="t", body="b",
        url="https://github.com/acme/widget/issues/7", state="OPEN",
        comments=[_comment(bad) for _ in range(4)])
    assert wi.is_parked(item4) is False


def test_is_parked_mix_of_valid_and_malformed_markers():
    """Malformed markers (index-keyed) and valid distinct pr_refs both count.
    Two malformed + three distinct valid pr_refs = 5 distinct attempts -> parked."""
    bad = _GATE_FAIL_MARKER + "\nno payload\n"
    good = [_comment(_gate_fail_body(f"acme/widget#{200 + i}")) for i in range(3)]
    comments = [_comment(bad), _comment(bad)] + good
    item = wi.WorkItem(
        id="acme/widget#7", number=7, title="t", body="b",
        url="https://github.com/acme/widget/issues/7", state="OPEN",
        comments=comments)
    assert wi.is_parked(item) is True


def test_is_parked_ignores_non_marker_comments():
    """Comments without the gate-fail marker are not attempts at all, even if the
    item has many of them."""
    item = wi.WorkItem(
        id="acme/widget#7", number=7, title="t", body="b",
        url="https://github.com/acme/widget/issues/7", state="OPEN",
        comments=[_comment("just chatting") for _ in range(10)])
    assert wi.is_parked(item) is False


# ==========================================================================
# E2E Behaviour: PULL EXCLUDES parked items — UNCONDITIONALLY (even with the
# default work_own_filings=True). A parked item never reaches work_items.
# ==========================================================================

def test_pull_excludes_parked_item_default_flag():
    ctx = _fresh_ctx()
    items = [_item_with_distinct_prs(5, number=7), _normal_item(9)]
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
    items = [_item_with_distinct_prs(5, number=7), _normal_item(9)]
    state = wi.Pull(source=_stub_source(items), work_own_filings=True)

    result = state.run(ctx)
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    numbers = {w["number"] for w in ctx.read("work_items")}
    assert numbers == {9}


def test_pull_includes_same_pr_re_gated_item():
    # Same PR re-gated 5 times is ONE distinct attempt -> NOT parked -> included.
    ctx = _fresh_ctx()
    items = [_item_with_pr_refs(["acme/widget#100"] * 5, number=7)]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)
    assert {w["number"] for w in ctx.read("work_items")} == {7}


def test_pull_includes_below_threshold_item():
    # Four distinct failed PRs is below threshold -> NOT parked -> included.
    ctx = _fresh_ctx()
    items = [_item_with_distinct_prs(4, number=7)]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)
    assert {w["number"] for w in ctx.read("work_items")} == {7}


def test_pull_all_parked_emits_empty():
    ctx = _fresh_ctx()
    items = [
        _item_with_distinct_prs(5, number=7),
        _item_with_distinct_prs(6, number=8),
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
        _item_with_distinct_prs(5, number=7),   # 5 distinct PRs -> parked
        _normal_item(10),
        _item_with_distinct_prs(4, number=8),   # 4 distinct PRs -> kept
    ]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    numbers = {w["number"] for w in ctx.read("work_items")}
    # 7 parked (dropped); 8 (4 distinct PRs), 9, 10 kept.
    assert numbers == {8, 9, 10}


# ==========================================================================
# E2E Behaviour: park is a PULL exclusion, NOT a TRIAGE reject. A parked item
# is simply absent from work_items; TRIAGE never sees it, so it is never routed
# to a close path — it stays OPEN on the tracker.
# ==========================================================================

def test_park_is_pull_exclusion_not_triage_reject():
    ctx = _fresh_ctx()
    parked = _item_with_distinct_prs(5, number=7)
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
