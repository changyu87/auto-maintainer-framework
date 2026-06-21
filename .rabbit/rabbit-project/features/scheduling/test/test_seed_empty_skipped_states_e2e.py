#!/usr/bin/env python3
"""End-to-end tests for the SEEDED-EMPTY producible read-product slots.

A route may SKIP a producing state via a signal branch:

  - `VERIFY EMPTY -> PERSIST` skips INTEGRATE/CLEANUP, so INTEGRATE never writes
    the `integration_result` slot.
  - `TRIAGE EMPTY -> PERSIST` skips PRIORITIZE/IMPLEMENT, so IMPLEMENT never
    writes the `handoffs` slot.

Before this fix, the terminal persist block ran `ctx.read(<slot>)` for every
producible slot whose producing STATE was in the route — even when that state
was skipped this tick — raising a `ContractError("slot has not been written")`
and CRASHING the whole tick.

This cycle makes `_seed_context` WRITE a schema-valid empty default for every
producible read-product slot it registers (`work_items`/`work_orders`/
`execution_plan`/`handoffs`/`verdicts`/`integration_result`). A skipped producer
then persists its product EMPTY (the #64-correct value) instead of crashing. A
state that DOES run overwrites its seeded empty (unchanged behaviour).

scheduling CONSUMES verify-integrate / work-intake / prioritize / implement
UNCHANGED via sys.path.

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
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import prioritize as pr  # noqa: E402
import verify_integrate as vi  # noqa: E402
import run_tick as rt  # noqa: E402


# A single OPEN issue so PULL writes one work_item (so the tick is not trivially
# empty at PULL — the EMPTY signal we exercise comes from TRIAGE / VERIFY).
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
  }
]"""


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


def _empty_source():
    def source(repo=None):
        return []
    return source


def _patch_no_open_prs():
    """Make VERIFY's open-PR source return [] so VERIFY emits EMPTY. Returns a
    restore callable."""
    saved = vi.gh_open_pr_source

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return []

    vi.gh_open_pr_source = _open

    def restore():
        vi.gh_open_pr_source = saved
    return restore


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route, f)


