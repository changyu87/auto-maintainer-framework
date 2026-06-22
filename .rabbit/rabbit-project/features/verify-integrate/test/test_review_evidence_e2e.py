#!/usr/bin/env python3
"""End-to-end + unit tests for the #255 evidence-gated REVIEW trust check.

Bug #255: the REVIEW gate rubber-stamps and INTEGRATE merges on contentless
blanket-approvals. The fix is DETERMINISTIC (it does NOT trust the model
reviewer): an approved ReviewVerdict MUST carry evidence — the concrete files
the reviewer examined plus a substantive rationale. INTEGRATE ANDs "the verdict
carries evidence" into its merge condition:

  - an approved verdict with empty/missing evidence is INVALID — INTEGRATE
    refuses to merge it (routed to `skipped`, the loop re-reviews next tick);
  - an all-approved + zero-findings + no-evidence batch is UNTRUSTWORTHY (the
    fabricated rubber-stamp signature) — INTEGRATE merges NONE of it;
  - a genuine approved+evidence verdict still merges (no regression).

These tests drive INTEGRATE exactly as tick-orchestrator will — a real
fsm-contracts TickContext with the slots registered, the state run, and its
StateResult committed through `fc.apply_result`.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
_FSM_SRC = os.path.join(_FEATURES_DIR, "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)
_SG_SRC = os.path.join(_FEATURES_DIR, "safety-governance", "src")
if _SG_SRC not in sys.path:
    sys.path.insert(0, _SG_SRC)
_LD_SRC = os.path.join(_FEATURES_DIR, "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402

_DEFAULT_BRANCH = "main"

_GOOD_EVIDENCE = {
    "files_examined": ["src/foo.py", "test/test_foo.py"],
    "rationale": ("Read the base..head diff: it implements exactly the issue's "
                  "ask, tests cover the new branch, no out-of-scope edits."),
}


# --------------------------------------------------------------------------
# Fixtures.
# --------------------------------------------------------------------------

def _verdict(number=1, ok=True, base=_DEFAULT_BRANCH, mergeable=True,
             ci_state="passing", reasons=None):
    return vi.Verdict(
        pr_ref=f"acme/widget#{number}",
        url=f"https://github.com/acme/widget/pull/{number}",
        ok=ok, ci_state=ci_state, mergeable=mergeable, base=base,
        reasons=list(reasons or []),
    ).to_dict()


def _recording_sink():
    calls = []

    def sink(pr_ref, repo=None):  # noqa: ARG001
        calls.append(pr_ref)
        return {"pr_ref": pr_ref, "url": ""}

    sink.calls = calls
    return sink


def _fresh_ctx():
    ctx = fc.TickContext()
    for slot in (vi.VERDICTS_SLOT, vi.REVIEW_VERDICTS_SLOT,
                 vi.INTEGRATION_RESULT_SLOT):
        ctx.register_slot(slot["name"], slot["schema"], version=slot["version"])
    ctx.write(vi.REVIEW_VERDICTS_SLOT["name"], [])
    return ctx


def _run(integrate, ctx):
    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)
    return ctx.read("integration_result")


# ==========================================================================
# Unit: review_evidence_valid — the deterministic evidence validator.
# ==========================================================================

def test_evidence_valid_accepts_files_and_rationale():
    rv = vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True,
                          evidence=_GOOD_EVIDENCE).to_dict()
    assert vi.review_evidence_valid(rv) is True


def test_evidence_invalid_when_no_files_examined():
    rv = vi.ReviewVerdict(
        pr_ref="acme/widget#1", approved=True,
        evidence={"files_examined": [], "rationale": "looks fine"}).to_dict()
    assert vi.review_evidence_valid(rv) is False


def test_evidence_invalid_when_rationale_blank():
    rv = vi.ReviewVerdict(
        pr_ref="acme/widget#1", approved=True,
        evidence={"files_examined": ["a.py"], "rationale": "   "}).to_dict()
    assert vi.review_evidence_valid(rv) is False


def test_evidence_invalid_when_rationale_too_thin():
    """A one-word rationale is not substantive — it is a rubber-stamp."""
    rv = vi.ReviewVerdict(
        pr_ref="acme/widget#1", approved=True,
        evidence={"files_examined": ["a.py"], "rationale": "ok"}).to_dict()
    assert vi.review_evidence_valid(rv) is False


def test_evidence_invalid_when_evidence_missing_entirely():
    rv = vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True).to_dict()
    assert vi.review_evidence_valid(rv) is False


# ==========================================================================
# Unit: is_review_approved ANDs evidence-validity into approval.
# ==========================================================================

def test_approved_without_evidence_is_not_approved():
    """An approved verdict lacking evidence is INVALID — it must read as
    not-approved so INTEGRATE never treats a rubber-stamp as a blessing."""
    rvs = [vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True).to_dict()]
    assert vi.is_review_approved(rvs, "acme/widget#1") is False


def test_approved_with_evidence_is_approved():
    rvs = [vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True,
                            evidence=_GOOD_EVIDENCE).to_dict()]
    assert vi.is_review_approved(rvs, "acme/widget#1") is True


# ==========================================================================
# Unit: batch_is_untrustworthy — the fabricated-batch signature.
# ==========================================================================

def test_untrustworthy_batch_all_approved_no_findings_no_evidence():
    rvs = [vi.ReviewVerdict(pr_ref=f"acme/widget#{n}", approved=True).to_dict()
           for n in (1, 2, 3)]
    assert vi.batch_is_untrustworthy(rvs) is True


def test_batch_with_evidence_is_trustworthy():
    rvs = [vi.ReviewVerdict(pr_ref=f"acme/widget#{n}", approved=True,
                            evidence=_GOOD_EVIDENCE).to_dict()
           for n in (1, 2, 3)]
    assert vi.batch_is_untrustworthy(rvs) is False


def test_batch_with_a_rejection_is_trustworthy():
    """A batch that contains a real rejection is not the rubber-stamp pattern."""
    rvs = [vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True).to_dict(),
           vi.ReviewVerdict(pr_ref="acme/widget#2", approved=False,
                            severity="high",
                            findings=[{"kind": "spec", "severity": "high",
                                       "file": "x", "line": 1,
                                       "note": "wrong thing"}]).to_dict()]
    assert vi.batch_is_untrustworthy(rvs) is False


def test_empty_batch_is_not_untrustworthy():
    assert vi.batch_is_untrustworthy([]) is False
    assert vi.batch_is_untrustworthy(None) is False


# ==========================================================================
# E2E: a fabricated batch (approved / findings:[] / no-evidence) is REJECTED —
# nothing merges, every PR is skipped with an untrustworthy reason.
# ==========================================================================

def test_integrate_e2e_rejects_fabricated_untrustworthy_batch():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2),
                           _verdict(number=3)])
    # The fabricated rubber-stamp: all approved, no findings, no evidence.
    ctx.write("review_verdicts", [
        vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True).to_dict(),
        vi.ReviewVerdict(pr_ref="acme/widget#2", approved=True).to_dict(),
        vi.ReviewVerdict(pr_ref="acme/widget#3", approved=True).to_dict(),
    ])

    res = _run(integrate, ctx)

    assert sink.calls == []
    assert res["merged"] == []
    assert {s["pr_ref"] for s in res["skipped"]} == {
        "acme/widget#1", "acme/widget#2", "acme/widget#3"}
    for s in res["skipped"]:
        assert ("untrustworthy" in s["reason"].lower()
                or "evidence" in s["reason"].lower())


# ==========================================================================
# E2E: a single approved-without-evidence verdict (the batch is not fully a
# rubber-stamp — there is also a real rejection) is still NOT merged; the skip
# reason names the missing evidence.
# ==========================================================================

def test_integrate_e2e_approved_without_evidence_not_merged():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])
    ctx.write("review_verdicts", [
        # #1 approved but NO evidence — invalid, must not merge.
        vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True).to_dict(),
        # #2 a genuine rejection, so the batch is NOT the all-rubber-stamp shape.
        vi.ReviewVerdict(pr_ref="acme/widget#2", approved=False,
                         severity="blocker",
                         findings=[{"kind": "bug", "severity": "blocker",
                                    "file": "y", "line": 2,
                                    "note": "real defect"}]).to_dict(),
    ])

    res = _run(integrate, ctx)

    assert sink.calls == []
    assert res["merged"] == []
    skipped = {s["pr_ref"]: s["reason"] for s in res["skipped"]}
    assert "acme/widget#1" in skipped
    assert "evidence" in skipped["acme/widget#1"].lower()


# ==========================================================================
# E2E (no regression): a genuine approved+evidence verdict STILL merges at
# auto-merge — the fix does not block honest reviews.
# ==========================================================================

def test_integrate_e2e_approved_with_evidence_still_merges():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=7)])
    ctx.write("review_verdicts", [vi.ReviewVerdict(
        pr_ref="acme/widget#7", approved=True,
        evidence=_GOOD_EVIDENCE).to_dict()])

    res = _run(integrate, ctx)

    assert sink.calls == ["acme/widget#7"]
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#7"]
    assert res["skipped"] == []


# ==========================================================================
# E2E (doc contract): the reviewer subagent doc instructs the model to read the
# ACTUAL diff (`gh pr diff`) and to POPULATE the evidence (files_examined +
# rationale) on every approval — the human-facing half of the #255 fix.
# ==========================================================================

def test_reviewer_agent_doc_requires_diff_and_evidence():
    agent_path = os.path.join(_FEATURE_DIR, "ship", "agents",
                              "auto-maintainer-reviewer.md")
    text = open(agent_path, encoding="utf-8").read()
    lower = text.lower()
    # It must tell the reviewer to read the real diff (not trust a report).
    assert "gh pr diff" in lower
    # It must require the evidence fields on an approval.
    assert "evidence" in lower
    assert "files_examined" in lower
    assert "rationale" in lower
