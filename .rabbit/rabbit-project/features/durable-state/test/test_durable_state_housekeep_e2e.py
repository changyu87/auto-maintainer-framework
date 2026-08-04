#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the durable-state doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md is
slimmed of dead/redundant prose (a proven-dead docs/ROADMAP.md reference and a
"## Current behaviour" section that restated the Public surface) without losing
any load-bearing claim. docs/contract.md is already minimal and is unchanged.
These tests are the deterministic gate on that wave — they are e2e in that they
read the SHIPPED doc artifacts (not a mock) and run the SHIPPED behaviour suite,
asserting the wave's three contractual properties end to end:

  Gate 0 — BEHAVIOR PRESERVED (the one MANDATORY gate). The existing
  durable-state behaviour suite (test_durable_state_e2e.py, the 11 tests incl.
  the truncate->resume exactly-once crash-safety test) still passes after the
  slim. A doc edit that broke the feature suite FAILS. This gate runs that
  module in a subprocess so it never recurses into this housekeep module.

  Gate 1 — MEASURED REDUCTION. The current doc surfaces (docs/spec.md +
  docs/contract.md) are no larger than the committed pre-wave baseline recorded
  in housekeep_doc_baseline.json, and spec.md specifically is STRICTLY smaller.
  Measurement is delegated to the script-tier tool measure-reduction.py with
  --docs-only, so the housekeeping test THIS wave adds under test/ is never
  counted as bloat. The reduce|no-op verdict is produced by the tool's own
  `diff` subcommand. A no-op is a valid pass, but this wave is expected to
  reduce spec.md.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface type,
  the journal/dedup convention, the two anchor states, the uniform signature,
  the consumed fsm-contracts symbols, and the contract
  provides/reads/invokes/never block keys MUST still appear in the slimmed docs.
  A slim that drops a load-bearing token FAILS.

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

# The behaviour suite this wave must keep green (the one MANDATORY gate).
_BEHAVIOUR_SUITE = os.path.join(_TEST_DIR, "test_durable_state_e2e.py")

# measure-reduction.py is the script-tier line-accounting tool (rabbit-housekeep
# scripts dir). This test lives at
# <repo>/rabbit-project/features/durable-state/test/, so walk up to the repo
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


def _declared_load_bearing_tokens():
    """Read the load-bearing token declaration (test/load_bearing_tokens.json),
    the single source of truth shared with the #353 doc-survival GATE. The gate
    and this test MUST assert the same token set, so both read this one file
    rather than each keeping an independent copy that could silently drift."""
    with open(os.path.join(_TEST_DIR, "load_bearing_tokens.json"), "r") as f:
        return tuple(json.load(f)["tokens"])


# Load-bearing tokens that MUST survive the slim, read from the shared
# declaration (single source of truth with the #353 GATE).
_LOAD_BEARING_DOCS = _declared_load_bearing_tokens()

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 0 — behavior preserved: the existing behaviour suite stays green.
# This is the ONE mandatory gate of a measured-reduction wave.
# ==========================================================================

def _load_behaviour_module(alias):
    """Import the behaviour module (test_durable_state_e2e.py) under a private
    alias and return it. Importing it here, not via the directory runner, keeps
    this housekeep module from recursing into itself."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(alias, _BEHAVIOUR_SUITE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_behaviour_suite_still_green():
    """The pre-existing durable-state behaviour suite (11 tests incl. the
    truncate->resume exactly-once crash-safety test) still passes after the doc
    slim. Imported and run in-process so this housekeep module is NOT discovered
    (no recursion); a raised assertion fails this gate loudly."""
    module = _load_behaviour_module("durable_state_behaviour_suite")
    fns = sorted(
        n for n in dir(module)
        if n.startswith("test_") and callable(getattr(module, n)))
    assert len(fns) == 11, (
        f"expected the 11-test behaviour suite, found {len(fns)}: {fns}")
    for fn_name in fns:
        getattr(module, fn_name)()


def test_behaviour_suite_has_eleven_tests_including_crash_safety():
    """The behaviour suite count is exactly 11 and includes the headline
    truncate->resume exactly-once crash-safety test the spec names."""
    module = _load_behaviour_module("durable_state_behaviour_suite_count")
    fns = sorted(
        n for n in dir(module)
        if n.startswith("test_") and callable(getattr(module, n)))
    assert len(fns) == 11, f"expected 11 behaviour tests, found {len(fns)}: {fns}"
    assert any("crash_safety" in n for n in fns), (
        "the truncate->resume exactly-once crash-safety test must be present")


# ==========================================================================
# Gate 1 — measured reduction: current doc surfaces no larger than baseline,
# spec.md strictly smaller; verdict produced by the script-tier tool.
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
    housekeeping test (and its baseline fixture) this wave adds under test/ are
    NOT counted. The live snapshot keys must be exactly the two doc surfaces."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_doc_reduction_verdict_is_reduced_or_no_op():
    """The reduce|no-op verdict is produced by measure-reduction.py's own `diff`
    subcommand over the --docs-only snapshots. A no-op is a valid pass (nothing
    was dead); a measured reduction is expected here. The total must never
    GROW."""
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
    assert result["verdict"] in ("reduced", "no-op"), result["verdict"]
    # The wave never grows the doc surface total.
    assert result["total_delta"] <= 0, (
        f"doc surfaces grew: total_before={result['total_before']} "
        f"total_after={result['total_after']} delta={result['total_delta']}")


def test_spec_is_strictly_smaller_than_baseline():
    """spec.md specifically — the surface this wave slimmed — must have FEWER
    lines than the pre-wave baseline (the expected, non-no-op reduction)."""
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
# Cleanup gate — the dead docs/ROADMAP.md reference is removed (coding-rules §6).
# `find` proves no ROADMAP.md exists under the feature dir, so the spec's
# reference to it was proven-dead prose.
# ==========================================================================

def test_spec_drops_dead_roadmap_reference():
    """spec.md no longer references docs/ROADMAP.md — that file does not exist in
    the feature dir, so the reference was dead (coding-rules §6)."""
    text = _read(_SPEC)
    assert "ROADMAP.md" not in text, (
        "dead docs/ROADMAP.md reference must be removed from spec.md")
    assert not os.path.exists(os.path.join(_DOCS_DIR, "ROADMAP.md")), (
        "no ROADMAP.md should exist in the feature docs dir")
