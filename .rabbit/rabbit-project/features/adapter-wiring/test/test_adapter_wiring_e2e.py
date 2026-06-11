#!/usr/bin/env python3
"""End-to-end conformance tests for adapter-wiring (route-as-data + adapter wiring).

Every behaviour in docs/spec.md has an e2e test here. The headline proof is a
build_loop(...) over STUB factory modules whose resolved (manifest, run) pairs
drive a REAL tick_orchestrator.run to a terminal state — proving the loader +
resolver + validator produce exactly what the orchestrator consumes.

These stub adapters carry ZERO maintainer-domain meaning. They are written to a
temporary directory placed on sys.path, addressed by "module:factory" strings,
and exist only as test fixtures — never promoted to features. adapter-wiring is
a generic mechanism: it imports adapters dynamically by string, so the tests do
NOT depend on any real adapter.

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

# adapter-wiring consumes the already-implemented fsm-contracts + tick-orchestrator.
_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator"):
    _dep_src = os.path.join(_FEATURES_DIR, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import adapter_wiring as aw  # noqa: E402


# --------------------------------------------------------------------------
# Stub adapter modules. Each is a "module:factory" addressable factory that
# returns (StateManifest, run_callable). The run_callable has the fsm-contracts
# signature run(TickContext) -> StateResult. They form a GUARD -> WORK ->
# PERSIST -> EXIT spine over a single integer `count` slot and a GO/STOP/DONE
# closed signal set.
#
# GUARD: writes count (seeds it), emits GO.   (entry anchor)
# WORK:  reads count, writes count+1, emits GO.
# PERSIST: reads count, emits DONE.           (PERSIST before EXIT)
# EXIT:  terminal anchor (never runs).
# --------------------------------------------------------------------------

_STUB_GUARD = '''
import fsm_contracts as fc

def make(runtime):
    manifest = fc.StateManifest(reads=[], writes=["count"], emits=["GO"])
    def run(ctx):
        ctx_seed = runtime["seed"]
        return fc.StateResult(signal="GO", writes={"count": ctx_seed})
    return manifest, run
'''

_STUB_WORK = '''
import fsm_contracts as fc

def make(runtime):
    manifest = fc.StateManifest(reads=["count"], writes=["count"], emits=["GO"])
    def run(ctx):
        n = ctx.read("count") + 1
        return fc.StateResult(signal="GO", writes={"count": n})
    return manifest, run
'''

_STUB_PERSIST = '''
import fsm_contracts as fc

def make(runtime):
    manifest = fc.StateManifest(reads=["count"], writes=[], emits=["DONE"])
    def run(ctx):
        ctx.read("count")
        return fc.StateResult(signal="DONE", writes={})
    return manifest, run
'''

_STUB_EXIT = '''
import fsm_contracts as fc

def make(runtime):
    # Terminal anchor: never run() but resolvable so validators see its manifest.
    manifest = fc.StateManifest(reads=[], writes=[], emits=[])
    def run(ctx):  # pragma: no cover - terminal never executes
        raise AssertionError("terminal state EXIT must never run()")
    return manifest, run
'''


def _write_stub_modules(dirpath):
    """Write the stub adapter .py files into dirpath and put it on sys.path."""
    for name, body in (
        ("stub_guard.py", _STUB_GUARD),
        ("stub_work.py", _STUB_WORK),
        ("stub_persist.py", _STUB_PERSIST),
        ("stub_exit.py", _STUB_EXIT),
    ):
        with open(os.path.join(dirpath, name), "w") as f:
            f.write(body)
    if dirpath not in sys.path:
        sys.path.insert(0, dirpath)


# A conforming GUARD -> WORK -> PERSIST -> EXIT route + adapter map.
_SPINE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "WORK", "PERSIST", "EXIT"],
    "edges": [
        {"state": "GUARD", "signal": "GO", "next": "WORK"},
        {"state": "WORK", "signal": "GO", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "DONE", "next": "EXIT"},
    ],
    "terminal": ["EXIT"],
}

_SPINE_MAP = {
    "GUARD": "stub_guard:make",
    "WORK": "stub_work:make",
    "PERSIST": "stub_persist:make",
    "EXIT": "stub_exit:make",
}

_SIGNALS = ["GO", "DONE"]


def _runtime(project_dir):
    return {"project_dir": project_dir, "seed": 0}


# ==========================================================================
# Behaviour 1 — load_route: project-local route.json overrides default; default
# used when absent; malformed route is a locatable error.
# ==========================================================================

def test_load_route_uses_default_when_no_project_file():
    with tempfile.TemporaryDirectory() as proj:
        route = aw.load_route(_SPINE_ROUTE, proj)
        assert route == _SPINE_ROUTE


def test_load_route_prefers_project_local_file():
    other = {
        "schema_version": "1.0.0",
        "states": ["GUARD", "EXIT"],
        "edges": [{"state": "GUARD", "signal": "GO", "next": "EXIT"}],
        "terminal": ["EXIT"],
    }
    with tempfile.TemporaryDirectory() as proj:
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(other, f)
        route = aw.load_route(_SPINE_ROUTE, proj)
        assert route == other
        assert route != _SPINE_ROUTE


def test_load_route_malformed_is_locatable_error():
    """A project-local route.json that fails validate_route raises a locatable
    error (not a silent fall-through to the default)."""
    bad = {"states": ["GUARD"]}  # missing edges + terminal
    with tempfile.TemporaryDirectory() as proj:
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(bad, f)
        try:
            aw.load_route(_SPINE_ROUTE, proj)
        except aw.WiringError as exc:
            # The error names where the malformed route lives.
            assert "route.json" in str(exc)
        else:
            raise AssertionError("malformed route.json must raise WiringError")


# ==========================================================================
# Behaviour 2 — load_adapter_map: same override logic.
# ==========================================================================

def test_load_adapter_map_uses_default_when_no_project_file():
    with tempfile.TemporaryDirectory() as proj:
        amap = aw.load_adapter_map(_SPINE_MAP, proj)
        assert amap == _SPINE_MAP


def test_load_adapter_map_prefers_project_local_file():
    override = {"GUARD": "x:y", "EXIT": "p:q"}
    with tempfile.TemporaryDirectory() as proj:
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
            json.dump(override, f)
        amap = aw.load_adapter_map(_SPINE_MAP, proj)
        assert amap == override
        assert amap != _SPINE_MAP


# ==========================================================================
# Behaviour 3 — resolve_states: import each module:factory, call factory(runtime),
# assemble {state: (manifest, run)}. Unknown port / unimportable module /
# missing factory => locatable error naming the offending port.
# ==========================================================================

def test_resolve_states_builds_states_from_stub_factories():
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_SPINE_ROUTE, _SPINE_MAP, _runtime(proj))
        assert set(states) == set(_SPINE_ROUTE["states"])
        for name, (manifest, run) in states.items():
            assert isinstance(manifest, fc.StateManifest)
            assert callable(run)


def test_resolve_states_unknown_port_errors_naming_port():
    """A route state with no adapter-map entry is a locatable error naming it."""
    route = {
        "schema_version": "1.0.0",
        "states": ["GUARD", "EXIT"],
        "edges": [{"state": "GUARD", "signal": "GO", "next": "EXIT"}],
        "terminal": ["EXIT"],
    }
    amap = {"GUARD": "stub_guard:make"}  # EXIT missing
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        try:
            aw.resolve_states(route, amap, _runtime(proj))
        except aw.WiringError as exc:
            assert "EXIT" in str(exc)
        else:
            raise AssertionError("unknown port must raise WiringError naming the port")


def test_resolve_states_unimportable_module_errors_naming_port():
    amap = dict(_SPINE_MAP)
    amap["WORK"] = "no_such_module_xyz:make"
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        try:
            aw.resolve_states(_SPINE_ROUTE, amap, _runtime(proj))
        except aw.WiringError as exc:
            assert "WORK" in str(exc)
        else:
            raise AssertionError("unimportable module must raise WiringError naming the port")


def test_resolve_states_missing_factory_errors_naming_port():
    amap = dict(_SPINE_MAP)
    amap["WORK"] = "stub_work:no_such_factory"
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        try:
            aw.resolve_states(_SPINE_ROUTE, amap, _runtime(proj))
        except aw.WiringError as exc:
            assert "WORK" in str(exc)
        else:
            raise AssertionError("missing factory must raise WiringError naming the port")


def test_resolve_states_bad_address_format_errors_naming_port():
    """An address without the module:factory shape is a locatable error."""
    amap = dict(_SPINE_MAP)
    amap["WORK"] = "stub_work_no_colon"
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        try:
            aw.resolve_states(_SPINE_ROUTE, amap, _runtime(proj))
        except aw.WiringError as exc:
            assert "WORK" in str(exc)
        else:
            raise AssertionError("bad address format must raise WiringError naming the port")


# ==========================================================================
# Behaviour 4 — validate_wiring: signals + data-readiness + anchor invariants.
# ==========================================================================

def _spine_manifests(proj):
    states = aw.resolve_states(_SPINE_ROUTE, _SPINE_MAP, _runtime(proj))
    return {name: m for name, (m, _r) in states.items()}


def test_validate_wiring_accepts_conforming_spine():
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        manifests = _spine_manifests(proj)
        res = aw.validate_wiring(_SPINE_ROUTE, manifests, start="GUARD",
                                 initial=[])
        assert res.passed is True


def test_validate_wiring_rejects_entry_not_guard():
    """An entry/start state that is not GUARD violates the anchor invariant."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        manifests = _spine_manifests(proj)
        res = aw.validate_wiring(_SPINE_ROUTE, manifests, start="WORK",
                                 initial=[])
        assert res.passed is False


