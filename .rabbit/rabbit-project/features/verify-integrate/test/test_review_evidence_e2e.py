#!/usr/bin/env python3
"""Unit tests for the retained #255 evidence-validator helpers.

After the loop redesign (DESIGN §3.7.3/§3.7.7) REVIEW is ADVISORY and INTEGRATE
no longer gates merge on a model review approval, so the evidence-gated MERGE
backstop is gone. But two deterministic helpers — `review_evidence_valid(rv)` and
`batch_is_untrustworthy(review_verdicts)` — are KEPT: they remain part of the
shipped verify_integrate lib (a packaging-config release gate asserts the
committed plugin lib carries the #255 evidence gate, and scheduling still
consumes the ReviewVerdict surface). These tests pin the pure validators; the
advisory REVIEW contract is covered in test_review_advisory_e2e.py and the thin
INTEGRATE contract in test_integrate_cleanup_e2e.py.

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

import verify_integrate as vi  # noqa: E402

_GOOD_EVIDENCE = {
    "files_examined": ["src/foo.py", "test/test_foo.py"],
    "rationale": ("Read the base..head diff: it implements exactly the issue's "
                  "ask, tests cover the new branch, no out-of-scope edits."),
}


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
