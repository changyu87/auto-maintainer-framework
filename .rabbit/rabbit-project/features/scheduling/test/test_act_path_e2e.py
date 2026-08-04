#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling: wiring PRIORITIZE + IMPLEMENT.

This cycle WIRES the two new deterministic adapters into the route-as-data loop
and surfaces their per-tick read products. It consumes prioritize + implement
UNCHANGED (DESIGN §1.1, §2.6); edits live ONLY in scheduling:

  - PRIORITIZE (prioritize.py): reads work_orders, writes execution_plan
    (an object slot {ordered, status}), emits OK | EMPTY.
  - IMPLEMENT (implement.py, dry-run): reads execution_plan, writes handoffs
    (an array slot), emits OK | BLOCKED. INERT — no VCS, no filesystem effect.

Behaviours exercised here (extending docs/spec.md route-as-data + #64):

  1. DEFAULT_ADAPTER_MAP maps PRIORITIZE + IMPLEMENT to their built-in factories
     (resolvable even though the DEFAULT_ROUTE omits them — the ports-and-adapters
     promise), and BLOCKED is in the signal vocabulary.
  2. HEADLINE act-path: a project-local route.json wiring
     GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->PERSIST->EXIT validates +
     runs with NO code change; after the tick the persisted execution_plan count
     and handoffs count both equal the number of accepted work_orders, and the
     trace + status both show `execution_plan=N handoffs=N`.
  3. Default route (no PRIORITIZE/IMPLEMENT) -> execution_plan=0 handoffs=0 in
     BOTH the trace and status (per-tick ephemeral, not stale).
  4. #64 symmetry: an act-path tick (handoffs=N) then a default tick in the SAME
     runtime dir -> handoffs back to 0 (no carry-forward); cross-tick durable
     facts survive.
  5. INERTness end-to-end: after the act-path tick, the ONLY files touched under
     the runtime dir are the durable-state + journal files (the dry-run IMPLEMENT
     writes nothing outward).
  6. The new helpers persisted_execution_plan/_count + persisted_handoffs/_count.

scheduling CONSUMES prioritize + implement + the loop-core / work-intake /
adapter-wiring features UNCHANGED via sys.path; it does NOT edit or fork them.

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
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402,F401
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402,F401
import prioritize as pr  # noqa: E402
import implement as im  # noqa: E402
import run_tick as rt  # noqa: E402
import status as st  # noqa: E402


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

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="sched-actpath-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# The full act-path override route: TRIAGE -> PRIORITIZE -> IMPLEMENT between
# PULL and PERSIST. Every stage's predecessor writes the slot it reads, so the
# data-readiness validator is satisfied (work_items -> work_orders ->
# execution_plan -> handoffs).
_ACT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PRIORITIZE"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PRIORITIZE"},
        {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
        {"state": "PRIORITIZE", "signal": "EMPTY", "next": "IMPLEMENT"},
        {"state": "IMPLEMENT", "signal": "OK", "next": "PERSIST"},
        {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "PERSIST"},
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


# --------------------------------------------------------------------------
# Behaviour 1 — DEFAULT_ADAPTER_MAP maps PRIORITIZE + IMPLEMENT; BLOCKED in vocab.
# --------------------------------------------------------------------------

def test_default_adapter_map_includes_prioritize_and_implement():
    amap = rt.DEFAULT_ADAPTER_MAP
    assert "PRIORITIZE" in amap, amap
    assert "IMPLEMENT" in amap, amap
    assert amap["PRIORITIZE"].split(":")[1] == "make_prioritize", amap["PRIORITIZE"]
    assert amap["IMPLEMENT"].split(":")[1] == "make_implement", amap["IMPLEMENT"]


def test_default_route_omits_prioritize_and_implement():
    """The default route is still the read-and-idle spine — PRIORITIZE/IMPLEMENT
    are in the map (wireable by override) but NOT in the default route."""
    route = rt.DEFAULT_ROUTE
    assert "PRIORITIZE" not in route["states"], route["states"]
    assert "IMPLEMENT" not in route["states"], route["states"]


def test_prioritize_and_implement_factories_wrap_sibling_adapters():
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None}
    p_manifest, p_run = rt.make_prioritize(rti)
    i_manifest, i_run = rt.make_implement(rti)
    assert p_manifest is pr.PRIORITIZE_MANIFEST
    assert p_run is pr.run
    assert i_manifest is im.IMPLEMENT_MANIFEST
    assert i_run is im.run


