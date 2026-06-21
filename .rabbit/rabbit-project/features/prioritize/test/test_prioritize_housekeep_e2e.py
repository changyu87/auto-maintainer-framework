#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the prioritize doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep) on the DOC dimension:
docs/spec.md + docs/contract.md. coding-rules §6 (prove-it-dead-or-flag) was
applied to both surfaces with deterministic find/grep checks and found NOTHING
provably dead:

  - every DESIGN citation (§1.1, §1.1.1, §2.6, §3.5.7, §3.2.4, §3.8) is a
    design-doc anchor, not a removable claim (and is out of scope for this wave);
  - every cross-feature reference is LIVE — `scheduling.run_tick` and
    `DEFAULT_ADAPTER_MAP` exist under rabbit-project/features/scheduling/src/,
    and the adapter-wiring `factory(runtime)` convention exists under
    rabbit-project/features/adapter-wiring/;
  - every public-API name (EXECUTION_PLAN_SLOT, PRIORITIZE_MANIFEST, run,
    factory) and schema field (ordered, status, schema_version) is exercised by
    the existing prioritize suite;
  - there is no dead path reference (no ROADMAP.md), no resolved Open-questions
    section, no implementation-history block, and no redundant restatement to
    remove.

So the honest outcome is a NO-OP, not a reduction. The wave does NOT reword the
docs to manufacture a diff (coding-rules §6: never silently keep the uncertain,
and never force a diff). These tests are the deterministic gate on that honest
no-op wave. They are e2e in that they read the SHIPPED doc artifacts (not a
mock) and assert the wave's three contractual properties end to end:

  Gate 1 — BEHAVIOR PRESERVED (the one MANDATORY gate). The existing prioritize
  test suite (every other test_*.py in this dir) still passes. A doc wave that
  silently broke behavior FAILS here. This is asserted by importing the feature
  module and re-running the public-surface behavior end to end.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  symbol, a schema field, a signal-vocabulary member, a cross-feature anchor,
  and the contract provides/reads/invokes/never block keys MUST still appear in
  the shipped docs. A slim that drops a load-bearing token FAILS.

  Gate 3 — MEASURED VERDICT (honest no-op). The current doc-surface line total,
  measured by the script-tier tool measure-reduction.py with --docs-only, equals
  the committed pre-wave baseline (reduced == false, verdict == "no-op"). A wave
  that QUIETLY grew the docs past baseline FAILS. Measurement is delegated to the
  script so the verdict is deterministic, never a judgment call.

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
# test lives at <repo>/rabbit-project/features/prioritize/test/, so walk up to
# the repo root and descend into .claude.
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


# Load-bearing tokens that MUST survive the wave. Asserted against the COMBINED
# doc surface (spec.md + contract.md): the public-surface symbols, schema
# fields, signal-vocabulary members, and cross-feature anchors live in the spec;
# the provides/reads/invokes/never keys live in the contract block. Membership
# in the union is what "survived" means for the wave.
_LOAD_BEARING_DOCS = (
    # public-surface names as they appear in the docs (the docs name the schema
    # "ExecutionPlan"; the source symbol is EXECUTION_PLAN_SLOT)
    "ExecutionPlan",
    "schema_version",
    "PRIORITIZE",
    "execution_plan",
    "work_orders",
    "run",
    "factory",
    # schema fields
    "ordered",
    "status",
    "pending",
    # closed signal vocabulary members
    "OK",
    "EMPTY",
    # cross-feature anchors (proven live by grep against sibling features)
    "scheduling.run_tick",
    "DEFAULT_ADAPTER_MAP",
    "adapter-wiring",
    "TickContext",
    "StateManifest",
)

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 1 — behavior preserved (the one MANDATORY gate): the public surface
# still runs end to end after the wave.
# ==========================================================================