# A route where VERIFY EMPTY branches straight to PERSIST, SKIPPING INTEGRATE +
# CLEANUP. INTEGRATE is still in `states` (so integration_result IS registered),
# but never runs when VERIFY emits EMPTY — the exact skipped-producer case.
_VERIFY_EMPTY_SKIP_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "VERIFY", "INTEGRATE", "CLEANUP",
               "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "VERIFY"},
        {"state": "PULL", "signal": "EMPTY", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "INTEGRATE"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "INTEGRATE", "signal": "OK", "next": "CLEANUP"},
        {"state": "CLEANUP", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


# A route where TRIAGE EMPTY branches straight to PERSIST, SKIPPING PRIORITIZE +
# IMPLEMENT. IMPLEMENT is still in `states` (so handoffs IS registered), but
# never runs when TRIAGE emits EMPTY.
_TRIAGE_EMPTY_SKIP_ROUTE = {
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
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PERSIST"},
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


def _run(project_dir, route, source, restore=None):
    _write_project_route(project_dir, route)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    try:
        result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=source, return_run_result=True)
    finally:
        if restore is not None:
            restore()
    return result, state_path


# --------------------------------------------------------------------------
# _seed_context unit-level: the seeded empties are WRITTEN, not just registered.
# --------------------------------------------------------------------------

def test_seed_context_writes_empty_defaults_for_producible_slots():
    project_dir = tempfile.mkdtemp(prefix="sched-seedwrite-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    # A route registering every producible slot.
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl",
                           _read_all_producibles_route())
    # Every producible read-product slot is READABLE (written), never raising
    # "slot has not been written".
    assert ctx.read(wi.WORK_ITEMS_SLOT["name"]) == []
    assert ctx.read(wi.WORK_ORDERS_SLOT["name"]) == []
    assert ctx.read(im_handoffs_name()) == []
    assert ctx.read(vi.VERDICTS_SLOT["name"]) == []
    # review_verdicts (#209): seeded empty when REVIEW/INTEGRATE is routed.
    assert ctx.read(vi.REVIEW_VERDICTS_SLOT["name"]) == []
    ep = ctx.read(pr.EXECUTION_PLAN_SLOT["name"])
    assert ep["ordered"] == [], ep
    assert ep["schema_version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION, ep
    ir = ctx.read(vi.INTEGRATION_RESULT_SLOT["name"])
    assert ir["merged"] == [] and ir["skipped"] == [] and ir["errors"] == [], ir
    assert ir["schema_version"] == vi.INTEGRATION_RESULT_SCHEMA_VERSION, ir


def im_handoffs_name():
    import implement as im
    return im.HANDOFFS_SLOT["name"]


def _read_all_producibles_route():
    return {
        "schema_version": "1.0.0",
        "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                   "IMPLEMENT", "VERIFY", "INTEGRATE", "CLEANUP", "PERSIST",
                   "EXIT", "DONE", "HALTED"],
        "edges": [
            {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
            {"state": "DRAIN", "signal": "OK", "next": "PULL"},
            {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
            {"state": "TRIAGE", "signal": "OK", "next": "PRIORITIZE"},
            {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
            {"state": "IMPLEMENT", "signal": "OK", "next": "VERIFY"},
            {"state": "VERIFY", "signal": "OK", "next": "INTEGRATE"},
            {"state": "INTEGRATE", "signal": "OK", "next": "CLEANUP"},
            {"state": "CLEANUP", "signal": "OK", "next": "PERSIST"},
            {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
            {"state": "EXIT", "signal": "idle", "next": "DONE"},
        ],
        "terminal": ["DONE", "HALTED"],
    }


# --------------------------------------------------------------------------
# Test 1 — VERIFY EMPTY -> PERSIST (skips INTEGRATE/CLEANUP) reaches DONE
# without crashing; integration_result + verdicts persist empty.
# --------------------------------------------------------------------------

def test_verify_empty_skips_integrate_reaches_done_no_crash():
    project_dir = tempfile.mkdtemp(prefix="sched-verifyskip-")
    restore = _patch_no_open_prs()
    result, state_path = _run(project_dir, _VERIFY_EMPTY_SKIP_ROUTE,
                              _stub_source(), restore=restore)
    # The tick reached DONE — no ContractError crash.
    assert result.final_state == "DONE", result.path
    # VERIFY emitted EMPTY and branched straight to PERSIST, skipping
    # INTEGRATE + CLEANUP.
    assert "VERIFY" in result.path, result.path
    assert "INTEGRATE" not in result.path, result.path
    assert "CLEANUP" not in result.path, result.path
    # The skipped INTEGRATE's product persists EMPTY (its seeded default),
    # NOT crashing on read.
    doc = ds.DurableState(state_path).load()
    ir = doc.get("integration_result", {})
    assert ir.get("merged", []) == [], ir
    assert ir.get("skipped", []) == [], ir
    assert ir.get("errors", []) == [], ir
    # VERIFY ran and wrote verdicts EMPTY (no open PRs).
    assert doc.get("verdicts", []) == [], doc


# --------------------------------------------------------------------------
# Test 2 — TRIAGE EMPTY -> PERSIST (skips PRIORITIZE/IMPLEMENT) reaches DONE
# without crashing; handoffs persists empty.
# --------------------------------------------------------------------------

def test_triage_empty_skips_implement_reaches_done_no_crash():
    project_dir = tempfile.mkdtemp(prefix="sched-triageskip-")
    # Empty PULL source -> no work_items -> TRIAGE accepts nothing -> EMPTY.
    result, state_path = _run(project_dir, _TRIAGE_EMPTY_SKIP_ROUTE,
                              _empty_source())
    assert result.final_state == "DONE", result.path
    assert "TRIAGE" in result.path, result.path
    # TRIAGE EMPTY skipped PRIORITIZE + IMPLEMENT.
    assert "IMPLEMENT" not in result.path, result.path
    assert "PRIORITIZE" not in result.path, result.path
    # The skipped IMPLEMENT's product persists EMPTY (its seeded default).
    doc = ds.DurableState(state_path).load()
    assert doc.get("handoffs", []) == [], doc


# --------------------------------------------------------------------------
# Test 3 — a state that DOES run overwrites its seeded empty (unchanged).
# Full close route, VERIFY finds no open PRs -> verdicts EMPTY but INTEGRATE
# still runs (VERIFY EMPTY -> INTEGRATE) and writes integration_result; the
# seeded empty is overwritten by the real product.
# --------------------------------------------------------------------------

def test_running_state_overwrites_seeded_empty():
    project_dir = tempfile.mkdtemp(prefix="sched-overwrite-")
    # The full close-the-loop route (VERIFY EMPTY -> INTEGRATE, not skipped).
    full_route = {
        "schema_version": "1.0.0",
        "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                   "IMPLEMENT", "VERIFY", "INTEGRATE", "CLEANUP", "PERSIST",
                   "EXIT", "DONE", "HALTED"],
        "edges": [
            {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
            {"state": "DRAIN", "signal": "OK", "next": "PULL"},
            {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
            {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
            {"state": "TRIAGE", "signal": "OK", "next": "PRIORITIZE"},
            {"state": "TRIAGE", "signal": "EMPTY", "next": "PRIORITIZE"},
            {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
            {"state": "PRIORITIZE", "signal": "EMPTY", "next": "IMPLEMENT"},
            {"state": "IMPLEMENT", "signal": "OK", "next": "VERIFY"},
            {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "VERIFY"},
            {"state": "VERIFY", "signal": "OK", "next": "INTEGRATE"},
            {"state": "VERIFY", "signal": "EMPTY", "next": "INTEGRATE"},
            {"state": "INTEGRATE", "signal": "OK", "next": "CLEANUP"},
            {"state": "CLEANUP", "signal": "OK", "next": "PERSIST"},
            {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
            {"state": "EXIT", "signal": "idle", "next": "DONE"},
            {"state": "EXIT", "signal": "refire", "next": "DONE"},
            {"state": "EXIT", "signal": "break", "next": "DONE"},
            {"state": "EXIT", "signal": "halt", "next": "DONE"},
        ],
        "terminal": ["DONE", "HALTED"],
    }
    restore = _patch_no_open_prs()
    result, state_path = _run(project_dir, full_route, _stub_source(),
                              restore=restore)
    assert result.final_state == "DONE", result.path
    # INTEGRATE ran (VERIFY EMPTY -> INTEGRATE) and wrote integration_result —
    # the real (still-empty-content) product, proving the run path is intact.
    assert "INTEGRATE" in result.path, result.path
    doc = ds.DurableState(state_path).load()
    ir = doc.get("integration_result", {})
    assert ir.get("schema_version") == vi.INTEGRATION_RESULT_SCHEMA_VERSION, ir
