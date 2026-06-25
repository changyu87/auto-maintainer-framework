#!/usr/bin/env python3
"""End-to-end conformance tests for the doer's run_tick governance on ACTING
agent-states: the acted-ledger (idempotency), the budget pre-gate (skip dispatch
when the budget window is exhausted), and spend metering on resume (--spent).

This cycle completes the run_tick governance for ACTING agent-states (those whose
dispatch entry carries a truthy `effect`, from the prior trust-gate cycle). On the
PERMITTED (dispatch) path it adds THREE things — ALL only for acting agent-states;
non-acting (TRIAGE) + pure-script routes are UNCHANGED:

  1. Acted-ledger (idempotency, §3.2.4) — a new durable cross-tick key
     ACTED_LEDGER_KEY = "acted_ledger" maps {work_order_id: {outcome, ref}}. At an
     acting agent-state the per_item dispatch set is FILTERED to drop any
     work_order_id already in the ledger (already acted — never re-dispatch). If
     after filtering NO items remain, the state does NOT pause — it synthesizes an
     inert result, computes the signal, and continues. On RESUME each newly-acted
     item is recorded into the ledger (load-modify-save just ACTED_LEDGER_KEY,
     preserving every other durable key).

  2. Budget pre-gate — at an acting agent-state on the permitted path, BEFORE
     pausing, run_tick evaluates the budget window; if allowed is False (per-day
     exhausted) it does NOT pause/dispatch — it synthesizes a deferred result
     (handoffs status:"blocked", blocked_reason naming the budget exhaustion) for
     the not-yet-acted items, computes the signal, and continues. NO spend, NO
     dispatch; the items stay un-acted (NOT in the ledger) so they retry next
     window. TRIAGE / read-only states are NOT budget-pre-gated.

  3. Spend metering on resume — run_tick(resume=True, spent=N) (and the CLI
     --resume --spent N) records the spend into the durable budget window
     (record_spend) and persists it. Default spent 0 (back-compatible).

scheduling CONSUMES safety-governance + durable-state + agent-dispatch +
adapter-wiring + the loop-core features UNCHANGED via sys.path; it does NOT edit
or fork them. Edits live ONLY in scheduling (run_tick.py).

Owner: changyu87
"""

import io
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

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


# A fixed local offset pins window_key to the injected now's LOCAL date so the
# suite never depends on the host tz.
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
# Agent-adapter fixtures (mirror test_agent_trust_gate_e2e.py): TRIAGE is a
# NON-acting agent (no `effect`); IMPLEMENT is an ACTING agent (effect=implement,
# per_item over execution_plan.ordered). PRIORITIZE stays a SCRIPT.
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


def _setup_agent_project(mode="propose", budget=None):
    """A project dir wired with the agent route + agent adapter-map override and
    the runtime paths under it. Governance.json carries `mode` and (when given) a
    `budget` dict. Returns (project_dir, runtime_dir, state_path, journal_path)."""
    project_dir = tempfile.mkdtemp(prefix="sched-ledger-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    gov = {"mode": mode}
    if budget is not None:
        gov["budget"] = budget
    _write_governance(project_dir, gov)
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


def _canned_handoff(work_order_id, status="opened", ref="PR#1"):
    """A canned REAL (non-planned) handoff a dispatched implement-doer would
    write: an opened PR with an artifact ref."""
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": status, "artifact": {"kind": "pr", "ref": ref},
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


def _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                   now=None):
    """Step TRIAGE (a non-acting agent) past its pause by writing the canned
    work_orders + resuming. Returns the SECOND structured return (the IMPLEMENT
    pause / acting-state branch result)."""
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=now)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=_stub_source(), now=now, resume=True)


# ==========================================================================
# Behaviour 0 — the ledger helpers + key exist.
# ==========================================================================

def test_acted_ledger_key_and_helper_exist():
    assert rt.ACTED_LEDGER_KEY == "acted_ledger"
    # An untouched durable state has an empty ledger.
    root = tempfile.mkdtemp(prefix="sched-ledger-empty-")
    state_path = os.path.join(root, "state.json")
    assert rt.persisted_acted_ledger(state_path) == {}