def test_blocked_is_in_signal_vocabulary():
    """IMPLEMENT emits BLOCKED, so the closed vocab must accept it."""
    assert rt._VOCAB.is_member("BLOCKED"), rt._VOCAB.members()


# --------------------------------------------------------------------------
# Behaviour 6 — the new persisted-helper surface (default: empty).
# --------------------------------------------------------------------------

def test_persisted_helpers_default_empty_without_act_route():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    assert rt.persisted_execution_plan(state_path) == {}
    assert rt.persisted_execution_plan_count(state_path) == 0
    assert rt.persisted_handoffs(state_path) == []
    assert rt.persisted_handoffs_count(state_path) == 0


# --------------------------------------------------------------------------
# Behaviour 2 — HEADLINE act-path: override route wires PRIORITIZE + IMPLEMENT,
# validates + runs; execution_plan + handoffs counts == accepted work_orders.
# --------------------------------------------------------------------------

def test_act_path_override_runs_and_persists_plan_and_handoffs():
    project_dir = tempfile.mkdtemp(prefix="sched-actproj-")
    _write_project_route(project_dir, _ACT_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), return_run_result=True)
    # The active route now runs TRIAGE -> PRIORITIZE -> IMPLEMENT.
    assert result.path[:8] == ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                               "IMPLEMENT", "PERSIST", "EXIT"], result.path
    assert result.final_state == "DONE", result.path

    n_orders = rt.persisted_work_orders_count(state_path)
    assert n_orders == 2, n_orders
    # Both downstream read products equal the accepted-order count.
    assert rt.persisted_execution_plan_count(state_path) == n_orders
    assert rt.persisted_handoffs_count(state_path) == n_orders
    # The plan is the object slot {ordered, status}; handoffs is the array slot.
    plan = rt.persisted_execution_plan(state_path)
    assert len(plan["ordered"]) == n_orders, plan
    handoffs = rt.persisted_handoffs(state_path)
    assert all(h["status"] == "planned" for h in handoffs), handoffs


