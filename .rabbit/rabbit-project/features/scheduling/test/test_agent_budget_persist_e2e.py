#!/usr/bin/env python3
"""End-to-end conformance tests for the durable budget window on AGENT routes.

Regression for auto-maintainer-framework#123: the durable budget window was not
persisted on agent-route ticks. On an agent route the fresh `--step` computes the
rolled budget window but `_run_agent_tick` PAUSES and run_tick RETURNS EARLY
(the `return agent_outcome[0]` on PAUSE / invalid_output) BEFORE the
budget-persist block, so the window is never saved (durable `budget={}`). And on
`--resume` the is_resume branch read the persisted window (`{}` because the pause
never saved it) without carrying the evaluated window forward, so it persisted
`{}` again — `/status` then shows `win=` empty.

The fix (scheduling/run_tick.py only; safety-governance consumed UNCHANGED):

  1. On the PAUSE / invalid_output early-return path, persist the budget window
     durably (load-modify-save just BUDGET_KEY) so the fresh tick's rolled window
     survives the pause.
  2. On resume, after evaluate_budget, carry the evaluated window forward
     (new_budget_state = budget["budget_state"]) so even a `{}` persisted value
     yields a real {window_key, spent_tokens}.

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  1. Agent fresh `--step` (pauses at TRIAGE) -> durable budget now has a
     window_key == window_key(now) (previously {}).
  2. Agent step -> write TRIAGE output -> resume -> ... -> DONE -> durable budget
     = {window_key, spent_tokens}; governance_status / status shows win=<date>
     (not empty).
  3. The budget window survives across two agent ticks in the same runtime dir;
     a `now` on a later local day rolls the window over (window_key advances,
     spent resets) on the next fresh tick.
  4. The PAUSE-path budget persist preserves all OTHER durable keys (checkpoint,
     read products) — only BUDGET_KEY is added.
  5. Pure-script route budget persistence is UNCHANGED (regression guard).

scheduling CONSUMES safety-governance + the loop-core / work-intake /
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
from datetime import datetime, timedelta, timezone

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
import safety_governance as sg  # noqa: E402
import run_tick as rt  # noqa: E402
import status as st  # noqa: E402


# A fixed local offset pins window_key to the injected now's LOCAL date so the
# suite never depends on the host tz. Two days, before/after the local midnight.
_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)
_DAY2 = datetime(2026, 5, 2, 9, 0, 0, tzinfo=_TZ)


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
# Agent-adapter fixtures (mirror test_agent_yield_resume_e2e.py): TRIAGE +
# IMPLEMENT wired as AGENT entries; PRIORITIZE stays a SCRIPT.
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
    project_dir = tempfile.mkdtemp(prefix="sched-agentbudget-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
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


def _drive_agent_tick_to_done(project_dir, runtime_dir, state_path,
                              journal_path, now=None):
    """Drive a full agent tick (step -> write+resume TRIAGE -> write+resume
    IMPLEMENT) to the DONE terminal. Returns the terminal disposition signal."""
    r = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source(), now=now)
    assert r["status"] == "paused" and r["state"] == "TRIAGE", r
    _write_outputs(r, [_CANNED_WORK_ORDERS])
    r = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source(), now=now, resume=True)
    assert r["status"] == "paused" and r["state"] == "IMPLEMENT", r
    _write_outputs(r, [_canned_handoff(d["item"]) for d in r["dispatches"]])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=now, resume=True)
    return signal


# ==========================================================================
# Behaviour 1 — agent fresh --step (pauses at TRIAGE) persists the budget window.
# ==========================================================================

def test_agent_fresh_pause_persists_budget_window():
    """THE #123 repro: a fresh agent --step PAUSES at TRIAGE and RETURNS EARLY,
    but the rolled budget window MUST still be persisted (previously {})."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result
    # The durable budget window is NOT empty: it carries the fresh tick's rolled
    # window keyed off the injected now.
    bs = rt.persisted_budget_state(state_path)
    assert bs != {}, "agent-route pause must persist the budget window"
    assert bs["window_key"] == sg.window_key(_DAY1), bs
    assert bs["window_key"] == "2026-05-01", bs


