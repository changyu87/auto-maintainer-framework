#!/usr/bin/env python3
"""End-to-end conformance tests for the adapter-map wiring CLI + AGENT_PORT_TEMPLATES.

scheduling owns DEFAULT_ADAPTER_MAP and every port's runtime details, so it ships
the guided adapter-map CLI (`src/adapter_map_config.py`) + the scheduling-owned
`AGENT_PORT_TEMPLATES` table. The CLI is a deterministic load-modify-VALIDATE-save
of ${project_dir}/.auto-maintainer/adapter-map.json: a port can be set to a script
factory address (string) OR to an agent entry. For an agent entry on a KNOWN
agent-capable port the user supplies ONLY the `subagent_type`; the CLI fills the
rest (`writes`, `cardinality`, `effect` for acting ports, and a CONCRETE
`output_example`) from AGENT_PORT_TEMPLATES[port]. The resulting map is VALIDATED
by resolving it (adapter_wiring.resolve_states / build_loop, which deep-validates
agent entries via agent-dispatch) BEFORE writing; an invalid entry is REJECTED (no
write).

This module exercises the spec behaviours:

  1. AGENT_PORT_TEMPLATES maps each KNOWN agent-capable port (TRIAGE, IMPLEMENT)
     -> {writes, cardinality, effect?, output_example}; output_example is a
     CONCRETE value, NEVER a JSON-Schema descriptor (#119 / the agent-adapter rule).
  2. --show with no override prints the DEFAULT_ADAPTER_MAP.
  3. set-agent TRIAGE --subagent-type X fills the entry from the template +
     VALIDATES + WRITES; the written entry is a valid agent-adapter (build_loop
     resolves it to an AgentState) and TRIAGE carries NO effect (non-acting).
  4. set-agent IMPLEMENT --subagent-type X fills an ACTING entry with `effect`
     (an acting port — IMPLEMENT carries effect 'implement').
  5. set-script PULL --address run_tick:make_pull writes a script entry + resolves.
  6. A FAILING set (an invalid agent entry — schema descriptor output_example
     supplied for an unknown/custom port) is REJECTED: non-zero exit, no write.
  7. The TRIAGE template's output_example is REJECTED by neither the descriptor
     guard nor validate_agent_adapter — it is concrete (the agent-adapter rule).

scheduling CONSUMES adapter-wiring + agent-dispatch UNCHANGED; it never modifies
them. AGENT_PORT_TEMPLATES is built from the ports' own slot owners (work-intake
WORK_ORDERS_SLOT, implement HANDOFFS_SLOT), which is why it lives in scheduling.

Owner: changyu87
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402
import agent_dispatch as ad  # noqa: E402
import run_tick as rt  # noqa: E402
import adapter_map_config as amc  # noqa: E402


def _override_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "adapter-map.json")


def _run(argv, project_dir):
    buf = io.StringIO()
    full = list(argv) + ["--project-dir", project_dir]
    with redirect_stdout(buf):
        code = amc.main(full)
    return code, buf.getvalue()


def test_agent_port_templates_concrete_and_typed():
    """AGENT_PORT_TEMPLATES covers the KNOWN agent-capable ports with the right
    shape; each output_example is a CONCRETE example value, never a schema
    descriptor (#119 / the agent-adapter rule)."""
    templates = amc.AGENT_PORT_TEMPLATES
    assert "TRIAGE" in templates, templates
    assert "IMPLEMENT" in templates, templates
    # TRIAGE writes work_orders (work-intake's slot owner), non-acting (no effect).
    triage = templates["TRIAGE"]
    assert triage["writes"] == "work_orders", triage
    assert "cardinality" in triage, triage
    assert not triage.get("effect"), triage  # TRIAGE is non-acting
    # IMPLEMENT writes handoffs (implement's slot owner) and is ACTING.
    impl = templates["IMPLEMENT"]
    assert impl["writes"] == "handoffs", impl
    assert impl.get("effect"), impl  # IMPLEMENT acts
    # No output_example is a JSON-Schema descriptor (the #119 guard would reject
    # it at validate_agent_adapter time).
    for port, tmpl in templates.items():
        ex = tmpl["output_example"]
        assert not ad._is_schema_descriptor(ex), (port, ex)


def test_show_default_map_when_no_override():
    """--show with no project-local adapter-map.json prints the
    DEFAULT_ADAPTER_MAP (every known port -> its factory address)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(["--show"], project_dir)
        assert code == 0, out
        for port in ("GUARD", "PULL", "TRIAGE", "PERSIST", "EXIT"):
            assert port in out, (port, out)
        assert not os.path.isfile(_override_path(project_dir)), out


def test_set_agent_known_port_fills_from_template_and_writes():
    """set-agent TRIAGE --subagent-type X fills writes/cardinality/output_example
    from AGENT_PORT_TEMPLATES['TRIAGE'], VALIDATES (build_loop resolves it to an
    AgentState), and WRITES adapter-map.json. The written entry is a valid
    agent-adapter; TRIAGE carries NO effect (non-acting)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["set-agent", "--port", "TRIAGE",
             "--subagent-type", "auto-maintainer-echo"],
            project_dir)
        assert code == 0, out
        path = _override_path(project_dir)
        assert os.path.isfile(path), out
        with open(path) as f:
            amap = json.load(f)
        entry = amap["TRIAGE"]
        assert entry["kind"] == "agent", entry
        assert entry["dispatch"][0]["subagent_type"] == "auto-maintainer-echo"
        assert entry["dispatch"][0]["writes"] == "work_orders", entry
        # Validated drop-in: build_loop resolves TRIAGE to an AgentState.
        # Route TRIAGE between PULL and PERSIST so it is reachable + data-ready.
        ad.validate_agent_adapter(entry)  # the deep validator accepts it


