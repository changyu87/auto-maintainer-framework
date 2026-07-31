#!/usr/bin/env python3
"""End-to-end + unit tests for the verify-integrate RECONCILE-support surface.

RECONCILE is a deterministic, SCRIPT-TIER, ADVISORY reconciler of the PREVIOUS
tick's leftover loop PRs (DESIGN §3.7 convergence; mirrors Integrate). Scheduling's
make_reconcile wraps it into a route state run BEFORE PULL — the wiring is NOT owned
here; this feature owns the reconcile LOGIC + the ReconcileResult schema.

It reads an injected `acted_ledger` slot (the durable `opened` entries) and, per
entry, reads the PR's live state via injectable seams:
  - (A) a MERGED PR whose source issue is still OPEN has its issue CLOSED (the
    Closes-keyword fallback) — never a human-closed issue.
  - (B) an OPEN + CONFLICTING PR is recovered by a TIER-1 rebase (worktree helper +
    force-push); a real textual conflict falls back to TIER-2 close-PR + comment-
    issue re-land.

Every mutating act is trust-gated by permits('merge', mode) (auto-merge only); at
dry-run/propose the would-act intent is recorded under `skipped`. RECONCILE is
ADVISORY: a single-entry fault is recorded under `errors`, never raised, and the
tick still emits OK.

The e2e tests drive RECONCILE exactly as scheduling will — building a real
fsm-contracts TickContext, registering the slots, running the state, and committing
its StateResult through `fc.apply_result` under the manifest + signal vocabulary.
Every GitHub/git effect is behind an injected FAKE — no network, no real git.

Owner: changyu87
"""

