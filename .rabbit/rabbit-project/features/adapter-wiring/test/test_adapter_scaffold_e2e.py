#!/usr/bin/env python3
"""End-to-end conformance tests for the §3.4.4 adapter authoring/scaffold tool.

The headline proof: scaffold a skeleton adapter for a port, have the author fill
the manifest + run body, wire it into project-local route/map files, then run
validate_adapter_conformance over the result — exercising scaffold -> author ->
wire -> CHECK end to end, all on the SAME resolver/validator a live load uses.

These adapters carry ZERO maintainer-domain meaning; they exist only as test
fixtures in a temp dir placed on sys.path. The scaffold tool is generic.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "agent-dispatch"):
    _dep_src = os.path.join(_FEATURES_DIR, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import adapter_wiring as aw  # noqa: E402


def _runtime(project_dir):
    return {"project_dir": project_dir}


def _make_importable(d):
    if d not in sys.path:
        sys.path.insert(0, d)


# --------------------------------------------------------------------------
# scaffold_adapter — emits a skeleton conforming to the factory convention.
# --------------------------------------------------------------------------

def test_scaffold_emits_a_resolvable_factory_module():
    """The emitted skeleton imports and resolves: its factory returns a
    (StateManifest, callable run) pair — the factory convention verbatim."""
    with tempfile.TemporaryDirectory() as src:
        path, address = aw.scaffold_adapter("MYPORT", src)
        assert os.path.isfile(path)
        assert address == "myport:make"
        _make_importable(src)
        manifest, run = aw._resolve_factory("MYPORT", address, _runtime(src))
        assert isinstance(manifest, fc.StateManifest)
        assert callable(run)
        # The skeleton's run returns a well-formed StateResult out of the box.
        result = run(None)
        assert fc.validate_state_result(result).passed
        assert result.signal in manifest.emits


def test_scaffold_derives_a_safe_module_name_from_the_port():
    """A port with non-identifier chars yields a safe lowercased module name."""
    with tempfile.TemporaryDirectory() as src:
        path, address = aw.scaffold_adapter("MY-PORT", src)
        assert address == "my_port:make"
        assert os.path.basename(path) == "my_port.py"


def test_scaffold_refuses_to_clobber_without_overwrite():
    """A second scaffold of the same port is a locatable WiringError unless
    overwrite=True, so an author's work is never silently destroyed."""
    with tempfile.TemporaryDirectory() as src:
        aw.scaffold_adapter("PORT", src)
        try:
            aw.scaffold_adapter("PORT", src)
        except aw.WiringError as exc:
            assert "PORT" in str(exc)
        else:
            raise AssertionError("expected WiringError on clobber")
        # overwrite=True succeeds.
        path, _ = aw.scaffold_adapter("PORT", src, overwrite=True)
        assert os.path.isfile(path)


# --------------------------------------------------------------------------
# wire_adapter — records the port -> adapter map + adds the route state.
# --------------------------------------------------------------------------

def test_wire_adapter_writes_project_local_route_and_map():
    """wire_adapter records port -> address in adapter-map.json and adds the
    port as a state in route.json under ${project_dir}/.auto-maintainer/."""
    with tempfile.TemporaryDirectory() as proj:
        route, amap = aw.wire_adapter("MYPORT", "myport:make", proj)
        assert amap["MYPORT"] == "myport:make"
        assert "MYPORT" in route["states"]
        # Persisted to disk, reloadable by load_route / load_adapter_map.
        loaded_map = aw.load_adapter_map({}, proj)
        assert loaded_map["MYPORT"] == "myport:make"
        cfg = os.path.join(proj, ".auto-maintainer")
        assert os.path.isfile(os.path.join(cfg, "route.json"))
        assert os.path.isfile(os.path.join(cfg, "adapter-map.json"))


def test_wire_adapter_extends_an_existing_route_and_map():
    """Wiring a new port preserves existing states/entries and is idempotent on
    the state list (no duplicate state)."""
    with tempfile.TemporaryDirectory() as proj:
        base_route = {"schema_version": "1.0.0", "states": ["GUARD"],
                      "edges": [], "terminal": ["GUARD"]}
        aw.wire_adapter("GUARD", "guard:make", proj, default_route=base_route,
                        default_map={"GUARD": "guard:make"})
        route, amap = aw.wire_adapter("NEWPORT", "newport:make", proj)
        assert route["states"] == ["GUARD", "NEWPORT"]
        assert amap == {"GUARD": "guard:make", "NEWPORT": "newport:make"}
        # Re-wiring an existing port does not duplicate the state.
        route2, _ = aw.wire_adapter("NEWPORT", "newport:make", proj)
        assert route2["states"].count("NEWPORT") == 1


