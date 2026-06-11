#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling slice 3: route-as-data.

The route is now DATA, loaded + validated + resolved by adapter-wiring
(DESIGN §3.4.3) instead of hardcoded in run_tick. This module exercises the
slice-3 behaviours from docs/spec.md "The real loop (slice 3: route-as-data)":

  1. DEFAULT_ROUTE / DEFAULT_ADAPTER_MAP — the shipped read-and-idle spine
     GUARD->DRAIN->PULL->PERSIST->EXIT is the default route; the default
     adapter-map maps EVERY known port (incl. TRIAGE) to its built-in factory,
     even though the default route uses only a subset.
  2. Built-in adapter factories — each make_<port>(runtime) -> (manifest, run)
     wraps the EXISTING sibling adapter unchanged (the factory convention
     adapter-wiring resolves by "module:factory").
  3. HEADLINE: a project-local route.json that inserts TRIAGE
     (GUARD->DRAIN->PULL->TRIAGE->PERSIST->EXIT) wires TRIAGE from the default
     adapter-map at LOAD with NO code change, and run_tick then persists
     work_orders — the ports-and-adapters promise made real at runtime.
  4. Bad override rejected at LOAD — an invalid project-local route.json (e.g.
     TRIAGE before PULL, so TRIAGE reads work_items that is not yet written) is
     rejected by build_loop's validate_wiring; the tick does NOT silently run a
     broken route.
  5. The default-route behaviour is BYTE-for-byte the same as before: the tick
     still reproduces GUARD->DRAIN->PULL->PERSIST->EXIT, persists work_items,
     and idles. (Regression guard for the route-as-data refactor.)

scheduling CONSUMES adapter-wiring (build_loop) + the loop-core / work-intake
features UNCHANGED via sys.path; it does NOT edit or fork them.

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

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402,F401
import adapter_wiring as aw  # noqa: E402
import work_intake as wi  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import run_tick as rt  # noqa: E402


GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}, {"name": "p1"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  },
  {
    "number": 9,
    "title": "Add retry knob",
    "body": "",
    "url": "https://github.com/acme/widget/issues/9",
    "state": "OPEN",
    "labels": [],
    "author": {"login": "hubber"},
    "createdAt": "2026-05-03T08:00:00Z",
    "updatedAt": "2026-05-03T08:00:00Z"
  }
]"""


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="sched-routedata-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# --------------------------------------------------------------------------
# Behaviour 1 — DEFAULT_ROUTE / DEFAULT_ADAPTER_MAP shape.
# --------------------------------------------------------------------------

def test_default_route_is_the_read_and_idle_spine():
    route = rt.DEFAULT_ROUTE
    # The default route's runnable spine is GUARD->DRAIN->PULL->PERSIST->EXIT.
    for s in ("GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"):
        assert s in route["states"], (s, route["states"])
    # TRIAGE is NOT in the default route (only in the adapter-map).
    assert "TRIAGE" not in route["states"], route["states"]
    # The default route is a valid route.json shape.
    assert fc.validate_route(route).passed, route


def test_default_route_validates_through_adapter_wiring_load():
    """The DEFAULT_ROUTE passes adapter-wiring's load_route shape validation."""
    with tempfile.TemporaryDirectory() as proj:
        loaded = aw.load_route(rt.DEFAULT_ROUTE, proj)
        assert loaded == rt.DEFAULT_ROUTE


def test_default_adapter_map_maps_every_known_port_including_triage():
    amap = rt.DEFAULT_ADAPTER_MAP
    # Every routed port + the terminals are addressable.
    for port in ("GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"):
        assert port in amap, (port, amap)
        assert amap[port].count(":") == 1, amap[port]
    # TRIAGE is in the map even though the default route omits it — so a pure
    # route.json edit can wire it with NO code change.
    assert "TRIAGE" in amap, amap
    assert amap["TRIAGE"].split(":")[1] == "make_triage", amap["TRIAGE"]


