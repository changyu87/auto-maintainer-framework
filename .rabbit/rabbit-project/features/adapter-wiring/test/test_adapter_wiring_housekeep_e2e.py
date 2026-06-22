#!/usr/bin/env python3
"""End-to-end housekeeping conformance for the adapter-wiring doc-slim wave.

This wave is a MEASURED doc reduction (rabbit-housekeep): docs/spec.md and
docs/contract.md are slimmed of dead/redundant prose without losing any
load-bearing claim. These tests are the deterministic gate on that wave —
they are e2e in that they read the SHIPPED doc artifacts (not a mock) and
assert the wave's two contractual properties end to end:

  Gate 1 — NO DOC GROWTH. The current spec.md / contract.md are no LARGER than
  the committed reference recorded in housekeep_doc_baseline.json. The reference
  was re-baselined when the §3.4.4 scaffold tool (#52) added a public-surface
  section (a restructure the fixture's deprecation_criterion authorizes); the
  gate now forbids growth past that new reference. Doc bloat FAILS.

  Gate 2 — LOAD-BEARING SURVIVAL. Every token that names a public-surface
  function, a resolved type, a core anchor, the factory convention, or a
  contract block key MUST still appear in the slimmed docs (the per-doc lists
  below say which tokens each doc owns). A slim that drops a load-bearing
  token FAILS.

Owner: changyu87
"""

import json
import os

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_TEST_DIR)
_DOCS_DIR = os.path.join(_FEATURE_DIR, "docs")
_BASELINE_PATH = os.path.join(_TEST_DIR, "housekeep_doc_baseline.json")

_SPEC = os.path.join(_DOCS_DIR, "spec.md")
_CONTRACT = os.path.join(_DOCS_DIR, "contract.md")


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _line_count(path):
    with open(path, "r") as f:
        return sum(1 for _ in f)


def _baseline():
    with open(_BASELINE_PATH, "r") as f:
        return json.load(f)


# Load-bearing tokens that MUST survive the slim. The spec list names the
# public surface, the resolved agent type, the error type, the factory
# convention, the agent-dispatch helpers, and the four core anchors
# (GUARD/DRAIN/PERSIST/EXIT). The contract list (below) drops the anchors —
# they live in the spec, not the contract block — and adds the
# provides/reads/invokes/never block keys instead.
_LOAD_BEARING_SPEC = (
    "load_route",
    "load_adapter_map",
    "resolve_states",
    "validate_wiring",
    "build_loop",
    "scaffold_adapter",
    "wire_adapter",
    "validate_adapter_conformance",
    "AgentState",
    "WiringError",
    "module:factory",
    "is_agent_entry",
    "validate_agent_adapter",
    "GUARD",
    "DRAIN",
    "PERSIST",
    "EXIT",
)

_LOAD_BEARING_CONTRACT = (
    "load_route",
    "load_adapter_map",
    "resolve_states",
    "validate_wiring",
    "build_loop",
    "scaffold_adapter",
    "wire_adapter",
    "validate_adapter_conformance",
    "AgentState",
    "module:factory",
    "is_agent_entry",
    "validate_agent_adapter",
    "provides",
    "reads",
    "invokes",
    "never",
)


# ==========================================================================
# Gate 1 — no doc growth: current docs no larger than the re-baselined reference.
# ==========================================================================

def test_baseline_fixture_is_present_and_well_formed():
    """The committed baseline fixture exists and records both doc line counts —
    without it there is nothing to measure reduction against."""
    base = _baseline()
    docs = base["docs"]
    assert docs["docs/spec.md"] > 0
    assert docs["docs/contract.md"] > 0


def test_spec_is_not_larger_than_baseline():
    """spec.md must be no larger than the re-baselined reference — the gate
    forbids doc growth past the recorded reference."""
    base = _baseline()
    before = base["docs"]["docs/spec.md"]
    after = _line_count(_SPEC)
    assert after <= before, (
        f"spec.md grew past the reference: {after} lines now vs {before}")


def test_contract_is_not_larger_than_baseline():
    """contract.md must be no larger than the re-baselined reference."""
    base = _baseline()
    before = base["docs"]["docs/contract.md"]
    after = _line_count(_CONTRACT)
    assert after <= before, (
        f"contract.md grew past the reference: {after} lines now vs {before}")


# ==========================================================================
# Gate 2 — load-bearing survival: required tokens still present after the slim.
# ==========================================================================

def test_spec_retains_all_load_bearing_tokens():
    """Every load-bearing token in the spec survived the slim."""
    text = _read(_SPEC)
    missing = [tok for tok in _LOAD_BEARING_SPEC if tok not in text]
    assert not missing, f"spec.md dropped load-bearing tokens: {missing}"


def test_contract_retains_all_load_bearing_tokens():
    """Every load-bearing token in the contract survived the slim, including the
    provides/reads/invokes/never block keys."""
    text = _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_CONTRACT if tok not in text]
    assert not missing, f"contract.md dropped load-bearing tokens: {missing}"


# ==========================================================================
# Cleanup gate — the dead reads.external claim is removed (coding-rules §6).
# AGENT_ADAPTER_SCHEMA_VERSION is owned by agent-dispatch and is NOT consumed
# by adapter-wiring's src; claiming it in reads.external is proven-dead prose.
# ==========================================================================

def test_contract_drops_dead_agent_adapter_schema_version_claim():
    """contract.md no longer claims to read agent-dispatch's
    AGENT_ADAPTER_SCHEMA_VERSION — that symbol is never consumed by
    adapter-wiring src, so the reads.external claim was dead (coding-rules §6).
    The live agent-dispatch helpers it DOES consume remain declared."""
    text = _read(_CONTRACT)
    assert "AGENT_ADAPTER_SCHEMA_VERSION" not in text, (
        "dead reads.external claim AGENT_ADAPTER_SCHEMA_VERSION must be removed")
    # The two helpers adapter-wiring actually consumes stay declared.
    assert "is_agent_entry" in text
    assert "validate_agent_adapter" in text
