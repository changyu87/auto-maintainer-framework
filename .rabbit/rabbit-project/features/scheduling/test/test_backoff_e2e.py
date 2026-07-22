#!/usr/bin/env python3
"""End-to-end conformance tests for backoff (§3.8.5): bounded-retry -> escalate
-> defer for blocked work orders.

A valid work order the doer reports `blocked` must be worked toward an end, not
silently leaked, and must never halt the loop (DESIGN §3.8.5). This cycle adds
that to run_tick's acting-state governance. It consumes observability (escalate)
UNCHANGED; edits live ONLY in scheduling (run_tick.py).

Behaviours covered:
  1. The leak fix: _record_acted_ledger records ONLY completed outcomes
     (opened/closed) into the acted-ledger — NEVER blocked. A blocked item stays
     retryable (not filtered as "already acted").
  2. Backoff ledger (durable, keyed on work_item_id): BACKOFF_LEDGER_KEY="backoff"
     maps {work_item_id: {blocked_count, deferred_at_updated_at}};
     persisted_backoff_ledger reads it (default {}); the threshold is
     CONFIG-DRIVEN (sg.load_config's backoff.threshold, default 5).
  3. On resume of an acting state, a blocked handoff increments blocked_count;
     at the threshold the item is deferred (deferred_at_updated_at set to the
     issue's current updated_at) AND an escalation is posted once via an
     injectable sink (message names the count + reason). Below the threshold the
     item retries (not deferred).
  4. opened/closed clears the item's backoff entry.
  5. IMPLEMENT per_item filter ALSO skips a work_order that is deferred AND whose
     issue updated_at == deferred_at_updated_at (deferred-unchanged). When the
     issue updated_at advances, the item re-enters (not skipped) and its backoff
     entry resets.
  6. Escalation never crashes the tick (a raising sink is swallowed).

scheduling CONSUMES safety-governance + durable-state + agent-dispatch +
adapter-wiring + observability + the loop-core features UNCHANGED via sys.path; it
does NOT edit or fork them. Edits live ONLY in scheduling (run_tick.py).

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
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import safety_governance as sg  # noqa: E402,F401
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)


# Issue #7's updated_at in the fixture below ("2026-05-02T11:30:00Z") is the
# value the backoff ledger records as deferred_at_updated_at. The "advanced"
# fixture bumps it so the deferred item re-enters.
_UPDATED_T1 = "2026-05-02T11:30:00Z"
_UPDATED_T2 = "2026-05-09T08:00:00Z"


def _gh_fixture(updated_7=_UPDATED_T1):
    return json.dumps([
        {
            "number": 7,
            "title": "Crash on empty config",
            "body": "Steps to reproduce ...",
            "url": "https://github.com/acme/widget/issues/7",
            "state": "OPEN",
            "labels": [{"name": "bug"}],
            "author": {"login": "octocat"},
            "createdAt": "2026-05-01T10:00:00Z",
            "updatedAt": updated_7,
        },
    ])


def _stub_source(json_text=None):
    items = wi.parse_gh_issues(json_text or _gh_fixture())

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


# --------------------------------------------------------------------------
# Agent-adapter fixtures: TRIAGE non-acting agent; IMPLEMENT acting agent
# (effect=implement, per_item over execution_plan.ordered). One work_item (#7)
# so a single dispatch reaches IMPLEMENT.
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


def _setup_agent_project(mode="propose", budget=None, backoff_threshold=3):
    project_dir = tempfile.mkdtemp(prefix="sched-backoff-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    gov = {"mode": mode}
    if budget is not None:
        gov["budget"] = budget
    # The backoff threshold is config-driven (§3.8.5): run_tick reads
    # backoff.threshold from sg.load_config. The block-N behaviour tests below
    # pin it to 3 so they reach the deferral at the 3rd block; the default-5
    # behaviour is asserted separately. `None` leaves it unset (the default 5).
    if backoff_threshold is not None:
        gov["backoff"] = {"threshold": backoff_threshold}
    _write_governance(project_dir, gov)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# One work_order for issue #7.
_CANNED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def _canned_handoff(work_order_id, status="opened", ref="PR#1",
                    blocked_reason=None):
    artifact = ({"kind": "pr", "ref": ref} if status in ("opened", "closed")
                else {"kind": "none", "ref": None})
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": status, "artifact": artifact,
        "discovered_work": [], "blocked_reason": blocked_reason,
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
                   now=None, source=None):
    """Step TRIAGE (non-acting agent) past its pause; return the SECOND return
    (the IMPLEMENT pause / acting-state branch result)."""
    src = source or _stub_source()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=now)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=now, resume=True)


def _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dependency missing", now=_DAY1, source=None,
                escalate_sink=None):
    """Run ONE full tick that reaches IMPLEMENT, blocks the single work order,
    and resumes to DONE. Returns the final signal."""
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=now, source=source)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(
        paused["dispatches"][0]["item"], status="blocked",
        blocked_reason=blocked_reason)])
    kwargs = dict(project_dir=project_dir, runtime_dir=runtime_dir,
                  state_path=state_path, journal_path=journal_path,
                  source=source or _stub_source(), now=now, resume=True)
    if escalate_sink is not None:
        kwargs["escalate_sink"] = escalate_sink
    return rt.run_tick(**kwargs)


# ==========================================================================
# Behaviour 0 — the backoff key + helper exist; the threshold is config-driven
# (default 5, read from sg.load_config's backoff.threshold).
# ==========================================================================

def test_backoff_key_helper_exist():
    assert rt.BACKOFF_LEDGER_KEY == "backoff"
    root = tempfile.mkdtemp(prefix="sched-backoff-empty-")
    state_path = os.path.join(root, "state.json")
    assert rt.persisted_backoff_ledger(state_path) == {}


def test_backoff_threshold_default_is_five():
    """The DEFAULT backoff threshold is 5 (safety-governance's documented
    default), read from sg.load_config — never a hardcoded module constant."""
    project_dir = tempfile.mkdtemp(prefix="sched-backoff-default-")
    # No config.json / governance.json -> the documented defaults apply.
    cfg = sg.load_config(project_dir)
    assert cfg["backoff"]["threshold"] == 5, cfg


# ==========================================================================
# Behaviour 1 (leak fix) — a blocked handoff is NOT written to the acted-ledger;
# the item stays retryable.
# ==========================================================================

def test_blocked_handoff_not_recorded_in_acted_ledger():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    signal = _block_once(project_dir, runtime_dir, state_path, journal_path)
    # POOL-based refire (§3.3.3): a blocked item BELOW the backoff threshold is NOT
    # deferred — it stays retryable, so it remains pool-workable and the acting
    # propose route refires (runs the next tick immediately to retry). Only a
    # blocked-PAST-threshold (deferred-unchanged) item goes inert and idles.
    assert signal == "refire", signal
    # The blocked work order is NOT in the acted-ledger (leak fixed).
    assert rt.persisted_acted_ledger(state_path) == {}, \
        rt.persisted_acted_ledger(state_path)


def test_opened_handoff_still_recorded_in_acted_ledger():
    """The leak fix is surgical: a completed (opened) outcome is STILL recorded."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(paused["dispatches"][0]["item"],
                                            status="opened", ref="PR#1")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True)
    assert set(rt.persisted_acted_ledger(state_path)) == {"wo-acme/widget#7"}


