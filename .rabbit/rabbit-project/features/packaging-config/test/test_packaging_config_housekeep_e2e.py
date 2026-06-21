#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the packaging-config doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep, DOC dimension):
docs/spec.md is slimmed of proven-dead / proven-resolved prose — a dead
docs/ROADMAP.md reference (no such file exists under the feature dir) and the
"Open questions" section whose two questions are now answered by the shipped
implementation (the core libs live in `lib/`; the plugin version is pinned
explicitly in plugin.json). docs/contract.md is already minimal and is
unchanged. These tests are the deterministic gate on that wave — they are e2e
in that they read the SHIPPED doc artifacts (not a mock) and exercise the
script-tier measurement end to end, asserting the wave's contractual
properties:

  Gate 0 — BEHAVIOR PRESERVED (the one MANDATORY gate). The feature's existing
  build test suite (test_build_plugin.py) still passes. A doc slim that broke
  the build would FAIL here. Reduction is REPORTED; behavior-preserved is
  MANDATED.

  Gate 1 — MEASURED REDUCTION verdict. The current doc surfaces (docs/spec.md +
  docs/contract.md) are no LARGER than the committed pre-wave baseline recorded
  in housekeep_doc_baseline.json, and the script-tier verdict is `reduced` or
  `no-op` (never grew). Measurement is delegated to measure-reduction.py with
  --docs-only, so the housekeeping test this wave adds under test/ is never
  counted as bloat. The honest verdict is recorded either way: a wave that
  removed dead prose reports `reduced`; a wave that found nothing dead reports
  `no-op` — both are SUCCESS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  artifact, a shipped/normalized lib, a config asset, the cross-feature source
  references, and the contract provides/reads/invokes/never block keys MUST
  still appear in the slimmed docs. A slim that drops a load-bearing token
  FAILS.

  Gate 3 — CLEANUP PROOF. The dead docs/ROADMAP.md reference is gone from
  spec.md (coding-rules §6: `find` proves no ROADMAP.md under the feature dir,
  so the reference was dead prose).

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
_RUN_PY = os.path.join(_TEST_DIR, "run.py")

# measure-reduction.py is the script-tier line-accounting tool (rabbit-housekeep
# scripts dir). This test lives at
# <repo>/rabbit-project/features/packaging-config/test/, so walk up to the repo
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
    the parsed JSON snapshot. --docs-only restricts the walk to doc surfaces
    (docs/spec.md, docs/contract.md, skills/*/SKILL.md), so the test/ tree this
    wave adds is excluded and never flips the verdict."""
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


# Load-bearing tokens that MUST survive the slim, asserted against the COMBINED
# doc surface (spec.md + contract.md). These name the public surface, the
# shipped/normalized libs, the config assets, the cross-feature source refs, and
# the headline no-leak token. Membership in the union is what "survived the
# slim" means for the wave.
_LOAD_BEARING_DOCS = (
    # public surface
    "build_plugin.py",
    "marketplace.json",
    "plugin.json",
    "hooks.json",
    "auto-maintainer",
    "_NORMALIZED_LIBS",
    "ship/",
    "default-config",
    # shipped / normalized libs (proven live: present in plugins/.../lib/)
    "fsm_contracts",
    "tick_orchestrator",
    "configure.py",
    "route_config.py",
    "adapter_map_config.py",
    "verify_integrate.py",
    "work_intake.py",
    "run_tick.py",
    # cross-feature source references
    "scheduling",
    "safety-governance",
    # the headline clean-ship invariant token
    ".rabbit",
)

# The contract block keys are asserted against contract.md specifically.
_CONTRACT_KEYS = ("provides", "reads", "invokes", "never")


# ==========================================================================
# Gate 0 — behavior preserved: the existing build suite still passes (MANDATORY).
# ==========================================================================

def test_existing_build_suite_still_green():
    """The one MANDATORY housekeep gate: the feature's existing test suite
    (build_plugin tests) stays green after the doc slim. Reduction is reported;
    behavior-preserved is mandated."""
    proc = subprocess.run(
        [sys.executable, "-c",
         "import importlib.util,sys; "
         f"spec=importlib.util.spec_from_file_location('tbp', "
         f"{os.path.join(_TEST_DIR, 'test_build_plugin.py')!r}); "
         "m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m); "
         "fns=sorted(n for n in dir(m) if n.startswith('test_') "
         "and callable(getattr(m,n))); "
         "[getattr(m,n)() for n in fns]; "
         "print('OK', len(fns))"],
        capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"existing build suite did not stay green: {proc.stdout}\n{proc.stderr}")


# ==========================================================================
# Gate 1 — measured reduction verdict (reduced | no-op; never grew).
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
    """--docs-only restricts the walk to doc surfaces, so the housekeeping test
    this wave adds under test/ is NOT counted. The live snapshot keys must be
    exactly the two doc surfaces — no test/ paths."""
    snap = _measure_docs_only(_FEATURE_DIR)
    keys = sorted(k for k in snap if k != "__total__")
    assert keys == sorted([
        os.path.normpath(_SPEC), os.path.normpath(_CONTRACT)]), (
        f"--docs-only snapshot must cover only doc surfaces, got: {keys}")


def test_doc_surfaces_verdict_is_reduced_or_no_op():
    """The wave's measured verdict, produced by measure-reduction.py's own `diff`
    subcommand, is `reduced` or `no-op` — the docs did NOT grow. A wave that
    removed dead prose reports `reduced`; an already-clean wave reports `no-op`.
    Either is a SUCCESS; growth is the only failure."""
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
    assert result["verdict"] in ("reduced", "no-op"), (
        f"unexpected verdict {result['verdict']!r}")
    assert result["total_delta"] <= 0, (
        f"doc surfaces GREW: total_before={result['total_before']} "
        f"total_after={result['total_after']} delta={result['total_delta']}")


def test_spec_not_larger_than_baseline():
    """spec.md — the surface this wave slims — must not have grown past its
    pre-wave baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/spec.md"]
    spec_after = after[os.path.normpath(_SPEC)]
    assert spec_after <= before, (
        f"spec.md grew: {spec_after} lines now vs baseline {before}")


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
# Gate 3 — cleanup proof: the dead docs/ROADMAP.md reference is removed.
# `find` proves no ROADMAP.md exists under the feature dir (coding-rules §6),
# so the spec's feature-relative reference to it was proven-dead prose.
# ==========================================================================

def test_spec_drops_dead_roadmap_reference():
    """spec.md no longer references docs/ROADMAP.md — that file does not exist in
    the feature dir, so the reference was dead (coding-rules §6)."""
    assert not os.path.exists(os.path.join(_DOCS_DIR, "ROADMAP.md")), (
        "no ROADMAP.md should exist in the feature docs dir")
    text = _read(_SPEC)
    assert "ROADMAP.md" not in text, (
        "dead docs/ROADMAP.md reference must be removed from spec.md")
