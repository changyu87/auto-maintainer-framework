#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the agent-dispatch doc-slim wave.

The doc-slim wave itself is COMPLETE and shipped: docs/spec.md was slimmed of
proven-redundant restatement (the Invariants section's duplicate of the
`output_path`/`## Handoff` mechanics and the `build_envelopes` `schema`-key
parenthetical). Its one-time STRICT-REDUCTION size gates (spec/doc-surface
line totals strictly below the pre-wave baseline, plus the diff-verdict record)
have been RETIRED: they were single-use measured-reduction assertions whose
deprecation criterion — superseded by a later agent-dispatch change to the doc
surfaces — is now met by the per-item id->work_order join spec addition
(feat/agent-dispatch-peritem-join). A perpetual "spec must stay below the
pre-slim baseline forever" assertion would forbid all legitimate functional
spec growth, contradicting Designed Deprecation; the reduction verdict was
already recorded green at ship time.

What REMAINS is the ongoing, non-single-use gate — the properties that must
hold for every future state of the docs regardless of size:

  Gate — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  function, the agent-adapter schema keys, the closed signal vocabulary, the
  cardinality vocabulary, the envelope output_contract keys, and the contract
  provides/reads/invokes/never block keys MUST still appear in the docs. A doc
  edit that drops a load-bearing token FAILS. contract.md, already minimal, must
  not grow past its baseline. These read the SHIPPED doc artifacts (not a mock)
  and assert their contractual properties end to end.

Behavior-preserved — the one MANDATORY gate of any housekeep wave — is covered
by the feature's existing e2e/unit suite (test_agent_dispatch_e2e.py) staying
green under the same run.py.

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
# <repo>/rabbit-project/features/agent-dispatch/test/, so walk up to the repo
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
# Baseline + measurement plumbing (kept from the completed doc-slim wave): the
# baseline fixture and the script-tier --docs-only measurement still back the
# contract.md non-growth check below. The strict spec/doc-surface reduction
# gates and the diff-verdict record have been retired (see module docstring).
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


def test_contract_not_larger_than_baseline():
    """contract.md was already minimal and is unchanged by this wave; it must
    not have grown past its baseline."""
    after = _measure_docs_only(_FEATURE_DIR)
    before = _baseline()["docs"]["docs/contract.md"]
    contract_after = after[os.path.normpath(_CONTRACT)]
    assert contract_after <= before, (
        f"contract.md grew: {contract_after} lines now vs baseline {before}")


# ==========================================================================
# Load-bearing survival: required tokens still present in the docs.
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
