#!/usr/bin/env python3
"""End-to-end tests for the ADVISORY REVIEW state (DESIGN §3.7.7).

After the loop redesign REVIEW is no longer a merge gate. It is an advisory
quality state: the auto-maintainer-reviewer subagent reads each open loop PR's
base..head diff and emits MATERIAL quality findings as machine-first, durable
DISCOVERY records. Those records conform EXACTLY to work-intake's DiscoveredIssue
schema so the downstream REPORT port can file them unchanged (a contract-bound
cross-feature producer relationship). A lazy reviewer now costs only missed
quality notes, never an unsafe merge — which structurally defuses the #255
rubber-stamp danger.

verify-integrate OWNS the SCHEMA + SLOT + MANIFEST for this advisory output:

  - REVIEW_FINDINGS_SLOT  — the versioned slot descriptor (array of records).
  - REVIEW_MANIFEST       — reads `verdicts`, writes `review_findings`,
                            emits OK | EMPTY.
  - ReviewFinding / review_finding_record(...) — a DiscoveredIssue-conforming
    record builder (fields exactly matching work-intake DiscoveredIssue.to_dict).

The actual per-PR judgment is the model reviewer's, captured as these structured
records; the dispatch/collection is wired in scheduling (out of scope here).
These tests assert the deterministic schema/slot/manifest surface AND the
reviewer-agent doc contract.

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
# work-intake owns the DiscoveredIssue schema the review_findings records must
# conform to (a contract-bound cross-feature producer relationship). Put its
# src/ on the path so the conformance test compares against the REAL schema.
_WI_SRC = os.path.join(_FEATURES_DIR, "work-intake", "src")
if _WI_SRC not in sys.path:
    sys.path.insert(0, _WI_SRC)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402
import work_intake as wi  # noqa: E402


# ==========================================================================
# Behaviour: REVIEW_FINDINGS_SLOT is a typed, versioned array slot descriptor
# mirroring the other slot descriptors (name/schema/version).
# ==========================================================================

def test_review_findings_slot_descriptor_is_versioned():
    slot = vi.REVIEW_FINDINGS_SLOT
    assert slot["name"] == "review_findings"
    assert slot["schema"] == {"type": "array"}
    assert slot["version"] == vi.REVIEW_FINDINGS_SCHEMA_VERSION


# ==========================================================================
# Behaviour (loop redesign §3.7.7): REVIEW is advisory — its manifest reads
# `verdicts` (the open-PR set to review), WRITES `review_findings` (NOT the old
# merge-gating review_verdicts), and emits OK | EMPTY.
# ==========================================================================

def test_review_manifest_writes_review_findings():
    m = vi.REVIEW_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("verdicts",)
    assert m.writes == ("review_findings",)
    assert "review_verdicts" not in m.writes
    assert set(m.emits) == {"OK", "EMPTY"}


# ==========================================================================
# Behaviour: a review_finding record conforms EXACTLY to work-intake's
# DiscoveredIssue.to_dict field set (so REPORT files it unchanged).
# ==========================================================================

def test_review_finding_record_conforms_to_discovered_issue():
    rec = vi.review_finding_record(
        pr_ref="acme/widget#42",
        title="Unbounded recursion in parser",
        body="The recursive descent has no depth guard; deep input overflows.",
        kind="bug",
        severity="high",
        slug="parser-recursion",
    )
    expected_fields = set(wi.DiscoveredIssue(
        title="t", body="b", kind="bug", severity="high",
        target="project", dedup_key="k").to_dict().keys())
    assert set(rec.keys()) == expected_fields
    # And it round-trips through the REAL DiscoveredIssue.from_dict unchanged.
    di = wi.DiscoveredIssue.from_dict(rec)
    assert di.to_dict() == rec


def test_review_finding_record_field_values():
    rec = vi.review_finding_record(
        pr_ref="acme/widget#42",
        title="Missing test for the empty-config branch",
        body="The new branch added in this PR has no test coverage.",
        kind="enhancement",
        severity="medium",
        slug="empty-config-test",
    )
    assert rec["schema_version"] == wi.DISCOVERED_ISSUE_SCHEMA_VERSION
    assert rec["kind"] == "enhancement"
    assert rec["severity"] == "medium"
    assert rec["target"] == "project"
    assert rec["filed_by"] == "autonomous-maintainer"
    # dedup_key is STABLE and derived from the pr_ref + slug.
    assert rec["dedup_key"] == "review:acme/widget#42:empty-config-test"


def test_review_finding_record_dedup_key_is_stable():
    """Same pr_ref + slug -> same dedup_key (idempotent filing through REPORT)."""
    a = vi.review_finding_record(
        pr_ref="acme/widget#7", title="x", body="y", kind="chore",
        severity="low", slug="lint-nit")
    b = vi.review_finding_record(
        pr_ref="acme/widget#7", title="x2", body="y2", kind="chore",
        severity="low", slug="lint-nit")
    assert a["dedup_key"] == b["dedup_key"]


# ==========================================================================
# E2E Behaviour: a tick's review findings are a list of conforming records that
# can be written to the review_findings slot and committed through apply_result.
# ==========================================================================

def test_review_findings_slot_accepts_records_e2e():
    ctx = fc.TickContext()
    slot = vi.REVIEW_FINDINGS_SLOT
    ctx.register_slot(slot["name"], slot["schema"], version=slot["version"])

    records = [
        vi.review_finding_record(
            pr_ref="acme/widget#1", title="Bug A", body="body A",
            kind="bug", severity="high", slug="bug-a"),
        vi.review_finding_record(
            pr_ref="acme/widget#1", title="Nit B", body="body B",
            kind="chore", severity="low", slug="nit-b"),
    ]
    ctx.write("review_findings", records)
    out = ctx.read("review_findings")
    assert len(out) == 2
    # Each record is DiscoveredIssue-conforming.
    for rec in out:
        assert wi.DiscoveredIssue.from_dict(rec).to_dict() == rec


# ==========================================================================
# E2E Behaviour: a reviewer that emits ZERO material findings yields an EMPTY
# review_findings list — advisory, never a merge block.
# ==========================================================================

def test_zero_findings_is_empty_list():
    ctx = fc.TickContext()
    slot = vi.REVIEW_FINDINGS_SLOT
    ctx.register_slot(slot["name"], slot["schema"], version=slot["version"])
    ctx.write("review_findings", [])
    assert ctx.read("review_findings") == []


# ==========================================================================
# Doc contract: the reviewer subagent doc instructs an ADVISORY quality review —
# read the actual diff, emit material findings with kind+severity and a stable
# dedup_key, respect a severity floor (no nitpicks), and NEVER merge/approve.
# ==========================================================================

def test_reviewer_agent_doc_is_advisory_not_a_merge_gate():
    agent_path = os.path.join(_FEATURE_DIR, "ship", "agents",
                              "auto-maintainer-reviewer.md")
    text = open(agent_path, encoding="utf-8").read()
    lower = text.lower()
    # Reads the ACTUAL diff.
    assert "gh pr diff" in lower
    # Emits review-finding records with the conforming fields.
    assert "review_findings" in lower or "review finding" in lower
    assert "dedup_key" in lower
    assert "kind" in lower
    assert "severity" in lower
    # Severity floor — material findings only, no nitpicks/spam.
    assert "severity floor" in lower or "no nitpick" in lower \
        or "never nitpick" in lower or "not nitpick" in lower
    # It is NOT a merge gate: never merges, never approves/blocks.
    assert "advisory" in lower
    assert "never merge" in lower or "does not merge" in lower \
        or "not a merge gate" in lower


def test_reviewer_agent_doc_does_not_emit_approval_verdict():
    """The advisory reviewer must NOT instruct an approve/reject merge verdict —
    that was the old merge-gate contract (removed)."""
    agent_path = os.path.join(_FEATURE_DIR, "ship", "agents",
                              "auto-maintainer-reviewer.md")
    text = open(agent_path, encoding="utf-8").read()
    lower = text.lower()
    # No "approved: true/false" merge-verdict instruction remains.
    assert "approved: true" not in lower
    assert "approved: false" not in lower
