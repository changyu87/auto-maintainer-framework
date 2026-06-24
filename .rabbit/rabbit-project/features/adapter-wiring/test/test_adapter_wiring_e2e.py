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

# adapter-wiring consumes the already-implemented fsm-contracts +
# tick-orchestrator + agent-dispatch (the agent-adapter helper lib).
_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "agent-dispatch"):
    _dep_src = os.path.join(_FEATURES_DIR, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import agent_dispatch as ad  # noqa: E402
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


# ==========================================================================
# Behaviour 6 — agent-adapter object entries (DESIGN §2.8 / §3.4.6). An
# adapter-map entry may be EITHER a "module:factory" string (script, unchanged)
# OR an agent-adapter dict. resolve_states validates the dict via agent-dispatch
# and yields (manifest, AgentState); the manifest participates in validation
# exactly as a script-state manifest does. Agent states are RESOLVED + VALIDATED
# here but NOT executed (no dispatch) — execution is a later slice.
# ==========================================================================

def _agent_entry():
    """A well-formed agent-adapter object entry for the AGENT port. Reads
    `count` (written by GUARD), writes `result`, emits the `GO` edge signal."""
    return {
        "kind": "agent",
        "manifest": {"reads": ["count"], "writes": ["result"], "emits": ["GO"]},
        "dispatch": [
            {
                "subagent_type": "stub-worker",
                "inputs": ["count"],
                "cardinality": "once",
                "writes": "result",
                "task": "do the thing",
            }
        ],
        "signal": {"rule": "nonempty_else_empty"},
    }


# A route threading an AGENT state between GUARD and PERSIST. GUARD seeds count;
# AGENT reads count + emits GO; PERSIST reads count + emits DONE; EXIT terminal.
_AGENT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "AGENT", "PERSIST", "EXIT"],
    "edges": [
        {"state": "GUARD", "signal": "GO", "next": "AGENT"},
        {"state": "AGENT", "signal": "GO", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "DONE", "next": "EXIT"},
    ],
    "terminal": ["EXIT"],
}


def _agent_map():
    return {
        "GUARD": "stub_guard:make",
        "AGENT": _agent_entry(),
        "PERSIST": "stub_persist:make",
        "EXIT": "stub_exit:make",
    }


def test_resolve_states_yields_agent_state_for_agent_entry():
    """An agent-adapter object entry resolves to (manifest, AgentState): the
    manifest mirrors the entry's manifest; the AgentState carries the dispatch,
    signal, and the raw entry."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_AGENT_ROUTE, _agent_map(), _runtime(proj))
        assert set(states) == set(_AGENT_ROUTE["states"])

        manifest, second = states["AGENT"]
        # First element is uniformly the manifest (script OR agent).
        assert isinstance(manifest, fc.StateManifest)
        assert list(manifest.reads) == ["count"]
        assert list(manifest.writes) == ["result"]
        assert list(manifest.emits) == ["GO"]

        # Second element is the resolved AgentState (not a run callable).
        assert isinstance(second, aw.AgentState)
        assert second.manifest is manifest
        assert second.dispatch == _agent_entry()["dispatch"]
        assert second.signal == _agent_entry()["signal"]
        assert second.entry["kind"] == "agent"


def test_resolve_states_mixed_map_resolves_both_kinds():
    """A MIXED adapter-map (script strings + an agent object) resolves both: the
    script states yield (manifest, run-callable); the agent state yields
    (manifest, AgentState)."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_AGENT_ROUTE, _agent_map(), _runtime(proj))

        # Script states keep their run callable as the second element.
        for script_state in ("GUARD", "PERSIST", "EXIT"):
            manifest, second = states[script_state]
            assert isinstance(manifest, fc.StateManifest)
            assert callable(second)
            assert not isinstance(second, aw.AgentState)

        # The agent state's second element is an AgentState, never callable.
        _m, agent_second = states["AGENT"]
        assert isinstance(agent_second, aw.AgentState)