import json
import os
import sys
import types

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
_FSM_SRC = os.path.join(_FEATURES_DIR, "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

# safety-governance is a sibling consumed UNCHANGED (permits); put its src/ on the
# path so RECONCILE resolves it by module name, mirroring the other adapters.
_SG_SRC = os.path.join(_FEATURES_DIR, "safety-governance", "src")
if _SG_SRC not in sys.path:
    sys.path.insert(0, _SG_SRC)
_LD_SRC = os.path.join(_FEATURES_DIR, "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

import fsm_contracts as fc  # noqa: E402
import safety_governance as sg  # noqa: E402
import verify_integrate as vi  # noqa: E402


_DEFAULT_BRANCH = "main"
_REPO = "acme/widget"


# --------------------------------------------------------------------------
# Fixtures — ledger entry builder, fake sources/sinks/helper, fresh ctx.
# --------------------------------------------------------------------------

def _entry(number=1, issue=None, repo=_REPO, work_order_id=None):
    """An `opened` acted-ledger entry (the shape scheduling seeds)."""
    pr_ref = f"{repo}#{number}"
    return {
        "work_order_id": work_order_id or f"wo-{number}",
        "pr_ref": pr_ref,
        "issue_ref": (f"{repo}#{issue}" if issue is not None else None),
        "repo": repo,
    }


def _pr_state_source(states):
    """A pr_state_source over a {pr_ref: {state, merged, mergeable}} map. Records
    the pr_refs it was queried for."""
    calls = []

    def source(pr_ref, repo=None):  # noqa: ARG001
        calls.append(pr_ref)
        return dict(states[pr_ref])

    source.calls = calls
    return source


def _issue_state_source(states):
    """An issue_state_source over a {issue_ref: {state}} map. Records queries."""
    calls = []

    def source(issue_ref, repo=None):  # noqa: ARG001
        calls.append(issue_ref)
        return dict(states.get(issue_ref, {"state": "OPEN"}))

    source.calls = calls
    return source


def _recording_issue_close_sink():
    calls = []

    def sink(issue_ref, repo=None, comment=None):
        calls.append({"issue_ref": issue_ref, "repo": repo, "comment": comment})

    sink.calls = calls
    return sink


def _recording_pr_close_sink():
    calls = []

    def sink(pr_ref, repo=None):
        calls.append({"pr_ref": pr_ref, "repo": repo})

    sink.calls = calls
    return sink


def _recording_comment_sink():
    calls = []

    def sink(issue_ref, body, repo=None):
        calls.append({"issue_ref": issue_ref, "body": body, "repo": repo})

    sink.calls = calls
    return sink


def _open_pr_closing_issue_source(entries=()):
    """A gh_open_pr_closing_issue_source over a list of {pr_ref, url, issue_ref}
    entries (the LIVE open loop-PR set with each PR's first closing-issue ref).
    Records calls. Defaults to the EMPTY set (no same-issue duplicates)."""
    calls = []

    def source(repo=None):  # noqa: ARG001
        calls.append(repo)
        return [dict(e) for e in entries]

    source.calls = calls
    return source


def _worktree_helper(rebased_by_ref):
    """A tier-1 rebase worktree helper over a {pr_ref: bool} map (True = clean
    rebase + force-push; False = real conflict). Records calls."""
    calls = []

    def helper(pr_ref, default_branch, repo=None):
        calls.append({"pr_ref": pr_ref, "default_branch": default_branch,
                      "repo": repo})
        return {"rebased": rebased_by_ref[pr_ref]}

    helper.calls = calls
    return helper


def _never_helper():
    """A worktree helper that fails the test if called (the not-permitted paths
    and the branch-A path must NEVER invoke it)."""
    def helper(pr_ref, default_branch, repo=None):  # noqa: ARG001
        raise AssertionError("worktree_helper must not be called here")
    return helper


def _fresh_ctx():
    """A TickContext with the slots RECONCILE touches: the injected acted_ledger
    (scheduling-seeded, registered here as a generic array) and reconcile_result."""
    ctx = fc.TickContext()
    ctx.register_slot("acted_ledger", {"type": "array"}, version="1.0.0")
    ctx.register_slot(
        vi.RECONCILE_RESULT_SLOT["name"],
        vi.RECONCILE_RESULT_SLOT["schema"],
        version=vi.RECONCILE_RESULT_SLOT["version"],
    )
    return ctx


def _run(reconcile, ledger):
    """Drive RECONCILE end-to-end: seed the ledger, run, commit through
    fc.apply_result under the manifest + signal vocab, return reconcile_result."""
    ctx = _fresh_ctx()
    ctx.write("acted_ledger", ledger)
    result = reconcile.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(vi.RECONCILE_SIGNALS)
    fc.apply_result(ctx, vi.RECONCILE_MANIFEST, result, vocab)
    return ctx.read("reconcile_result")


def _auto_reconcile(**kw):
    """A Reconcile at auto-merge mode with the REAL sg.permits, defaulting the
    injected seams the individual test overrides."""
    kw.setdefault("default_branch", _DEFAULT_BRANCH)
    kw.setdefault("repo", _REPO)
    # Default the (C) same-issue dedup source to the EMPTY set so tests that do
    # not exercise dedup drive no gh call and see no dedup effect.
    kw.setdefault("open_pr_closing_issue_source",
                  _open_pr_closing_issue_source())
    return vi.Reconcile(mode="auto-merge", **kw)


# ==========================================================================
# Schema: ReconcileResult is typed, machine-first, versioned, round-trips.
# ==========================================================================

def test_reconcile_result_round_trip():
    r = vi.ReconcileResult(
        closed_issues=[{"issue_ref": "acme/widget#5", "pr_ref": "acme/widget#7"}],
        rebased=[{"pr_ref": "acme/widget#8"}],
        relanded=[{"pr_ref": "acme/widget#9", "issue_ref": "acme/widget#3"}],
        skipped=[{"ref": "acme/widget#10", "reason": "would recover"}],
        errors=[{"ref": "acme/widget#11", "reason": "boom"}],
    )
    d = r.to_dict()
    assert d["schema_version"] == vi.RECONCILE_RESULT_SCHEMA_VERSION
    assert d["closed_issues"] == [{"issue_ref": "acme/widget#5",
                                   "pr_ref": "acme/widget#7"}]
    assert d["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert d["relanded"] == [{"pr_ref": "acme/widget#9",
                              "issue_ref": "acme/widget#3"}]
    assert vi.ReconcileResult.from_dict(d) == r


def test_reconcile_result_empty_round_trip():
    r = vi.ReconcileResult()
    d = r.to_dict()
    assert d["closed_issues"] == []
    assert d["rebased"] == []
    assert d["relanded"] == []
    assert d["skipped"] == []
    assert d["errors"] == []
    assert d["deduped"] == []
    assert d["auto_merged"] == []
    assert vi.ReconcileResult.from_dict(d) == r


def test_reconcile_result_deduped_round_trips():
    r = vi.ReconcileResult(
        deduped=[{"pr_ref": "acme/widget#7", "issue_ref": "acme/widget#3",
                  "kept_pr_ref": "acme/widget#9"}])
    d = r.to_dict()
    assert d["deduped"] == [{"pr_ref": "acme/widget#7",
                             "issue_ref": "acme/widget#3",
                             "kept_pr_ref": "acme/widget#9"}]
    assert vi.ReconcileResult.from_dict(d) == r


def test_reconcile_result_auto_merged_round_trips():
    # (A) auto_merged: every merged acted_ledger PR seen this tick — a pure
    # observability record (no GitHub write), kept SEPARATE from closed_issues.
    r = vi.ReconcileResult(
        auto_merged=[{"pr_ref": "acme/widget#7", "issue_ref": "acme/widget#3"}])
    d = r.to_dict()
    assert d["auto_merged"] == [{"pr_ref": "acme/widget#7",
                                 "issue_ref": "acme/widget#3"}]
    assert vi.ReconcileResult.from_dict(d) == r


def test_reconcile_result_1_1_0_dict_deserializes_auto_merged_empty():
    # A 1.1.0-shaped dict has NO auto_merged key; from_dict defaults it to [] so a
    # prior-version result deserializes unchanged (additive, back-compatible).
    legacy = {
        "schema_version": "1.1.0",
        "closed_issues": [],
        "rebased": [],
        "relanded": [],
        "deduped": [],
        "skipped": [],
        "errors": [],
    }
    r = vi.ReconcileResult.from_dict(legacy)
    assert r.auto_merged == []


def test_reconcile_result_schema_version_is_1_2_0():
    # Additive bump: the `auto_merged` list was added to the 1.1.0 shape (which
    # had added `deduped` to 1.0.0) — auto-merge-completion observability (A).
    assert vi.RECONCILE_RESULT_SCHEMA_VERSION == "1.2.0"


def test_reconcile_result_slot_descriptor_is_versioned():
    slot = vi.RECONCILE_RESULT_SLOT
    assert slot["name"] == "reconcile_result"
    assert slot["schema"] == {"type": "object"}
    assert slot["version"] == vi.RECONCILE_RESULT_SCHEMA_VERSION


def test_reconcile_manifest_declares_reads_writes_emits():
    m = vi.RECONCILE_MANIFEST
    # `prior_verdicts` (the previous tick's VERIFY verdicts) is the OPTIONAL (B)
    # race-breaker read; scheduling's make_reconcile seeds it.
    assert list(m.reads) == ["acted_ledger", "prior_verdicts"]
    assert list(m.writes) == ["reconcile_result"]
    assert list(m.emits) == ["OK"]


def test_reconcile_signal_vocabulary_is_closed():
    assert vi.RECONCILE_SIGNALS == ["OK"]
    vocab = fc.SignalVocabulary(vi.RECONCILE_SIGNALS)
    assert vocab.is_member("OK")
    assert not vocab.is_member("EMPTY")


# ==========================================================================
# (A) Merged-PR issue-close fallback.
# ==========================================================================

def test_reconcile_e2e_merged_pr_open_issue_closes_issue():
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})
    iss = _issue_state_source({"acme/widget#5": {"state": "OPEN"}})
    close = _recording_issue_close_sink()
    reconcile = _auto_reconcile(pr_state_source=pr, issue_state_source=iss,
                                issue_close_sink=close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=5)])

    assert res["closed_issues"] == [{"issue_ref": "acme/widget#5",
                                     "pr_ref": "acme/widget#7"}]
    assert res["rebased"] == []
    assert res["relanded"] == []
    # (A) auto_merged records the merged PR too — BOTH auto_merged and
    # closed_issues carry it when the issue was still open and got closed.
    assert res["auto_merged"] == [{"pr_ref": "acme/widget#7",
                                   "issue_ref": "acme/widget#5"}]
    assert len(close.calls) == 1
    assert close.calls[0]["issue_ref"] == "acme/widget#5"
    # the close comment NAMES the merged PR (attributable convergence write).
    assert "acme/widget#7" in close.calls[0]["comment"]


def test_reconcile_e2e_merged_pr_closed_issue_records_auto_merged_only():
    # A MERGED PR whose issue is already CLOSED is recorded in auto_merged
    # (observability of the async completion) but NOT in closed_issues (the
    # still-open-issue close path is unchanged and never re-closes/comments).
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})
    iss = _issue_state_source({"acme/widget#5": {"state": "CLOSED"}})
    close = _recording_issue_close_sink()
    reconcile = _auto_reconcile(pr_state_source=pr, issue_state_source=iss,
                                issue_close_sink=close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=5)])

    assert res["auto_merged"] == [{"pr_ref": "acme/widget#7",
                                   "issue_ref": "acme/widget#5"}]
    assert res["closed_issues"] == []
    # auto_merged drives NO GitHub write — the issue-close sink was never called.
    assert close.calls == []