def test_default_adapter_map_addresses_resolve_to_run_tick_factories():
    """Each known port maps to 'run_tick:make_<port>' (the factory convention)."""
    for port in ("GUARD", "DRAIN", "PULL", "PERSIST", "EXIT", "TRIAGE"):
        module, factory = rt.DEFAULT_ADAPTER_MAP[port].split(":")
        assert module == "run_tick", rt.DEFAULT_ADAPTER_MAP[port]
        assert hasattr(rt, factory), (port, factory)


# --------------------------------------------------------------------------
# Behaviour 2 — built-in adapter factories wrap the EXISTING sibling adapters.
# --------------------------------------------------------------------------

def _runtime(project_dir, runtime_dir, source=None):
    return {
        "project_dir": project_dir,
        "runtime_dir": runtime_dir,
        "source": source,
        "now": None,
    }


def test_factory_make_pull_wraps_work_intake_pull():
    rti = _runtime("/tmp/x", "/tmp/x/runtime", source=_stub_source())
    manifest, run = rt.make_pull(rti)
    # The wrapped manifest is work-intake's PULL manifest (unchanged).
    assert manifest is wi.PULL_MANIFEST
    # Running it through a TickContext writes the work_items slot.
    ctx = fc.TickContext()
    ctx.register_slot("work_items", {"type": "array"}, version="1.0.0")
    result = run(ctx)
    assert result.signal in ("OK", "EMPTY"), result.signal
    assert len(result.writes["work_items"]) == 2


def test_factory_make_triage_wraps_work_intake_triage():
    rti = _runtime("/tmp/x", "/tmp/x/runtime")
    manifest, run = rt.make_triage(rti)
    assert manifest is wi.TRIAGE_MANIFEST


def test_factory_make_guard_and_exit_bind_runtime_dir():
    runtime_dir, _state, _journal = _paths()
    rti = _runtime("/tmp/x", runtime_dir)
    g_manifest, _g_run = rt.make_guard(rti)
    e_manifest, _e_run = rt.make_exit(rti)
    # GUARD/EXIT manifests come straight from lifecycle-dispositions anchors.
    assert "OK" in g_manifest.emits
    assert "idle" in e_manifest.emits


def test_factory_make_drain_and_persist_are_durable_state_anchors():
    import durable_state as ds
    rti = _runtime("/tmp/x", "/tmp/x/runtime")
    d_manifest, d_run = rt.make_drain(rti)
    p_manifest, p_run = rt.make_persist(rti)
    assert d_manifest is ds.DRAIN_MANIFEST
    assert p_manifest is ds.PERSIST_MANIFEST


# --------------------------------------------------------------------------
# Behaviour 5 — default-route regression: byte-for-byte the old behaviour.
# --------------------------------------------------------------------------

def test_default_route_reproduces_pull_persist_idle():
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source(),
                         return_run_result=True)
    assert result.path[:5] == ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"], \
        result.path
    assert result.final_state == "DONE", result.path
    assert rt.persisted_work_items_count(state_path) == 2
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE


def test_default_route_does_not_run_triage_no_work_orders():
    """With the default route (no TRIAGE) the tick produces NO work_orders."""
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    assert rt.persisted_work_orders(state_path) == []


# --------------------------------------------------------------------------
# Behaviour 3 — HEADLINE: project-local route.json inserting TRIAGE wires it
# from the default adapter-map with NO code change; run_tick persists
# work_orders.
# --------------------------------------------------------------------------

_TRIAGE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
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
    ],
    "terminal": ["DONE", "HALTED"],
}


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route, f)


def test_config_override_inserts_triage_and_persists_work_orders():
    """HEADLINE: a project-local route.json inserting TRIAGE makes run_tick run
    GUARD->DRAIN->PULL->TRIAGE->PERSIST->EXIT and persist work_orders, with NO
    code change — TRIAGE resolves from the default adapter-map."""
    project_dir = tempfile.mkdtemp(prefix="sched-triageproj-")
    _write_project_route(project_dir, _TRIAGE_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), return_run_result=True)
    # The active route now runs TRIAGE between PULL and PERSIST.
    assert "TRIAGE" in result.path, result.path
    assert result.path[:6] == ["GUARD", "DRAIN", "PULL", "TRIAGE", "PERSIST",
                               "EXIT"], result.path
    # work_orders were produced by TRIAGE and persisted (both fixture issues are
    # well-formed + open + fresh, so both are accepted).
    orders = rt.persisted_work_orders(state_path)
    assert len(orders) == 2, orders
    assert all(o["decision"] == "accepted" for o in orders), orders
    # work_items are still persisted too.
    assert rt.persisted_work_items_count(state_path) == 2