def test_validate_wiring_rejects_unwritten_read_slot():
    """Reuses data-readiness: a state reading a slot no predecessor wrote fails."""
    # GUARD does not write count here, so WORK's read of count is unfulfilled.
    manifests = {
        "GUARD": fc.StateManifest(reads=[], writes=[], emits=["GO"]),
        "WORK": fc.StateManifest(reads=["count"], writes=["count"], emits=["GO"]),
        "PERSIST": fc.StateManifest(reads=["count"], writes=[], emits=["DONE"]),
        "EXIT": fc.StateManifest(reads=[], writes=[], emits=[]),
    }
    res = aw.validate_wiring(_SPINE_ROUTE, manifests, start="GUARD", initial=[])
    assert res.passed is False


def test_validate_wiring_rejects_edge_signal_not_in_emits():
    """Reuses signal-validity: an edge signal absent from emits fails."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        manifests = _spine_manifests(proj)
        # GUARD emits ["GO"] only; reroute on a signal it never declares.
        bad_route = {
            **_SPINE_ROUTE,
            "edges": _SPINE_ROUTE["edges"] + [
                {"state": "GUARD", "signal": "NOPE", "next": "EXIT"}],
        }
        res = aw.validate_wiring(bad_route, manifests, start="GUARD", initial=[])
        assert res.passed is False


def test_validate_wiring_rejects_missing_terminal():
    """A route with no terminal state violates the EXIT-terminal anchor invariant."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        manifests = _spine_manifests(proj)
        no_terminal = {**_SPINE_ROUTE, "terminal": []}
        res = aw.validate_wiring(no_terminal, manifests, start="GUARD",
                                 initial=[])
        assert res.passed is False


