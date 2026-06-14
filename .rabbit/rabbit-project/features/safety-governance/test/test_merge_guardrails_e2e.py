#!/usr/bin/env python3
"""End-to-end + unit conformance tests for merge guardrails (§3.8.1).

merge_guardrails(pr_meta, default_branch, delete_branch=None) -> {ok, violations}
is a pure, deterministic declarative check INTEGRATE consults before an
autonomous merge — a hard backstop BELOW the trust ladder. It enforces:

  - never-merge-wrong-base   — pr_meta.base != default_branch => violation.
  - never-merge-dirty        — pr_meta.mergeable not cleanly mergeable
                               (CONFLICTING / UNKNOWN / missing) => violation;
                               clean only when True or 'MERGEABLE'
                               (case-insensitive).
  - never-delete-non-matching-branch — only when delete_branch is supplied AND
                               != pr_meta.head => violation.

ok is True with an empty violations list only when every check passes.
Deterministic: pure function of the passed metadata, no I/O.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# safety_governance imports lifecycle_dispositions (which imports
# fsm_contracts); put those sibling srcs on the path too, mirroring
# test_safety_governance_e2e.py.
_LD_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import safety_governance as sg  # noqa: E402


# ==========================================================================
# E2E Behaviour: a clean PR (base==default, cleanly mergeable, no delete
# target) clears every guardrail -> ok True, empty violations.
# ==========================================================================

def test_clean_pr_ok():
    pr_meta = {"base": "main", "mergeable": "MERGEABLE", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is True
    assert out["violations"] == []


def test_clean_pr_mergeable_bool_true_ok():
    # mergeable as the boolean True is also cleanly mergeable.
    pr_meta = {"base": "main", "mergeable": True, "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is True
    assert out["violations"] == []


def test_clean_pr_mergeable_case_insensitive_ok():
    # 'mergeable' (lower-case) is accepted case-insensitively.
    pr_meta = {"base": "main", "mergeable": "mergeable", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is True
    assert out["violations"] == []


# ==========================================================================
# E2E Behaviour: never-merge-wrong-base — base != default_branch -> violation.
# ==========================================================================

def test_wrong_base_violation():
    pr_meta = {"base": "develop", "mergeable": "MERGEABLE", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is False
    assert any("wrong-base" in v for v in out["violations"])
    # Only the base check failed.
    assert len(out["violations"]) == 1


# ==========================================================================
# E2E Behaviour: never-merge-dirty — a CONFLICTING tree -> dirty violation.
# ==========================================================================

def test_conflicting_is_dirty_violation():
    pr_meta = {"base": "main", "mergeable": "CONFLICTING", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is False
    assert any("dirty" in v for v in out["violations"])
    assert len(out["violations"]) == 1


# ==========================================================================
# E2E Behaviour: never-merge-dirty — UNKNOWN or a missing mergeable key
# (not-yet-computed tree) -> dirty violation.
# ==========================================================================

def test_unknown_mergeable_is_dirty_violation():
    pr_meta = {"base": "main", "mergeable": "UNKNOWN", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is False
    assert any("dirty" in v for v in out["violations"])
    assert len(out["violations"]) == 1


def test_missing_mergeable_is_dirty_violation():
    pr_meta = {"base": "main", "head": "feat/x"}  # no mergeable key
    out = sg.merge_guardrails(pr_meta, "main")
    assert out["ok"] is False
    assert any("dirty" in v for v in out["violations"])
    assert len(out["violations"]) == 1


# ==========================================================================
# E2E Behaviour: never-delete-non-matching-branch — only checked when a
# delete_branch target is supplied. delete_branch == head -> ok; absent
# delete_branch -> the check is skipped entirely.
# ==========================================================================

def test_delete_branch_matching_head_ok():
    pr_meta = {"base": "main", "mergeable": "MERGEABLE", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main", delete_branch="feat/x")
    assert out["ok"] is True
    assert out["violations"] == []


def test_delete_branch_none_skips_check():
    # A clean PR with no delete target stays ok (delete check skipped).
    pr_meta = {"base": "main", "mergeable": "MERGEABLE", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main", delete_branch=None)
    assert out["ok"] is True
    assert out["violations"] == []


# ==========================================================================
# E2E Behaviour: never-delete-non-matching-branch — delete_branch != head
# -> violation (CLEANUP must bound branch deletion to the PR's own head).
# ==========================================================================

def test_delete_branch_non_matching_violation():
    pr_meta = {"base": "main", "mergeable": "MERGEABLE", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main", delete_branch="main")
    assert out["ok"] is False
    assert any("delete-non-matching-branch" in v for v in out["violations"])
    assert len(out["violations"]) == 1


# ==========================================================================
# E2E Behaviour: multiple violations accumulate — wrong base AND dirty AND a
# non-matching delete target all surface together (machine-first: INTEGRATE
# records each reason).
# ==========================================================================

def test_multiple_violations_accumulate():
    pr_meta = {"base": "develop", "mergeable": "CONFLICTING", "head": "feat/x"}
    out = sg.merge_guardrails(pr_meta, "main", delete_branch="main")
    assert out["ok"] is False
    assert any("wrong-base" in v for v in out["violations"])
    assert any("dirty" in v for v in out["violations"])
    assert any("delete-non-matching-branch" in v for v in out["violations"])
    assert len(out["violations"]) == 3


# ==========================================================================
# Invariant: merge_guardrails is pure — it does not mutate the passed
# pr_meta dict.
# ==========================================================================

def test_does_not_mutate_pr_meta():
    pr_meta = {"base": "main", "mergeable": "MERGEABLE", "head": "feat/x"}
    snapshot = dict(pr_meta)
    sg.merge_guardrails(pr_meta, "main", delete_branch="feat/x")
    assert pr_meta == snapshot
