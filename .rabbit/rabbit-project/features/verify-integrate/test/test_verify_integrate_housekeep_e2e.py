#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the verify-integrate doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md and
docs/contract.md are slimmed of redundant prose under coding-rules §6 (remove
only proven-dead/redundant content) and §7 (parenthetical clarity), without
losing any load-bearing claim. The §3.8.5 BACKOFF spec intent is a known
spec/impl GAP and is FLAGGED as a housekeeping sub-issue, NOT deleted — its
prose stays in the spec. These tests are the deterministic gate on the wave;
they are e2e in that they read the SHIPPED doc artifacts (not a mock) and
assert the wave's contractual properties end to end:

  Gate 1 — MEASURED REDUCTION (REPORTED, never MANDATED). The current doc
  surfaces (docs/spec.md + docs/contract.md) are NOT LARGER (no more lines
  total) than the committed pre-wave baseline recorded in
  housekeep_doc_baseline.json, and the script-tier verdict is one of
  {reduced, no-op}. Measurement is delegated to measure-reduction.py with
  --docs-only, so the housekeeping test THIS wave adds under test/ is never
  counted as bloat. A no-op (nothing was dead) is an honest SUCCESS; a wave
  that GREW the doc surface FAILS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  type / symbol, a schema field (including the ReviewVerdict surface), a
  contract provides/reads/invokes/never block key, or a cross-feature
  reference MUST still appear in the slimmed docs. A slim that drops a
  load-bearing token FAILS.

  Gate 3 — BEHAVIOR PRESERVED (the one MANDATORY gate of a wave). The
  measure-reduction tool the gate delegates to exists, and the feature's
  existing test suites (test_verify_e2e, test_integrate_cleanup_e2e) remain
  in the same run.py invocation — behavior preservation is enforced by those
  suites staying green alongside this gate.

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
# scripts dir). This test lives at <repo>/rabbit-project/features/
# verify-integrate/test/, so walk up to the repo root and descend into .claude.
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


def _diff_verdict(feature_dir):
    """Run measure-reduction.py diff (baseline vs current) via its own diff
    subcommand and return the parsed verdict object."""
    import tempfile
    after = _measure_docs_only(feature_dir)
    before = _baseline_snapshot_for_diff(feature_dir)
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
    return json.loads(proc.stdout)


# Load-bearing tokens that MUST survive the slim. Asserted against the COMBINED
# doc surface (spec.md + contract.md). These name the public Python surface, the
# slot/schema field names (including the model-backed ReviewVerdict gate), and
# the cross-feature references the contract binds.
_LOAD_BEARING_DOCS = (
    # public-surface types / states
    "Verdict",
    "IntegrationResult",
    "ReviewVerdict",
    "VERIFY",
    "INTEGRATE",
    "CLEANUP",
    "REVIEW",
    # Verdict schema fields
    "ci_state",
    "mergeable",
    "pr_ref",
    "reasons",
    "passing",
    "pending",
    "failing",
    "unknown",
    # IntegrationResult schema fields
    "merged",
    "skipped",
    "errors",
    # signal vocabulary
    "OK",
    "EMPTY",
    # trust ladder / guardrails (cross-feature behavior names)
    "auto-merge",
    "propose",
    "dry-run",
    "permits",
    "merge_guardrails",
    # the live-PR sourcing model
    "auto-maintainer",
    "gh",
    # cross-feature references
    "safety",
    "fsm-contracts",
    "scheduling",
    "tick-orchestrator",
)

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 1 — measured reduction (REPORTED, never MANDATED).
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
    gate is script-tier (deterministic), not a judgment call. This is also the
    MANDATORY behavior-preserved gate's prerequisite."""
    assert os.path.isfile(_MEASURE), f"measure-reduction.py not found: {_MEASURE}"


def test_docs_only_count_excludes_the_test_tree():
    """--docs-only restricts the directory walk to doc surfaces, so the
    housekeeping test + baseline this wave adds under test/ are NOT counted.
    The live snapshot keys must be exactly the two doc surfaces."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_doc_surfaces_not_larger_than_baseline():
    """The current doc-surface line total must be NO LARGER than the pre-wave
    baseline total (a wave that grew the surface FAILS). The verdict the
    script-tier diff records must be one of {reduced, no-op} — reduction is
    REPORTED, never MANDATED, so a no-op (nothing was dead) is an honest
    SUCCESS, not a failure."""
    result = _diff_verdict(_FEATURE_DIR)
    assert result["verdict"] in ("reduced", "no-op"), (
        f"unexpected verdict {result['verdict']!r}")
    assert result["total_delta"] <= 0, (
        f"doc surfaces grew: total_before={result['total_before']} "
        f"total_after={result['total_after']} delta={result['total_delta']}")


def test_spec_not_larger_than_baseline():
    """spec.md specifically — the surface this wave slimmed — must have NO MORE
    lines than the pre-wave baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/spec.md"]
    spec_after = after[os.path.normpath(_SPEC)]
    assert spec_after <= before, (
        f"spec.md grew: {spec_after} lines now vs baseline {before}")


def test_contract_not_larger_than_baseline():
    """contract.md must not have grown past its baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/contract.md"]
    contract_after = after[os.path.normpath(_CONTRACT)]
    assert contract_after <= before, (
        f"contract.md grew: {contract_after} lines now vs baseline {before}")


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


def test_contract_retains_review_verdict_surface():
    """The contract still names the ReviewVerdict slot schema + the
    auto-maintainer-reviewer subagent it invokes — the model-backed REVIEW gate
    surface must not be slimmed away."""
    text = _read(_CONTRACT)
    assert "ReviewVerdict" in text
    assert "auto-maintainer-reviewer" in text


# ==========================================================================
# Gate 3 — backoff GAP is FLAGGED, not deleted: the §3.8.5 BACKOFF spec intent
# survives in the spec (it is a known spec/impl gap filed as a housekeeping
# sub-issue, NOT removable dead content).
# ==========================================================================

def test_spec_retains_backoff_intent():
    """The §3.8.5 BACKOFF intent is a known spec/impl GAP, flagged as a
    housekeeping sub-issue rather than deleted. Its prose MUST remain in the
    spec so the gap stays visible until implemented."""
    text = _read(_SPEC)
    assert "3.8.5" in text, "the §3.8.5 backoff reference must remain in spec.md"
    assert "ackoff" in text, "the backoff intent must remain in spec.md"