# ==========================================================================
# Behaviour 2 — blocked_count increments per blocked resume (below threshold:
# not deferred, retries next tick).
# ==========================================================================

def test_blocked_count_increments_below_threshold():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Tick 1 — block once.
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["blocked_count"] == 1, backoff
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] in (None, ""), \
        backoff

    # Tick 2 — block again; count rises to 2; still below the configured
    # threshold (3, set by _setup_agent_project) -> the item is still dispatched
    # (retryable), not skipped.
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", paused
    assert len(paused["dispatches"]) == 1, paused
    _write_outputs(paused, [_canned_handoff(paused["dispatches"][0]["item"],
                                            status="blocked",
                                            blocked_reason="still missing")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True)
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["blocked_count"] == 2, backoff
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] in (None, ""), \
        backoff


# ==========================================================================
# Behaviour 3 — at the Kth (3rd) block the item is deferred (deferred_at set to
# the issue's current updated_at) AND escalation is posted once via the
# injectable sink, naming the count + reason.
# ==========================================================================

def test_kth_block_defers_and_escalates_once():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    calls = []

    def sink(target_ref, body):
        calls.append((target_ref, body))
        return "commented"

    # Block 3 times. The escalation fires on the 3rd.
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dep A", escalate_sink=sink)
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dep B", escalate_sink=sink)
    assert calls == [], calls  # not yet at threshold
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dep C", escalate_sink=sink)

    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["blocked_count"] == 3, backoff
    # Deferred at the issue's current updated_at (from work_items).
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] == _UPDATED_T1, \
        backoff

    # Exactly ONE escalation, on the triggering issue, naming the count + reason.
    assert len(calls) == 1, calls
    target_ref, body = calls[0]
    assert target_ref == "acme/widget#7", target_ref
    assert "3" in body, body
    assert "dep C" in body, body