def test_reconcile_e2e_open_pr_not_in_auto_merged():
    # A non-merged (OPEN, mergeable) PR is not recorded in auto_merged — only a
    # MERGED ledger PR is.
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "MERGEABLE"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=8, issue=3)])

    assert res["auto_merged"] == []


def test_reconcile_e2e_merged_pr_closed_issue_untouched():
    # A MERGED PR whose issue is already CLOSED (e.g. human-closed) is NEVER
    # touched — only a MERGED-PR-with-still-OPEN issue is closed.
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})
    iss = _issue_state_source({"acme/widget#5": {"state": "CLOSED"}})
    close = _recording_issue_close_sink()
    reconcile = _auto_reconcile(pr_state_source=pr, issue_state_source=iss,
                                issue_close_sink=close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=5)])

    assert res["closed_issues"] == []
    assert close.calls == []


def test_reconcile_e2e_merged_pr_no_issue_ref_is_noop():
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})
    close = _recording_issue_close_sink()
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                issue_close_sink=close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=None)])

    assert res["closed_issues"] == []
    assert close.calls == []


# ==========================================================================
# (B) Conflict-recovery ladder — tier 1 rebase / tier 2 re-land.
# ==========================================================================

def test_reconcile_e2e_conflicting_clean_rebase_records_rebased():
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})
    helper = _worktree_helper({"acme/widget#8": True})
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run(reconcile, [_entry(number=8, issue=3)])

    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert res["relanded"] == []
    # tier-1 rebase ran (the helper force-pushes internally); no PR was closed.
    assert len(helper.calls) == 1
    assert helper.calls[0]["pr_ref"] == "acme/widget#8"
    assert helper.calls[0]["default_branch"] == _DEFAULT_BRANCH
    assert prclose.calls == []
    assert comment.calls == []


def test_reconcile_e2e_conflicting_dirty_relands():
    pr = _pr_state_source({
        "acme/widget#9": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})
    helper = _worktree_helper({"acme/widget#9": False})  # real conflict
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run(reconcile, [_entry(number=9, issue=3)])

    assert res["relanded"] == [{"pr_ref": "acme/widget#9",
                                "issue_ref": "acme/widget#3"}]
    assert res["rebased"] == []
    # tier-2: the PR was CLOSED and its issue COMMENTED to re-land next tick.
    assert prclose.calls == [{"pr_ref": "acme/widget#9", "repo": _REPO}]
    assert len(comment.calls) == 1
    assert comment.calls[0]["issue_ref"] == "acme/widget#3"
    assert "acme/widget#9" in comment.calls[0]["body"]


# ==========================================================================
# Trust gating — at dry-run/propose the would-act intent is recorded under
# skipped and NO sink/helper is ever called.
# ==========================================================================

def test_reconcile_e2e_dry_run_all_skipped_no_effects():
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"},
        "acme/widget#9": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})
    iss = _issue_state_source({"acme/widget#5": {"state": "OPEN"}})
    close = _recording_issue_close_sink()
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = vi.Reconcile(
        mode="dry-run", pr_state_source=pr, issue_state_source=iss,
        issue_close_sink=close, pr_close_sink=prclose, comment_sink=comment,
        worktree_helper=_never_helper(), default_branch=_DEFAULT_BRANCH,
        repo=_REPO,
        open_pr_closing_issue_source=_open_pr_closing_issue_source())

    res = _run(reconcile, [_entry(number=7, issue=5), _entry(number=9, issue=3)])

    assert res["closed_issues"] == []
    assert res["rebased"] == []
    assert res["relanded"] == []
    assert {s["ref"] for s in res["skipped"]} == {"acme/widget#5",
                                                  "acme/widget#9"}
    assert close.calls == []
    assert prclose.calls == []
    assert comment.calls == []


def test_reconcile_e2e_propose_conflicting_skipped():
    pr = _pr_state_source({
        "acme/widget#9": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})
    reconcile = vi.Reconcile(
        mode="propose", pr_state_source=pr,
        issue_state_source=_issue_state_source({}),
        worktree_helper=_never_helper(), default_branch=_DEFAULT_BRANCH,
        repo=_REPO,
        open_pr_closing_issue_source=_open_pr_closing_issue_source())

    res = _run(reconcile, [_entry(number=9, issue=3)])

    assert res["rebased"] == []
    assert res["relanded"] == []
    assert [s["ref"] for s in res["skipped"]] == ["acme/widget#9"]


def test_reconcile_uses_real_safety_governance_permits():
    # Sanity: the real gate permits merge only at auto-merge.
    assert sg.permits("merge", "auto-merge") is True
    assert sg.permits("merge", "propose") is False


# ==========================================================================
# Advisory: an OPEN mergeable PR is left alone; a single-entry fault is
# recorded under errors and the tick still emits OK.
# ==========================================================================

def test_reconcile_e2e_open_mergeable_pr_left_alone():
    pr = _pr_state_source({
        "acme/widget#4": {"state": "OPEN", "merged": False,
                          "mergeable": "MERGEABLE"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=4, issue=3)])

    assert res["closed_issues"] == []
    assert res["rebased"] == []
    assert res["relanded"] == []
    assert res["skipped"] == []
    assert res["errors"] == []


def test_reconcile_e2e_single_entry_fault_recorded_tick_ok():
    def boom_pr_state(pr_ref, repo=None):  # noqa: ARG001
        if pr_ref == "acme/widget#7":
            raise RuntimeError("gh pr view exploded")
        return {"state": "MERGED", "merged": True, "mergeable": "UNKNOWN"}

    iss = _issue_state_source({"acme/widget#6": {"state": "OPEN"}})
    close = _recording_issue_close_sink()
    reconcile = _auto_reconcile(pr_state_source=boom_pr_state,
                                issue_state_source=iss, issue_close_sink=close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=5), _entry(number=8, issue=6)])

    # the faulting entry is recorded under errors, the OTHER entry still processed.
    assert [e["ref"] for e in res["errors"]] == ["acme/widget#7"]
    assert "gh pr view exploded" in res["errors"][0]["reason"]
    assert res["closed_issues"] == [{"issue_ref": "acme/widget#6",
                                     "pr_ref": "acme/widget#8"}]


def test_reconcile_e2e_close_sink_fault_recorded_never_raises():
    def raising_close(issue_ref, repo=None, comment=None):  # noqa: ARG001
        raise RuntimeError("gh issue close 403")

    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})
    iss = _issue_state_source({"acme/widget#5": {"state": "OPEN"}})
    reconcile = _auto_reconcile(pr_state_source=pr, issue_state_source=iss,
                                issue_close_sink=raising_close,
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=7, issue=5)])

    assert res["closed_issues"] == []
    assert [e["ref"] for e in res["errors"]] == ["acme/widget#7"]


