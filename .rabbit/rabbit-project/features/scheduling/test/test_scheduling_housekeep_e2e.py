#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the scheduling doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md is slimmed
of PROVEN-DEAD prose — a `docs/ROADMAP.md` reference (no such file exists in the
feature dir or anywhere in the product tree) and the resolved "Open questions"
section (the recurring-vs-one-shot heartbeat shape and the /start-records /
/stop-cancels question is settled: /start schedules a recurring prompt-heartbeat
and records the durable loop-intent, /stop cancels it via CronDelete and clears
the intent) — without losing any load-bearing claim. docs/contract.md is NOT
edited by this wave (the two known contract gaps below are FLAGGED, not patched,
because the fixes are additive work, not deletions). These tests are the
deterministic gate on the wave — they are e2e in that they read the SHIPPED doc
artifacts (not a mock) and assert the wave's contractual properties end to end:

  Gate 1 — MEASURED REDUCTION. The current doc surfaces (docs/spec.md +
  docs/contract.md) are STRICTLY smaller (fewer lines total) than the committed
  pre-wave baseline recorded in housekeep_doc_baseline.json. Measurement is
  delegated to the script-tier tool measure-reduction.py with --docs-only, so
  the housekeeping test THIS wave adds under test/ is never counted as bloat
  (the --docs-only mode restricts the directory walk to doc surfaces only). A
  reword that does not actually reduce the doc-surface line total FAILS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface symbol,
  a route/state/anchor name (GUARD/DRAIN/PULL/PERSIST/EXIT/REVIEW/TRIAGE/...), a
  trust mode, a durable-state key, the route/adapter-map schema names, and the
  contract provides/reads/invokes/never block keys MUST still appear in the
  slimmed docs. A slim that drops a load-bearing token FAILS.

  Gate 3 — BEHAVIOR PRESERVED is the one MANDATORY gate of a wave; it is enforced
  by the feature's existing 319-test suite staying green (run.py), not measured
  here. This module is wave overhead, not the behavior gate.

The two FLAGGED (not fixed) contract gaps this wave deliberately leaves for
additive follow-up work (asserted below so the flags do not silently rot):

  FLAG A — STALE never-clause. contract.md's `never` block still says scheduling
  "makes the tick interval configurable (deferred to ... #17)", but the code now
  DOES make it configurable (start.py reads heartbeat.interval_minutes). The
  clause is stale-but-wrong; it is NOT deleted (a never-clause is load-bearing) —
  it needs an additive correction, tracked separately.

  FLAG B — contract-too-small. The REVIEW gate (#209) and the verify-integrate
  VERIFY/INTEGRATE/CLEANUP factory consumption are implemented + tested in src/
  but absent from the contract reads/provides. The fix is additive (add the
  missing entries), out of scope for a deletion-only doc-slim wave.

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
# <repo>/rabbit-project/features/scheduling/test/, so walk up to the repo root
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


# Load-bearing tokens that MUST survive the slim, asserted against the COMBINED
# doc surface (spec.md + contract.md). Route/state/anchor names, trust modes,
# durable-state keys, public symbols, and schema names live in the spec; the
# provides/reads/invokes/never keys live in the contract block. Membership in the
# union is what "survived the slim" means for the wave.
_LOAD_BEARING_DOCS = (
    # core route + anchors + the close-the-loop / act-side ports
    "GUARD",
    "DRAIN",
    "PULL",
    "PERSIST",
    "EXIT",
    "TRIAGE",
    "PRIORITIZE",
    "IMPLEMENT",
    "VERIFY",
    "INTEGRATE",
    "CLEANUP",
    "REVIEW",
    # trust modes
    "dry-run",
    "propose",
    "auto-merge",
    # route + adapter-map public surface
    "DEFAULT_ROUTE",
    "DEFAULT_ADAPTER_MAP",
    "AGENT_PORT_TEMPLATES",
    "route.json",
    "adapter-map.json",
    # durable-state cross-tick keys / read products
    "BUDGET_KEY",
    "ACTED_LEDGER_KEY",
    "REPORT_LEDGER_KEY",
    "BACKOFF_LEDGER_KEY",
    "TRIAGE_MEMORY_KEY",
    "TICK_CHECKPOINT_KEY",
    "work_items",
    "work_orders",
    "execution_plan",
    "handoffs",
    # config-driven interval (the #17-resolved fact must survive)
    "heartbeat.interval_minutes",
    # public scripts/skills
    "run_tick",
    "start.py",
    "stop.py",
    "status.py",
    "heartbeat.py",
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


def test_contract_not_larger_than_baseline():
    """contract.md is NOT edited by this wave (the two known gaps are FLAGGED,
    not patched); it must not have grown past its baseline."""
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
# Cleanup gate — proven-dead prose removed (coding-rules §6). `find` proves no
# ROADMAP.md exists, and the implementation answers the former "Open questions".
# ==========================================================================

def test_spec_drops_dead_roadmap_reference():
    """spec.md no longer references docs/ROADMAP.md — no such file exists in the
    feature dir, so the reference was dead (coding-rules §6 path-reference check:
    `find` empty == dead)."""
    text = _read(_SPEC)
    assert "ROADMAP.md" not in text, (
        "dead docs/ROADMAP.md reference must be removed from spec.md")
    assert not os.path.exists(os.path.join(_DOCS_DIR, "ROADMAP.md")), (
        "no ROADMAP.md should exist in the feature docs dir")


def test_spec_drops_resolved_open_questions_section():
    """spec.md no longer carries the "Open questions" section — its single
    question (the recurring heartbeat shape + how /start records and /stop cancels
    the heartbeat) is RESOLVED by the shipped implementation, so the open-question
    prose was dead (coding-rules §6 described-behavior check: a reachable code
    path exists)."""
    text = _read(_SPEC)
    assert "## Open questions" not in text, (
        "resolved 'Open questions' section must be removed from spec.md")


# ==========================================================================
# FLAG gate — the two deliberately-unfixed contract gaps are still present
# (this wave is deletion-only; the fixes are additive follow-up work). These
# assertions keep the flags honest: they document that the wave did NOT silently
# patch or delete them.
# ==========================================================================

def test_flag_a_stale_interval_never_clause_left_intact():
    """FLAG A: the stale `never` clause about the tick interval being
    'deferred to ... #17' is NOT deleted by this wave (a never-clause is
    load-bearing; the correct fix is additive). It must still be in contract.md
    so the additive follow-up has something to correct."""
    text = _read(_CONTRACT)
    assert "makes the tick interval configurable" in text, (
        "the stale interval never-clause is FLAGGED for additive correction, "
        "not deleted by this deletion-only wave")


def test_flag_b_review_gate_implemented_but_not_in_contract():
    """FLAG B: the REVIEW gate (#209) is implemented in src/ but the contract
    does NOT yet name it in reads/provides. This wave leaves the gap (additive
    follow-up). The assertion proves the gap is real: REVIEW lives in the source
    but not in the contract."""
    src_dir = os.path.join(_FEATURE_DIR, "src")
    src_text = ""
    for name in sorted(os.listdir(src_dir)):
        if name.endswith(".py"):
            src_text += _read(os.path.join(src_dir, name))
    assert "REVIEW" in src_text, (
        "REVIEW gate (#209) is expected to be implemented in src/")
    contract_text = _read(_CONTRACT)
    assert "REVIEW" not in contract_text, (
        "FLAG B assumes the contract does not yet document REVIEW; if it now "
        "does, this flag is resolved and the test should be updated")
