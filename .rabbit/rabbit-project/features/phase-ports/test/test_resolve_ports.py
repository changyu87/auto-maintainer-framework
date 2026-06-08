#!/usr/bin/env python3
"""Contract tests for the phase-ports v1 implementation surface.

Covers every spec behaviour ("Resolved decisions (v1)" + "v1 implementation
surface"):

  schemas/domain-types.json
    - carries a top-level schema_version "1.0.0".
    - defines the six shared domain types (WorkItem, WorkOrder,
      ExecutionPlan, Handoff, Verdict, IntegrationResult) plus Workspace.
    - Workspace exposes exactly scratch_dir + branch in v1 (decision 4).

  schemas/port-contracts.json
    - bundle carries a single schema_version "1.0.0" (decision 6).
    - declares all seven port contracts (PULL..CLEANUP) with the input/output
      signatures from the spec's "Port type contracts" table.
    - the input/output types reference the domain types defined above
      (decisions 1 + 7: phase-ports owns the types; JSON serialization).

  scripts/resolve_ports.py
    - resolve_ports(config_path, contracts_path) returns all seven port names.
    - an empty ports map resolves every port to the sentinel "default"
      (decision 3: absent port -> default adapter, owned by sibling feature).
    - a named override resolves to its script path; unnamed ports stay default.
    - a ports.json schema_version != the contract bundle schema_version is a
      hard error naming the mismatch (decision 6 / spec validation behaviour).
    - an unknown port key in ports.json is a hard error naming the port.
    - phase-ports owns NO default adapter implementation (decision 2): the
      sentinel is the literal string, not a path inside this feature.

  E2E
    - a real ports.json on disk (the override-resolution wire-up the framework
      performs at pipeline startup) resolves through resolve_ports against the
      shipped port-contracts.json bundle end to end.

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: superseded when per-port independent contract
  versioning replaces the single-bundle version (deferred to v2).
"""

import json
import os
import tempfile
import unittest

from scripts.resolve_ports import resolve_ports, PortResolutionError

HERE = os.path.dirname(os.path.abspath(__file__))
FEATURE_DIR = os.path.dirname(HERE)
SCHEMA_DIR = os.path.join(FEATURE_DIR, "schemas")
DOMAIN_TYPES_PATH = os.path.join(SCHEMA_DIR, "domain-types.json")
PORT_CONTRACTS_PATH = os.path.join(SCHEMA_DIR, "port-contracts.json")

BUNDLE_VERSION = "1.0.0"

CANONICAL_PORTS = [
    "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT", "VERIFY", "INTEGRATE", "CLEANUP",
]

# The spec's "Port type contracts" table, encoded as (input, output) pairs.
PORT_SIGNATURES = {
    "PULL":       (None, "WorkItem[]"),
    "TRIAGE":     ("WorkItem[]", "WorkOrder[]"),
    "PRIORITIZE": ("WorkOrder[]", "ExecutionPlan"),
    "IMPLEMENT":  (["WorkOrder", "Workspace"], "Handoff"),
    "VERIFY":     ("Handoff", "Verdict"),
    "INTEGRATE":  ("Verdict[]", "IntegrationResult"),
    "CLEANUP":    ("IntegrationResult", None),
}

DOMAIN_TYPES = [
    "WorkItem", "WorkOrder", "ExecutionPlan", "Handoff", "Verdict",
    "IntegrationResult",
]


def _load(path):
    with open(path, "r") as f:
        return json.load(f)


def _write_ports_json(dirpath, payload):
    path = os.path.join(dirpath, "ports.json")
    with open(path, "w") as f:
        json.dump(payload, f)
    return path


# ---------------------------------------------------------------------------
# schemas/domain-types.json
# ---------------------------------------------------------------------------

class TestDomainTypesSchema(unittest.TestCase):
    def setUp(self):
        self.doc = _load(DOMAIN_TYPES_PATH)

    def test_schema_version(self):
        self.assertEqual(self.doc.get("schema_version"), BUNDLE_VERSION)

    def test_defines_six_domain_types(self):
        defs = self.doc.get("$defs", {})
        for name in DOMAIN_TYPES:
            self.assertIn(name, defs, f"missing domain type {name}")

    def test_defines_workspace(self):
        defs = self.doc.get("$defs", {})
        self.assertIn("Workspace", defs)

    def test_workspace_exactly_scratch_dir_and_branch(self):
        # Decision 4: Workspace exposes EXACTLY two fields in v1.
        ws = self.doc["$defs"]["Workspace"]
        props = ws.get("properties", {})
        self.assertEqual(set(props.keys()), {"scratch_dir", "branch"})
        self.assertEqual(set(ws.get("required", [])), {"scratch_dir", "branch"})


# ---------------------------------------------------------------------------
# schemas/port-contracts.json
# ---------------------------------------------------------------------------