def test_reconcile_e2e_empty_ledger_is_ok_all_empty():
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())
    res = _run(reconcile, [])
    assert res == {
        "schema_version": vi.RECONCILE_RESULT_SCHEMA_VERSION,
        "closed_issues": [], "rebased": [], "relanded": [],
        "skipped": [], "errors": [], "deduped": [], "auto_merged": [],
    }


def test_reconcile_e2e_mixed_batch_partitions():
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"},   # A -> closed_issues
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"},  # B tier1 -> rebased
        "acme/widget#9": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"},  # B tier2 -> relanded
        "acme/widget#4": {"state": "OPEN", "merged": False,
                          "mergeable": "MERGEABLE"}})   # ignored
    iss = _issue_state_source({"acme/widget#5": {"state": "OPEN"}})
    helper = _worktree_helper({"acme/widget#8": True, "acme/widget#9": False})
    close = _recording_issue_close_sink()
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr, issue_state_source=iss,
                                issue_close_sink=close, worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run(reconcile, [
        _entry(number=7, issue=5),
        _entry(number=8, issue=1),
        _entry(number=9, issue=2),
        _entry(number=4, issue=3),
    ])

    assert res["closed_issues"] == [{"issue_ref": "acme/widget#5",
                                     "pr_ref": "acme/widget#7"}]
    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert res["relanded"] == [{"pr_ref": "acme/widget#9",
                                "issue_ref": "acme/widget#2"}]
    assert res["skipped"] == []
    assert res["errors"] == []


# ==========================================================================
# Production seams (deterministic, exercised with a FAKE runner — no network).
# ==========================================================================

def _proc(stdout="", stderr="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, stderr=stderr,
                                 returncode=returncode)


def test_gh_pr_state_source_shape_merged():
    seen = []

    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        seen.append(cmd)
        return _proc(stdout=json.dumps({
            "state": "MERGED", "mergedAt": "2026-07-24T00:00:00Z",
            "mergeable": "UNKNOWN"}))

    out = vi.gh_pr_state_source("acme/widget#7", repo="acme/widget", runner=runner)
    assert out == {"state": "MERGED", "merged": True, "mergeable": "UNKNOWN"}
    assert seen[0][:3] == ["gh", "pr", "view"]
    assert "acme/widget" in seen[0]


def test_gh_pr_state_source_shape_open_not_merged():
    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        return _proc(stdout=json.dumps({
            "state": "OPEN", "mergedAt": None, "mergeable": "CONFLICTING"}))

    out = vi.gh_pr_state_source("acme/widget#9", runner=runner)
    assert out == {"state": "OPEN", "merged": False, "mergeable": "CONFLICTING"}


def test_gh_issue_state_source_shape():
    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        assert cmd[:3] == ["gh", "issue", "view"]
        return _proc(stdout="OPEN\n")

    assert vi.gh_issue_state_source("acme/widget#5", runner=runner) == {
        "state": "OPEN"}


def test_gh_issue_close_sink_command():
    seen = []

    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        seen.append(cmd)
        return _proc()

    vi.gh_issue_close_sink("acme/widget#5", repo="acme/widget",
                           comment="done", runner=runner)
    cmd = seen[0]
    assert cmd[:3] == ["gh", "issue", "close"]
    assert "5" in cmd
    assert "--comment" in cmd and "done" in cmd
    assert "--repo" in cmd and "acme/widget" in cmd


# --------------------------------------------------------------------------
# reconcile_rebase_worktree: the production TIER-1 helper, exercised with a
# scripted fake runner (no real git). Clean rebase -> force-push + rebased True;
# a real conflict -> rebase --abort + rebased False (no push).
# --------------------------------------------------------------------------

def _scripted_git_runner(rebase_rc):
    """A fake runner that scripts the git/gh commands the tier-1 helper issues,
    with the `git rebase` return code configurable. Records every command."""
    seen = []

    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        seen.append(cmd)
        # _gh_pr_head_ref
        if cmd[:3] == ["gh", "pr", "view"]:
            return _proc(stdout="feature-branch\n")
        # git rebase origin/<default> (not the --abort form)
        if len(cmd) >= 5 and cmd[3] == "rebase" and "--abort" not in cmd:
            return _proc(returncode=rebase_rc, stdout="CONFLICT" if rebase_rc
                         else "")
        return _proc(returncode=0)

    runner.seen = seen
    return runner


def test_reconcile_rebase_worktree_clean_force_pushes():
    runner = _scripted_git_runner(rebase_rc=0)
    out = vi.reconcile_rebase_worktree(
        "acme/widget#8", _DEFAULT_BRANCH, repo="acme/widget", runner=runner,
        worktree_dir="/tmp/am-reconcile-test")
    assert out["rebased"] is True
    # a clean rebase force-pushes the rebased branch back onto the PR head.
    pushes = [c for c in runner.seen
              if "push" in c and "--force" in c]
    assert len(pushes) == 1
    assert "HEAD:feature-branch" in pushes[0]


def test_reconcile_rebase_worktree_conflict_returns_false_no_push():
    runner = _scripted_git_runner(rebase_rc=1)
    out = vi.reconcile_rebase_worktree(
        "acme/widget#9", _DEFAULT_BRANCH, repo="acme/widget", runner=runner,
        worktree_dir="/tmp/am-reconcile-test")
    assert out["rebased"] is False
    # a real conflict aborts the rebase and NEVER force-pushes.
    assert any(c[-1] == "--abort" for c in runner.seen if "rebase" in c)
    assert not any("push" in c and "--force" in c for c in runner.seen)


# ==========================================================================
# (C) Same-issue open-PR dedup (deterministic supersede backstop).
#
# Two open auto-maintainer PRs closing the SAME still-open issue -> KEEP the
# highest-numbered PR (the newest re-land), CLOSE the rest via the existing
# PR-close sink, record under `deduped`. NEVER touch the sole PR for an issue,
# NEVER a group whose issue is closed, NEVER cross issues. Trust-gated exactly
# like INTEGRATE; a close fault lands under `errors`.
# ==========================================================================

def _dedup_entry(number, issue):
    return {"pr_ref": f"{_REPO}#{number}",
            "url": f"https://github.com/{_REPO}/pull/{number}",
            "issue_ref": f"{_REPO}#{issue}"}


