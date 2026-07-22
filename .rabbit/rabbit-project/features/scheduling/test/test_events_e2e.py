#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's structured event log.

This cycle makes run_tick emit a structured EVENT LOG (observability §3.9.1) to
${runtime_dir}/events.jsonl each tick. It CONSUMES the observability lib
UNCHANGED (observability.EventLog + EVENT_KINDS); edits live ONLY in scheduling
(run_tick.py).

Event emission is ADDITIVE: it changes NO existing behaviour — the trace line,
signals, disposition, slot persistence, #64 ephemerality, the budget window, and
every existing scheduling test stay green. The event `ts` comes ONLY from the
injected `now` (deterministic; never an implicit wall clock); seq is monotonic
(observability assigns it via the file's line count).

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  A. After a pure-script DEFAULT tick, events.jsonl contains, in order:
     tick_start, state_run/signal for GUARD,DRAIN,PULL,PERSIST,EXIT, a
     disposition, and tick_end (with the four read-product counts in detail);
     seq monotonic; ts == injected now.
  B. An AGENT route (TRIAGE as agent): a fresh --step run emits tick_start +
     pause + dispatch (subagent_type in detail); the resume emits resume then
     continues across PRIORITIZE/IMPLEMENT to tick_end. The whole step->resume
     sequence appends to ONE events.jsonl with monotonic seq.
  C. Every emitted kind is a member of observability.EVENT_KINDS (no kind is
     ever emitted outside the closed vocabulary).
  D. ts is the injected now on a default tick; an empty pull still emits the
     full sequence.

scheduling CONSUMES observability + the loop-core / work-intake / prioritize /
implement / adapter-wiring / agent-dispatch / safety-governance / durable-state
features UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import work_intake as wi  # noqa: E402
import observability as ob  # noqa: E402
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

# A FIXED tz-aware clock so every event ts is deterministic.
_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="sched-events-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _events(runtime_dir):
    """Read events.jsonl back from the runtime dir via the observability lib."""
    return ob.EventLog(os.path.join(runtime_dir, "events.jsonl")).read()


def _kinds(events):
    return [e["kind"] for e in events]


# ==========================================================================
# Behaviour A — pure-script DEFAULT tick: the full ordered event sequence.
# ==========================================================================

def test_default_tick_emits_full_event_sequence_in_order():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    kinds = _kinds(events)
    # tick_start, then state_run/signal per visited non-terminal state
    # (GUARD,DRAIN,PULL,PERSIST,EXIT), then disposition, then tick_end.
    expected = (
        ["tick_start"]
        + ["state_run", "signal"] * 5
        + ["disposition", "tick_end"]
    )
    assert kinds == expected, kinds


def test_default_tick_state_run_events_name_the_visited_states_in_order():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    state_runs = [e["state"] for e in events if e["kind"] == "state_run"]
    assert state_runs == ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"], \
        state_runs
    # Each signal event carries the signal the matching state emitted.
    signals = [e["signal"] for e in events if e["kind"] == "signal"]
    assert signals == ["OK", "OK", "OK", "OK", "idle"], signals


def test_default_tick_seq_is_monotonic():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(events))), seqs


def test_default_tick_ts_is_the_injected_now():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    assert events, "no events emitted"
    for e in events:
        assert e["ts"] == _NOW.isoformat(), e


def test_default_tick_tick_start_detail_carries_route_source_and_mode():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    start = next(e for e in events if e["kind"] == "tick_start")
    assert start["detail"] is not None, start
    assert start["detail"].get("source") == "default", start
    assert "mode" in start["detail"], start


def test_default_tick_disposition_event_reports_idle():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    disp = next(e for e in events if e["kind"] == "disposition")
    # The disposition event carries the resulting disposition + EXIT signal.
    assert disp["signal"] == "idle", disp


def test_default_tick_tick_end_detail_carries_read_product_counts():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    end = next(e for e in events if e["kind"] == "tick_end")
    detail = end["detail"]
    assert detail is not None, end
    assert detail.get("work_items") == 2, detail
    assert detail.get("work_orders") == 0, detail
    assert detail.get("execution_plan") == 0, detail
    assert detail.get("handoffs") == 0, detail
    # tick_end carries the final signal too.
    assert end["signal"] == "idle", end


# ==========================================================================
# Behaviour C — every emitted kind is in the closed EVENT_KINDS vocabulary.
# ==========================================================================

def test_no_event_kind_outside_event_kinds_vocabulary():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    for e in events:
        assert e["kind"] in ob.EVENT_KINDS, e["kind"]


# ==========================================================================
# Behaviour D — empty pull still emits the full sequence; ts deterministic.
# ==========================================================================

