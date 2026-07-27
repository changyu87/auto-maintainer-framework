#!/usr/bin/env python3
"""End-to-end + unit tests for the deterministic reject disposition.

A SEMANTICALLY-rejected issue (the AI triager's `decision: rejected` + `reason`)
is disposed of DETERMINISTICALLY at TRIAGE-time — it is COMMENTED and LABELED,
never CLOSED — so a human can see why and the loop stops re-pulling it. The
primitives live here (work-intake owns tracker labels + tracker I/O):

  - REJECTED_LABEL / REJECT_MARKER — the fixed label + comment marker literals.
  - reject_dispositions(work_orders) -> [{work_item_id, issue_ref, reason}] — a
    pure selector returning the disposition payload for every rejected order.
  - gh_issue_reject_sink(issue_ref, repo, reason, label=REJECTED_LABEL,
    runner=...) — the injectable tracker sink: ENSURE label, ONE marked comment
    carrying the reason, add-label; NEVER closes; idempotent no-op when already
    labeled.

Determinism: reject_dispositions is pure; the only non-deterministic edge — the
live `gh` calls — sits behind the injectable subprocess `runner`, so these tests
pass a fake runner (NO network; a failure is locatable to the sink boundary).

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

import work_intake as wi  # noqa: E402


class _Result:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


def _order(work_item_id, decision, reason="", number=None, url=None):
    number = number if number is not None else work_item_id
    url = url if url is not None else (
        f"https://github.com/acme/widget/issues/{number}")
    return wi.WorkOrder(
        id=f"wo-{work_item_id}",
        work_item_id=work_item_id,
        title="t",
        body="b",
        url=url,
        decision=decision,
        reason=reason,
    )


# ==========================================================================
# Behaviour: the fixed literals exist and match the spec's marker strings.
# ==========================================================================

def test_rejected_label_and_marker_literals():
    assert wi.REJECTED_LABEL == "auto-maintainer-rejected"
    assert wi.REJECT_MARKER == "<!-- auto-maintainer:rejected -->"


# ==========================================================================
# Behaviour: reject_dispositions selects ONLY decision=rejected orders, with
# the {work_item_id, issue_ref, reason} payload; accepted orders are dropped.
# ==========================================================================

def test_reject_dispositions_selects_only_rejected_orders():
    orders = [
        _order("acme/widget#1", "accepted"),
        _order("acme/widget#2", "rejected", "advertising spam, unrelated"),
        _order("acme/widget#3", "accepted"),
        _order("acme/widget#4", "rejected", "off-topic, not about this repo"),
    ]
    out = wi.reject_dispositions(orders)

    assert [d["work_item_id"] for d in out] == ["acme/widget#2", "acme/widget#4"]
    assert out[0]["reason"] == "advertising spam, unrelated"
    assert out[1]["reason"] == "off-topic, not about this repo"
    # issue_ref is a gh-actionable reference (the issue URL).
    assert out[0]["issue_ref"] == "https://github.com/acme/widget/issues/2"


def test_reject_dispositions_accepts_machine_first_dicts():
    """work_orders in the slot are machine-first dicts; the selector reads them
    too (scheduling reads the work_orders slot, not WorkOrder objects)."""
    orders = [
        _order("acme/widget#1", "accepted").to_dict(),
        _order("acme/widget#2", "rejected", "spam").to_dict(),
    ]
    out = wi.reject_dispositions(orders)
    assert len(out) == 1
    assert out[0]["work_item_id"] == "acme/widget#2"
    assert out[0]["reason"] == "spam"


def test_reject_dispositions_empty_when_none_rejected():
    orders = [_order("acme/widget#1", "accepted")]
    assert wi.reject_dispositions(orders) == []


# ==========================================================================
# E2E Behaviour: gh_issue_reject_sink ENSURES the label, posts ONE marked
# comment carrying the reason, applies the label — and NEVER closes the issue.
# Driven by an INJECTED fake runner — NO network.
# ==========================================================================

def _reject_runner(existing_labels=None):
    """A fake runner: serves `gh issue view --json labels` from `existing_labels`
    and records every command so a test can assert the exact argv sequence."""
    existing_labels = existing_labels or []
    calls = []

    def runner(cmd, capture_output=True, text=True, check=True):
        calls.append((cmd, {"check": check}))
        if cmd[:3] == ["gh", "issue", "view"]:
            return _Result(json.dumps(
                {"labels": [{"name": n} for n in existing_labels]}))
        return _Result("")

    runner.calls = calls
    return runner


def _argvs(runner):
    return [c for c, _ in runner.calls]


def test_gh_issue_reject_sink_comments_labels_and_never_closes():
    runner = _reject_runner(existing_labels=[])
    wi.gh_issue_reject_sink(
        "https://github.com/acme/widget/issues/2", repo="acme/widget",
        reason="advertising spam, unrelated to this repo", runner=runner)

    argvs = _argvs(runner)
    # It NEVER closes the issue.
    assert not any(c[:3] == ["gh", "issue", "close"] for c in argvs), (
        "reject disposition must NEVER close the issue")

    # The label is ENSURED (idempotent gh label create, check=False tolerated).
    label_create = next(
        (c, k) for c, k in runner.calls if c[:3] == ["gh", "label", "create"])
    assert label_create[0][3] == "auto-maintainer-rejected"
    assert label_create[1]["check"] is False

    # ONE comment carrying the FIXED marker AND the reason.
    comment_cmd = next(c for c in argvs if c[:3] == ["gh", "issue", "comment"])
    body = comment_cmd[comment_cmd.index("--body") + 1]
    assert "<!-- auto-maintainer:rejected -->" in body
    assert "advertising spam, unrelated to this repo" in body

    # The label is APPLIED via gh issue edit --add-label.
    edit_cmd = next(c for c in argvs if c[:3] == ["gh", "issue", "edit"])
    assert "--add-label" in edit_cmd
    assert edit_cmd[edit_cmd.index("--add-label") + 1] == "auto-maintainer-rejected"

    # --repo carries through to the comment + edit.
    assert comment_cmd[comment_cmd.index("--repo") + 1] == "acme/widget"
    assert edit_cmd[edit_cmd.index("--repo") + 1] == "acme/widget"


def test_gh_issue_reject_sink_idempotent_when_already_labeled():
    """If the item already carries REJECTED_LABEL it is a NO-OP: no duplicate
    comment, no re-edit, and (of course) no close."""
    runner = _reject_runner(existing_labels=["auto-maintainer-rejected"])
    wi.gh_issue_reject_sink(
        "https://github.com/acme/widget/issues/2", repo="acme/widget",
        reason="spam", runner=runner)

    argvs = _argvs(runner)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in argvs)
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in argvs)
    assert not any(c[:3] == ["gh", "issue", "close"] for c in argvs)


def test_gh_issue_reject_sink_omits_repo_flag_when_unset():
    runner = _reject_runner(existing_labels=[])
    wi.gh_issue_reject_sink(
        "https://github.com/acme/widget/issues/2", repo=None,
        reason="spam", runner=runner)
    for c in _argvs(runner):
        assert "--repo" not in c


# ==========================================================================
# E2E Behaviour (full disposition pipeline): a batch of triager work_orders is
# selected by reject_dispositions and each reject is enacted through the sink —
# every rejected issue is commented + labeled, and NONE is closed. Accepted
# orders never reach the sink.
# ==========================================================================

def test_reject_disposition_pipeline_e2e():
    orders = [
        _order("acme/widget#1", "accepted"),
        _order("acme/widget#2", "rejected", "advertising spam", number=2),
        _order("acme/widget#3", "rejected", "off-topic", number=3),
    ]

    enacted = []

    def runner(cmd, capture_output=True, text=True, check=True):
        enacted.append(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            return _Result(json.dumps({"labels": []}))
        return _Result("")

    for disp in wi.reject_dispositions(orders):
        wi.gh_issue_reject_sink(
            disp["issue_ref"], repo="acme/widget", reason=disp["reason"],
            runner=runner)

    # Exactly the two rejects were disposed (their issue URLs were acted on).
    commented = [c for c in enacted if c[:3] == ["gh", "issue", "comment"]]
    commented_refs = {c[3] for c in commented}
    assert commented_refs == {
        "https://github.com/acme/widget/issues/2",
        "https://github.com/acme/widget/issues/3",
    }
    # The accepted order's issue (#1) was never touched.
    assert not any("issues/1" in tok for c in enacted for tok in c)
    # No issue was ever closed.
    assert not any(c[:3] == ["gh", "issue", "close"] for c in enacted)