# ==========================================================================
# Behaviour 1 — propose IMPLEMENT, item not in ledger -> pauses to dispatch;
# resume (spent=N) -> DONE; the ledger records the item's {outcome, ref}; the
# budget spent_tokens increased by N.
# ==========================================================================

def test_implement_pauses_resume_records_ledger_and_meters_spend():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", paused
    assert len(paused["dispatches"]) == 2, paused
    # Each implement-doer writes an opened PR handoff for its work_order_id.
    _write_outputs(paused, [_canned_handoff(d["item"], status="opened",
                                            ref=f"PR#{i}")
                            for i, d in enumerate(paused["dispatches"])])
    signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1, resume=True,
                         spent=120)
    assert signal == "idle", signal
    assert rt.persisted_handoffs_count(state_path) == 2
    # The ledger now records BOTH acted items with their outcome + ref.
    ledger = rt.persisted_acted_ledger(state_path)
    assert set(ledger.keys()) == {"wo-acme/widget#7", "wo-acme/widget#9"}, ledger
    for wo_id, rec in ledger.items():
        assert rec["outcome"] == "opened", rec
        assert rec["ref"].startswith("PR#"), rec
    # The budget window metered the spend on resume.
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens") == 120, budget
    assert budget.get("window_key") == "2026-05-01", budget


# ==========================================================================
# Behaviour 2 — next tick, the SAME item re-pulled -> it is in the ledger ->
# SKIPPED (not dispatched; no second PR); idempotent.
# ==========================================================================

def test_already_acted_item_is_skipped_on_next_tick():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    # Tick 1: act on both items.
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(d["item"]) for d in
                            paused["dispatches"]])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True, spent=50)
    ledger_after_t1 = rt.persisted_acted_ledger(state_path)
    assert set(ledger_after_t1) == {"wo-acme/widget#7", "wo-acme/widget#9"}

    # Tick 2: SAME items re-pulled. TRIAGE pauses again (non-acting); resume past
    # it. At IMPLEMENT both items are in the ledger -> NO pause, NO second
    # dispatch; the tick completes.
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert result == "idle", result
    # No second PR: the spend did not increase further (default spent 0 on the
    # tick-2 resume; and the acting state never dispatched).
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens") == 50, budget
    # The ledger is unchanged (idempotent).
    assert rt.persisted_acted_ledger(state_path) == ledger_after_t1


# ==========================================================================
# Behaviour 3 — ALL items already in the ledger -> the acting state does NOT
# pause (no dispatch), the tick completes.
# ==========================================================================

def test_all_items_in_ledger_acting_state_does_not_pause():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    # Pre-seed the ledger with BOTH work_order_ids (as if a prior tick acted).
    # The triage memory is seeded in lock-step (a prior tick records `done` in
    # BOTH the acted-ledger AND triage_memory at resume) so the §3.3.3 POOL refire
    # filter sees both items as done-and-unchanged and idles — matching production,
    # where acted ⇒ recorded in triage_memory.
    doc = ds.DurableState(state_path).load()
    doc[rt.ACTED_LEDGER_KEY] = {
        "wo-acme/widget#7": {"outcome": "opened", "ref": "PR#7"},
        "wo-acme/widget#9": {"outcome": "opened", "ref": "PR#9"},
    }
    doc[rt.TRIAGE_MEMORY_KEY] = {
        "acme/widget#7": {"status": "done", "updated_at": "2026-05-02T11:30:00Z"},
        "acme/widget#9": {"status": "done", "updated_at": "2026-05-03T08:00:00Z"},
    }
    ds.DurableState(state_path).save(doc)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = _resume_triage(project_dir, runtime_dir, state_path,
                                journal_path, now=_DAY1)
    # The acting state did NOT pause (every item already acted) -> the resume
    # drove straight to DONE; the pool is fully done-and-unchanged -> idle.
    assert result == "idle", result
    # No checkpoint left at IMPLEMENT (it never paused).
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc
    # No new dispatch output files for IMPLEMENT.
    out_dir = os.path.join(runtime_dir, "dispatch-out")
    if os.path.isdir(out_dir):
        impl_files = [f for f in os.listdir(out_dir)
                      if f.startswith("IMPLEMENT-")]
        assert impl_files == [], impl_files
    # The trace still shows IMPLEMENT ran (inert) on the stitched path.
    assert "IMPLEMENT" in buf.getvalue(), buf.getvalue()


