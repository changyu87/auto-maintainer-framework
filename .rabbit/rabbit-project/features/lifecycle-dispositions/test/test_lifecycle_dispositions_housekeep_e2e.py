#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the lifecycle-dispositions doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md is
slimmed of dead/redundant prose without losing any load-bearing claim. The
proven-dead content removed this wave is (coding-rules §6, deterministic
verification):

  - a `docs/ROADMAP.md` reference — `find` proves no ROADMAP.md exists under
    the feature dir, so the reference was dead path prose;
  - volatile implementation-history restatement ("Implemented and merged ...
    20 passing tests") — the live tdd_state lives in feature.json
    (philosophy.md §1: the machine-first artifact is the source of truth), so
    the prose restatement was redundant;
  - the "Open questions" section asking how mutex ownership identity is stamped
    (PID + start-time vs a session token) — `grep` proves src/ settled this as
    (PID, process start-time), so the open question is resolved, not open.

docs/contract.md is already minimal and is unchanged by this wave. These tests
are the deterministic gate on the wave — they are e2e in that they read the
SHIPPED doc artifacts (not a mock) and assert the wave's contractual properties
end to end:

  Gate 1 — MEASURED REDUCTION. The current doc surfaces (docs/spec.md +
  docs/contract.md) are STRICTLY smaller (fewer lines total) than the committed
  pre-wave baseline recorded in housekeep_doc_baseline.json. Measurement is
  delegated to the script-tier tool measure-reduction.py with --docs-only, so
  the housekeeping test THIS wave adds under test/ is never counted as bloat
  (the --docs-only mode restricts the directory walk to doc surfaces only). A
  reword that does not actually reduce the doc-surface line total FAILS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  symbol, a Disposition member, a closed signal-vocabulary member, the GUARD /
  EXIT core anchors, the cross-feature refs (fsm-contracts, durable-state,
  scheduling, tick-orchestrator), and the contract provides/reads/invokes/never
  block keys MUST still appear in the slimmed docs. A slim that drops a
  load-bearing token FAILS.

  Cleanup gate — the proven-dead content is actually gone: spec.md no longer
  references docs/ROADMAP.md, no ROADMAP.md exists in the feature docs dir, and
  the resolved "Open questions" section is removed.

Reduction is REPORTED, never MANDATED (measure-reduction.py docstring): if the
docs were already lean, a no-op verdict would be an honest PASS. This wave DID
find proven-dead prose, so the recorded verdict is `reduced`; the test asserts
the verdict the wave actually produced.

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
# scripts dir). The feature dir layout is
# <repo>/.claude/features/rabbit-housekeep/scripts/measure-reduction.py and this
# test lives at <repo>/rabbit-project/features/lifecycle-dispositions/test/, so
# walk up to the repo root and descend into .claude.
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
# doc surface (spec.md + contract.md): the public-surface symbols, Disposition
# members, signal vocabulary, and core anchors live in the spec; the
# provides/reads/invokes/never keys live in the contract block. Membership in
# the union is what "survived the slim" means for this wave.
_LOAD_BEARING_DOCS = (
    # Public-surface symbols (src/lifecycle_dispositions.py).
    "Disposition",
    "read_disposition",
    "write_disposition",
    "acquire_lock",
    "release_lock",
    "lock_is_held",
    "Guard",
    "Exit",
    "run(TickContext)",
    "StateResult",
    # Disposition closed-set members.
    "RUNNING",
    "IDLE",
    "STOPPED",
    "ABORTED",
    "RESTART_NEEDED",
    # GUARD/EXIT emitted signals + EXIT outcome vocabulary.
    "OK",
    "HALT_REQUESTED",
    "RESTART_REQUIRED",
    "refire",
    "idle",
    "break",
    "halt",
    # Core anchor states + single-writer mutex concept.
    "GUARD",
    "EXIT",
    "single-writer mutex",
    "stale-marker detection",
    "host-agnostic resumption",
    # Cross-feature references (bounded-scope contract surface).
    "fsm-contracts",
    "durable-state",
    "scheduling",
    "tick-orchestrator",
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
    assert result["total_delta"] < 0
    # This wave found proven-dead prose, so the honest verdict is `reduced`.
    assert result["verdict"] == "reduced"


def test_spec_is_strictly_smaller_than_baseline():
    """spec.md specifically — the surface this wave slimmed — must have FEWER
    lines than the pre-wave baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/spec.md"]
    spec_after = after[os.path.normpath(_SPEC)]
    assert spec_after < before, (
        f"spec.md did not shrink: {spec_after} lines now vs baseline {before}")


def test_contract_not_larger_than_baseline():
    """contract.md was already minimal and is unchanged by this wave; it must
    not have grown past its baseline."""
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


# ==========================================================================
# Cleanup gate — the proven-dead content is removed (coding-rules §6).
# ==========================================================================

def test_spec_drops_dead_roadmap_reference():
    """spec.md no longer references docs/ROADMAP.md — that file does not exist in
    the feature dir, so the reference was dead (coding-rules §6)."""
    text = _read(_SPEC)
    assert "ROADMAP.md" not in text, (
        "dead docs/ROADMAP.md reference must be removed from spec.md")
    assert not os.path.exists(os.path.join(_DOCS_DIR, "ROADMAP.md")), (
        "no ROADMAP.md should exist in the feature docs dir")


def test_spec_drops_resolved_open_questions_section():
    """The "Open questions" section asked how mutex ownership identity is stamped;
    src/ settled it as (PID, process start-time), so the question is resolved and
    the section was dead prose (coding-rules §6)."""
    text = _read(_SPEC)
    assert "## Open questions" not in text, (
        "resolved Open questions section must be removed from spec.md")
