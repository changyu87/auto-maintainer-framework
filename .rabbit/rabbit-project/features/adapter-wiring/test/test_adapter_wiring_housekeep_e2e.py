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
  contract block key MUST still appear in the slimmed docs. The required set is
  read from the shared declaration (test/load_bearing_tokens.json) and asserted
  against the combined doc surface. A slim that drops a load-bearing token FAILS.

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


def _declared_load_bearing_tokens():
    """Read the load-bearing token declaration (test/load_bearing_tokens.json),
    the single source of truth shared with the #353 doc-survival GATE. The gate
    and this test MUST assert the same token set, so both read this one file
    rather than each keeping an independent copy that could silently drift."""
    with open(os.path.join(_TEST_DIR, "load_bearing_tokens.json"), "r") as f:
        return tuple(json.load(f)["tokens"])


# Load-bearing tokens that MUST survive the slim, read from the shared
# declaration (single source of truth with the #353 GATE). The declaration is a
# flat set asserted against the COMBINED doc surface (spec.md + contract.md):
# the public surface, resolved types, factory convention and core anchors live
# in the spec; the provides/reads/invokes/never block keys live in the contract.
# Membership in the union is what "survived the slim" means for the wave.
_LOAD_BEARING_DOCS = _declared_load_bearing_tokens()


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

def test_docs_retain_all_load_bearing_tokens():
    """Every declared load-bearing token survived the slim, asserted against the
    combined doc surface (spec.md + contract.md) — the same union-membership
    semantics the #353 GATE applies to the shared declaration."""
    text = _read(_SPEC) + "\n" + _read(_CONTRACT)
    missing = [tok for tok in _LOAD_BEARING_DOCS if tok not in text]
    assert not missing, f"docs dropped load-bearing tokens: {missing}"


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