# ==========================================================================
# Behaviour 3b — the threshold is CONFIG-DRIVEN (default 5): with no backoff
# config the item is NOT deferred at the 3rd block (the old hardcoded-3 behaviour
# is gone); it is deferred at the 5th. A config-set threshold of 5 behaves the
# same as the default.
# ==========================================================================

def test_default_threshold_five_defers_at_fifth_block_not_third():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        backoff_threshold=None)  # unset -> the documented default 5
    calls = []

    def sink(target_ref, body):
        calls.append((target_ref, body))
        return "commented"

    # Blocks 1..4: below the default threshold (5) -> NOT deferred, no escalation.
    for i in range(4):
        _block_once(project_dir, runtime_dir, state_path, journal_path,
                    blocked_reason=f"dep {i}", escalate_sink=sink)
        backoff = rt.persisted_backoff_ledger(state_path)
        assert backoff["acme/widget#7"]["blocked_count"] == i + 1, backoff
        assert backoff["acme/widget#7"]["deferred_at_updated_at"] in (None, ""), \
            (i, backoff)
        assert calls == [], (i, calls)

    # Block 5: reaches the default threshold -> deferred + escalated once.
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dep 5", escalate_sink=sink)
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["blocked_count"] == 5, backoff
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] == _UPDATED_T1, \
        backoff
    assert len(calls) == 1, calls
    assert "5" in calls[0][1], calls


# ==========================================================================
# Behaviour 4 — a deferred-AND-unchanged work_order is filtered from the per_item
# set (not dispatched; no thrash).
# ==========================================================================

def test_deferred_unchanged_item_is_skipped():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Pre-seed the backoff ledger: #7 deferred at the issue's current updated_at.
    doc = ds.DurableState(state_path).load()
    doc[rt.BACKOFF_LEDGER_KEY] = {
        "acme/widget#7": {"blocked_count": 3,
                          "deferred_at_updated_at": _UPDATED_T1},
    }
    ds.DurableState(state_path).save(doc)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = _resume_triage(project_dir, runtime_dir, state_path,
                                journal_path, now=_DAY1)
    # The only item is deferred-unchanged -> IMPLEMENT does NOT pause; drives to
    # DONE.
    assert result == "idle", result
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc
    out_dir = os.path.join(runtime_dir, "dispatch-out")
    if os.path.isdir(out_dir):
        impl_files = [f for f in os.listdir(out_dir)
                      if f.startswith("IMPLEMENT-")]
        assert impl_files == [], impl_files


# ==========================================================================
# Behaviour 5 — when the issue updated_at ADVANCES, the deferred item re-enters
# (not skipped) AND its backoff entry resets (blocked_count 0, deferral cleared).
# ==========================================================================

def test_advanced_updated_at_reenters_and_resets_backoff():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    doc = ds.DurableState(state_path).load()
    doc[rt.BACKOFF_LEDGER_KEY] = {
        "acme/widget#7": {"blocked_count": 3,
                          "deferred_at_updated_at": _UPDATED_T1},
    }
    ds.DurableState(state_path).save(doc)

    # The issue's updated_at has advanced (a human commented / relabelled).
    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    result = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1, source=advanced_src)
    # The item re-enters: IMPLEMENT pauses to dispatch it again.
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result
    assert len(result["dispatches"]) == 1, result
    assert result["dispatches"][0]["item"] == "wo-acme/widget#7", result
    # Its backoff entry was reset on re-entry.
    backoff = rt.persisted_backoff_ledger(state_path)
    entry = backoff.get("acme/widget#7", {})
    assert entry.get("blocked_count", 0) == 0, backoff
    assert entry.get("deferred_at_updated_at") in (None, ""), backoff


# ==========================================================================
# Behaviour 6 — opened/closed clears the item's backoff entry (success resets
# the counter).
# ==========================================================================

def test_opened_clears_backoff_entry():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Block twice (count -> 2).
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    assert rt.persisted_backoff_ledger(state_path)["acme/widget#7"][
        "blocked_count"] == 2

    # Now the doer succeeds: an opened handoff clears the backoff entry.
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=_DAY1)
    assert paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(paused["dispatches"][0]["item"],
                                            status="opened", ref="PR#9")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True)
    backoff = rt.persisted_backoff_ledger(state_path)
    assert "acme/widget#7" not in backoff or backoff["acme/widget#7"] in (
        None, {}), backoff
    # And the success IS recorded in the acted-ledger.
    assert set(rt.persisted_acted_ledger(state_path)) == {"wo-acme/widget#7"}


# ==========================================================================
# Behaviour 7 — a raising escalation sink does NOT crash the tick.
# ==========================================================================

def test_escalation_sink_raising_does_not_crash_tick():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()

    def boom(target_ref, body):
        raise RuntimeError("network down")

    # Block to the threshold; the 3rd block triggers escalation which raises.
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                escalate_sink=boom)
    _block_once(project_dir, runtime_dir, state_path, journal_path,
                escalate_sink=boom)
    signal = _block_once(project_dir, runtime_dir, state_path, journal_path,
                         escalate_sink=boom)
    # The tick still reached its terminal despite the escalation failure.
    assert signal == "idle", signal
    # The item is still deferred (the escalation attempt does not block deferral).
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["blocked_count"] == 3, backoff
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] == _UPDATED_T1, \
        backoff


# ==========================================================================
# Behaviour 8 — strictly per-item / non-acting unchanged: a NON-acting TRIAGE is
# never backoff-filtered (a pre-seeded backoff ledger does not suppress it).
# ==========================================================================

def test_non_acting_triage_not_backoff_filtered():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    doc = ds.DurableState(state_path).load()
    doc[rt.BACKOFF_LEDGER_KEY] = {
        "acme/widget#7": {"blocked_count": 3,
                          "deferred_at_updated_at": _UPDATED_T1},
    }
    ds.DurableState(state_path).save(doc)
    # TRIAGE (non-acting, first agent-state) still pauses to dispatch.
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result


# ==========================================================================
# Behaviour 9 — the pure-script DEFAULT route is UNCHANGED (no backoff key).
# ==========================================================================

def test_pure_script_default_route_no_backoff_key():
    project_dir = tempfile.mkdtemp(prefix="sched-backoff-pure-")
    _write_governance(project_dir, {"mode": "propose"})
    root = tempfile.mkdtemp(prefix="sched-backoff-pure-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
    assert signal == "idle", signal
    doc = ds.DurableState(state_path).load()
    assert rt.BACKOFF_LEDGER_KEY not in doc, doc
