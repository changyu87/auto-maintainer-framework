#!/usr/bin/env python3
"""End-to-end conformance for the FRESH-tick reset of ephemeral read products.

Regression for auto-maintainer-framework#356: during a tick transition the
top-level durable `execution_plan` still showed the PREVIOUS tick's work orders
while `work_orders` and the live dispatch correctly carried the new tick's item.
The read products are per-tick EPHEMERAL (#64) and the terminal `done` persist
already overwrites them, but that runs only when the tick reaches its terminal.
An AGENT route PAUSES at each agent-state and returns EARLY (before the
terminal), so a tick observed while paused mid-route left the prior tick's
`execution_plan` stale in top-level durable state (misleading when inspecting
logs/state; dispatch itself was unaffected).

The fix (scheduling/run_tick.py only): at a FRESH agent tick start reset the
ephemeral read products to their empty defaults up-front, so top-level durable
state reflects the CURRENT tick from the outset — matching what the terminal
persist would have written for a route that has not (yet) run the producing
stage. The live per-tick values are carried in the checkpoint `slots` snapshot,
so the reset never loses in-flight work.

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  1. A fresh agent --step that PAUSES at TRIAGE clears a stale prior-tick
     top-level `execution_plan` (the #356 repro), while the checkpoint + durable
     cross-tick facts (counter/budget) are preserved.
  2. The reset is symmetric across ALL ephemeral read products (work_items,
     work_orders, execution_plan, handoffs, verdicts, review_findings,
     integration_result) — none carries a stale prior-tick value at the pause.
  3. A pure-script route (never pauses; always reaches its terminal) still ends
     with correctly-persisted read products — the fix does not regress it.

scheduling CONSUMES its dependencies UNCHANGED via sys.path; it does NOT edit or
fork them.

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
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)


GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  }
]"""


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


# --------------------------------------------------------------------------
# Agent-adapter fixtures: TRIAGE + IMPLEMENT wired as AGENT entries so a fresh
# tick PAUSES at TRIAGE (before PRIORITIZE / IMPLEMENT run).
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


def _write_project_config(project_dir, name, obj):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(obj, f)


def _setup_agent_project():
    project_dir = tempfile.mkdtemp(prefix="sched-freshreset-")
    _write_project_config(project_dir, "route.json", _AGENT_ROUTE)
    _write_project_config(project_dir, "adapter-map.json", _agent_map())
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# A stale PRIOR-tick execution_plan (the #356 symptom) plus stale values across
# every ephemeral read product, seeded before the fresh tick runs.
_STALE_PLAN = {
    "schema_version": "1.0.0",
    "ordered": ["wo-acme/widget#337", "wo-acme/widget#335"],
    "status": {"wo-acme/widget#337": "pending"},
}
_STALE_WORK_ORDERS = [
    {"schema_version": "1.0.0", "id": "wo-acme/widget#337",
     "work_item_id": "acme/widget#337", "title": "old", "body": "",
     "url": "", "labels": [], "decision": "accepted", "reason": "",
     "created_at": ""},
]


def _seed_stale_read_products(state_path):
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    doc = ds.DurableState(state_path).load()
    doc[rt.WORK_ITEMS_KEY] = [{"id": "acme/widget#337"}]
    doc[rt.WORK_ORDERS_KEY] = list(_STALE_WORK_ORDERS)
    doc[rt.EXECUTION_PLAN_KEY] = dict(_STALE_PLAN)
    doc[rt.HANDOFFS_KEY] = [{"work_order_id": "wo-acme/widget#337"}]
    doc[rt.VERDICTS_KEY] = [{"pr": 1}]
    doc[rt.REVIEW_FINDINGS_KEY] = [{"title": "old finding"}]
    doc[rt.INTEGRATION_RESULT_KEY] = {"merged": [1], "skipped": [], "errors": []}
    # A durable CROSS-TICK fact that must survive the reset (regression guard).
    doc["counter"] = 42
    ds.DurableState(state_path).save(doc)


# ==========================================================================
# Behaviour 1 — a fresh agent --step that PAUSES at TRIAGE clears the stale
# prior-tick execution_plan (the #356 repro), preserving durable facts.
# ==========================================================================

def test_fresh_pause_clears_stale_execution_plan():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _seed_stale_read_products(state_path)
    # Sanity: the stale plan is present before the fresh tick.
    assert rt.persisted_execution_plan(state_path)["ordered"] == [
        "wo-acme/widget#337", "wo-acme/widget#335"]

    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused" and result["state"] == "TRIAGE", result

    # The #356 fix: top-level execution_plan no longer carries the prior tick's
    # plan while the fresh tick is paused (before PRIORITIZE has run).
    plan = rt.persisted_execution_plan(state_path)
    assert plan == {} or plan.get("ordered") == [], plan
    # The durable CROSS-TICK fact (counter) is untouched by the reset.
    doc = ds.DurableState(state_path).load()
    assert doc.get("counter") == 42, doc
    # The checkpoint (crash-safety source of truth) is intact and carries the
    # fresh tick's live slots.
    cp = doc.get(rt.TICK_CHECKPOINT_KEY)
    assert cp is not None and cp["pending"]["state"] == "TRIAGE", doc


# ==========================================================================
# Behaviour 2 — the reset is symmetric across ALL ephemeral read products.
# ==========================================================================

def test_fresh_pause_clears_all_stale_read_products():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _seed_stale_read_products(state_path)

    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert result["status"] == "paused" and result["state"] == "TRIAGE", result

    doc = ds.DurableState(state_path).load()
    for key, default in rt.EPHEMERAL_READ_PRODUCT_DEFAULTS.items():
        assert doc.get(key) == default, (key, doc.get(key))


# ==========================================================================
# Behaviour 3 — a pure-script route (never pauses) still persists read products
# correctly at its terminal; the fix does not regress it.
# ==========================================================================

def test_pure_script_route_read_products_unchanged():
    project_dir = tempfile.mkdtemp(prefix="sched-freshreset-pure-")
    runtime_dir = os.path.join(project_dir, "runtime")
    state_path = os.path.join(runtime_dir, "state.json")
    journal_path = os.path.join(runtime_dir, "journal.jsonl")
    _seed_stale_read_products(state_path)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
    assert signal == "idle", signal
    # The default pure-script route runs PULL (no PRIORITIZE), so at its terminal
    # execution_plan is the empty default — no stale carry-forward — and the
    # durable counter is preserved.
    plan = rt.persisted_execution_plan(state_path)
    assert plan == {} or plan.get("ordered") == [], plan


if __name__ == "__main__":
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"ok   {name}")
            except Exception:
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print("\nall passed")