def test_config_override_uses_project_dir_resolution_by_default():
    """When project_dir is resolved from CLAUDE_PROJECT_DIR (no explicit
    project_dir), the project-local route.json under it still drives the loop."""
    project_dir = tempfile.mkdtemp(prefix="sched-triageenv-")
    _write_project_route(project_dir, _TRIAGE_ROUTE)
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        result = rt.run_tick(source=_stub_source(), return_run_result=True)
        _runtime_dir, state_path, _journal = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "TRIAGE" in result.path, result.path
    assert len(rt.persisted_work_orders(state_path)) == 2


# --------------------------------------------------------------------------
# Behaviour 4 — bad override rejected at LOAD (build_loop validate_wiring).
# --------------------------------------------------------------------------

_BAD_TRIAGE_BEFORE_PULL = {
    "schema_version": "1.0.0",
    # TRIAGE reads work_items but here it runs BEFORE PULL writes it.
    "states": ["GUARD", "DRAIN", "TRIAGE", "PULL", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PULL"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "PERSIST"},
        {"state": "PULL", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def test_bad_override_triage_before_pull_rejected_at_load():
    """A route.json that puts TRIAGE before PULL is rejected by build_loop's
    data-readiness check at LOAD — the tick must NOT silently run it. A source
    that would explode if PULL ran proves no tick body executed."""
    project_dir = tempfile.mkdtemp(prefix="sched-badroute-")
    _write_project_route(project_dir, _BAD_TRIAGE_BEFORE_PULL)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    def _exploding_source(repo=None):
        raise AssertionError("PULL must not run for a rejected route")

    raised = False
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_exploding_source)
    except aw.WiringError as exc:
        raised = True
        # The failure is locatable (names the offending read/slot).
        assert "work_items" in str(exc) or "TRIAGE" in str(exc), str(exc)
    assert raised, "an invalid override route must raise WiringError at load"
    # No state was persisted: the broken route never ran.
    assert rt.persisted_work_items_count(state_path) == 0


_BAD_UNKNOWN_PORT = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "NOPE", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "NOPE"},
        {"state": "PULL", "signal": "EMPTY", "next": "NOPE"},
        {"state": "NOPE", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def test_bad_override_unknown_port_rejected_at_load():
    """A route.json naming an unknown port (no adapter-map entry) is rejected at
    LOAD, naming the offending port."""
    project_dir = tempfile.mkdtemp(prefix="sched-badport-")
    _write_project_route(project_dir, _BAD_UNKNOWN_PORT)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    raised = False
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    except aw.WiringError as exc:
        raised = True
        assert "NOPE" in str(exc), str(exc)
    assert raised, "an unknown port must raise WiringError at load"
    assert rt.persisted_work_items_count(state_path) == 0


# --------------------------------------------------------------------------
# Multi-tick + STOPPED latch still hold under route-as-data.
# --------------------------------------------------------------------------

def test_default_route_multi_tick_repulls_idempotently():
    runtime_dir, state_path, journal_path = _paths()
    source = _stub_source()
    for _ in range(3):
        sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                          journal_path=journal_path, source=source)
        assert sig == "idle", sig
        assert rt.persisted_work_items_count(state_path) == 2


def test_stopped_latch_still_halts_under_route_as_data():
    runtime_dir, state_path, journal_path = _paths()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)

    def _exploding_source(repo=None):
        raise AssertionError("PULL must not run while STOPPED is latched")

    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_exploding_source,
                         return_run_result=True)
    assert "PULL" not in result.path, result.path
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED
