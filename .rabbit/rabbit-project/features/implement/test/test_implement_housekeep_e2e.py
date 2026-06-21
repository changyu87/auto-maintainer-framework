#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the implement doc-reduction wave.

This wave is a MEASURED doc reduction (rabbit-housekeep, DOC dimension): the
coding-rules §6 verify-or-flag pass was applied to docs/spec.md and
docs/contract.md. Every path reference, cross-feature reference, and described
behaviour in those docs was checked with find/grep/ls and verified LIVE — so the
honest §6 verdict for this feature is `no-op` (nothing was proven dead), which is
a valid SUCCESS, not a failure (reduction is REPORTED, never MANDATED;
measure-reduction.py contract / coding-rules §6).

These tests are the deterministic gate on the wave. They are e2e in that they
read the SHIPPED doc artifacts (not a mock) and assert the wave's contractual
properties end to end:

  Gate 1 — BEHAVIOR PRESERVED (the one MANDATORY gate). The feature's existing
  test modules still pass. A doc-slim that breaks the code's behaviour FAILS.

  Gate 2 — MEASURED VERDICT. The current doc surfaces (docs/spec.md +
  docs/contract.md) are measured against the committed pre-wave baseline via the
  script-tier tool measure-reduction.py with --docs-only (the test/ tree this
  wave adds is never counted as bloat). The verdict MUST be one of {reduced,
  no-op} and the docs MUST NOT have GROWN past baseline. A no-op is an accepted
  PASS — this wave does NOT mandate a reduction; it forbids growth and forbids a
  reword that inflates the surface.

  Gate 3 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  symbol, a Handoff schema field, the slot/manifest vocabulary, the shipped
  subagent artifact path, a live cross-feature reference, and the contract
  provides/reads/invokes/never block keys MUST still appear in the docs. A slim
  that drops a load-bearing token FAILS.

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
# scripts dir). This test lives at <repo>/rabbit-project/features/implement/test/,
# so walk up to the repo root and descend into .claude.
_REPO_ROOT = os.path.dirname(   # <repo>
    os.path.dirname(            # rabbit-project/features
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
    """Run measure-reduction.py diff between the committed baseline and the live
    --docs-only snapshot; return the parsed verdict object."""
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
# doc surface (spec.md + contract.md): the public-surface symbols, the Handoff
# schema fields, the slot/manifest vocabulary, the shipped subagent artifact
# path, and live cross-feature references all live in the spec; the
# provides/reads/invokes/never keys live in the contract block.
_LOAD_BEARING_DOCS = (
    # public-surface symbols / slot vocabulary
    "TickContext",
    "StateManifest",
    "execution_plan",
    "handoffs",
    "DEFAULT_ADAPTER_MAP",
    "run_tick",
    # Handoff schema fields (the load-bearing seam)
    "work_order_id",
    "status",
    "artifact",
    "discovered_work",
    "concerns",
    "blocked_reason",
    "schema_version",
    # closed signal vocabulary
    "OK",
    "BLOCKED",
    # shipped subagent artifact path (verified LIVE by find)
    "ship/agents/auto-maintainer-implementer.md",
    # live cross-feature references
    "adapter-wiring",
    "safety-governance",
    "auto-maintainer",
)

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 1 — behavior preserved (the one MANDATORY gate): the feature's existing
# test modules still pass after the doc slim.
# ==========================================================================

def test_behavior_preserved_existing_suite_green():
    """The MANDATORY gate of any housekeep wave: behaviour is preserved. Run the
    feature's existing (non-housekeep) test modules and assert every test
    function passes. A doc slim that broke the code's behaviour FAILS here."""
    existing = ("test_implement_e2e.py", "test_implementer_agent.py")
    for mod in existing:
        path = os.path.join(_TEST_DIR, mod)
        assert os.path.isfile(path), f"expected existing test module: {mod}"

    import importlib.util
    import traceback

    failures = []
    for mod in existing:
        path = os.path.join(_TEST_DIR, mod)
        name = os.path.splitext(mod)[0]
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for fn_name in sorted(
                n for n in dir(module)
                if n.startswith("test_") and callable(getattr(module, n))):
            try:
                getattr(module, fn_name)()
            except Exception:
                failures.append(f"{name}::{fn_name}\n{traceback.format_exc()}")
    assert not failures, "behavior NOT preserved:\n" + "\n".join(failures)


# ==========================================================================
# Gate 2 — measured verdict: reduced|no-op, docs not larger than baseline.
# ==========================================================================

def test_baseline_fixture_is_present_and_well_formed():
    """The committed baseline fixture exists and records both doc line counts —
    without it there is nothing to measure the wave against."""
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
    housekeeping fixtures/tests this wave adds under test/ are NOT counted. The
    live snapshot keys must be exactly the two doc surfaces — no test/ paths."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_measured_verdict_is_reduced_or_no_op():
    """The measured verdict (from measure-reduction.py's own `diff` subcommand)
    MUST be one of {reduced, no-op}. Reduction is REPORTED, never MANDATED: an
    already-clean no-op is a valid PASS. The ONLY failing outcome is growth."""
    result = _diff_verdict(_FEATURE_DIR)
    assert result["verdict"] in ("reduced", "no-op"), (
        f"unexpected verdict: {result['verdict']}")
    # The wave forbids GROWTH: total_delta must not be positive.
    assert result["total_delta"] <= 0, (
        f"doc surfaces GREW: total_before={result['total_before']} "
        f"total_after={result['total_after']} delta={result['total_delta']}")
    # `reduced` is True iff a real measured reduction happened; consistent with
    # the verdict label.
    assert result["reduced"] is (result["verdict"] == "reduced")


def test_each_doc_surface_not_larger_than_baseline():
    """Neither doc surface grew past its committed baseline. A no-op leaves them
    equal; a reduction leaves them smaller; growth FAILS."""
    after = _measure_docs_only(_FEATURE_DIR)
    base = _baseline()["docs"]
    spec_after = after[os.path.normpath(_SPEC)]
    contract_after = after[os.path.normpath(_CONTRACT)]
    assert spec_after <= base["docs/spec.md"], (
        f"spec.md grew: {spec_after} now vs baseline {base['docs/spec.md']}")
    assert contract_after <= base["docs/contract.md"], (
        f"contract.md grew: {contract_after} now vs baseline "
        f"{base['docs/contract.md']}")


# ==========================================================================
# Gate 3 — load-bearing survival: required tokens still present after the slim.
# ==========================================================================

def test_docs_retain_all_load_bearing_tokens():
    """Every load-bearing token survived the slim, asserted against the combined
    doc surface (spec.md + contract.md): public-surface symbols, Handoff schema
    fields, slot/manifest vocabulary, the shipped subagent artifact path, and
    live cross-feature references."""
    text = _read(_SPEC) + "\n" + _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_DOCS if tok not in text]
    assert not missing, f"docs dropped load-bearing tokens: {missing}"


def test_contract_retains_block_keys():
    """The contract provides/reads/invokes/never block keys survived the slim."""
    text = _read(_CONTRACT)
    missing = [k for k in _CONTRACT_KEYS if k not in text]
    assert not missing, f"contract.md dropped block keys: {missing}"
