#!/usr/bin/env python3
"""End-to-end conformance tests for the deterministic empty-skip (#306).

On an IDLE tick (PULL finds no actionable work_items) the loop used to still
dispatch the non-acting TRIAGE subagent on an EMPTY batch, then idle — a wasted
model call on every heartbeat, forever. This cycle makes a NON-ACTING `once`
agent-state whose signal rule yields the empty-signal on empty input
(`nonempty_else_empty`) SKIP the subagent dispatch when its dispatch input is
empty, emitting EMPTY directly (no model call). The skip mirrors the §3.5.3
triage-memory filter, so a fully-filtered TRIAGE (every pulled item already
done/deferred-AND-unchanged) skips too.

Behaviours covered:
  1. The empty-skip helper exists and is conservative (only fires for a
     `once` + `nonempty_else_empty` agent-state over an empty input).
  2. PULL EMPTY -> TRIAGE does NOT pause/dispatch: the tick runs straight to
     idle with NO subagent dispatch (the idle-tick triager burn is gone).
  3. The skip emits EMPTY and the route branches on it (TRIAGE EMPTY ->
     PRIORITIZE), so the tick still reaches the terminal.
  4. A NON-empty TRIAGE still pauses/dispatches as before (the skip never
     swallows real work).
  5. A fully-filtered TRIAGE (all pulled items done-AND-unchanged) ALSO skips —
     no dispatch even though PULL found items.
  6. An empty REVIEW-shaped `once` non-acting agent-state over an empty input
     skips too (the generalization beyond TRIAGE).

scheduling CONSUMES the loop-core features UNCHANGED via sys.path; edits live
ONLY in scheduling (run_tick.py).

Owner: changyu87
"""

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

import fsm_contracts as fc  # noqa: E402
import work_intake as wi  # noqa: E402
import adapter_wiring as aw  # noqa: E402
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)

_UPDATED_7 = "2026-05-02T11:30:00Z"


def _gh_fixture(with_7=True):
    issues = []
    if with_7:
        issues.append({
            "number": 7,
            "title": "Crash on empty config",
            "body": "Steps to reproduce ...",
            "url": "https://github.com/acme/widget/issues/7",
            "state": "OPEN",
            "labels": [{"name": "bug"}],
            "author": {"login": "octocat"},
            "createdAt": "2026-05-01T10:00:00Z",
            "updatedAt": _UPDATED_7,
        })
    return json.dumps(issues)


def _stub_source(json_text=None):
    items = wi.parse_gh_issues(json_text if json_text is not None
                              else _gh_fixture())

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


# --------------------------------------------------------------------------
# Agent-adapter fixtures: a NON-ACTING TRIAGE agent (reads work_items, `once`,
# nonempty_else_empty). Mirrors the deployed wiring.
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


def _agent_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    return amap


