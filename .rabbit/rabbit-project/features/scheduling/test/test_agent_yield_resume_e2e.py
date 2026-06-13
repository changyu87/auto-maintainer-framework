#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's agent yield/resume seam.

This cycle gives run_tick a YIELD/RESUME seam (DESIGN §2.8 executor protocol):
a route that contains agent-states pauses at each agent-state (emitting a
rendered dispatch request) and resumes when given the dispatch result. It
CONSUMES agent-dispatch + adapter-wiring + tick-orchestrator + work-intake +
prioritize + implement + safety-governance + durable-state UNCHANGED; edits live
ONLY in scheduling (run_tick.py).

The PAUSED/resume return contract (the settled shape):

  - A fresh run on a route with >=1 agent-state returns a PAUSED dict
        {"status": "paused", "state": <agent-state name>,
         "dispatches": [ {"subagent_type", "prompt" (rendered markdown),
                          "writes", "schema_ref", "signal_rule",
                          "cardinality", "item"? } ... ]}
    and persists a durable checkpoint under TICK_CHECKPOINT_KEY; the script does
    NOT call the Agent tool (that is the executor's job, a later slice).
  - run_tick(resume_dispatch=[<raw subagent output strings>]) loads the
    checkpoint, validates each output against the writes-slot schema, applies the
    collected slot value, and continues the driver to the next pause or terminal.
    On a validation failure it returns {"status": "invalid_output", "state",
    "reason"} and leaves the checkpoint intact (re-dispatchable) — no crash.
  - On reaching the terminal (DONE) it clears the checkpoint, persists the read
    products (#64), prints the stitched one-line trace, and returns the
    disposition signal (a string) exactly as a pure-script tick does.
  - Crash-safety: a fresh run_tick with no resume_dispatch that finds an existing
    checkpoint re-emits the SAME PAUSED dispatch request from the checkpoint.

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  A. Fresh run on an agent route -> PAUSED at TRIAGE; the dispatch carries a
     rendered prompt (markdown), writes="work_orders", signal_rule, cardinality;
     checkpoint persisted; work_items already produced/persisted.
  B. Resume with a canned valid work_orders output -> applies work_orders, runs
     PRIORITIZE (script), returns PAUSED at IMPLEMENT (per_item over
     execution_plan.ordered -> one dispatch per order, each with `item`).
  C. Resume with canned handoffs -> DONE; handoffs persisted; signal idle;
     checkpoint cleared; trace shows the full stitched path.
  D. Crash-safety: after the first PAUSE, a fresh run_tick (no resume_dispatch)
     re-emits the same TRIAGE dispatch from the checkpoint.
  E. Invalid agent output (wrong type) on resume -> status "invalid_output" with
     a reason, no crash, checkpoint intact.
  F. Pure-script DEFAULT route unchanged: still runs via to.run, same trace.

scheduling CONSUMES the loop-core / work-intake / prioritize / implement /
adapter-wiring / agent-dispatch features UNCHANGED via sys.path; it does NOT edit
or fork them.

Owner: changyu87
"""

import io
import contextlib
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
             "prioritize", "implement", "agent-dispatch", "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import agent_dispatch as ad  # noqa: E402,F401
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
    root = tempfile.mkdtemp(prefix="sched-agent-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# --------------------------------------------------------------------------
# Agent-adapter fixtures: TRIAGE and IMPLEMENT wired as AGENT entries. TRIAGE
# reads work_items -> writes work_orders (cardinality once). IMPLEMENT reads
# execution_plan -> writes handoffs (cardinality per_item over
# execution_plan.ordered: one dispatch per order). PRIORITIZE stays a SCRIPT.
# --------------------------------------------------------------------------

_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "triage-doer",
            "inputs": ["work_items"],
            "writes": "work_orders",
            "cardinality": "once",
            "task": "Triage the work_items into accepted work_orders.",
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}

_IMPLEMENT_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["execution_plan"], "writes": ["handoffs"],
                 "emits": ["OK", "BLOCKED"]},
    "dispatch": [
        {
            "subagent_type": "implement-doer",
            "inputs": ["execution_plan"],
            "writes": "handoffs",
            "cardinality": {"per_item": "execution_plan.ordered"},
            "task": "Implement one work_order.",
        }
    ],
    "signal": {"rule": "blocked_if_any"},
}


# A route with agent TRIAGE + agent IMPLEMENT and a SCRIPT PRIORITIZE between
# them: GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->PERSIST->EXIT.
_AGENT_ROUTE = {
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


def _agent_map():
    """The DEFAULT_ADAPTER_MAP with TRIAGE + IMPLEMENT swapped to agent entries;
    PRIORITIZE stays the script factory address."""
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    amap["IMPLEMENT"] = dict(_IMPLEMENT_AGENT)
    return amap


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route, f)


def _write_project_map(project_dir, amap):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)


def _setup_agent_project():
    """A project dir wired with the agent route + agent adapter-map override and
    the runtime paths under it. Returns (project_dir, runtime_dir, state_path,
    journal_path)."""
    project_dir = tempfile.mkdtemp(prefix="sched-agentproj-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# A canned VALID work_orders output (a JSON array) the executor would have
# produced from dispatching the TRIAGE agent. Two accepted orders.
_CANNED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
    {"schema_version": "1.0.0", "id": "wo-acme/widget#9",
     "work_item_id": "acme/widget#9", "title": "Add retry knob",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def _canned_handoff(work_order_id):
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": "planned", "artifact": {"kind": "none", "ref": None},
        "discovered_work": [], "blocked_reason": None,
    })


# ==========================================================================
# Behaviour A — fresh run on an agent route PAUSES at TRIAGE.
# ==========================================================================

def test_fresh_agent_route_pauses_at_triage():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    assert isinstance(result, dict), result
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result
    assert len(result["dispatches"]) == 1, result
    d = result["dispatches"][0]
    assert d["subagent_type"] == "triage-doer", d
    assert d["writes"] == "work_orders", d
    assert d["schema_ref"] == "work_orders", d
    assert d["signal_rule"] == "nonempty_else_empty", d
    assert d["cardinality"] == "once", d
    # The prompt is RENDERED markdown (not raw JSON) — agent-dispatch.render.
    assert d["prompt"].startswith("# Dispatch: TRIAGE"), d["prompt"][:60]
    assert "## Inputs" in d["prompt"], d["prompt"]


def test_fresh_agent_route_persists_checkpoint_and_work_items():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    doc = ds.DurableState(state_path).load()
    cp = doc.get(rt.TICK_CHECKPOINT_KEY)
    assert cp is not None, doc
    assert cp["next_state"] == "TRIAGE", cp
    assert cp["pending"]["state"] == "TRIAGE", cp
    assert cp["pending"]["writes"] == "work_orders", cp
    # work_items was already produced by PULL and snapshotted in the checkpoint.
    assert "work_items" in cp["slots"], cp["slots"].keys()
    assert len(cp["slots"]["work_items"]) == 2, cp["slots"]["work_items"]


def test_fresh_agent_route_does_not_call_agent_or_persist_read_products():
    """run_tick MUST NOT apply any read product on the PAUSE — only on resume.
    work_orders/execution_plan/handoffs are not yet persisted at the first
    pause."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    # No work_orders yet (the agent has not been dispatched/resumed).
    assert rt.persisted_work_orders_count(state_path) == 0
    assert rt.persisted_handoffs_count(state_path) == 0


# ==========================================================================
# Behaviour B — resume TRIAGE -> run PRIORITIZE (script) -> PAUSE at IMPLEMENT.
# ==========================================================================

def test_resume_triage_runs_prioritize_and_pauses_at_implement():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    # Resume with the canned valid work_orders output.
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(),
                         resume_dispatch=[_CANNED_WORK_ORDERS])
    assert isinstance(result, dict), result
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result
    # IMPLEMENT is per_item over execution_plan.ordered (2 orders) -> 2 dispatches.
    assert len(result["dispatches"]) == 2, result
    for d in result["dispatches"]:
        assert d["subagent_type"] == "implement-doer", d
        assert d["writes"] == "handoffs", d
        assert d["cardinality"] == {"per_item": "execution_plan.ordered"}, d
        # Each per-item dispatch carries its `item` (the order id).
        assert "item" in d, d
        assert d["item"] in ("wo-acme/widget#7", "wo-acme/widget#9"), d


def test_resume_triage_applies_work_orders_to_durable_state():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(),
                resume_dispatch=[_CANNED_WORK_ORDERS])
    # The applied work_orders is carried in the checkpoint slot snapshot, so the
    # IMPLEMENT pause can read execution_plan (derived from work_orders).
    doc = ds.DurableState(state_path).load()
    cp = doc[rt.TICK_CHECKPOINT_KEY]
    assert len(cp["slots"]["work_orders"]) == 2, cp["slots"]["work_orders"]
    # PRIORITIZE (a SCRIPT state) ran during the resume and produced the plan.
    assert len(cp["slots"]["execution_plan"]["ordered"]) == 2, cp["slots"]