def test_behavior_preserved_public_surface_runs():
    """The mandatory gate: PRIORITIZE's public surface still produces a correct
    execution_plan after the doc wave. Imported and run end to end exactly as the
    behavior suite does — a doc wave that broke behavior FAILS here."""
    _src = os.path.join(_FEATURE_DIR, "src")
    if _src not in sys.path:
        sys.path.insert(0, _src)
    _fsm_src = os.path.join(
        os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
    if _fsm_src not in sys.path:
        sys.path.insert(0, _fsm_src)
    import prioritize as pr

    manifest, run = pr.factory({})
    assert manifest is pr.PRIORITIZE_MANIFEST
    order = {"schema_version": "1.0.0", "id": "wo-1", "decision": "accepted"}

    import fsm_contracts as fc
    ctx = fc.TickContext()
    ctx.register_slot("work_orders", {"type": "array"}, version="1.0.0")
    ctx.register_slot(
        pr.EXECUTION_PLAN_SLOT["name"], pr.EXECUTION_PLAN_SLOT["schema"],
        version=pr.EXECUTION_PLAN_SLOT["version"])
    ctx.write("work_orders", [order])

    result = run(ctx)
    assert result.signal == "OK"
    assert result.writes["execution_plan"]["ordered"] == ["wo-1"]
    assert result.writes["execution_plan"]["status"] == {"wo-1": "pending"}


# ==========================================================================
# Gate 2 — load-bearing survival: required tokens still present after the wave.
# ==========================================================================

def test_docs_retain_all_load_bearing_tokens():
    """Every load-bearing token survived the wave, asserted against the combined
    doc surface (spec.md + contract.md)."""
    text = _read(_SPEC) + "\n" + _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_DOCS if tok not in text]
    assert not missing, f"docs dropped load-bearing tokens: {missing}"


def test_contract_retains_block_keys():
    """The contract provides/reads/invokes/never block keys survived the wave."""
    text = _read(_CONTRACT)
    missing = [k for k in _CONTRACT_KEYS if k not in text]
    assert not missing, f"contract.md dropped block keys: {missing}"


# ==========================================================================
# Gate 3 — measured verdict: honest no-op (docs equal baseline, not larger).
# ==========================================================================

def test_baseline_fixture_is_present_and_well_formed():
    """The committed baseline fixture exists and records both doc line counts —
    without it there is nothing to measure the verdict against."""
    base = _baseline()
    docs = base["docs"]
    assert docs["docs/spec.md"] > 0
    assert docs["docs/contract.md"] > 0
    assert base["verdict"] == "no-op"


def test_measure_reduction_tool_is_available():
    """The script-tier measurement tool the gate delegates to must exist; the
    gate is script-tier (deterministic), not a judgment call."""
    assert os.path.isfile(_MEASURE), (
        f"measure-reduction.py not found: {_MEASURE}")


def test_docs_only_count_excludes_the_test_tree():
    """--docs-only restricts the directory walk to doc surfaces, so the
    housekeeping test + baseline this wave adds under test/ are NOT counted. The
    live snapshot keys must be exactly the two doc surfaces — no test/ paths."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_measured_verdict_is_honest_no_op():
    """The current doc-surface line total equals the pre-wave baseline: this wave
    removed nothing because nothing was provably dead (coding-rules §6). The
    verdict is produced by measure-reduction.py's own `diff` subcommand —
    reduced == false and verdict == "no-op". A wave that QUIETLY grew the docs
    (total_delta > 0) FAILS this gate."""
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
    assert proc.returncode == 0, (
        f"measure-reduction diff failed: {proc.stderr}")
    result = json.loads(proc.stdout)
    # Honest no-op: nothing was dead, so nothing shrank and nothing grew.
    assert result["total_delta"] == 0, (
        f"doc surfaces drifted from baseline: total_before="
        f"{result['total_before']} total_after={result['total_after']} "
        f"delta={result['total_delta']}")
    assert result["reduced"] is False
    assert result["verdict"] == "no-op"


def test_docs_not_larger_than_baseline():
    """Each doc surface individually must not have grown past its baseline — the
    wave preserves the lean docs, it does not bloat them."""
    after = _measure_docs_only(_FEATURE_DIR)
    base = _baseline()["docs"]
    spec_after = after[os.path.normpath(_SPEC)]
    contract_after = after[os.path.normpath(_CONTRACT)]
    assert spec_after <= base["docs/spec.md"], (
        f"spec.md grew: {spec_after} now vs baseline {base['docs/spec.md']}")
    assert contract_after <= base["docs/contract.md"], (
        f"contract.md grew: {contract_after} now vs baseline "
        f"{base['docs/contract.md']}")