def test_reconcile_e2e_dedup_keeps_highest_closes_the_rest():
    dedup = _open_pr_closing_issue_source([
        _dedup_entry(7, 3), _dedup_entry(9, 3)])  # same open issue #3
    iss = _issue_state_source({"acme/widget#3": {"state": "OPEN"}})
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=iss,
                                worktree_helper=_never_helper(),
                                pr_close_sink=prclose, comment_sink=comment,
                                open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])  # no ledger entries — pure (C) dedup.

    # highest number (#9) kept; the lower (#7) closed + recorded in deduped.
    assert res["deduped"] == [{"pr_ref": "acme/widget#7",
                               "issue_ref": "acme/widget#3",
                               "kept_pr_ref": "acme/widget#9"}]
    assert prclose.calls == [{"pr_ref": "acme/widget#7", "repo": _REPO}]
    # a comment names the KEPT PR as the superseding same-issue re-land.
    assert len(comment.calls) == 1
    assert comment.calls[0]["issue_ref"] == "acme/widget#3"
    assert "acme/widget#9" in comment.calls[0]["body"]
    assert res["closed_issues"] == []
    assert res["rebased"] == []
    assert res["relanded"] == []


def test_reconcile_e2e_dedup_sole_pr_untouched():
    dedup = _open_pr_closing_issue_source([_dedup_entry(7, 3)])  # sole PR
    iss = _issue_state_source({"acme/widget#3": {"state": "OPEN"}})
    prclose = _recording_pr_close_sink()
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=iss,
                                worktree_helper=_never_helper(),
                                pr_close_sink=prclose,
                                open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])

    assert res["deduped"] == []
    assert prclose.calls == []


def test_reconcile_e2e_dedup_closed_issue_group_untouched():
    # More than one open PR for issue #3, but the issue is CLOSED -> NEVER touch
    # (an orphaned duplicate whose issue closed is INTEGRATE's/orphan concern).
    dedup = _open_pr_closing_issue_source([
        _dedup_entry(7, 3), _dedup_entry(9, 3)])
    iss = _issue_state_source({"acme/widget#3": {"state": "CLOSED"}})
    prclose = _recording_pr_close_sink()
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=iss,
                                worktree_helper=_never_helper(),
                                pr_close_sink=prclose,
                                open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])

    assert res["deduped"] == []
    assert prclose.calls == []


def test_reconcile_e2e_dedup_never_crosses_issues():
    # Two PRs, but for DIFFERENT issues -> each is the sole PR for its issue,
    # nothing is deduped.
    dedup = _open_pr_closing_issue_source([
        _dedup_entry(7, 3), _dedup_entry(9, 4)])
    iss = _issue_state_source({"acme/widget#3": {"state": "OPEN"},
                               "acme/widget#4": {"state": "OPEN"}})
    prclose = _recording_pr_close_sink()
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=iss,
                                worktree_helper=_never_helper(),
                                pr_close_sink=prclose,
                                open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])

    assert res["deduped"] == []
    assert prclose.calls == []


def test_reconcile_e2e_dedup_dry_run_records_skipped_no_effects():
    dedup = _open_pr_closing_issue_source([
        _dedup_entry(7, 3), _dedup_entry(9, 3)])
    iss = _issue_state_source({"acme/widget#3": {"state": "OPEN"}})
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = vi.Reconcile(
        mode="propose", pr_state_source=_pr_state_source({}),
        issue_state_source=iss, worktree_helper=_never_helper(),
        pr_close_sink=prclose, comment_sink=comment,
        default_branch=_DEFAULT_BRANCH, repo=_REPO,
        open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])

    # the would-close intent is recorded under skipped; NO sink fires.
    assert res["deduped"] == []
    assert [s["ref"] for s in res["skipped"]] == ["acme/widget#7"]
    assert prclose.calls == []
    assert comment.calls == []


def test_reconcile_e2e_dedup_close_fault_recorded_in_errors():
    def raising_close(pr_ref, repo=None):  # noqa: ARG001
        raise RuntimeError("gh pr close 403")

    dedup = _open_pr_closing_issue_source([
        _dedup_entry(7, 3), _dedup_entry(9, 3)])
    iss = _issue_state_source({"acme/widget#3": {"state": "OPEN"}})
    reconcile = _auto_reconcile(pr_state_source=_pr_state_source({}),
                                issue_state_source=iss,
                                worktree_helper=_never_helper(),
                                pr_close_sink=raising_close,
                                open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [])

    # the fault is recorded, never raised; deduped stays empty for the faulting PR.
    assert res["deduped"] == []
    assert [e["ref"] for e in res["errors"]] == ["acme/widget#7"]
    assert "gh pr close 403" in res["errors"][0]["reason"]


def _dedup_source_runner(list_payload, view_by_number):
    """A fake runner scripting the (C) dedup source's TWO gh command shapes: the
    `gh pr list` (supported fields only) and the per-PR `gh pr view <n> --json
    closingIssuesReferences` delegation to gh_closing_issue_ref. Records argv."""
    seen = []

    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        seen.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            return _proc(stdout=list_payload)
        if cmd[:3] == ["gh", "pr", "view"]:
            return _proc(stdout=view_by_number[cmd[3]])
        raise AssertionError(f"unexpected gh command {cmd}")

    runner.seen = seen
    return runner


def test_gh_open_pr_closing_issue_source_shape():
    # FIX 1 — the LIST requests only SUPPORTED `gh pr list --json` fields
    # (number,url); closingIssuesReferences is resolved per-PR via `gh pr view`.
    list_payload = json.dumps([
        {"number": 7, "url": "https://github.com/acme/widget/pull/7"},
        {"number": 9, "url": "https://github.com/acme/widget/pull/9"},  # no issue
    ])
    view_by_number = {
        "7": json.dumps([{"number": 3}, {"number": 99}]),  # first ref -> #3
        "9": json.dumps([]),                               # closes no issue
    }
    runner = _dedup_source_runner(list_payload, view_by_number)

    out = vi.gh_open_pr_closing_issue_source(repo="acme/widget", runner=runner)
    # only the PR WITH a closing issue is returned, mapped to its FIRST ref.
    assert out == [{"pr_ref": "acme/widget#7",
                    "url": "https://github.com/acme/widget/pull/7",
                    "issue_ref": "acme/widget#3"}]
    cmd = runner.seen[0]
    assert cmd[:3] == ["gh", "pr", "list"]
    assert "--label" in cmd and cmd[cmd.index("--label") + 1] == "auto-maintainer"
    assert "--state" in cmd and cmd[cmd.index("--state") + 1] == "open"
    json_fields = cmd[cmd.index("--json") + 1]
    for f in ("number", "url"):
        assert f in json_fields
    assert "--repo" in cmd and cmd[cmd.index("--repo") + 1] == "acme/widget"


# ==========================================================================
# HOTFIX v0.25.1 — (C) dedup source uses SUPPORTED gh pr list fields + the
# whole dedup step is fault-isolated (the v0.25.0 dedup crashed the tick on
# gh 2.69.0 by requesting closingIssuesReferences on `gh pr list`).
# ==========================================================================