def _write_json(project_dir, name, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(payload, f)


def _setup_agent_project(mode="propose"):
    project_dir = tempfile.mkdtemp(prefix="sched-empty-skip-")
    _write_json(project_dir, "route.json", _AGENT_ROUTE)
    _write_json(project_dir, "adapter-map.json", _agent_map())
    _write_json(project_dir, "governance.json", {"mode": mode})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


def _agentstate(entry):
    """Build the AgentState the driver consumes from an agent-adapter dict
    (mirrors adapter_wiring's resolver: the helper only reads `.entry`)."""
    m = entry["manifest"]
    manifest = fc.StateManifest(reads=m["reads"], writes=m["writes"],
                               emits=m["emits"])
    return aw.AgentState(manifest=manifest, dispatch=entry["dispatch"],
                         signal=entry["signal"], entry=entry)


# ==========================================================================
# Behaviour 1 — the empty-skip helper exists and is conservative.
# ==========================================================================

def test_empty_skip_helper_fires_on_empty_input():
    state_path = os.path.join(tempfile.mkdtemp(prefix="es-h-"), "state.json")
    agentstate = _agentstate(dict(_TRIAGE_AGENT))
    # Empty input -> skip (writes slot, [], EMPTY).
    result = rt._empty_skip_result("TRIAGE", agentstate,
                                   {"work_items": []}, state_path)
    assert result == ("work_orders", [], "EMPTY"), result


def test_empty_skip_helper_none_on_nonempty_input():
    state_path = os.path.join(tempfile.mkdtemp(prefix="es-h2-"), "state.json")
    agentstate = _agentstate(dict(_TRIAGE_AGENT))
    items = wi.parse_gh_issues(_gh_fixture())
    result = rt._empty_skip_result("TRIAGE", agentstate,
                                   {"work_items": items}, state_path)
    assert result is None, result


def test_empty_skip_helper_none_for_non_nonempty_rule():
    state_path = os.path.join(tempfile.mkdtemp(prefix="es-h3-"), "state.json")
    entry = dict(_TRIAGE_AGENT)
    entry["signal"] = {"rule": "always_ok"}
    agentstate = _agentstate(entry)
    result = rt._empty_skip_result("TRIAGE", agentstate,
                                   {"work_items": []}, state_path)
    assert result is None, result


def test_empty_skip_helper_none_for_per_item():
    state_path = os.path.join(tempfile.mkdtemp(prefix="es-h4-"), "state.json")
    entry = dict(_TRIAGE_AGENT)
    entry["dispatch"] = [dict(entry["dispatch"][0],
                              cardinality={"per_item": "work_items"})]
    agentstate = _agentstate(entry)
    result = rt._empty_skip_result("TRIAGE", agentstate,
                                   {"work_items": []}, state_path)
    assert result is None, result


# ==========================================================================
# Behaviour 2 + 3 — PULL EMPTY -> TRIAGE does NOT pause/dispatch; tick idles.
# ==========================================================================

def test_idle_tick_skips_triage_dispatch():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # No issues -> PULL EMPTY -> TRIAGE over an empty batch.
    src = _stub_source(_gh_fixture(with_7=False))
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    # The tick reaches the terminal (a signal string), NOT a TRIAGE pause.
    assert not isinstance(result, dict), result
    assert result == "idle", result
    # No checkpoint was written (the dispatch was skipped, not paused).
    assert rt.persisted_tick_checkpoint(state_path) == {}, \
        rt.persisted_tick_checkpoint(state_path)


# ==========================================================================
# Behaviour 4 — a NON-empty TRIAGE still pauses/dispatches.
# ==========================================================================

def test_nonempty_tick_still_dispatches_triage():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    src = _stub_source(_gh_fixture(with_7=True))
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    assert isinstance(result, dict), result
    assert result["status"] == "paused" and result["state"] == "TRIAGE", result
    assert len(result["dispatches"]) == 1, result


# ==========================================================================
# Behaviour 5 — a fully-filtered TRIAGE (all pulled items done-AND-unchanged)
# ALSO skips: no dispatch even though PULL found items.
# ==========================================================================

def test_fully_filtered_triage_skips_dispatch():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Seed triage memory: #7 is done at its current updated_at -> filtered out.
    import durable_state as ds
    doc = ds.DurableState(state_path).load()
    doc[rt.TRIAGE_MEMORY_KEY] = {
        "acme/widget#7": {"status": "done", "updated_at": _UPDATED_7},
    }
    ds.DurableState(state_path).save(doc)

    src = _stub_source(_gh_fixture(with_7=True))
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    # PULL found #7, but it is done-AND-unchanged -> the TRIAGE dispatch input is
    # fully filtered -> the dispatch is skipped (no pause), tick idles.
    assert not isinstance(result, dict), result
    assert result == "idle", result
    assert rt.persisted_tick_checkpoint(state_path) == {}, \
        rt.persisted_tick_checkpoint(state_path)
    # The full PULL set is still persisted (the filter never pollutes the read
    # product).
    assert rt.persisted_work_items_count(state_path) == 1, \
        rt.persisted_work_items(state_path)


# ==========================================================================
# Behaviour 6 — the generalization: a REVIEW-shaped `once` non-acting agent over
# an empty input slot skips too.
# ==========================================================================

def test_review_shaped_agent_skips_on_empty_input():
    state_path = os.path.join(tempfile.mkdtemp(prefix="es-rev-"), "state.json")
    review_entry = {
        "kind": "agent",
        "manifest": {"reads": ["verdicts"], "writes": ["review_verdicts"],
                     "emits": ["OK", "EMPTY"]},
        "dispatch": [
            {
                "subagent_type": "review-doer",
                "inputs": ["verdicts"],
                "writes": "review_verdicts",
                "cardinality": "once",
            }
        ],
        "signal": {"rule": "nonempty_else_empty"},
    }
    agentstate = _agentstate(review_entry)
    result = rt._empty_skip_result("REVIEW", agentstate,
                                   {"verdicts": []}, state_path)
    assert result == ("review_verdicts", [], "EMPTY"), result