def test_agent_fresh_pause_budget_persist_preserves_checkpoint():
    """Persisting the budget window on the PAUSE path must NOT disturb the
    durable checkpoint or read products — only BUDGET_KEY is added."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1)
    doc = ds.DurableState(state_path).load()
    # The checkpoint is intact (crash-safety source of truth preserved).
    cp = doc.get(rt.TICK_CHECKPOINT_KEY)
    assert cp is not None and cp["pending"]["state"] == "TRIAGE", doc
    # The budget window was added without clobbering the checkpoint.
    assert doc.get(rt.BUDGET_KEY, {}).get("window_key") == "2026-05-01", doc


def test_agent_invalid_output_resume_persists_budget_window():
    """The invalid_output early-return path (a missing/invalid output file) also
    persists the budget window — the same early `return agent_outcome[0]`."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1)
    # Resume with NO file written -> invalid_output (early return). The budget
    # window (carried from the persisted fresh window) must remain non-empty.
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1, resume=True)
    assert result["status"] == "invalid_output", result
    bs = rt.persisted_budget_state(state_path)
    assert bs.get("window_key") == "2026-05-01", bs


# ==========================================================================
# Behaviour 2 — full agent tick to DONE persists {window_key, spent_tokens};
# status surfaces win=<date> (not empty).
# ==========================================================================

def test_agent_tick_to_done_persists_budget_and_status_shows_win():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    signal = _drive_agent_tick_to_done(project_dir, runtime_dir, state_path,
                                       journal_path, now=_DAY1)
    assert signal == "idle", signal
    bs = rt.persisted_budget_state(state_path)
    assert bs["window_key"] == "2026-05-01", bs
    assert bs["spent_tokens"] == 0, bs
    # governance_status surfaces the durable window — win is NOT empty.
    line = rt.governance_status(project_dir, state_path)
    assert "win=2026-05-01" in line, line
    assert "win= " not in line and not line.rstrip().endswith("win="), line


def test_agent_tick_status_line_shows_win_not_empty():
    """status.status_line() (the /status surface) shows win=<date>, not empty,
    after a full agent tick — the user-visible symptom of #123 is fixed."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        _drive_agent_tick_to_done(project_dir, runtime_dir, state_path,
                                  journal_path, now=_DAY1)
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "win=2026-05-01" in line, line


# ==========================================================================
# Behaviour 3 — the budget window survives across two agent ticks; a later
# local day rolls the window over on the next fresh tick.
# ==========================================================================

def test_budget_window_survives_and_rolls_across_two_agent_ticks():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # tick-1: full agent tick on DAY1.
    _drive_agent_tick_to_done(project_dir, runtime_dir, state_path,
                              journal_path, now=_DAY1)
    bs1 = rt.persisted_budget_state(state_path)
    assert bs1["window_key"] == "2026-05-01", bs1
    # tick-2: a FRESH agent --step on DAY2 (a LATER local day). The fresh-start
    # gate rolls the window over: window_key advances. Assert at the PAUSE (the
    # rolled window is persisted on the early-return PAUSE path).
    result2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), now=_DAY2)
    assert result2["status"] == "paused" and result2["state"] == "TRIAGE", result2
    bs2 = rt.persisted_budget_state(state_path)
    assert bs2["window_key"] == "2026-05-02", bs2
    assert bs2["spent_tokens"] == 0, bs2


def test_resume_does_not_reroll_window_carries_it_forward():
    """The budget is evaluated at FRESH start only and REUSED on resume (spec):
    a resume on the SAME day must keep the fresh window's date, never re-roll."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    # Resume (even if a different wall-clock now were used, resume reuses the
    # persisted window). The carried-forward window keeps DAY1's key.
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True)
    bs = rt.persisted_budget_state(state_path)
    assert bs["window_key"] == "2026-05-01", bs


# ==========================================================================
# Behaviour 4 — pure-script route budget persistence is UNCHANGED (regression
# guard). The fix must touch ONLY the agent path.
# ==========================================================================

def _pure_paths():
    root = tempfile.mkdtemp(prefix="sched-purebudget-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def test_pure_script_route_budget_persistence_unchanged():
    runtime_dir, state_path, journal_path = _pure_paths()
    project_dir = tempfile.mkdtemp(prefix="sched-pureproj-")
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, project_dir=project_dir,
                         source=_stub_source(), now=_DAY1, tick_spend=777)
    assert signal == "idle", signal
    bs = rt.persisted_budget_state(state_path)
    assert bs["window_key"] == "2026-05-01", bs
    assert bs["spent_tokens"] == 777, bs