def test_resolve_states_malformed_agent_entry_errors_naming_port():
    """A malformed agent entry (delegated to agent-dispatch.validate_agent_adapter)
    raises a locatable WiringError naming the offending port."""
    bad_cases = [
        # missing manifest
        {"kind": "agent", "dispatch": [{"subagent_type": "x", "inputs": [],
         "cardinality": "once", "writes": "result"}],
         "signal": {"rule": "always_ok"}},
        # empty dispatch
        {"kind": "agent",
         "manifest": {"reads": ["count"], "writes": ["result"], "emits": ["GO"]},
         "dispatch": [], "signal": {"rule": "always_ok"}},
        # bad cardinality
        {"kind": "agent",
         "manifest": {"reads": ["count"], "writes": ["result"], "emits": ["GO"]},
         "dispatch": [{"subagent_type": "x", "inputs": [],
          "cardinality": "twice", "writes": "result"}],
         "signal": {"rule": "always_ok"}},
        # bad signal rule
        {"kind": "agent",
         "manifest": {"reads": ["count"], "writes": ["result"], "emits": ["GO"]},
         "dispatch": [{"subagent_type": "x", "inputs": [],
          "cardinality": "once", "writes": "result"}],
         "signal": {"rule": "no_such_rule"}},
    ]
    for bad in bad_cases:
        amap = {
            "GUARD": "stub_guard:make",
            "AGENT": bad,
            "PERSIST": "stub_persist:make",
            "EXIT": "stub_exit:make",
        }
        with tempfile.TemporaryDirectory() as proj:
            _write_stub_modules(proj)
            try:
                aw.resolve_states(_AGENT_ROUTE, amap, _runtime(proj))
            except aw.WiringError as exc:
                assert "AGENT" in str(exc), (
                    f"WiringError must name the port for {bad}: {exc}")
            else:
                raise AssertionError(
                    f"malformed agent entry must raise WiringError: {bad}")


def test_load_adapter_map_accepts_object_entries():
    """load_adapter_map accepts a map whose values are EITHER strings OR agent
    objects — it does NOT deep-validate the agent dict (that is resolve_states'
    job via agent-dispatch)."""
    with tempfile.TemporaryDirectory() as proj:
        amap = aw.load_adapter_map(_agent_map(), proj)
        assert amap == _agent_map()
        assert isinstance(amap["AGENT"], dict)


def test_load_adapter_map_rejects_non_str_non_dict_entry():
    """An entry that is neither a str nor a dict is a locatable WiringError."""
    bad_map = {"GUARD": "stub_guard:make", "AGENT": 123}
    with tempfile.TemporaryDirectory() as proj:
        try:
            aw.load_adapter_map(bad_map, proj)
        except aw.WiringError as exc:
            assert "AGENT" in str(exc)
        else:
            raise AssertionError(
                "a non-str/non-dict entry must raise WiringError naming the port")


def test_validate_wiring_passes_for_satisfied_agent_state():
    """validate_wiring PASSES for a route whose agent-state reads are satisfied by
    a predecessor's writes — proving the agent manifest participates in
    data-readiness + signal validation."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_AGENT_ROUTE, _agent_map(), _runtime(proj))
        manifests = {name: m for name, (m, _s) in states.items()}
        res = aw.validate_wiring(_AGENT_ROUTE, manifests, start="GUARD",
                                 initial=[])
        assert res.passed is True


def test_validate_wiring_fails_when_agent_reads_unwritten_slot():
    """validate_wiring FAILS (data-readiness) when the agent-state reads a slot no
    predecessor writes — the agent manifest is enforced like any other."""
    # GUARD seeds `count`, but the AGENT here reads `missing`, which nothing
    # writes. resolve a map whose AGENT entry reads an unfulfilled slot.
    bad_agent = _agent_entry()
    bad_agent["manifest"]["reads"] = ["missing"]
    amap = {
        "GUARD": "stub_guard:make",
        "AGENT": bad_agent,
        "PERSIST": "stub_persist:make",
        "EXIT": "stub_exit:make",
    }
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_AGENT_ROUTE, amap, _runtime(proj))
        manifests = {name: m for name, (m, _s) in states.items()}
        res = aw.validate_wiring(_AGENT_ROUTE, manifests, start="GUARD",
                                 initial=[])
        assert res.passed is False


def test_e2e_build_loop_resolves_mixed_route_and_validates():
    """End-to-end: build_loop over a MIXED route (script GUARD/PERSIST/EXIT + an
    agent AGENT) loads + resolves + validates, returning (route, states) with the
    agent state resolved to an AgentState and the wiring validated at LOAD."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        route, states = aw.build_loop(
            _AGENT_ROUTE, _agent_map(), _runtime(proj),
            start="GUARD", initial=[])
        assert route == _AGENT_ROUTE
        assert set(states) == set(_AGENT_ROUTE["states"])
        # The agent state survived load+resolve+validate as an AgentState.
        _m, agent_second = states["AGENT"]
        assert isinstance(agent_second, aw.AgentState)
        # Script states are unchanged: run callables.
        assert callable(states["GUARD"][1])


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