# ==========================================================================
# Behaviour C — resume IMPLEMENT -> DONE; handoffs persisted; checkpoint cleared.
# ==========================================================================

def test_resume_implement_reaches_done_persists_handoffs_clears_checkpoint():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(),
                resume_dispatch=[_CANNED_WORK_ORDERS])
    # Resume IMPLEMENT with two canned handoffs (one per per_item dispatch).
    handoffs = [_canned_handoff("wo-acme/widget#7"),
                _canned_handoff("wo-acme/widget#9")]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), resume_dispatch=handoffs)
    # A terminal resume returns the disposition signal STRING (read-and-idle).
    assert signal == "idle", signal
    # handoffs persisted (#64), count == number of orders.
    assert rt.persisted_handoffs_count(state_path) == 2
    assert rt.persisted_work_orders_count(state_path) == 2
    assert rt.persisted_execution_plan_count(state_path) == 2
    # Checkpoint cleared on reaching the terminal.
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc
    # The stitched trace shows the full path across all segments.
    line = buf.getvalue()
    for token in ["GUARD", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
                  "PERSIST", "EXIT"]:
        assert token in line, (token, line)


# ==========================================================================
# Behaviour D — crash-safety: a fresh run_tick (no resume) re-emits the same
# TRIAGE dispatch from the persisted checkpoint.
# ==========================================================================

