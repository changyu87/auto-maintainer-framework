#!/usr/bin/env python3
"""End-to-end + unit tests for the In-flight guard (convergence).

An issue whose work is ALREADY IN FLIGHT — an open auto-maintainer PR is already
addressing it — must NOT be re-triaged or re-implemented; doing so re-opens
duplicate/superseding PRs and burns tokens re-deriving work the open PR already
carries. PULL therefore UNCONDITIONALLY EXCLUDES any candidate work_item whose
issue ref is a member of an injected `in_flight_issue_refs` set, via the pure
`is_in_flight(item, in_flight_issue_refs)` predicate.

Bounded scope: work-intake CONSUMES the set (threaded in by scheduling, computed
from EXISTING verify-integrate open-PR / closing-issue seams and/or the acted
ledger's `opened` entries) — it adds NO new gh plumbing and NO new cross-feature
reads. The excluded issue is LEFT OPEN and untouched (PULL neither closes,
comments on, nor labels it — its open PR's own lifecycle resolves it). Like the
loopback and park guards this is a PULL exclusion, NOT a TRIAGE reject.
Merged/closed PRs do NOT exclude: the set carries ONLY refs with an OPEN loop PR.
`Pull(in_flight_issue_refs=...)` DEFAULTS to the empty set, a no-op that pulls
every open issue exactly as before (non-breaking).

These tests are fully deterministic — no network. They cover:
  1. is_in_flight true iff the item's issue ref (owner/repo#N, derived from the
     item's url/number) is in the injected set; pure (WorkItem or dict); tolerant
     of a full-URL item and of full-URL / short-form set entries; empty set is
     always False.
  2. PULL EXCLUDES an in-flight item — UNCONDITIONALLY (even with the default
     work_own_filings=True) — and leaves it untouched.
  3. A candidate NOT in the set (no loop PR, or only a merged/closed PR) is kept.
  4. The default (no in_flight_issue_refs) is a no-op: every open issue pulled.
  5. A mixed batch drops ONLY the in-flight items; all in-flight -> EMPTY.
  6. In-flight is a PULL exclusion, NOT a TRIAGE reject.

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


def _item(number, labels=None):
    """A normal open WorkItem for acme/widget#<number>."""
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title=f"Issue {number}",
        body="Please fix ...",
        url=f"https://github.com/acme/widget/issues/{number}",
        state="OPEN",
        labels=list(labels or ["bug"]),
        author="octocat",
        created_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-02T11:30:00Z",
        comments=[],
    )


def _stub_source(items):
    def source(repo=None, issue_filter=None):
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
# Behaviour: is_in_flight is a pure predicate over the item's issue ref.
# ==========================================================================

def test_is_in_flight_true_when_ref_in_set():
    item = _item(7)
    refs = {"acme/widget#7"}
    assert wi.is_in_flight(item, refs) is True
    assert wi.is_in_flight(item.to_dict(), refs) is True


def test_is_in_flight_false_when_ref_not_in_set():
    item = _item(9)
    refs = {"acme/widget#7"}
    assert wi.is_in_flight(item, refs) is False
    assert wi.is_in_flight(item.to_dict(), refs) is False


def test_is_in_flight_empty_set_is_always_false():
    item = _item(7)
    assert wi.is_in_flight(item, set()) is False
    assert wi.is_in_flight(item, frozenset()) is False
    assert wi.is_in_flight(item, None) is False


def test_is_in_flight_matches_regardless_of_url_form():
    """The item's ref is derived as owner/repo#N from its url/number, so a
    full-URL item matches a short-form (owner/repo#N) set entry."""
    item = _item(7)  # url is a full github.com/.../issues/7 URL
    assert wi.is_in_flight(item, {"acme/widget#7"}) is True


def test_is_in_flight_tolerates_full_url_set_entry():
    """A set entry given as a full issue URL is normalized to owner/repo#N so it
    still matches an item whose ref is the short form."""
    item = _item(7)
    assert wi.is_in_flight(
        item, {"https://github.com/acme/widget/issues/7"}) is True