def test_partial_ledger_filters_only_acted_item():
    """ONE of two items already in the ledger -> the acting state pauses, but for
    the REMAINING (un-acted) item only (one dispatch, not two)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    doc = ds.DurableState(state_path).load()
    doc[rt.ACTED_LEDGER_KEY] = {
        "wo-acme/widget#7": {"outcome": "opened", "ref": "PR#7"},
    }
    ds.DurableState(state_path).save(doc)
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result
    # Only the un-acted #9 is dispatched.
    assert len(result["dispatches"]) == 1, result
    assert result["dispatches"][0]["item"] == "wo-acme/widget#9", result


# ==========================================================================
# Behaviour 4 — budget exhausted (finite per_day, persisted spent >= ceiling) ->
# the acting state does NOT dispatch; handoffs status:"blocked" with a budget
# reason; NO spend; items NOT added to the ledger (retry next window).
# ==========================================================================

def test_budget_exhausted_acting_state_defers_no_dispatch_no_spend():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose", budget={"per_day_tokens": 100})
    # Pre-seed the budget window AT _DAY1 with spent == the ceiling (exhausted).
    doc = ds.DurableState(state_path).load()
    doc[rt.BUDGET_KEY] = {"window_key": "2026-05-01", "spent_tokens": 100}
    ds.DurableState(state_path).save(doc)

    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    # The acting state did NOT pause (budget pre-gate) -> the resume drove to
    # DONE with the deferred (blocked) handoffs. POOL-based refire (§3.3.3): the
    # items are budget-blocked this window but NOT recorded done/deferred (a budget
    # block is below the backoff threshold), so they stay pool-workable and EXIT
    # refires to retry. (The retry is bounded by GUARD's per-tick mutex; the items
    # actually act once the budget window resets.)
    assert result == "refire", result
    handoffs = rt.persisted_handoffs(state_path)
    assert len(handoffs) == 2, handoffs
    for h in handoffs:
        assert h["status"] == "blocked", h
        assert h["blocked_reason"], h
        assert "budget" in h["blocked_reason"].lower(), h
    # No checkpoint at IMPLEMENT (it never paused).
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc
    # No spend recorded by the pre-gate skip (spend stays at the seeded ceiling).
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens") == 100, budget
    # The items were NOT added to the ledger (they were never acted) -> retry.
    assert rt.persisted_acted_ledger(state_path) == {}, \
        rt.persisted_acted_ledger(state_path)


def test_budget_not_exhausted_acting_state_still_dispatches():
    """A finite per_day ceiling that is NOT yet reached -> the acting state
    dispatches normally (the pre-gate only skips when exhausted)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose", budget={"per_day_tokens": 1000})
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result


# ==========================================================================
# Behaviour 5 — a NON-acting TRIAGE (no effect) is NOT budget-pre-gated and NOT
# ledger-filtered: it always dispatches (unchanged), even when the budget is
# exhausted.
# ==========================================================================

def test_non_acting_triage_not_budget_pregated_when_exhausted():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose", budget={"per_day_tokens": 100})
    doc = ds.DurableState(state_path).load()
    doc[rt.BUDGET_KEY] = {"window_key": "2026-05-01", "spent_tokens": 100}
    ds.DurableState(state_path).save(doc)
    # TRIAGE (non-acting) is the FIRST agent-state; it must pause to dispatch
    # even though the budget is exhausted (the pre-gate is acting-only).
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result


