#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the safety-governance doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep) over the SAFETY layer,
so it is held to a higher bar: docs/spec.md is slimmed only of PROVEN-redundant
prose, and every load-bearing safety token is asserted to survive. The single
removal this wave makes is two self-contradictory entries in the
"Deferred (NOT in this slice)" section that are labelled "IMPLEMENTED this
slice" and merely forward-reference body sections that already document them in
full (Merge guardrails -> `merge_guardrails`; Config writer -> the guided
`--setup` walk-through). Their DESIGN citations (§3.8.1, §3.10.1) survive
elsewhere in the spec, so no citation is dropped.

docs/contract.md is left UNCHANGED: it is already minimal.

A KNOWN stale claim is FLAGGED, NOT removed or reworded: spec.md still says
`safety_governance.py READS + decides over governance.json`, but the loader now
reads `config.json` (governance.json is only a one-time migration source). That
is a stale-but-wrong same-line claim; per coding-rules §6 it is FLAGGED as a
separate housekeeping sub-issue rather than reworded to force a diff. This test
documents the flag (test_flag_stale_governance_json_read_claim_is_recorded) but
does NOT assert the claim away — rewording to force a diff is exactly what the
measured-reduction discipline forbids.

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

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-API symbol,
  a trust-ladder mode, a merge-guardrail never-clause, a budget/gate behavior,
  a schema field, a disposition, a DESIGN citation, a cross-feature reference,
  and the contract provides/reads/invokes/never block keys MUST still appear in
  the slimmed docs. A slim that drops a load-bearing token FAILS.

The MANDATORY gate of any wave — behavior preserved — is enforced by the rest
of the safety-governance suite staying green (run.py runs every test_*.py
together); a doc slim that broke a behavior would surface as a failure there.

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
# <repo>/rabbit-project/features/safety-governance/test/, so walk up to the repo
# root and descend into .claude.
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
# doc surface (spec.md + contract.md): the public API, trust-ladder modes,
# merge-guardrail never-clauses, schema fields, dispositions, and cross-feature
# references live in the spec; the provides/reads/invokes/never keys live in the
# contract block. Membership in the union is what "survived the slim" means.
_LOAD_BEARING_DOCS = (
    # Public API surface (named in the spec / contract prose).
    "load_config",
    "load_governance",
    "permits",
    "permits(effect_kind, mode)",
    "merge_guardrails",
    "window_key",
    "GOVERNANCE_SCHEMA_VERSION",
    "MAINTAINER_REPO",
    "configure.py",
    "--describe",
    "--setup",
    # Trust-ladder modes (closed set + tolerated legacy alias).
    "dry-run",
    "propose",
    "auto-merge",
    "gated-merge",
    # Merge-guardrail never-clauses (the safety backstop names).
    "never-merge-wrong-base",
    "never-merge-dirty",
    "never-delete-non-matching-branch",
    # Schema fields + the central config file.
    "schema_version",
    "per_day_tokens",
    "window_tz",
    "interval_minutes",
    "threshold",
    "work_own_filings",
    "config.json",
    "2.1.0",
    "2.4.0",
    # Dispositions (budget auto-resume vs fault latch).
    "ABORTED",
    "IDLE",
    # DESIGN citations + cross-feature references (cross-scope contract refs).
    "DESIGN",
    "scheduling",
    "verify-integrate",
    "lifecycle-dispositions",
    "observability",
    "outbound-report",
    "safety_governance",
    # The two DESIGN citations carried by the removed deferred entries MUST
    # survive elsewhere in the spec — removal must not drop a unique citation.
    "§3.8.1",
    "§3.10.1",
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
    FAILS. (An honest no-op wave would instead drop this assertion; this wave
    DID remove proven-redundant prose, so it asserts a real reduction.)"""
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


def test_contract_not_larger_than_baseline():
    """contract.md is left unchanged by this wave (it is already minimal); it
    must not have grown past its baseline."""
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
    doc surface (spec.md + contract.md). For a SAFETY feature this is the higher
    bar: public API, trust-ladder modes, merge-guardrail never-clauses, schema
    fields, dispositions, DESIGN citations, and cross-feature refs all survive."""
    text = _read(_SPEC) + "\n" + _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_DOCS if tok not in text]
    assert not missing, f"docs dropped load-bearing tokens: {missing}"


def test_contract_retains_block_keys():
    """The contract provides/reads/invokes/never block keys survived the slim."""
    text = _read(_CONTRACT)
    missing = [k for k in _CONTRACT_KEYS if k not in text]
    assert not missing, f"contract.md dropped block keys: {missing}"


def test_removed_design_citations_survive_in_body():
    """The two DESIGN citations carried by the removed deferred entries (§3.8.1
    Merge guardrails, §3.10.1 userConfig) MUST still appear in the spec body —
    the removal trimmed forward-reference restatements, not the citations."""
    text = _read(_SPEC)
    assert "§3.8.1" in text and "## Merge guardrails (§3.8.1)" in text
    assert "§3.10.1" in text and "userConfig §3.10.1" in text


# ==========================================================================
# Cleanup gate — proven-redundant prose removed (coding-rules §6).
# The two "IMPLEMENTED this slice" entries inside the
# "Deferred (NOT in this slice)" section were self-contradictory
# forward-reference restatements of body sections; no test or cross-feature
# artifact references their phrasing (verified by grep at authoring time).
# ==========================================================================

def test_deferred_section_drops_implemented_restatements():
    """The 'Deferred (NOT in this slice)' section no longer carries the
    self-contradictory 'IMPLEMENTED this slice' restatements — they duplicated
    the Merge guardrails and Guided --setup body sections (coding-rules §6)."""
    text = _read(_SPEC)
    assert "IMPLEMENTED this slice" not in text, (
        "self-contradictory 'IMPLEMENTED this slice' deferred restatement "
        "must be removed")


def test_genuinely_deferred_items_are_preserved():
    """The genuinely-deferred items remain — they are the unique home for the
    §3.11.5 (loopback) and §3.8.6 (blast-radius) DESIGN citations; this wave
    removes only the IMPLEMENTED restatements, never live deferred work."""
    text = _read(_SPEC)
    assert "## Deferred (NOT in this slice)" in text
    assert "§3.11.5" in text, "loopback/provenance deferral must survive"
    assert "§3.8.6" in text, "blast-radius deferral must survive"
    assert "Backoff / circuit-breaker" in text, "backoff deferral must survive"


# ==========================================================================
# CORRECTED (coding-rules §6 verify-or-flag) — a prior housekeeping wave FLAGGED
# the stale-but-wrong claim (the loader reads config.json, not governance.json)
# and deferred the rewrite to a follow-up rather than rewording mid-wave. The
# follow-up corrected the claim to config.json; this test now asserts the stale
# wording is gone and the corrected wording is present.
# ==========================================================================

def test_stale_governance_json_read_claim_is_corrected():
    """The spec.md claim that safety_governance.py 'READS + decides over
    governance.json' was stale: the loader reads config.json (governance.json is
    only a one-time migration source). A prior housekeeping wave FLAGGED the
    claim and deferred the correction to a follow-up; that follow-up corrected it
    to config.json. This test asserts the stale wording is gone and the corrected
    wording is present."""
    text = _read(_SPEC)
    assert "READS + decides over `governance.json`" not in text, (
        "the stale read claim must be corrected to config.json")
    assert "READS + decides over `config.json`" in text, (
        "the corrected read claim should name config.json (the loader's source)")