def test_is_in_flight_does_not_match_different_repo_same_number():
    item = _item(7)
    assert wi.is_in_flight(item, {"other/repo#7"}) is False


# ==========================================================================
# E2E Behaviour: PULL EXCLUDES an in-flight item — UNCONDITIONALLY.
# ==========================================================================

def test_pull_excludes_in_flight_item_default_flag():
    ctx = _fresh_ctx()
    items = [_item(7), _item(9)]
    state = wi.Pull(source=_stub_source(items),
                    in_flight_issue_refs={"acme/widget#7"})

    result = state.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    numbers = {w["number"] for w in ctx.read("work_items")}
    # The in-flight item (7) is dropped; the other (9) survives.
    assert numbers == {9}


def test_pull_excludes_in_flight_item_unconditional_of_work_own_filings():
    # The in-flight exclusion is UNCONDITIONAL — not gated on work_own_filings.
    for flag in (True, False):
        ctx = _fresh_ctx()
        items = [_item(7), _item(9)]
        state = wi.Pull(source=_stub_source(items),
                        work_own_filings=flag,
                        in_flight_issue_refs={"acme/widget#7"})
        result = state.run(ctx)
        fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                        fc.SignalVocabulary(wi.PULL_SIGNALS))
        numbers = {w["number"] for w in ctx.read("work_items")}
        assert numbers == {9}, f"work_own_filings={flag}"


def test_pull_default_no_in_flight_set_is_noop():
    # No in_flight_issue_refs passed -> pulls every open issue exactly as before.
    ctx = _fresh_ctx()
    items = [_item(7), _item(9)]
    state = wi.Pull(source=_stub_source(items))

    result = state.run(ctx)
    assert result.signal == "OK"
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    assert {w["number"] for w in ctx.read("work_items")} == {7, 9}


def test_pull_merged_or_closed_pr_not_in_set_is_kept():
    """An issue whose only loop PR is merged/closed is NOT in the injected set
    (which carries ONLY refs with an OPEN loop PR), so it flows through PULL."""
    ctx = _fresh_ctx()
    items = [_item(7)]
    # Set is empty for #7 — its PR is merged/closed, so it is not in-flight.
    state = wi.Pull(source=_stub_source(items), in_flight_issue_refs=set())

    result = state.run(ctx)
    assert result.signal == "OK"
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    assert {w["number"] for w in ctx.read("work_items")} == {7}


def test_pull_mixed_batch_drops_only_in_flight():
    ctx = _fresh_ctx()
    items = [_item(7), _item(8), _item(9), _item(10)]
    state = wi.Pull(source=_stub_source(items),
                    in_flight_issue_refs={"acme/widget#8", "acme/widget#10"})

    result = state.run(ctx)
    assert result.signal == "OK"
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    assert {w["number"] for w in ctx.read("work_items")} == {7, 9}


def test_pull_all_in_flight_emits_empty():
    ctx = _fresh_ctx()
    items = [_item(7), _item(8)]
    state = wi.Pull(source=_stub_source(items),
                    in_flight_issue_refs={"acme/widget#7", "acme/widget#8"})

    result = state.run(ctx)
    assert result.signal == "EMPTY"
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    assert ctx.read("work_items") == []


# ==========================================================================
# E2E Behaviour: in-flight is a PULL exclusion, NOT a TRIAGE reject. The
# excluded item never enters work_items, so TRIAGE never sees it (no reject,
# no close path) — it stays OPEN on the tracker, resolved by its own PR.
# ==========================================================================

def test_in_flight_is_pull_exclusion_not_triage_reject():
    ctx = _fresh_ctx()
    state = wi.Pull(source=_stub_source([_item(7)]),
                    in_flight_issue_refs={"acme/widget#7"})

    result = state.run(ctx)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))

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
    # No work_order for the in-flight item — no reject, no close path.
    assert ctx.read("work_orders") == []