# ==========================================================================
# Behaviour 7 — build_loop optional `migrate` hook. A project may supply an
# optional pure dict -> dict callable that transforms the loaded adapter-map
# AFTER load and BEFORE resolve/validate, so scheduling can self-heal stale
# adapter-map entries on load. adapter-wiring stays template-agnostic: it only
# invokes the supplied callable; it knows nothing about what it rewrites.
# migrate=None (the default) is byte-for-byte unchanged behaviour.
# ==========================================================================

def test_build_loop_migrate_applies_before_resolve():
    """A migrate callable rewrites the loaded adapter-map before resolve, so the
    resolved states reflect the TRANSFORMED entry, not the original one."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        # The default map points WORK at a stale/non-resolvable address; the
        # migrate hook heals it to the real stub before resolve runs.
        stale_map = dict(_SPINE_MAP)
        stale_map["WORK"] = "stale_module_that_does_not_exist:make"

        def migrate(amap):
            healed = dict(amap)
            healed["WORK"] = "stub_work:make"
            return healed

        route, states = aw.build_loop(
            _SPINE_ROUTE, stale_map, _runtime(proj),
            start="GUARD", initial=[], migrate=migrate)

        # WORK resolved against the MIGRATED address, not the stale one.
        manifest, run = states["WORK"]
        assert isinstance(manifest, fc.StateManifest)
        assert callable(run)

        # And the resolved loop still drives to terminal end-to-end.
        ctx = fc.TickContext()
        ctx.register_slot("count", {"type": "integer"}, version="1.0.0")
        vocab = fc.SignalVocabulary(_SIGNALS)
        result = to.run(route, states, ctx, vocab, start="GUARD")
        assert result.final_state == "EXIT"
        assert result.path == ["GUARD", "WORK", "PERSIST", "EXIT"]


def test_build_loop_no_migrate_is_backward_compatible():
    """migrate=None (the default) leaves behaviour byte-for-byte unchanged: the
    loaded map is resolved as-is."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        # Default-kwarg call (no migrate) and explicit migrate=None must both
        # behave exactly like the legacy build_loop.
        route_a, states_a = aw.build_loop(
            _SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
            start="GUARD", initial=[])
        route_b, states_b = aw.build_loop(
            _SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
            start="GUARD", initial=[], migrate=None)

        assert route_a == route_b == _SPINE_ROUTE
        assert set(states_a) == set(states_b) == set(_SPINE_ROUTE["states"])

        ctx = fc.TickContext()
        ctx.register_slot("count", {"type": "integer"}, version="1.0.0")
        vocab = fc.SignalVocabulary(_SIGNALS)
        result = to.run(route_b, states_b, ctx, vocab, start="GUARD")
        assert result.final_state == "EXIT"
        assert result.path == ["GUARD", "WORK", "PERSIST", "EXIT"]


def test_build_loop_migrate_bad_map_surfaces_wiring_error():
    """A migrate that produces a MALFORMED map surfaces as a WiringError (never a
    silent pass): the migrated map feeds resolve + validate exactly like a
    loaded map, so a bad transform fails at LOAD."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)

        def migrate(amap):
            healed = dict(amap)
            # Heal WORK into an unimportable module — the migrated map must
            # fail resolution as a locatable WiringError naming the port.
            healed["WORK"] = "no_such_migrated_module:make"
            return healed

        try:
            aw.build_loop(_SPINE_ROUTE, _SPINE_MAP, _runtime(proj),
                          start="GUARD", initial=[], migrate=migrate)
        except aw.WiringError as exc:
            assert "WORK" in str(exc)
        else:
            raise AssertionError(
                "a malformed migrated map must raise WiringError")