def test_empty_pull_still_emits_full_sequence():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source("[]"), now=_NOW)
    events = _events(runtime_dir)
    kinds = _kinds(events)
    expected = (
        ["tick_start"]
        + ["state_run", "signal"] * 5
        + ["disposition", "tick_end"]
    )
    assert kinds == expected, kinds
    end = next(e for e in events if e["kind"] == "tick_end")
    assert end["detail"]["work_items"] == 0, end


# ==========================================================================
# Behaviour: a halted tick (STOPPED latched) still logs tick_start +
# disposition + tick_end (GUARD short-circuit, no PULL state_run).
# ==========================================================================

def test_stopped_tick_emits_events_without_pull_state_run():
    import lifecycle_dispositions as ld
    runtime_dir, state_path, journal_path = _paths()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)

    def _exploding(repo=None):
        raise AssertionError("PULL must not run while STOPPED is latched")

    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_exploding, now=_NOW)
    events = _events(runtime_dir)
    kinds = _kinds(events)
    # GUARD short-circuits to HALTED: a tick_start, GUARD's state_run/signal,
    # then a disposition + tick_end. No PULL state_run.
    assert "tick_start" in kinds, kinds
    assert "tick_end" in kinds, kinds
    state_runs = [e["state"] for e in events if e["kind"] == "state_run"]
    assert "PULL" not in state_runs, state_runs
    for e in events:
        assert e["kind"] in ob.EVENT_KINDS, e["kind"]


# ==========================================================================
# Behaviour B — AGENT route: step (pause+dispatch) -> resume -> tick_end, all
# appended to ONE events.jsonl with monotonic seq.
# ==========================================================================

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

_AGENT_ROUTE = {
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


def _agent_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    return amap


def _setup_agent_project():
    project_dir = tempfile.mkdtemp(prefix="sched-events-agentproj-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_AGENT_ROUTE, f)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(_agent_map(), f)
    runtime_dir = cfg
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


def _write_outputs(paused, contents):
    """Simulate the subagent: WRITE each content string to the matching
    paused dispatch's output_path file (the file-based resume handoff)."""
    for d, content in zip(paused["dispatches"], contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def test_agent_route_step_emits_tick_start_pause_and_dispatch():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    kinds = _kinds(events)
    assert "tick_start" in kinds, kinds
    assert "pause" in kinds, kinds
    assert "dispatch" in kinds, kinds
    # The pause + dispatch name the TRIAGE agent-state.
    pause = next(e for e in events if e["kind"] == "pause")
    assert pause["state"] == "TRIAGE", pause
    dispatch = next(e for e in events if e["kind"] == "dispatch")
    assert dispatch["state"] == "TRIAGE", dispatch
    assert dispatch["detail"].get("subagent_type") == "triage-doer", dispatch
    # No tick_end yet (the tick is paused, not done).
    assert "tick_end" not in kinds, kinds


def test_agent_route_step_then_resume_appends_to_one_log_monotonic_seq():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Step: pauses at TRIAGE.
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_NOW)
    n_after_step = len(_events(runtime_dir))
    # Simulate the subagent: WRITE work_orders to the TRIAGE output_path, THEN
    # resume (file-based). Resume applies work_orders, runs PERSIST/EXIT, reaches
    # tick_end.
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW, resume=True)
    events = _events(runtime_dir)
    kinds = _kinds(events)
    # The whole step->resume sequence is ONE append-only log.
    assert len(events) > n_after_step, (n_after_step, len(events))
    assert "resume" in kinds, kinds
    assert "tick_end" in kinds, kinds
    # The resume event names the resumed agent-state.
    resume = next(e for e in events if e["kind"] == "resume")
    assert resume["state"] == "TRIAGE", resume
    # seq stays monotonic across the two invocations (observability handles it).
    seqs = [e["seq"] for e in events]
    assert seqs == list(range(len(events))), seqs
    # The terminal tick_end carries the read-product counts (work_orders applied).
    end = next(e for e in events if e["kind"] == "tick_end")
    assert end["detail"]["work_orders"] == 2, end
    # Every emitted kind is valid.
    for e in events:
        assert e["kind"] in ob.EVENT_KINDS, e["kind"]


def test_agent_route_emits_state_run_for_script_states():
    """The agent-driver path emits state_run inline as each SCRIPT state runs."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW)
    events = _events(runtime_dir)
    state_runs = [e["state"] for e in events if e["kind"] == "state_run"]
    # GUARD, DRAIN, PULL ran (script states) before the TRIAGE agent pause.
    assert "GUARD" in state_runs, state_runs
    assert "PULL" in state_runs, state_runs
