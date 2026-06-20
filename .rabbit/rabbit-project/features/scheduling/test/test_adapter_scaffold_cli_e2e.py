#!/usr/bin/env python3
"""End-to-end conformance tests for the BYO-adapter scaffold tool (DESIGN §3.4.4).

scheduling ships the adapter-scaffold tool (`src/adapter_scaffold.py`) — the
provider-supplied convenience that makes bring-your-own-adapter a CHECKED
operation. Given a port it (1) EMITS a skeleton script adapter conforming to the
factory convention (manifest + run(TickContext)->StateResult + factory(runtime)),
(2) WIRES the port into the adapter-map + route (load-modify-save, reusing the
sibling route_config / adapter_map_config helpers), and (3) RUNS a
contract-conformance validator over the resulting wiring (adapter_wiring.build_loop
-> tick_orchestrator.validate_signals + validate_data_readiness + fsm-contracts).
A failure REJECTS the operation and rolls back every file written.

This module exercises the spec behaviours:

  1. --emit prints a skeleton conforming to the factory convention (factory +
     run + a fsm-contracts StateManifest) WITHOUT writing anything.
  2. The emitted skeleton is importable + resolvable: factory(runtime) returns a
     (StateManifest, run_callable), and run(ctx) returns a StateResult whose
     signal is in the manifest emits.
  3. `new` emits the adapter file, wires the adapter-map (port -> "<mod>:factory")
     AND the route (the new state inserted between --after/--before), and the
     resulting wiring VALIDATES via adapter_wiring.build_loop — proving the
     scaffolded adapter is a runnable, contract-conforming drop-in.
  4. A duplicate port (already a route state) is REJECTED — the scaffold authors
     NEW ports, not overrides.
  5. A bad insertion point (--after/--before not in the route) is REJECTED with
     NO partial config left behind (full rollback: no adapter file, no map/route
     override created).

scheduling CONSUMES adapter-wiring + agent-dispatch + fsm-contracts +
tick-orchestrator UNCHANGED (their validators); the scaffold tool never modifies
them.

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

import importlib.util  # noqa: E402

import fsm_contracts as fc  # noqa: E402
import adapter_wiring as aw  # noqa: E402
import run_tick as rt  # noqa: E402
import adapter_scaffold as asc  # noqa: E402


def _run(argv, project_dir):
    buf = io.StringIO()
    full = list(argv) + ["--project-dir", project_dir]
    with redirect_stdout(buf):
        code = asc.main(full)
    return code, buf.getvalue()


def _route_override(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "route.json")


def _map_override(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "adapter-map.json")


def _load_module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_emit_prints_skeleton_no_write():
    """--emit --port P prints a skeleton conforming to the factory convention
    (factory + run + a StateManifest), writing NOTHING."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(["--emit", "--port", "ENRICH"], project_dir)
        assert code == 0, out
        assert "def factory(runtime)" in out, out
        assert "def run(ctx)" in out, out
        assert "StateManifest" in out, out
        assert "StateResult" in out, out
        # No files written by --emit.
        assert not os.path.isdir(
            os.path.join(project_dir, ".auto-maintainer")), out


def test_emitted_skeleton_is_importable_and_resolves():
    """The rendered skeleton imports, and factory(runtime) returns a
    (StateManifest, run_callable); run(ctx) returns a StateResult whose signal is
    in the manifest emits — i.e. it satisfies the factory convention + the
    fsm-contracts state contract out of the box."""
    src = asc.render_skeleton("ENRICH")
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "enrich.py")
        with open(path, "w") as f:
            f.write(src)
        mod = _load_module(path, "enrich")
        manifest, run = mod.factory({"project_dir": d})
        assert isinstance(manifest, fc.StateManifest), manifest
        # StateManifest normalizes reads/writes/emits to tuples.
        assert manifest.reads == (), manifest.reads
        assert manifest.writes == ("enrich_result",), manifest.writes
        ctx = fc.TickContext()
        ctx.register_slot("enrich_result", {"type": "null"}, "1.0.0")
        result = run(ctx)
        assert isinstance(result, fc.StateResult), result
        assert result.signal in manifest.emits, result.signal
        # The state result envelope is well-formed.
        assert fc.validate_state_result(result).passed


def test_new_emits_wires_and_validates_end_to_end():
    """`new` emits the adapter, wires the adapter-map + route, and the resulting
    wiring VALIDATES via adapter_wiring.build_loop — the scaffolded adapter is a
    runnable, contract-conforming drop-in (the ports-and-adapters promise)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["new", "--port", "ENRICH", "--after", "PULL", "--before",
             "PERSIST"],
            project_dir)
        assert code == 0, out
        # 1. The adapter file was emitted.
        adapter_path = os.path.join(
            project_dir, ".auto-maintainer", "adapters", "enrich.py")
        assert os.path.isfile(adapter_path), out
        # 2. The adapter-map override wires ENRICH -> the emitted factory.
        with open(_map_override(project_dir)) as f:
            amap = json.load(f)
        assert amap["ENRICH"] == "enrich:factory", amap
        # 3. The route override inserts ENRICH between PULL and PERSIST.
        with open(_route_override(project_dir)) as f:
            route = json.load(f)
        assert "ENRICH" in route["states"], route
        states = route["states"]
        assert states.index("PULL") < states.index("ENRICH") < \
            states.index("PERSIST"), states
        # 4. The whole wiring resolves + validates with the adapters dir on path.
        adapters_dir = os.path.join(project_dir, ".auto-maintainer", "adapters")
        sys.path.insert(0, adapters_dir)
        try:
            runtime = {"project_dir": project_dir,
                       "runtime_dir": os.path.join(
                           project_dir, ".auto-maintainer"),
                       "source": None, "now": None,
                       "governance": {"mode": "dry-run"}}
            rroute, rstates = aw.build_loop(
                rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
                start="GUARD", initial=rt._INITIAL_SLOTS)
            assert "ENRICH" in rstates, rstates
            manifest, run = rstates["ENRICH"]
            assert isinstance(manifest, fc.StateManifest), manifest
            assert callable(run), run
        finally:
            sys.path.remove(adapters_dir)
            sys.modules.pop("enrich", None)


def test_duplicate_port_rejected():
    """A port that is already a route state is REJECTED — the scaffold authors
    NEW ports, not overrides of an existing one."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["new", "--port", "PULL", "--after", "DRAIN", "--before",
             "PERSIST"],
            project_dir)
        assert code != 0, out
        assert "REJECTED" in out, out
        # Nothing written.
        assert not os.path.isfile(_route_override(project_dir)), out
        assert not os.path.isfile(_map_override(project_dir)), out


def test_bad_insertion_point_rejected_full_rollback():
    """A bad insertion point (--after not in the route) is REJECTED with NO
    partial config left behind: no adapter file, no map/route override."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["new", "--port", "ENRICH", "--after", "NOPE", "--before",
             "PERSIST"],
            project_dir)
        assert code != 0, out
        assert "REJECTED" in out, out
        adapter_path = os.path.join(
            project_dir, ".auto-maintainer", "adapters", "enrich.py")
        assert not os.path.exists(adapter_path), out
        assert not os.path.isfile(_route_override(project_dir)), out
        assert not os.path.isfile(_map_override(project_dir)), out
