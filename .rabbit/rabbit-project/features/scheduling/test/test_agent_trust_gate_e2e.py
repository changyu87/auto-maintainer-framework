#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's trust-gate on ACTING agent-states.

This cycle adds the trust-gate for acting agent-states to run_tick (DESIGN §2.3 /
§3.8.2 trust ladder). An agent-state that performs outward effects (its dispatch
entry declares a truthy `effect`, e.g. "implement") is DISPATCHED only when the
trust mode permits that effect (safety-governance's permits(effect, mode)). In
`dry-run` — where permits returns False for every effect — run_tick does NOT pause
or dispatch; it deterministically synthesizes one INERT `planned` handoff per
dispatch item, applies them to the writes slot, computes the route signal, and
CONTINUES the driver (no PAUSE, no checkpoint, no subagent). The model never
decides whether to act — that is enforced deterministically in run_tick.

It ALSO carries `isolation` + `description` from the dispatch entry into the PAUSED
dispatches (so the executor can pass them to Agent(subagent_type, description=...,
isolation=...)).

It CONSUMES safety-governance + agent-dispatch + adapter-wiring + the loop-core
features UNCHANGED; edits live ONLY in scheduling (run_tick.py).

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  1. An IMPLEMENT-style ACTING agent route, mode=dry-run -> run_tick does NOT pause
     at IMPLEMENT; it writes `handoffs` all status:"planned" (one per
     execution_plan item), NO dispatch requested, NO subagent output files, and
     the tick COMPLETES (signal idle). No checkpoint is written.
  2. The SAME route, mode=propose -> run_tick PAUSES at IMPLEMENT; the PAUSED
     dispatches carry isolation:"worktree" and a description; resume (write canned
     handoff outputs) -> DONE.
  3. A NON-acting agent-state (TRIAGE, no `effect`) is UNCHANGED: it ALWAYS pauses
     to dispatch regardless of mode (even dry-run).
  4. The pure-script DEFAULT route is UNCHANGED.

scheduling CONSUMES safety-governance / agent-dispatch / adapter-wiring / the
loop-core features UNCHANGED via sys.path; it does NOT edit or fork them.

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
import safety_governance as sg  # noqa: E402,F401
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


# --------------------------------------------------------------------------
# Agent-adapter fixtures.
#
# TRIAGE is a NON-acting agent (no `effect`): reads work_items -> writes
# work_orders, cardinality once. IMPLEMENT is an ACTING agent: it declares
# effect="implement", isolation="worktree", a per_item dispatch over
# execution_plan.ordered, and an output_example (a concrete planned handoff).
# PRIORITIZE stays a SCRIPT between them.
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

# The concrete planned-handoff example the IMPLEMENT acting agent embeds. In
# dry-run run_tick synthesizes one of these per execution_plan item without
# dispatching; in propose it is rendered into the dispatch prompt to mimic.
_PLANNED_HANDOFF_EXAMPLE = {
    "work_order_id": None,
    "status": "planned",
    "artifact": {"kind": "none", "ref": None},
    "discovered_work": [],
    "blocked_reason": None,
}

_IMPLEMENT_ACTING_AGENT = {
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
            "effect": "implement",
            "isolation": "worktree",
            "description": "implement a work order in an isolated worktree",
            "output_example": _PLANNED_HANDOFF_EXAMPLE,
        }
    ],
    "signal": {"rule": "blocked_if_any"},
}


# A route with NON-acting agent TRIAGE + SCRIPT PRIORITIZE + ACTING agent
# IMPLEMENT: GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->PERSIST->EXIT.
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
    """The DEFAULT_ADAPTER_MAP with TRIAGE (non-acting agent) + IMPLEMENT
    (acting agent) swapped in; PRIORITIZE stays the script factory address."""
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    amap["IMPLEMENT"] = dict(_IMPLEMENT_ACTING_AGENT)
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


def _write_governance(project_dir, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump(payload, f)


def _setup_agent_project(mode=None):
    """A project dir wired with the agent route + agent adapter-map override and
    the runtime paths under it. When `mode` is given a governance.json is written
    with that trust mode. Returns (project_dir, runtime_dir, state_path,
    journal_path)."""
    project_dir = tempfile.mkdtemp(prefix="sched-trustgate-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    if mode is not None:
        _write_governance(project_dir, {"mode": mode})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


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


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _resume_triage(project_dir, runtime_dir, state_path, journal_path):
    """Step TRIAGE (a non-acting agent) past its pause by writing the canned
    work_orders + resuming. Returns the SECOND structured return (the IMPLEMENT
    pause in propose, or whatever the acting-state branch produces)."""
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=_stub_source(), resume=True)


# ==========================================================================
# Behaviour 1 — dry-run: an ACTING agent-state (IMPLEMENT, effect=implement) is
# NOT dispatched; run_tick synthesizes inert planned handoffs and CONTINUES.
# ==========================================================================

def test_dry_run_acting_state_does_not_pause_and_synthesizes_planned():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    # Resume past the non-acting TRIAGE agent. In dry-run the ACTING IMPLEMENT
    # state must NOT pause — the resume drives straight through to DONE.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = _resume_triage(project_dir, runtime_dir, state_path,
                                journal_path)
    # A terminal (no pause at IMPLEMENT) -> the disposition signal STRING.
    assert result == "idle", result
    # handoffs were synthesized inert: one per execution_plan item (2), all
    # status "planned".
    handoffs = rt.persisted_handoffs(state_path)
    assert len(handoffs) == 2, handoffs
    for h in handoffs:
        assert h["status"] == "planned", h
        assert h["artifact"] == {"kind": "none", "ref": None}, h
    assert rt.persisted_handoffs_count(state_path) == 2
    # The trace shows the full stitched path including IMPLEMENT (it ran inert).
    line = buf.getvalue()
    for token in ["GUARD", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
                  "PERSIST", "EXIT"]:
        assert token in line, (token, line)