class TestPortContractsBundle(unittest.TestCase):
    def setUp(self):
        self.bundle = _load(PORT_CONTRACTS_PATH)

    def test_single_bundle_version(self):
        # Decision 6: a single bundle schema_version for all seven ports.
        self.assertEqual(self.bundle.get("schema_version"), BUNDLE_VERSION)

    def test_declares_seven_ports(self):
        ports = self.bundle.get("ports", {})
        self.assertEqual(set(ports.keys()), set(CANONICAL_PORTS))

    def test_each_port_has_input_and_output(self):
        ports = self.bundle["ports"]
        for name in CANONICAL_PORTS:
            self.assertIn("input", ports[name], f"{name} missing input")
            self.assertIn("output", ports[name], f"{name} missing output")

    def test_signatures_match_spec_table(self):
        ports = self.bundle["ports"]
        for name, (inp, out) in PORT_SIGNATURES.items():
            self.assertEqual(ports[name]["input"], inp,
                             f"{name} input signature mismatch")
            self.assertEqual(ports[name]["output"], out,
                             f"{name} output signature mismatch")

    def test_referenced_types_exist_in_domain_types(self):
        # Decisions 1 + 7: port I/O references the domain types phase-ports
        # owns. Strip the [] array marker and unit/None, then confirm every
        # referenced structural type is defined in domain-types.json.
        defs = set(_load(DOMAIN_TYPES_PATH).get("$defs", {}).keys())
        ports = self.bundle["ports"]
        referenced = set()
        for name in CANONICAL_PORTS:
            for slot in ("input", "output"):
                val = ports[name][slot]
                items = val if isinstance(val, list) else [val]
                for item in items:
                    if item is None:
                        continue
                    referenced.add(item.replace("[]", ""))
        self.assertTrue(referenced.issubset(defs),
                        f"port types {referenced - defs} not in domain-types")


# ---------------------------------------------------------------------------
# scripts/resolve_ports.py
# ---------------------------------------------------------------------------

class TestResolvePorts(unittest.TestCase):
    def test_empty_ports_all_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {"schema_version": BUNDLE_VERSION, "ports": {}})
            resolved = resolve_ports(cfg, PORT_CONTRACTS_PATH)
            self.assertEqual(set(resolved.keys()), set(CANONICAL_PORTS))
            self.assertTrue(all(v == "default" for v in resolved.values()))

    def test_named_override_resolves_to_path(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {
                "schema_version": BUNDLE_VERSION,
                "ports": {"PULL": "/opt/adapters/pull.py"},
            })
            resolved = resolve_ports(cfg, PORT_CONTRACTS_PATH)
            self.assertEqual(resolved["PULL"], "/opt/adapters/pull.py")
            # Every other port stays default.
            for name in CANONICAL_PORTS:
                if name != "PULL":
                    self.assertEqual(resolved[name], "default")

    def test_all_seven_resolve(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {
                "schema_version": BUNDLE_VERSION,
                "ports": {p: f"/x/{p}.py" for p in CANONICAL_PORTS},
            })
            resolved = resolve_ports(cfg, PORT_CONTRACTS_PATH)
            self.assertEqual(set(resolved.keys()), set(CANONICAL_PORTS))
            for p in CANONICAL_PORTS:
                self.assertEqual(resolved[p], f"/x/{p}.py")

    def test_schema_version_mismatch_is_hard_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {"schema_version": "9.9.9", "ports": {}})
            with self.assertRaises(PortResolutionError) as ctx:
                resolve_ports(cfg, PORT_CONTRACTS_PATH)
            # The error must name the mismatch (both versions visible).
            msg = str(ctx.exception)
            self.assertIn("9.9.9", msg)
            self.assertIn(BUNDLE_VERSION, msg)

    def test_unknown_port_key_is_hard_error(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {
                "schema_version": BUNDLE_VERSION,
                "ports": {"BOGUS": "/x/bogus.py"},
            })
            with self.assertRaises(PortResolutionError) as ctx:
                resolve_ports(cfg, PORT_CONTRACTS_PATH)
            self.assertIn("BOGUS", str(ctx.exception))

    def test_default_sentinel_is_not_a_phase_ports_path(self):
        # Decision 2: phase-ports owns NO default adapter. An unresolved port
        # is the literal sentinel, never a path into this feature.
        with tempfile.TemporaryDirectory() as d:
            cfg = _write_ports_json(d, {"schema_version": BUNDLE_VERSION, "ports": {}})
            resolved = resolve_ports(cfg, PORT_CONTRACTS_PATH)
            for v in resolved.values():
                self.assertEqual(v, "default")
                self.assertNotIn("phase-ports", v)


# ---------------------------------------------------------------------------
# E2E: project-config override wire-up at pipeline startup
# ---------------------------------------------------------------------------

class TestEndToEndWireUp(unittest.TestCase):
    def test_project_ports_json_resolves_against_shipped_bundle(self):
        """Simulate the framework startup step: read a project's ports.json
        from disk and resolve all seven port names against the shipped
        port-contracts.json bundle. Overridden ports map to their script
        path; absent ports fall back to the sentinel default adapter (owned
        by the sibling feature, not phase-ports). This is the end-to-end
        contract for decision 3."""
        with tempfile.TemporaryDirectory() as project_config_root:
            cfg = _write_ports_json(project_config_root, {
                "schema_version": BUNDLE_VERSION,
                "ports": {
                    "TRIAGE": "/proj/adapters/my_triage.py",
                    "VERIFY": "/proj/adapters/my_verify.py",
                },
            })
            resolved = resolve_ports(cfg, PORT_CONTRACTS_PATH)

            self.assertEqual(resolved["TRIAGE"], "/proj/adapters/my_triage.py")
            self.assertEqual(resolved["VERIFY"], "/proj/adapters/my_verify.py")
            for name in ("PULL", "PRIORITIZE", "IMPLEMENT", "INTEGRATE", "CLEANUP"):
                self.assertEqual(resolved[name], "default")

    def test_e2e_incompatible_version_aborts_wire_up(self):
        """A project config referencing an incompatible contract version must
        fail at validation/wire-up time with a clear error identifying the
        mismatch (spec 'Current behaviour' final bullet; decision 6)."""
        with tempfile.TemporaryDirectory() as project_config_root:
            cfg = _write_ports_json(project_config_root, {
                "schema_version": "0.9.0",
                "ports": {"PULL": "/proj/adapters/pull.py"},
            })
            with self.assertRaises(PortResolutionError):
                resolve_ports(cfg, PORT_CONTRACTS_PATH)


if __name__ == "__main__":
    unittest.main()