def test_gh_open_pr_closing_issue_source_never_requests_closing_refs_on_list():
    # FIX 1 command-shape: the `gh pr list` argv requests number,url and does NOT
    # contain closingIssuesReferences anywhere; the closing-issue ref is resolved
    # by a SUBSEQUENT `gh pr view <n> --json closingIssuesReferences` per PR.
    list_payload = json.dumps([
        {"number": 7, "url": "https://github.com/acme/widget/pull/7"},
        {"number": 9, "url": "https://github.com/acme/widget/pull/9"},
    ])
    view_by_number = {
        "7": json.dumps([{"number": 3}]),
        "9": json.dumps([]),  # closes no issue -> excluded
    }
    runner = _dedup_source_runner(list_payload, view_by_number)

    out = vi.gh_open_pr_closing_issue_source(repo="acme/widget", runner=runner)

    # the returned entries are correct: #7 -> issue #3; the no-closing-issue PR
    # (#9) is EXCLUDED.
    assert out == [{"pr_ref": "acme/widget#7",
                    "url": "https://github.com/acme/widget/pull/7",
                    "issue_ref": "acme/widget#3"}]

    list_cmds = [c for c in runner.seen if c[:3] == ["gh", "pr", "list"]]
    assert len(list_cmds) == 1
    list_cmd = list_cmds[0]
    # the invalid field must NEVER appear on the list argv (it aborts the tick).
    assert "closingIssuesReferences" not in list_cmd
    list_json_fields = list_cmd[list_cmd.index("--json") + 1]
    assert "closingIssuesReferences" not in list_json_fields
    assert "number" in list_json_fields and "url" in list_json_fields

    # per-PR closing-issue resolution DID happen via `gh pr view ... --json
    # closingIssuesReferences` (the supported form) for EACH listed PR.
    view_cmds = [c for c in runner.seen if c[:3] == ["gh", "pr", "view"]]
    assert [c[3] for c in view_cmds] == ["7", "9"]
    for c in view_cmds:
        assert "closingIssuesReferences" in c


def _raising_open_pr_source(exc):
    """A (C) dedup open-PR source that RAISES — models the v0.25.0 crash (an
    invalid gh field, an unresolvable ref, or any source fault)."""
    def source(repo=None):  # noqa: ARG001
        raise exc
    return source


def test_reconcile_e2e_dedup_source_fault_isolated_ab_unchanged():
    # FIX 2 fault-isolation: a dedup-source fault is recorded under errors as
    # {ref:'dedup', ...}, the run still returns OK (never raises), and the (A)
    # merged-issue-close + (B) rebase outcomes are UNCHANGED (dedup no-op).
    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"},       # A -> closed_issues
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})  # B tier1 -> rebased
    iss = _issue_state_source({"acme/widget#5": {"state": "OPEN"}})
    helper = _worktree_helper({"acme/widget#8": True})
    close = _recording_issue_close_sink()
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    boom = _raising_open_pr_source(
        RuntimeError("gh pr list: unknown JSON field closingIssuesReferences"))
    reconcile = _auto_reconcile(
        pr_state_source=pr, issue_state_source=iss, issue_close_sink=close,
        worktree_helper=helper, pr_close_sink=prclose, comment_sink=comment,
        open_pr_closing_issue_source=boom)

    # the run must NOT raise (advisory) — _run asserts signal == OK.
    res = _run(reconcile, [_entry(number=7, issue=5), _entry(number=8, issue=1)])

    # the dedup fault is recorded under errors as a single {ref:'dedup'} entry.
    dedup_errors = [e for e in res["errors"] if e["ref"] == "dedup"]
    assert len(dedup_errors) == 1
    assert "closingIssuesReferences" in dedup_errors[0]["reason"]
    # dedup degraded to a no-op.
    assert res["deduped"] == []
    # (A) and (B) outcomes are UNCHANGED by the dedup fault.
    assert res["closed_issues"] == [{"issue_ref": "acme/widget#5",
                                     "pr_ref": "acme/widget#7"}]
    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert res["relanded"] == []


# ==========================================================================
# HOTFIX v0.25.3 — gh_pr_state_source resolves a transient mergeable=UNKNOWN
# via the SAME bounded poll VERIFY's gh_open_pr_source uses (poll_mergeability,
# injectable runner+sleep), so RECONCILE's (B) conflict-recovery ladder is not
# blind to a just-invalidated loop PR (GitHub reports UNKNOWN transiently right
# after a sibling merge). A MERGED PR short-circuits (no poll); a still-UNKNOWN
# result is returned as-is (deferred to the next tick, never a crash).
# ==========================================================================

def _pr_state_runner(initial, poll_values):
    """A fake runner scripting gh_pr_state_source's TWO gh command shapes: the
    initial `gh pr view <n> --json state,mergedAt,mergeable` (returns `initial`
    JSON) and the poll_mergeability `gh pr view <n> --json mergeable -q
    .mergeable` (returns successive `poll_values`). Records argv."""
    seen = []
    poll_iter = iter(poll_values)

    def runner(cmd, capture_output=None, text=None, check=None):  # noqa: ARG001
        seen.append(cmd)
        if "-q" in cmd:  # the poll_mergeability re-query
            return _proc(stdout=next(poll_iter))
        return _proc(stdout=json.dumps(initial))  # the initial state read

    runner.seen = seen
    return runner


def _noop_sleep():
    calls = []

    def sleep(seconds):
        calls.append(seconds)

    sleep.calls = calls
    return sleep


def test_gh_pr_state_source_polls_transient_unknown_settles_conflicting():
    # An OPEN, not-merged PR reported mergeable=UNKNOWN is re-queried via the
    # bounded poll until it settles to CONFLICTING; the injected no-op sleep means
    # no wall-clock wait, and the poll stops EARLY (bounded gh-call count).
    runner = _pr_state_runner(
        {"state": "OPEN", "mergedAt": None, "mergeable": "UNKNOWN"},
        poll_values=["UNKNOWN\n", "CONFLICTING\n"])
    sleep = _noop_sleep()

    out = vi.gh_pr_state_source("acme/widget#9", repo="acme/widget",
                                runner=runner, sleep=sleep)

    assert out == {"state": "OPEN", "merged": False, "mergeable": "CONFLICTING"}
    # 1 initial read + 2 poll re-queries (stopped early on CONFLICTING).
    assert len(runner.seen) == 3
    poll_cmds = [c for c in runner.seen if "-q" in c]
    assert len(poll_cmds) == 2
    # the poll slept once (between the two re-queries), via the injected sleep.
    assert sleep.calls == [vi.MERGEABILITY_POLL_INTERVAL_S]


def test_gh_pr_state_source_stays_unknown_returns_unknown_no_crash():
    # A PR whose mergeability never settles within the bounded attempts returns
    # 'UNKNOWN' (no crash) so _reconcile_one's CONFLICTING check stays False and
    # the entry is deferred to the next tick.
    runner = _pr_state_runner(
        {"state": "OPEN", "mergedAt": None, "mergeable": "UNKNOWN"},
        poll_values=["UNKNOWN\n"] * vi.MERGEABILITY_POLL_ATTEMPTS)
    sleep = _noop_sleep()

    out = vi.gh_pr_state_source("acme/widget#9", runner=runner, sleep=sleep)

    assert out == {"state": "OPEN", "merged": False, "mergeable": "UNKNOWN"}
    # bounded: 1 initial read + MERGEABILITY_POLL_ATTEMPTS poll re-queries.
    assert len(runner.seen) == 1 + vi.MERGEABILITY_POLL_ATTEMPTS


def test_gh_pr_state_source_merged_short_circuits_no_poll():
    # A MERGED PR (mergedAt set) returns merged=True and issues NO poll calls —
    # merge state is final, mergeability is irrelevant.
    runner = _pr_state_runner(
        {"state": "MERGED", "mergedAt": "2026-07-24T00:00:00Z",
         "mergeable": "UNKNOWN"},
        poll_values=[])
    sleep = _noop_sleep()

    out = vi.gh_pr_state_source("acme/widget#7", runner=runner, sleep=sleep)

    assert out == {"state": "MERGED", "merged": True, "mergeable": "UNKNOWN"}
    # exactly ONE gh call — the initial read; no poll re-query, no sleep.
    assert len(runner.seen) == 1
    assert not any("-q" in c for c in runner.seen)
    assert sleep.calls == []


def test_gh_pr_state_source_mergeable_no_poll():
    # A settled MERGEABLE result does not poll either (only UNKNOWN triggers it).
    runner = _pr_state_runner(
        {"state": "OPEN", "mergedAt": None, "mergeable": "MERGEABLE"},
        poll_values=[])
    sleep = _noop_sleep()

    out = vi.gh_pr_state_source("acme/widget#4", runner=runner, sleep=sleep)

    assert out == {"state": "OPEN", "merged": False, "mergeable": "MERGEABLE"}
    assert len(runner.seen) == 1
    assert sleep.calls == []


def test_reconcile_e2e_transient_unknown_settles_conflicting_triggers_rebase():
    # E2E: the PRODUCTION gh_pr_state_source (fake runner + no-op sleep) reports a
    # just-invalidated loop PR as UNKNOWN then CONFLICTING; RECONCILE's (B) ladder
    # engages and TIER-1 rebase populates result.rebased (it was silently skipped
    # before the poll was added to gh_pr_state_source).
    runner = _pr_state_runner(
        {"state": "OPEN", "mergedAt": None, "mergeable": "UNKNOWN"},
        poll_values=["CONFLICTING\n"])
    sleep = _noop_sleep()

    def pr_state_source(pr_ref, repo=None):
        return vi.gh_pr_state_source(pr_ref, repo=repo, runner=runner,
                                     sleep=sleep)

    helper = _worktree_helper({"acme/widget#8": True})
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr_state_source,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run(reconcile, [_entry(number=8, issue=3)])

    # the (B) ladder engaged on the settled CONFLICTING value: tier-1 rebase ran.
    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert res["relanded"] == []
    assert len(helper.calls) == 1
    assert helper.calls[0]["pr_ref"] == "acme/widget#8"
    assert prclose.calls == []


def test_reconcile_e2e_dedup_grouping_fault_isolated_returns_ok():
    # A fault raised INSIDE the grouping/issue-state phase (not the source call
    # nor a single close) is ALSO caught by the whole-step wrap: recorded under
    # errors as {ref:'dedup'}, the run returns OK, and (A)/(B) are untouched.
    def raising_issue_state(issue_ref, repo=None):  # noqa: ARG001
        raise RuntimeError("gh issue view boom during dedup grouping")

    pr = _pr_state_source({
        "acme/widget#7": {"state": "MERGED", "merged": True,
                          "mergeable": "UNKNOWN"}})       # A -> closed_issues
    close = _recording_issue_close_sink()
    prclose = _recording_pr_close_sink()
    dedup = _open_pr_closing_issue_source([
        _dedup_entry(11, 3), _dedup_entry(12, 3)])  # >1 PR -> triggers issue-state
    # (A) uses a DISTINCT issue (#5) resolved before dedup; dedup's issue-state
    # read (#3) is the one that raises.
    iss_a = {"acme/widget#5": {"state": "OPEN"}}

    def issue_state(issue_ref, repo=None):  # noqa: ARG001
        if issue_ref == "acme/widget#3":
            return raising_issue_state(issue_ref, repo=repo)
        return dict(iss_a.get(issue_ref, {"state": "OPEN"}))

    reconcile = _auto_reconcile(
        pr_state_source=pr, issue_state_source=issue_state,
        issue_close_sink=close, worktree_helper=_never_helper(),
        pr_close_sink=prclose, open_pr_closing_issue_source=dedup)

    res = _run(reconcile, [_entry(number=7, issue=5)])

    dedup_errors = [e for e in res["errors"] if e["ref"] == "dedup"]
    assert len(dedup_errors) == 1
    assert "boom during dedup grouping" in dedup_errors[0]["reason"]
    assert res["deduped"] == []
    assert prclose.calls == []  # dedup degraded to a no-op before any close.
    # (A) is unchanged.
    assert res["closed_issues"] == [{"issue_ref": "acme/widget#5",
                                     "pr_ref": "acme/widget#7"}]


# ==========================================================================
# (B) conflict determination — LIVE-PREFERRED with a prior-verdict race-breaker.
#
# RECONCILE runs FIRST in the route (tick-top), when GitHub is most likely to
# still report a just-invalidated loop PR as mergeable=UNKNOWN even after the
# bounded poll. The previous tick's VERIFY — which ran later, once mergeability
# settled — already recorded the hard CONFLICTING in the OPTIONAL `prior_verdicts`
# input. So ONLY on a live UNKNOWN (poll exhausted) does RECONCILE consult
# prior_verdicts and treat the PR as CONFLICTING iff its prior verdict was a
# CONFIRMED-CONFLICTING one (a hard `mergeable=CONFLICTING`, DISTINCT from a
# transient DEFERRED/UNKNOWN verdict). A settled live read is always authoritative:
# live MERGEABLE is left alone (never force-pushed), live CONFLICTING enters the
# ladder directly. prior_verdicts absent/empty => exactly today's behavior.
# ==========================================================================

def _prior_verdict(pr_ref, mergeable_raw, base=_DEFAULT_BRANCH):
    """A previous-tick VERIFY verdict dict for `pr_ref` built via the PRODUCTION
    `derive_verdict`, so the `reasons` strings are EXACTLY what VERIFY records: a
    CONFLICTING mergeable yields the hard `not mergeable (mergeable=CONFLICTING)`
    reason; an UNKNOWN yields the transient DEFERRED reason. Same shape as the
    `verdicts` slot (the cross-feature contract scheduling seeds)."""
    number = pr_ref.split("#")[-1]
    url = f"https://github.com/{_REPO}/pull/{number}"
    return vi.derive_verdict(
        {"number": int(number), "url": url, "baseRefName": base,
         "mergeable": mergeable_raw, "statusCheckRollup": []},
        _DEFAULT_BRANCH).to_dict()