def test_dry_run_acting_state_requests_no_dispatch_and_writes_no_output_files():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    assert result == "idle", result
    # No checkpoint left (the tick completed, never paused at IMPLEMENT).
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc
    # No subagent output files were produced for IMPLEMENT (no dispatch). The
    # dispatch-out dir holds at most the TRIAGE file, never an IMPLEMENT-*.json.
    out_dir = os.path.join(runtime_dir, "dispatch-out")
    if os.path.isdir(out_dir):
        implement_files = [f for f in os.listdir(out_dir)
                           if f.startswith("IMPLEMENT-")]
        assert implement_files == [], implement_files


def test_dry_run_acting_state_planned_handoffs_carry_work_order_ids():
    """The inert planned handoffs are built per execution_plan item, so each
    carries its order's work_order_id (cardinality is per_item)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    handoffs = rt.persisted_handoffs(state_path)
    ids = sorted(h["work_order_id"] for h in handoffs)
    assert ids == ["wo-acme/widget#7", "wo-acme/widget#9"], ids


# ==========================================================================
# Behaviour 2 — propose: the SAME ACTING agent-state DOES pause to dispatch; the
# PAUSED dispatches carry isolation + description; resume reaches DONE.
# ==========================================================================

def test_propose_acting_state_pauses_to_dispatch():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    assert isinstance(result, dict), result
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result
    # per_item over execution_plan.ordered (2 orders) -> 2 dispatches.
    assert len(result["dispatches"]) == 2, result
    for d in result["dispatches"]:
        assert d["subagent_type"] == "implement-doer", d
        assert d["writes"] == "handoffs", d


def test_propose_paused_dispatches_carry_isolation_and_description():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    assert result["status"] == "paused", result
    for d in result["dispatches"]:
        # isolation carried verbatim from the dispatch entry.
        assert d["isolation"] == "worktree", d
        # description present and non-empty (the executor passes it to Agent).
        assert d.get("description"), d


def test_propose_acting_state_resume_reaches_done():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT"
    _write_outputs(paused, [_canned_handoff(d["item"])
                            for d in paused["dispatches"]])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), resume=True)
    assert signal == "idle", signal
    assert rt.persisted_handoffs_count(state_path) == 2


def test_gated_merge_acting_state_also_pauses_to_dispatch():
    """gated-merge permits implement too -> the acting state PAUSES (dispatch),
    same as propose (not gated to inert)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="gated-merge")
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path)
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result


# ==========================================================================
# Behaviour 3 — a NON-acting agent-state (TRIAGE, no `effect`) is UNCHANGED: it
# ALWAYS pauses to dispatch regardless of mode (even dry-run).
# ==========================================================================

def test_non_acting_agent_pauses_even_in_dry_run():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    # TRIAGE has NO effect -> the trust-gate does not apply; it pauses to
    # dispatch even in dry-run.
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result
    d = result["dispatches"][0]
    assert d["subagent_type"] == "triage-doer", d
    assert d["writes"] == "work_orders", d


def test_non_acting_agent_pause_has_no_isolation_when_absent():
    """A dispatch entry without isolation/description still pauses; the dispatch
    record omits isolation (or carries null) and supplies a default description."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    d = result["dispatches"][0]
    # No isolation declared -> absent or null (never a spurious value).
    assert d.get("isolation") in (None, ""), d
    # A sensible default description is always present.
    assert d.get("description"), d


# ==========================================================================
# Behaviour 4 — the pure-script DEFAULT route is UNCHANGED.
# ==========================================================================

def _plain_paths():
    root = tempfile.mkdtemp(prefix="sched-trustgate-plain-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def test_pure_script_default_route_unchanged_in_dry_run():
    project_dir = tempfile.mkdtemp(prefix="sched-trustgate-pure-")
    _write_governance(project_dir, {"mode": "dry-run"})
    runtime_dir, state_path, journal_path = _plain_paths()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source())
    assert signal == "idle", signal
    line = buf.getvalue()
    assert "[tick] path=GUARD->DRAIN->PULL->PERSIST->EXIT->DONE" in line, line
    assert "work_items=2" in line, line
    # No checkpoint on a pure-script route.
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc, doc
