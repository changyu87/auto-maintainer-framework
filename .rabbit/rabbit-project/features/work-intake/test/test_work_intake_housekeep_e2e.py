#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the work-intake doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md is
slimmed of proven-dead / drifted prose — a drifted "Current behaviour"
implementation-history section, a resolved "Open questions" section, and stale
slice-staging clauses in Purpose — without losing any load-bearing claim.
docs/contract.md was the contract-too-SMALL counterpart: the doc-slim wave
flagged (but did not fix) its REPORT-slice under-documentation as a separate
sub-issue. That sub-issue (#241) has since ADDITIVELY declared the
DiscoveredIssue / ReportResult / file_discoveries / gh_issue_file_sink /
is_loop_filed public surface and the `gh issue create` / `gh label create`
external invocations in the contract block, so the contract now matches the src.

These tests are the deterministic gate on the wave. They are e2e in that they
read the SHIPPED doc artifacts (not a mock) and assert the wave's contractual
properties end to end:

  Gate 1 — MEASURED REDUCTION. The current doc surfaces (docs/spec.md +
  docs/contract.md) are STRICTLY smaller (fewer lines total) than the committed
  pre-wave baseline recorded in housekeep_doc_baseline.json. Measurement is
  delegated to the script-tier tool measure-reduction.py with --docs-only, so
  the housekeeping test THIS wave adds under test/ is never counted as bloat
  (the --docs-only mode restricts the directory walk to doc surfaces only). A
  reword that does not actually reduce the doc-surface line total FAILS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface type,
  schema field, state, signal-vocabulary member, the determinism-seam symbols,
  the loopback-guard symbols, the REPORT-slice public surface, the DESIGN
  citations, the cross-feature references, and the contract
  provides/reads/invokes/never block keys MUST still appear in the slimmed
  docs. A slim that drops a load-bearing token FAILS.

The MANDATORY gate of any wave — behavior preserved — is enforced by the rest
of the work-intake suite staying green (run.py runs every test_*.py together);
a doc slim that broke a behavior would surface as a failure there.

Owner: changyu87
"""

import json
import os
import subprocess
import sys

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_TEST_DIR)
_DOCS_DIR = os.path.join(_FEATURE_DIR, "docs")
_BASELINE_PATH = os.path.join(_TEST_DIR, "housekeep_doc_baseline.json")

_SPEC = os.path.join(_DOCS_DIR, "spec.md")
_CONTRACT = os.path.join(_DOCS_DIR, "contract.md")

# measure-reduction.py is the script-tier line-accounting tool (rabbit-housekeep
# scripts dir). This test lives at
# <repo>/rabbit-project/features/work-intake/test/, so walk up to the repo root
# and descend into .claude.
_REPO_ROOT = os.path.dirname(  # <repo>
    os.path.dirname(           # rabbit-project/features
        os.path.dirname(_FEATURE_DIR)))  # rabbit-project
_MEASURE = os.path.join(
    _REPO_ROOT, ".claude", "features", "rabbit-housekeep", "scripts",
    "measure-reduction.py")


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _baseline():
    with open(_BASELINE_PATH, "r") as f:
        return json.load(f)


def _measure_docs_only(feature_dir):
    """Run measure-reduction.py count --docs-only on the feature dir and return
    the parsed JSON snapshot. --docs-only restricts the directory walk to doc
    surfaces (docs/spec.md, docs/contract.md, skills/*/SKILL.md), so the test/
    tree this wave adds is excluded from the count and never flips the verdict."""
    proc = subprocess.run(
        [sys.executable, _MEASURE, "count", "--docs-only", feature_dir],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"measure-reduction count failed: {proc.stderr}")
    return json.loads(proc.stdout)


def _baseline_snapshot_for_diff(feature_dir):
    """Materialize the pre-wave baseline as a measure-reduction `count`-shaped
    snapshot keyed by the SAME absolute paths the live count emits, so the
    script's own `diff` subcommand can compare them. The baseline fixture stores
    feature-relative doc keys (docs/spec.md, docs/contract.md)."""
    base_docs = _baseline()["docs"]
    snap = {}
    total = 0
    for rel, n in base_docs.items():
        key = os.path.normpath(os.path.join(feature_dir, rel))
        snap[key] = n
        total += n
    snap["__total__"] = total
    return snap


# Load-bearing tokens that MUST survive the slim. Asserted against the COMBINED
# doc surface (spec.md + contract.md): the public-surface types, schema fields,
# states, signal vocabulary, and cross-feature references live in the spec; the
# provides/reads/invokes/never keys live in the contract block. Membership in
# the union is what "survived the slim" means for the wave.
_LOAD_BEARING_DOCS = (
    # Slice 1 — PULL public surface + WorkItem schema fields.
    "WorkItem",
    "PULL",
    "work_items",
    "run(TickContext)",
    "StateResult",
    "schema_version",
    "comments",
    "MAX_COMMENTS_PER_ITEM",
    "MAX_COMMENT_BODY_CHARS",
    "is_loop_filed",
    "gh issue list",
    "gh issue view",
    # Slice 2 — TRIAGE public surface + WorkOrder schema.
    "WorkOrder",
    "TRIAGE",
    "work_orders",
    "decision",
    "accepted",
    "rejected",
    "auto-maintainer-triager",
    # FT-B — TRIAGE cross-cutting-risk slot (DESIGN §3.5.9).
    "CrossCuttingRisk",
    "cross_cutting_risk",
    # Slice 3 — REPORT public surface (documented in spec; must survive).
    "DiscoveredIssue",
    "ReportResult",
    "file_discoveries",
    "gh_issue_file_sink",
    "gh_issue_source",
    "dedup_key",
    "filed_by",
    "filed-by:autonomous-maintainer",
    "am-dedup",
    "MAINTAINER_REPO",
    # Closed signal vocabulary.
    "OK",
    "EMPTY",
    # DESIGN citations + cross-feature references (cross-scope contract refs).
    "DESIGN",
    "fsm-contracts",
    "scheduling",
    "safety_governance",
    "adapter-wiring",
    "agent-dispatch",
)

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 1 — measured reduction: current doc surfaces strictly smaller than
# baseline, measured by the script-tier tool with --docs-only.
# ==========================================================================

def test_baseline_fixture_is_present_and_well_formed():
    """The committed baseline fixture exists and records both doc line counts —
    without it there is nothing to measure reduction against."""
    base = _baseline()
    docs = base["docs"]
    assert docs["docs/spec.md"] > 0
    assert docs["docs/contract.md"] > 0


def test_measure_reduction_tool_is_available():
    """The script-tier measurement tool the gate delegates to must exist; the
    gate is script-tier (deterministic), not a judgment call."""
    assert os.path.isfile(_MEASURE), f"measure-reduction.py not found: {_MEASURE}"


def test_docs_only_count_excludes_the_test_tree():
    """--docs-only restricts the directory walk to doc surfaces, so the
    housekeeping test this wave adds under test/ is NOT counted. The live
    snapshot keys must be exactly the two doc surfaces — no test/ paths."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_doc_surfaces_strictly_smaller_than_baseline():
    """The current doc-surface line total must be STRICTLY less than the pre-wave
    baseline total. The verdict is produced by measure-reduction.py's own `diff`
    subcommand (reduced == total_delta < 0). A reword that does not reduce
    FAILS."""
    import tempfile

    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline_snapshot_for_diff(_FEATURE_DIR)

    with tempfile.TemporaryDirectory() as td:
        before_path = os.path.join(td, "before.json")
        after_path = os.path.join(td, "after.json")
        with open(before_path, "w") as f:
            json.dump(before, f)
        with open(after_path, "w") as f:
            json.dump(after, f)
        proc = subprocess.run(
            [sys.executable, _MEASURE, "diff", before_path, after_path],
            capture_output=True, text=True)
    assert proc.returncode == 0, f"measure-reduction diff failed: {proc.stderr}"
    result = json.loads(proc.stdout)
    assert result["reduced"] is True, (
        f"doc surfaces did not shrink: total_before={result['total_before']} "
        f"total_after={result['total_after']} delta={result['total_delta']}")
    assert result["verdict"] == "reduced"
    assert result["total_delta"] < 0


def test_spec_is_strictly_smaller_than_baseline():
    """spec.md specifically — the surface this wave slimmed — must have FEWER
    lines than the pre-wave baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/spec.md"]
    spec_after = after[os.path.normpath(_SPEC)]
    assert spec_after < before, (
        f"spec.md did not shrink: {spec_after} lines now vs baseline {before}")


def test_contract_declares_report_slice():
    """contract.md now ADDITIVELY documents the REPORT slice (#241 — the
    contract-too-SMALL gap the doc-slim wave flagged as a separate sub-issue).
    The contract block's provides + invokes.external must name the REPORT public
    surface and its `gh issue create` / `gh label create` external invocations,
    which src/work_intake.py implements and ~23 tests exercise."""
    contract_text = open(_CONTRACT).read()
    for token in ("DiscoveredIssue", "ReportResult", "file_discoveries",
                  "gh_issue_file_sink", "is_loop_filed", "gh issue create",
                  "gh label create"):
        assert token in contract_text, (
            f"contract.md does not declare REPORT-slice token: {token!r}")


# ==========================================================================
# Gate 2 — load-bearing survival: required tokens still present after the slim.
# ==========================================================================

def test_docs_retain_all_load_bearing_tokens():
    """Every load-bearing token survived the slim, asserted against the combined
    doc surface (spec.md + contract.md)."""
    text = _read(_SPEC) + "\n" + _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_DOCS if tok not in text]
    assert not missing, f"docs dropped load-bearing tokens: {missing}"


def test_contract_retains_block_keys():
    """The contract provides/reads/invokes/never block keys survived the slim."""
    text = _read(_CONTRACT)
    missing = [k for k in _CONTRACT_KEYS if k not in text]
    assert not missing, f"contract.md dropped block keys: {missing}"


# ==========================================================================
# Cleanup gate — proven-dead / drifted prose removed (coding-rules §6).
# The drifted "Current behaviour" implementation-history and the resolved
# "Open questions" section are removed; the CHANGELOG is the machine-first home
# for implementation history. No test or cross-feature artifact references
# either section (verified by grep at authoring time).
# ==========================================================================

def test_spec_drops_drifted_current_behaviour_section():
    """spec.md no longer carries the drifted 'Current behaviour' section — it was
    implementation-history that had drifted (it described shipping the triager as
    'this cycle' long after REPORT slice 3 shipped). The CHANGELOG holds history
    (coding-rules §6)."""
    text = _read(_SPEC)
    assert "## Current behaviour" not in text, (
        "drifted '## Current behaviour' implementation-history must be removed")


def test_spec_drops_resolved_open_questions_section():
    """spec.md no longer carries the 'Open questions' section — both questions
    were resolved (the WorkItem field set shipped; PULL-vs-TRIAGE actionability
    is answered inline as 'TRIAGE's job'). Resolved open questions are dead
    (coding-rules §6)."""
    text = _read(_SPEC)
    assert "## Open questions" not in text, (
        "resolved '## Open questions' section must be removed")
