#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling: wiring safety-governance.

This cycle WIRES the safety-governance lib into the tick loop (slice 1: load +
surface + persist). It consumes safety-governance UNCHANGED (DESIGN §3.8); edits
live ONLY in scheduling:

  - Load governance once per tick: gov = sg.load_governance(project_dir), threaded
    into the runtime dict so future acting adapters can consult permits/budget.
  - Durable, cross-tick budget window: a new durable key "budget" stores
    {window_key, spent_tokens}. Each tick resolves a tz-aware `now`, calls
    sg.evaluate_budget(gov, prior_budget_state, now, tick_spend), and PERSISTS the
    returned budget_state (the lib performs window rollover / auto-resume).
  - Surface mode + a compact budget field in the tick trace AND status.py
    (always-shown, #69 style), plus a budget_paused=<reason> indicator when
    evaluate_budget returns allowed=False.
  - Act-skip on budget-blocked is DEFERRED to the acting doer (no model spender
    yet): this slice only SURFACES the paused reason; the tick still completes.

Behaviours exercised here (extending docs/spec.md governance wiring):

  1. Default (no governance.json) -> trace + status show mode=propose and a budget
     field with the default per_day ceiling; budget_paused absent.
  2. Project-local governance.json mode=gated-merge + per_day_tokens=null -> status
     shows mode=gated-merge and budget ceiling "none" (unlimited); never paused.
  3. Budget window persists across ticks: spend within the window is kept; a `now`
     on a LATER local day rolls the window over (window_key advances, spent resets).
  4. Injected tick_spend over a finite per_day ceiling -> evaluate_budget
     allowed=False -> trace/status show budget_paused=per_day_exhausted, but the
     tick still COMPLETES (no act-skip this slice).
  5. Governance is threaded into the runtime dict (so future acting adapters can
     consult permits/budget).

scheduling CONSUMES safety-governance + the loop-core / work-intake /
adapter-wiring features UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402,F401
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


def _paths():
    root = tempfile.mkdtemp(prefix="sched-gov-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _project_dir():
    return tempfile.mkdtemp(prefix="sched-gov-proj-")


def _write_governance(project_dir, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump(payload, f)


def _run_tick_capture(**kwargs):
    """Run a tick, returning (signal, trace_text)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        signal = rt.run_tick(**kwargs)
    return signal, buf.getvalue()


# --------------------------------------------------------------------------
# Behaviour 1 — default (no governance.json): mode=propose + default budget
# ceiling surfaced in BOTH the trace and status; budget_paused absent.
# --------------------------------------------------------------------------

def test_default_governance_trace_shows_mode_propose_and_budget():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()  # no governance.json -> defaults
    signal, trace = _run_tick_capture(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir,
        source=_stub_source(), now=_DAY1)
    assert signal == "idle", signal
    assert "mode=propose" in trace, trace
    # Default per_day ceiling is 200000 (safety-governance default); spent 0.
    assert "budget=0/200000" in trace, trace
    assert "win=2026-05-01" in trace, trace
    # Not paused on a fresh, unspent budget.
    assert "budget_paused" not in trace, trace


def test_default_governance_status_shows_mode_propose_and_budget():
    project_dir = _project_dir()
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source(), now=_DAY1)
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "mode=propose" in line, line
    assert "budget=0/200000" in line, line
    assert "budget_paused" not in line, line


# --------------------------------------------------------------------------
# Behaviour 2 — project-local governance.json: mode=gated-merge + null per_day
# (unlimited) -> ceiling "none"; never paused.
# --------------------------------------------------------------------------

def test_gated_merge_unlimited_budget_shows_mode_and_none_ceiling():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    _write_governance(project_dir, {
        "mode": "gated-merge",
        "budget": {"per_day_tokens": None},
    })
    signal, trace = _run_tick_capture(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir,
        source=_stub_source(), now=_DAY1)
    assert signal == "idle", signal
    assert "mode=gated-merge" in trace, trace
    # Null per_day ceiling renders as "none" (unlimited).
    assert "budget=0/none" in trace, trace
    # An unlimited budget never pauses, even with a large injected spend.
    assert "budget_paused" not in trace, trace


def test_gated_merge_unlimited_status_shows_mode_and_none_ceiling():
    project_dir = _project_dir()
    _write_governance(project_dir, {
        "mode": "gated-merge",
        "budget": {"per_day_tokens": None},
    })
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source(), now=_DAY1, tick_spend=10_000_000)
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "mode=gated-merge" in line, line
    assert "budget=0/none" in line, line
    assert "budget_paused" not in line, line


# --------------------------------------------------------------------------
# Behaviour 3 — durable, cross-tick budget window: persisted across ticks; a
# later local day rolls the window over (window_key advances, spent resets).
# --------------------------------------------------------------------------

def test_budget_window_persists_and_rolls_over_across_ticks():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()

    # Tick 1 on DAY1 with a real spend -> the spend is recorded into the durable
    # budget window for DAY1.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY1, tick_spend=1234)
    bs1 = rt.persisted_budget_state(state_path)
    assert bs1["window_key"] == "2026-05-01", bs1
    assert bs1["spent_tokens"] == 1234, bs1

    # Tick 2 still on DAY1 -> same window, spend accumulates.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY1, tick_spend=1000)
    bs2 = rt.persisted_budget_state(state_path)
    assert bs2["window_key"] == "2026-05-01", bs2
    assert bs2["spent_tokens"] == 2234, bs2

    # Tick 3 on DAY2 (a LATER local day) -> window rolls over: spent resets.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY2, tick_spend=0)
    bs3 = rt.persisted_budget_state(state_path)
    assert bs3["window_key"] == "2026-05-02", bs3
    assert bs3["spent_tokens"] == 0, bs3


def test_budget_is_durable_cross_tick_not_reset_like_read_products():
    """The budget window is a durable CROSS-TICK fact (like the counter), NOT a
    per-tick ephemeral read product (#64). A subsequent tick within the same
    window MUST carry the accumulated spend forward, never reset it to 0."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY1, tick_spend=5000)
    # Read products are ephemeral; budget is durable.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY1, tick_spend=0)
    bs = rt.persisted_budget_state(state_path)
    assert bs["spent_tokens"] == 5000, bs  # carried forward, not reset


# --------------------------------------------------------------------------
# Behaviour 4 — injected tick_spend over a finite per_day ceiling: blocked ->
# budget_paused=per_day_exhausted surfaced, but the tick still COMPLETES (no
# act-skip this slice).
# --------------------------------------------------------------------------

def test_per_day_exhausted_surfaces_paused_but_tick_completes():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    _write_governance(project_dir, {
        "mode": "propose",
        "budget": {"per_day_tokens": 1000},
    })
    # Pre-seed the durable budget at/over the ceiling so evaluate_budget blocks.
    doc = ds.DurableState(state_path).load()
    doc[rt.BUDGET_KEY] = {"window_key": "2026-05-01", "spent_tokens": 1000}
    ds.DurableState(state_path).save(doc)

    signal, trace = _run_tick_capture(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir,
        source=_stub_source(), now=_DAY1, tick_spend=0)

    # Surfaced as paused...
    assert "budget_paused=per_day_exhausted" in trace, trace
    # ...but the tick STILL completes (no act-skip this slice): read-and-idle.
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 2


def test_per_day_exhausted_surfaces_paused_in_status():
    project_dir = _project_dir()
    _write_governance(project_dir, {
        "mode": "propose",
        "budget": {"per_day_tokens": 1000},
    })
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        _rt, state_path, _j = rt.resolve_runtime_paths()
        doc = ds.DurableState(state_path).load()
        doc[rt.BUDGET_KEY] = {"window_key": "2026-05-01", "spent_tokens": 1000}
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        ds.DurableState(state_path).save(doc)
        rt.run_tick(source=_stub_source(), now=_DAY1, tick_spend=0)
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "budget_paused=per_day_exhausted" in line, line


# --------------------------------------------------------------------------
# Behaviour 5 — governance is threaded into the runtime dict (so future acting
# adapters can consult permits/budget).
# --------------------------------------------------------------------------

def test_governance_threaded_into_runtime_dict():
    """The factory `runtime` dict carries the loaded governance config under a
    'governance' key, without disturbing the existing keys. We assert via a
    factory that captures the runtime it is handed."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    _write_governance(project_dir, {"mode": "gated-merge"})

    captured = {}
    orig_make_pull = rt.make_pull

    def _spy_make_pull(runtime):
        captured["runtime"] = runtime
        return orig_make_pull(runtime)

    rt.make_pull = _spy_make_pull
    try:
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, project_dir=project_dir,
                    source=_stub_source(), now=_DAY1)
    finally:
        rt.make_pull = orig_make_pull

    runtime = captured["runtime"]
    assert "governance" in runtime, runtime
    assert runtime["governance"]["mode"] == "gated-merge", runtime["governance"]
    # Existing runtime keys are preserved (no regression).
    for key in ("project_dir", "runtime_dir", "source", "now"):
        assert key in runtime, (key, runtime)


# --------------------------------------------------------------------------
# Behaviour: tick_spend defaults to 0 in production (no model spender yet).
# --------------------------------------------------------------------------

def test_tick_spend_defaults_to_zero():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    # No tick_spend injected -> 0 -> nothing recorded.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source(), now=_DAY1)
    bs = rt.persisted_budget_state(state_path)
    assert bs["spent_tokens"] == 0, bs


# --------------------------------------------------------------------------
# Behaviour: an absent injected `now` falls back to a tz-aware host-local now
# (never a naive datetime), so window_key never crashes.
# --------------------------------------------------------------------------

def test_now_defaults_to_tz_aware_when_not_injected():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    # No `now` injected: the runner must default to a tz-aware now and persist a
    # window_key (the host local date) rather than crash on a naive datetime.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_stub_source())
    bs = rt.persisted_budget_state(state_path)
    assert bs["window_key"], bs  # a non-empty ISO date string
    # It is the host's local date.
    assert bs["window_key"] == datetime.now().astimezone().date().isoformat(), bs


# --------------------------------------------------------------------------
# Behaviour: budget is surfaced even on a GUARD-halt (STOPPED) tick, so the
# operator can always see the mode/budget state.
# --------------------------------------------------------------------------

def test_mode_and_budget_surfaced_even_on_halt_tick():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = _project_dir()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    signal, trace = _run_tick_capture(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir,
        source=_stub_source(), now=_DAY1)
    assert signal == "halt", signal
    assert "mode=propose" in trace, trace
    assert "budget=" in trace, trace