def test_act_path_tick_trace_shows_execution_plan_and_handoffs(capsys=None):
    """The tick trace prints execution_plan=N handoffs=N (after work_orders)."""
    import io
    import contextlib

    project_dir = tempfile.mkdtemp(prefix="sched-acttrace-")
    _write_project_route(project_dir, _ACT_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    line = buf.getvalue()
    assert "execution_plan=2" in line, line
    assert "handoffs=2" in line, line
    # Order: work_orders before execution_plan before handoffs.
    assert (line.index("work_orders=") < line.index("execution_plan=")
            < line.index("handoffs=")), line


def test_act_path_status_shows_execution_plan_and_handoffs():
    """status.py reports execution_plan=N handoffs=N after an act-path tick."""
    project_dir = tempfile.mkdtemp(prefix="sched-actstatus-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        _write_project_route(project_dir, _ACT_ROUTE)
        rt.run_tick(source=_stub_source())
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "execution_plan=2" in line, line
    assert "handoffs=2" in line, line
    # Field order: work_orders < execution_plan < handoffs.
    assert (line.index("work_orders=") < line.index("execution_plan=")
            < line.index("handoffs=")), line


# --------------------------------------------------------------------------
# Behaviour 3 — default route -> execution_plan=0 handoffs=0 in trace AND status.
# --------------------------------------------------------------------------

def test_default_route_shows_zero_plan_and_handoffs_in_trace():
    import io
    import contextlib

    runtime_dir, state_path, journal_path = _paths()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, source=_stub_source())
    line = buf.getvalue()
    assert "execution_plan=0" in line, line
    assert "handoffs=0" in line, line


def test_default_route_shows_zero_plan_and_handoffs_in_status():
    project_dir = tempfile.mkdtemp(prefix="sched-defstatus-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())  # default route
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "execution_plan=0" in line, line
    assert "handoffs=0" in line, line


def test_status_field_order_includes_plan_and_handoffs():
    """status field order: disposition, work_items, work_orders, execution_plan,
    handoffs, route, runtime_dir — all unconditional."""
    project_dir = tempfile.mkdtemp(prefix="sched-orderactstatus-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    for field in ("disposition=", "work_items=", "work_orders=",
                  "execution_plan=", "handoffs=", "route=", "runtime_dir="):
        assert field in line, (field, line)
    assert (line.index("work_orders=") < line.index("execution_plan=")
            < line.index("handoffs=") < line.index("route=")), line


# --------------------------------------------------------------------------
# Behaviour 4 — #64 symmetry: act-path tick then default tick -> handoffs back
# to 0 (no carry-forward); cross-tick durable facts survive.
# --------------------------------------------------------------------------

def test_act_then_default_tick_does_not_carry_stale_handoffs():
    project_dir = tempfile.mkdtemp(prefix="sched-64act-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    # Tick 1: act-path route -> execution_plan + handoffs persisted (count 2).
    _write_project_route(project_dir, _ACT_ROUTE)
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    assert rt.persisted_handoffs_count(state_path) == 2
    assert rt.persisted_execution_plan_count(state_path) == 2

    # Tick 2: remove override -> default route (no PRIORITIZE/IMPLEMENT) in the
    # SAME runtime dir. Both read products MUST reset to 0 — not the stale 2.
    os.remove(os.path.join(runtime_dir, "route.json"))
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    assert rt.persisted_handoffs_count(state_path) == 0, \
        "default-route tick must NOT carry forward the act tick's handoffs"
    assert rt.persisted_execution_plan_count(state_path) == 0, \
        "default-route tick must NOT carry forward the act tick's plan"
    # work_items is still fresh: PULL re-runs each tick.
    assert rt.persisted_work_items_count(state_path) == 2


def test_act_path_reset_preserves_cross_tick_durable_facts():
    project_dir = tempfile.mkdtemp(prefix="sched-64actfacts-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    os.makedirs(runtime_dir, exist_ok=True)
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 5})

    _write_project_route(project_dir, _ACT_ROUTE)
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    os.remove(os.path.join(runtime_dir, "route.json"))
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())

    doc = ds.DurableState(state_path).load()
    assert doc["counter"] == 5, doc
    assert doc["schema_version"] == ds.SCHEMA_VERSION, doc
    assert doc.get("handoffs", []) == [], doc
    assert doc.get("execution_plan", {}) == {}, doc


# --------------------------------------------------------------------------
# Behaviour 5 — INERTness end-to-end: the act-path tick mutates ONLY the
# durable-state + journal files under the runtime dir (the dry-run IMPLEMENT
# writes nothing outward).
# --------------------------------------------------------------------------

def test_act_path_tick_is_inert_no_outward_mutation():
    project_dir = tempfile.mkdtemp(prefix="sched-inert-")
    _write_project_route(project_dir, _ACT_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())

    # The only files anywhere under project_dir are the route.json override the
    # test wrote plus the durable-state / journal / disposition+lock markers the
    # loop owns. The dry-run IMPLEMENT produced NO outward artifact (no PR, no
    # branch, no commit, no extra file).
    seen = set()
    for root, _dirs, files in os.walk(project_dir):
        for f in files:
            seen.add(os.path.relpath(os.path.join(root, f), project_dir))
    cfg = ".auto-maintainer"
    allowed_prefixes = (cfg + os.sep,)
    for rel in seen:
        assert rel.startswith(allowed_prefixes), \
            ("act-path tick wrote outside the runtime dir", rel, seen)
    # The durable-state + journal files exist (the loop's own persistence).
    assert os.path.isfile(state_path), seen
    # route.json is the test fixture, not a loop artifact.
    assert os.path.join(cfg, "route.json") in seen, seen


# --------------------------------------------------------------------------
# An EMPTY pull through the act path: PRIORITIZE/IMPLEMENT still run and produce
# empty (but PRESENT) read products; execution_plan ordered=[] and handoffs=[].
# --------------------------------------------------------------------------

def test_act_path_empty_pull_yields_empty_plan_and_handoffs():
    project_dir = tempfile.mkdtemp(prefix="sched-actempty-")
    _write_project_route(project_dir, _ACT_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source("[]"))
    assert rt.persisted_execution_plan_count(state_path) == 0
    assert rt.persisted_handoffs_count(state_path) == 0
    # The plan object is still present (ordered=[]), not absent.
    plan = rt.persisted_execution_plan(state_path)
    assert plan.get("ordered") == [], plan
