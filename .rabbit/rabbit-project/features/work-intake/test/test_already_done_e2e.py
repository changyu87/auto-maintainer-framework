#!/usr/bin/env python3
"""End-to-end + unit tests for the already_done on-issue disposition and the
strong-reason guard.

When the IMPLEMENT doer determines the requested change is ALREADY PRESENT ON
`main` it returns the terminal `already_done` handoff. This slice gives that
outcome an on-issue disposition — parallel to the reject disposition — so a human
reading the issue sees the loop resolved it. It is a valid-but-already-satisfied
disposition, NOT a reject, and it NEVER closes the issue:

  - ALREADY_DONE_MARKER — the fixed comment marker, DISTINCT from REJECT_MARKER,
    so an already-done disposition is machine-distinguishable from a reject even
    though the two SHARE the label (REJECTED_LABEL).
  - gh_issue_already_done_sink(issue_ref, repo, reason, label=REJECTED_LABEL,
    runner=...) — the injectable tracker sink mirroring gh_issue_reject_sink:
    ENSURE label, ONE marked comment carrying the reason, add-label; NEVER
    closes; idempotent no-op when the ALREADY_DONE_MARKER is already present.

Plus the strong-reason guard both dispositions consult before enacting:

  - is_strong_reason(reason) -> bool — pure predicate: True iff the reason is
    substantive (>= 40 chars stripped AND free of reflexive-deferral
    boilerplate). No I/O.

Determinism: is_strong_reason is pure; the only non-deterministic edge — the live
`gh` calls — sits behind the injectable subprocess `runner`, so these tests pass
a fake runner (NO network; a failure is locatable to the sink boundary).

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


def _already_done_runner(existing_comments=None):
    """A fake runner: serves `gh issue view --json comments` from
    `existing_comments` (a list of comment-body strings) and records every
    command so a test can assert the exact argv sequence."""
    existing_comments = existing_comments or []
    calls = []

    def runner(cmd, capture_output=True, text=True, check=True):
        calls.append((cmd, {"check": check}))
        if cmd[:3] == ["gh", "issue", "view"]:
            return _Result(json.dumps(
                {"comments": [{"body": b} for b in existing_comments]}))
        return _Result("")

    runner.calls = calls
    return runner


def _argvs(runner):
    return [c for c, _ in runner.calls]


# ==========================================================================
# Behaviour: ALREADY_DONE_MARKER exists and is DISTINCT from REJECT_MARKER.
# ==========================================================================

def test_already_done_marker_literal_and_distinct_from_reject():
    assert wi.ALREADY_DONE_MARKER == "<!-- auto-maintainer:already-done -->"
    assert wi.ALREADY_DONE_MARKER != wi.REJECT_MARKER


# ==========================================================================
# E2E Behaviour: gh_issue_already_done_sink ENSURES the (shared) label, posts
# ONE comment carrying the ALREADY_DONE_MARKER + the reason, applies the label —
# and NEVER closes the issue. Driven by an INJECTED fake runner — NO network.
# ==========================================================================

def test_gh_issue_already_done_sink_comments_labels_and_never_closes():
    runner = _already_done_runner(existing_comments=[])
    reason = ("the requested change is already present on main as of commit "
              "abc1234; no code change was needed")
    wi.gh_issue_already_done_sink(
        "https://github.com/acme/widget/issues/9", repo="acme/widget",
        reason=reason, runner=runner)

    argvs = _argvs(runner)
    # It NEVER closes the issue.
    assert not any(c[:3] == ["gh", "issue", "close"] for c in argvs), (
        "already_done disposition must NEVER close the issue")

    # The (shared) label is ENSURED (idempotent gh label create, check=False).
    label_create = next(
        (c, k) for c, k in runner.calls if c[:3] == ["gh", "label", "create"])
    assert label_create[0][3] == "auto-maintainer-rejected"
    assert label_create[1]["check"] is False

    # ONE comment carrying the ALREADY_DONE_MARKER (NOT the reject marker) AND
    # the reason.
    comment_cmd = next(c for c in argvs if c[:3] == ["gh", "issue", "comment"])
    body = comment_cmd[comment_cmd.index("--body") + 1]
    assert "<!-- auto-maintainer:already-done -->" in body
    assert "<!-- auto-maintainer:rejected -->" not in body
    assert reason in body

    # The (shared) label is APPLIED via gh issue edit --add-label.
    edit_cmd = next(c for c in argvs if c[:3] == ["gh", "issue", "edit"])
    assert "--add-label" in edit_cmd
    assert edit_cmd[edit_cmd.index("--add-label") + 1] == "auto-maintainer-rejected"

    # --repo carries through to the comment + edit.
    assert comment_cmd[comment_cmd.index("--repo") + 1] == "acme/widget"
    assert edit_cmd[edit_cmd.index("--repo") + 1] == "acme/widget"


def test_gh_issue_already_done_sink_idempotent_when_marker_present():
    """If the issue already carries an ALREADY_DONE_MARKER comment it is a NO-OP:
    no duplicate comment, no re-edit, and (of course) no close. Idempotency keys
    off the MARKER (not the label — the label is shared with reject)."""
    runner = _already_done_runner(existing_comments=[
        "<!-- auto-maintainer:already-done -->\nalready fixed earlier"])
    wi.gh_issue_already_done_sink(
        "https://github.com/acme/widget/issues/9", repo="acme/widget",
        reason="present on main", runner=runner)

    argvs = _argvs(runner)
    assert not any(c[:3] == ["gh", "issue", "comment"] for c in argvs)
    assert not any(c[:3] == ["gh", "issue", "edit"] for c in argvs)
    assert not any(c[:3] == ["gh", "issue", "close"] for c in argvs)


def test_gh_issue_already_done_sink_not_deduped_by_reject_marker():
    """A prior REJECT_MARKER comment must NOT suppress an already_done comment —
    the two markers are distinct dispositions even though they share the label."""
    runner = _already_done_runner(existing_comments=[
        "<!-- auto-maintainer:rejected -->\nrejected earlier for some reason"])
    wi.gh_issue_already_done_sink(
        "https://github.com/acme/widget/issues/9", repo="acme/widget",
        reason="the change is already present on main; nothing to do here",
        runner=runner)
    argvs = _argvs(runner)
    # A fresh already_done comment IS posted (the reject marker does not dedup it).
    comment_cmd = next(c for c in argvs if c[:3] == ["gh", "issue", "comment"])
    body = comment_cmd[comment_cmd.index("--body") + 1]
    assert "<!-- auto-maintainer:already-done -->" in body


def test_gh_issue_already_done_sink_omits_repo_flag_when_unset():
    runner = _already_done_runner(existing_comments=[])
    wi.gh_issue_already_done_sink(
        "https://github.com/acme/widget/issues/9", repo=None,
        reason="the change is already present on main; nothing to do here",
        runner=runner)
    for c in _argvs(runner):
        assert "--repo" not in c


# ==========================================================================
# Behaviour: is_strong_reason accepts a concrete, substantive reason and
# rejects a short one or a reflexive-deferral boilerplate phrase. Pure, no I/O.
# ==========================================================================

def test_is_strong_reason_accepts_concrete_multi_clause_reason():
    assert wi.is_strong_reason(
        "advertising spam unrelated to this repository; links to an external "
        "product store and has no actionable maintenance content") is True


def test_is_strong_reason_rejects_short_reason():
    # A 10-char reason is below the 40-char minimum.
    assert wi.is_strong_reason("too short.") is False
    assert len("too short.") == 10


def test_is_strong_reason_rejects_boilerplate_phrases():
    for weak in ("deferred", "todo", "will look into", "not sure", "later",
                 "n/a", "no reason", "as discussed", "see above", "wontfix"):
        assert wi.is_strong_reason(weak) is False, weak


def test_is_strong_reason_rejects_padded_boilerplate_only_text():
    """A reason that clears the length bar but is composed SOLELY of boilerplate
    phrases + separators is still weak."""
    assert wi.is_strong_reason(
        "todo, deferred, will look into, later, not sure, as discussed") is False


def test_is_strong_reason_rejects_non_string():
    assert wi.is_strong_reason(None) is False