def test_non_acting_triage_not_ledger_filtered():
    """A pre-seeded acted-ledger does NOT suppress the non-acting TRIAGE pause."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    doc = ds.DurableState(state_path).load()
    doc[rt.ACTED_LEDGER_KEY] = {"anything": {"outcome": "opened", "ref": "x"}}
    ds.DurableState(state_path).save(doc)
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result


# ==========================================================================
# Behaviour 6 — spend metering: --spent on resume records into the budget; the
# default (no spent) is back-compatible (0).
# ==========================================================================

def test_resume_default_spent_is_zero_backcompatible():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(d["item"]) for d in
                            paused["dispatches"]])
    # Resume with NO spent param -> default 0.
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True)
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens", 0) == 0, budget


def test_cli_resume_spent_flag_meters_spend():
    """The --resume --spent <N> CLI flag records the spend into the budget."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    # Step TRIAGE pause via the CLI is not necessary; reuse the programmatic
    # path to reach the IMPLEMENT pause, then drive the resume via the CLI.
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(d["item"]) for d in
                            paused["dispatches"]])
    # Override the default PULL source so the CLI resume touches no network.
    orig = rt.DEFAULT_PULL_SOURCE
    rt.DEFAULT_PULL_SOURCE = _stub_source()
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = rt.main([
                "--resume", "--spent", "77",
                "--runtime-dir", runtime_dir, "--state", state_path,
                "--journal", journal_path, "--project-dir", project_dir,
            ])
    finally:
        rt.DEFAULT_PULL_SOURCE = orig
    assert code == 0, (code, buf.getvalue())
    env = json.loads(buf.getvalue().strip())
    assert env["status"] == "done", env
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens") == 77, budget


# ==========================================================================
# Behaviour 6b — spend metering is broadened to ALL agent-state resumes: a
# NON-acting TRIAGE resume with spent=N records N into the durable budget window.
# The budget is a token ceiling over ALL loop model spend (DESIGN §3.8.4), so
# TRIAGE spend counts too — previously this did NOT meter (acting-only guard).
# ==========================================================================

def test_non_acting_triage_resume_meters_spend():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    # Pause at TRIAGE (the first, non-acting agent-state).
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    # Resume the TRIAGE pause with a metered spend. Because TRIAGE is non-acting,
    # the OLD acting-only guard would have dropped this spend; the new behaviour
    # meters it into the durable budget window.
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True, spent=42)
    budget = rt.persisted_budget_state(state_path)
    assert budget.get("spent_tokens") == 42, budget
    assert budget.get("window_key") == "2026-05-01", budget


# ==========================================================================
# Behaviour 7 — recording the ledger preserves all OTHER durable keys.
# ==========================================================================

def test_ledger_record_preserves_other_durable_keys():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(d["item"]) for d in
                            paused["dispatches"]])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True, spent=10)
    doc = ds.DurableState(state_path).load()
    # The durable cross-tick facts survive alongside the new ledger.
    assert rt.ACTED_LEDGER_KEY in doc
    assert rt.BUDGET_KEY in doc
    assert "counter" in doc
    # The per-tick read products are present too.
    assert rt.HANDOFFS_KEY in doc


# ==========================================================================
# Behaviour 8 — the pure-script DEFAULT route is UNCHANGED (no ledger, no
# pre-gate touch).
# ==========================================================================

def test_pure_script_default_route_unchanged():
    project_dir = tempfile.mkdtemp(prefix="sched-ledger-pure-")
    _write_governance(project_dir, {"mode": "propose"})
    root = tempfile.mkdtemp(prefix="sched-ledger-pure-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
    assert signal == "idle", signal
    line = buf.getvalue()
    assert "[tick] path=GUARD->DRAIN->PULL->PERSIST->EXIT->DONE" in line, line
    # A pure-script route never writes the acted-ledger key.
    doc = ds.DurableState(state_path).load()
    assert rt.ACTED_LEDGER_KEY not in doc, doc