def test_crash_safety_reemits_same_dispatch_from_checkpoint():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    first = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
    # A fresh tick with NO resume_dispatch finds the existing checkpoint and
    # re-emits the SAME PAUSED request (idempotent — the checkpoint is the truth).
    again = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
    assert again["status"] == "paused", again
    assert again["state"] == "TRIAGE", again
    assert again["dispatches"][0]["prompt"] == first["dispatches"][0]["prompt"]
    assert again["dispatches"][0]["writes"] == "work_orders"


# ==========================================================================
# Behaviour E — invalid agent output (wrong type) on resume -> "invalid_output".
# ==========================================================================

def test_invalid_agent_output_returns_invalid_output_no_crash():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    # work_orders expects an array; resume with an OBJECT -> type mismatch.
    bad = json.dumps({"not": "an array"})
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), resume_dispatch=[bad])
    assert isinstance(result, dict), result
    assert result["status"] == "invalid_output", result
    assert result["state"] == "TRIAGE", result
    assert result.get("reason"), result
    # Checkpoint intact (re-dispatchable): still at TRIAGE, work_orders unapplied.
    doc = ds.DurableState(state_path).load()
    cp = doc.get(rt.TICK_CHECKPOINT_KEY)
    assert cp is not None and cp["pending"]["state"] == "TRIAGE", doc
    assert rt.persisted_work_orders_count(state_path) == 0


def test_invalid_agent_output_unparseable_json_returns_invalid_output():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(),
                         resume_dispatch=["this is not json {{{"])
    assert result["status"] == "invalid_output", result
    assert result.get("reason"), result


# ==========================================================================
# Behaviour F — pure-script DEFAULT route is UNCHANGED (still via to.run).
# ==========================================================================

def test_pure_script_default_route_returns_signal_string():
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source())
    # No agent-states -> the legacy path: returns the disposition signal STRING.
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 2
    # No checkpoint is ever written on a pure-script route.
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc, doc


def test_pure_script_default_route_trace_unchanged():
    runtime_dir, state_path, journal_path = _paths()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, source=_stub_source())
    line = buf.getvalue()
    assert "[tick] path=GUARD->DRAIN->PULL->PERSIST->EXIT->DONE" in line, line
    assert "work_items=2" in line, line
    assert "signal=idle" in line, line


def test_pure_script_default_route_return_run_result_still_works():
    """The return_run_result hook stays intact on the pure-script path."""
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source(),
                         return_run_result=True)
    assert result.final_state == "DONE", result
    assert result.path[0] == "GUARD" and result.path[-1] == "DONE", result.path