def _run_with_prior(reconcile, ledger, prior_verdicts):
    """Drive RECONCILE end-to-end with the OPTIONAL `prior_verdicts` slot seeded
    (as scheduling's make_reconcile will), committing through fc.apply_result under
    the manifest + signal vocab; returns reconcile_result."""
    ctx = _fresh_ctx()
    ctx.register_slot("prior_verdicts", {"type": "array"},
                      version=vi.VERDICT_SCHEMA_VERSION)
    ctx.write("acted_ledger", ledger)
    ctx.write("prior_verdicts", prior_verdicts)
    result = reconcile.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(vi.RECONCILE_SIGNALS)
    fc.apply_result(ctx, vi.RECONCILE_MANIFEST, result, vocab)
    return ctx.read("reconcile_result")


# --- the pure discriminator: a hard CONFLICTING prior verdict vs everything else.

def test_is_confirmed_conflicting_verdict_true_only_for_hard_conflict():
    conflicting = _prior_verdict("acme/widget#8", "CONFLICTING")
    deferred = _prior_verdict("acme/widget#8", "UNKNOWN")
    mergeable = _prior_verdict("acme/widget#8", "MERGEABLE")
    assert vi._is_confirmed_conflicting_verdict(conflicting) is True
    # a transient DEFERRED/UNKNOWN verdict is NOT a confirmed conflict.
    assert vi._is_confirmed_conflicting_verdict(deferred) is False
    # a mergeable (ok) verdict is not a conflict.
    assert vi._is_confirmed_conflicting_verdict(mergeable) is False
    # None / empty -> False (absent prior => today's behavior).
    assert vi._is_confirmed_conflicting_verdict(None) is False
    assert vi._is_confirmed_conflicting_verdict({}) is False


# --- (a) live UNKNOWN + prior CONFIRMED-CONFLICTING => (B) ladder engages.

def test_reconcile_e2e_live_unknown_prior_conflicting_tier1_rebases():
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "UNKNOWN"}})  # poll exhausted, still UNKNOWN
    helper = _worktree_helper({"acme/widget#8": True})
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run_with_prior(reconcile, [_entry(number=8, issue=3)],
                          [_prior_verdict("acme/widget#8", "CONFLICTING")])

    # the prior CONFIRMED-CONFLICTING verdict breaks the race: tier-1 rebase ran.
    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert res["relanded"] == []
    assert len(helper.calls) == 1
    assert prclose.calls == []


def test_reconcile_e2e_live_unknown_prior_conflicting_tier2_relands():
    pr = _pr_state_source({
        "acme/widget#9": {"state": "OPEN", "merged": False,
                          "mergeable": "UNKNOWN"}})
    helper = _worktree_helper({"acme/widget#9": False})  # real textual conflict
    prclose = _recording_pr_close_sink()
    comment = _recording_comment_sink()
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper,
                                pr_close_sink=prclose, comment_sink=comment)

    res = _run_with_prior(reconcile, [_entry(number=9, issue=3)],
                          [_prior_verdict("acme/widget#9", "CONFLICTING")])

    assert res["relanded"] == [{"pr_ref": "acme/widget#9",
                                "issue_ref": "acme/widget#3"}]
    assert res["rebased"] == []
    assert prclose.calls == [{"pr_ref": "acme/widget#9", "repo": _REPO}]


# --- (b) live UNKNOWN + prior DEFERRED (UNKNOWN) => left alone (not confirmed).

def test_reconcile_e2e_live_unknown_prior_deferred_left_alone():
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "UNKNOWN"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run_with_prior(reconcile, [_entry(number=8, issue=3)],
                          [_prior_verdict("acme/widget#8", "UNKNOWN")])

    # a transient DEFERRED prior verdict is NOT a confirmed conflict — the entry
    # stays DEFERRED to the next tick; the (B) ladder never engages.
    assert res["rebased"] == []
    assert res["relanded"] == []
    assert res["skipped"] == []
    assert res["errors"] == []


# --- (c) live UNKNOWN + prior_verdicts absent/empty => left alone (back-compat).

def test_reconcile_e2e_live_unknown_no_prior_verdicts_slot_left_alone():
    # No prior_verdicts slot registered at all (the un-wired route): the optional
    # read tolerates absence and RECONCILE reproduces exactly today's behavior.
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "UNKNOWN"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run(reconcile, [_entry(number=8, issue=3)])

    assert res["rebased"] == []
    assert res["relanded"] == []
    assert res["errors"] == []


def test_reconcile_e2e_live_unknown_empty_prior_verdicts_left_alone():
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "UNKNOWN"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run_with_prior(reconcile, [_entry(number=8, issue=3)], [])

    assert res["rebased"] == []
    assert res["relanded"] == []


# --- (d) live MERGEABLE + prior CONFLICTING => NOT touched (live preferred).

def test_reconcile_e2e_live_mergeable_prior_conflicting_left_alone():
    # The PR was fixed/rebased between ticks — live says MERGEABLE. The live read
    # is ALWAYS preferred when settled: the stale prior CONFLICTING verdict is
    # ignored and the PR is NEVER force-pushed.
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "MERGEABLE"}})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=_never_helper())

    res = _run_with_prior(reconcile, [_entry(number=8, issue=3)],
                          [_prior_verdict("acme/widget#8", "CONFLICTING")])

    assert res["rebased"] == []
    assert res["relanded"] == []
    assert res["skipped"] == []
    assert res["errors"] == []


# --- (e) live CONFLICTING settled => (B) ladder directly, prior irrelevant.

def test_reconcile_e2e_live_conflicting_settled_rebases_regardless_of_prior():
    pr = _pr_state_source({
        "acme/widget#8": {"state": "OPEN", "merged": False,
                          "mergeable": "CONFLICTING"}})
    helper = _worktree_helper({"acme/widget#8": True})
    reconcile = _auto_reconcile(pr_state_source=pr,
                                issue_state_source=_issue_state_source({}),
                                worktree_helper=helper)

    # even with a prior MERGEABLE verdict, a settled live CONFLICTING is
    # authoritative and enters the ladder.
    res = _run_with_prior(reconcile, [_entry(number=8, issue=3)],
                          [_prior_verdict("acme/widget#8", "MERGEABLE")])

    assert res["rebased"] == [{"pr_ref": "acme/widget#8"}]
    assert len(helper.calls) == 1