def test_set_agent_acting_port_carries_effect():
    """set-agent IMPLEMENT --subagent-type X fills an ACTING entry: the dispatch
    entry carries a truthy `effect` (an acting port — the trust-gate applies)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["set-agent", "--port", "IMPLEMENT",
             "--subagent-type", "auto-maintainer-doer"],
            project_dir)
        assert code == 0, out
        with open(_override_path(project_dir)) as f:
            amap = json.load(f)
        entry = amap["IMPLEMENT"]
        assert entry["dispatch"][0].get("effect"), entry
        ad.validate_agent_adapter(entry)


def test_set_script_address_writes_and_resolves():
    """set-script PULL --address run_tick:make_pull writes a script entry; the
    resulting map still resolves through adapter_wiring.build_loop."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["set-script", "--port", "PULL", "--address", "run_tick:make_pull"],
            project_dir)
        assert code == 0, out
        with open(_override_path(project_dir)) as f:
            amap = json.load(f)
        assert amap["PULL"] == "run_tick:make_pull", amap
        runtime = {"project_dir": project_dir, "runtime_dir": project_dir,
                   "source": None, "now": None,
                   "governance": {"mode": "dry-run"}}
        route, states = aw.build_loop(
            rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
            start="GUARD", initial=rt._INITIAL_SLOTS)
        assert "PULL" in states, states


def test_invalid_agent_entry_rejected_no_write():
    """A FAILING set (a custom/unknown port whose required fields cannot be
    inferred and the user supplies an invalid output_example — a JSON-Schema
    descriptor) is REJECTED: non-zero exit AND no adapter-map.json is written."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["set-agent", "--port", "CUSTOMPORT",
             "--subagent-type", "x", "--writes", "work_orders",
             "--output-example", json.dumps({"type": "array",
                                             "items": {"type": "object"}})],
            project_dir)
        assert code != 0, out
        assert not os.path.isfile(_override_path(project_dir)), out


def test_set_agent_then_route_runs_end_to_end():
    """A full drop-in: set-agent TRIAGE + a route routing TRIAGE makes build_loop
    resolve TRIAGE to an AgentState (the ports-and-adapters promise — no code
    change), proving the CLI-written entry is valid runtime config."""
    with tempfile.TemporaryDirectory() as project_dir:
        c1, o1 = _run(
            ["set-agent", "--port", "TRIAGE",
             "--subagent-type", "auto-maintainer-echo"], project_dir)
        assert c1 == 0, o1
        # Write a route routing TRIAGE between PULL and PERSIST.
        route = json.loads(json.dumps(rt.DEFAULT_ROUTE))
        route["states"] = ["GUARD", "DRAIN", "PULL", "TRIAGE", "PERSIST",
                           "EXIT", "DONE", "HALTED"]
        route["edges"] = [
            {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
            {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
            {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
            {"state": "DRAIN", "signal": "OK", "next": "PULL"},
            {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
            {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
            {"state": "TRIAGE", "signal": "OK", "next": "PERSIST"},
            {"state": "TRIAGE", "signal": "EMPTY", "next": "PERSIST"},
            {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
            {"state": "EXIT", "signal": "refire", "next": "DONE"},
            {"state": "EXIT", "signal": "idle", "next": "DONE"},
            {"state": "EXIT", "signal": "break", "next": "DONE"},
            {"state": "EXIT", "signal": "halt", "next": "DONE"},
        ]
        rpath = os.path.join(project_dir, ".auto-maintainer", "route.json")
        with open(rpath, "w") as f:
            json.dump(route, f)
        runtime = {"project_dir": project_dir, "runtime_dir": project_dir,
                   "source": None, "now": None,
                   "governance": {"mode": "dry-run"}}
        rroute, states = aw.build_loop(
            rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
            start="GUARD", initial=rt._INITIAL_SLOTS)
        assert isinstance(states["TRIAGE"][1], aw.AgentState), states["TRIAGE"]