# --------------------------------------------------------------------------
# validate_adapter_conformance — the CHECK that makes BYO a checked operation.
# --------------------------------------------------------------------------

def test_conformance_passes_for_a_conforming_scaffold():
    """A freshly scaffolded adapter passes the conformance check (resolvable
    factory, StateManifest + callable run, declared signals, signal/data
    validation over the synthetic single-state route)."""
    with tempfile.TemporaryDirectory() as src:
        _path, address = aw.scaffold_adapter("WORK", src)
        _make_importable(src)
        verdict = aw.validate_adapter_conformance(address, _runtime(src))
        assert verdict.passed, verdict.messages


def test_conformance_reports_an_unresolvable_address():
    """An address that cannot be resolved is a FAILED CheckResult, not a raised
    exception — the author gets a message, not a traceback."""
    verdict = aw.validate_adapter_conformance(
        "no_such_module:make", _runtime("/tmp"))
    assert verdict.passed is False
    assert any("no_such_module" in m for m in verdict.messages)


def test_conformance_rejects_a_factory_with_a_bad_return():
    """A factory that does not return (StateManifest, callable) FAILS the
    factory-shape check."""
    with tempfile.TemporaryDirectory() as src:
        mod = os.path.join(src, "badret.py")
        with open(mod, "w") as f:
            f.write("def make(runtime):\n    return 'not', 'a manifest'\n")
        _make_importable(src)
        verdict = aw.validate_adapter_conformance("badret:make", _runtime(src))
        assert verdict.passed is False
        assert any("StateManifest" in m for m in verdict.messages)


def test_conformance_rejects_a_manifest_reading_an_unwritten_slot():
    """A manifest that reads a slot no predecessor writes FAILS data-readiness
    (the reused tick-orchestrator validator), proving the CHECK is the real
    load-time validation, not a stub."""
    with tempfile.TemporaryDirectory() as src:
        mod = os.path.join(src, "reader.py")
        with open(mod, "w") as f:
            f.write(
                "import fsm_contracts as fc\n"
                "def make(runtime):\n"
                "    m = fc.StateManifest(reads=['missing'], writes=[],"
                " emits=['OK'])\n"
                "    return m, (lambda ctx: fc.StateResult('OK'))\n")
        _make_importable(src)
        verdict = aw.validate_adapter_conformance("reader:make", _runtime(src),
                                                  initial=[])
        assert verdict.passed is False
        assert any("missing" in m for m in verdict.messages)


def test_conformance_against_a_supplied_real_route():
    """When emits_route is supplied the adapter is validated in the topology it
    will actually run in, not the synthetic single-state route."""
    with tempfile.TemporaryDirectory() as src:
        _path, address = aw.scaffold_adapter("STEP", src, default_signal="GO")
        _make_importable(src)
        real_route = {
            "schema_version": "1.0.0",
            "states": ["STEP", "EXIT"],
            "edges": [{"state": "STEP", "signal": "GO", "next": "EXIT"}],
            "terminal": ["EXIT"],
        }
        verdict = aw.validate_adapter_conformance(
            address, _runtime(src), port="STEP", emits_route=real_route,
            initial=[])
        assert verdict.passed, verdict.messages


# --------------------------------------------------------------------------
# Full flow: scaffold -> author -> wire -> validate end to end.
# --------------------------------------------------------------------------

def test_e2e_scaffold_author_wire_validate_flow():
    """scaffold a skeleton, have the author replace the body, wire it into the
    project route/map, then run conformance over the wired adapter — the whole
    BYO-adapter authoring loop as a CHECKED operation."""
    with tempfile.TemporaryDirectory() as proj:
        src = os.path.join(proj, "adapters")
        path, address = aw.scaffold_adapter("EMIT", src, default_signal="DONE")
        # Author fills in a real body (here: write a slot it declares).
        with open(path, "w") as f:
            f.write(
                "import fsm_contracts as fc\n"
                "def make(runtime):\n"
                "    m = fc.StateManifest(reads=[], writes=['n'],"
                " emits=['DONE'])\n"
                "    def run(ctx):\n"
                "        return fc.StateResult('DONE', writes={'n': 1})\n"
                "    return m, run\n")
        _make_importable(src)
        aw.wire_adapter("EMIT", address, proj)
        verdict = aw.validate_adapter_conformance(address, _runtime(proj))
        assert verdict.passed, verdict.messages
        # The wiring persisted and reloads.
        loaded = aw.load_adapter_map({}, proj)
        assert loaded["EMIT"] == address