def test_validate_wiring_rejects_persist_after_exit():
    """PERSIST must precede EXIT on the path; a route where EXIT is reachable
    without passing PERSIST violates the invariant."""
    # GUARD --GO--> EXIT directly; PERSIST is bypassed.
    route = {
        "schema_version": "1.0.0",
        "states": ["GUARD", "PERSIST", "EXIT"],
        "edges": [
            {"state": "GUARD", "signal": "GO", "next": "EXIT"},
        ],
        "terminal": ["EXIT"],
    }
    manifests = {
        "GUARD": fc.StateManifest(reads=[], writes=[], emits=["GO"]),
        "PERSIST": fc.StateManifest(reads=[], writes=[], emits=["DONE"]),
        "EXIT": fc.StateManifest(reads=[], writes=[], emits=[]),
    }
    res = aw.validate_wiring(route, manifests, start="GUARD", initial=[])
    assert res.passed is False


# ==========================================================================
# Behaviour 5 — build_loop: load + resolve + validate, returning (route, states)
# that tick_orchestrator.run executes to terminal. The HEADLINE e2e proof.
# ==========================================================================

def test_e2e_build_loop_drives_orchestrator_to_terminal():
    """End-to-end: build_loop over STUB factories yields (route, states) that a
    REAL tick_orchestrator.run drives GUARD -> WORK -> PERSIST -> EXIT."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        route, states = aw.build_loop(
            _SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
            start="GUARD", initial=[])

        # Drive the resolved (manifest, run) pairs through the real orchestrator.
        ctx = fc.TickContext()
        ctx.register_slot("count", {"type": "integer"}, version="1.0.0")
        vocab = fc.SignalVocabulary(_SIGNALS)
        result = to.run(route, states, ctx, vocab, start="GUARD")

        assert result.final_state == "EXIT"
        assert result.path == ["GUARD", "WORK", "PERSIST", "EXIT"]
        assert result.signals == ["GO", "GO", "DONE"]
        # GUARD seeded count=0, WORK bumped to 1.
        assert ctx.read("count") == 1


def test_e2e_build_loop_honours_project_local_overrides():
    """build_loop reads project-local route.json + adapter-map.json when present,
    overriding the caller defaults, and still drives to terminal."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        # Project-local route: GUARD -> PERSIST -> EXIT (drop WORK).
        proj_route = {
            "schema_version": "1.0.0",
            "states": ["GUARD", "PERSIST", "EXIT"],
            "edges": [
                {"state": "GUARD", "signal": "GO", "next": "PERSIST"},
                {"state": "PERSIST", "signal": "DONE", "next": "EXIT"},
            ],
            "terminal": ["EXIT"],
        }
        proj_map = {
            "GUARD": "stub_guard:make",
            "PERSIST": "stub_persist:make",
            "EXIT": "stub_exit:make",
        }
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(proj_route, f)
        with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
            json.dump(proj_map, f)

        # Pass the FULL spine as defaults; the project-local files must win.
        route, states = aw.build_loop(
            _SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
            start="GUARD", initial=[])

        assert route == proj_route
        assert set(states) == {"GUARD", "PERSIST", "EXIT"}

        ctx = fc.TickContext()
        ctx.register_slot("count", {"type": "integer"}, version="1.0.0")
        vocab = fc.SignalVocabulary(_SIGNALS)
        result = to.run(route, states, ctx, vocab, start="GUARD")
        assert result.final_state == "EXIT"
        assert result.path == ["GUARD", "PERSIST", "EXIT"]


def test_e2e_build_loop_rejects_invalid_wiring_before_running():
    """build_loop validates at LOAD: an invalid wiring fails before any tick."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        # Entry is not GUARD-anchored: start state WORK reaches no GUARD.
        bad_route = {
            "schema_version": "1.0.0",
            "states": ["WORK", "EXIT"],
            "edges": [{"state": "WORK", "signal": "GO", "next": "EXIT"}],
            "terminal": ["EXIT"],
        }
        bad_map = {"WORK": "stub_work:make", "EXIT": "stub_exit:make"}
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(bad_route, f)
        with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
            json.dump(bad_map, f)
        try:
            aw.build_loop(_SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
                          start="WORK", initial=[])
        except aw.WiringError:
            pass
        else:
            raise AssertionError("build_loop must raise WiringError on invalid wiring")
