#!/usr/bin/env python3
"""End-to-end conformance tests for immediate-refire (§3.3.3).

Owner-requested enhancement: the loop should run the NEXT tick IMMEDIATELY when
actionable work remains, instead of waiting the heartbeat interval. This cycle
completes the refire mechanism in scheduling (run_tick.py + the tick executor
skill); it consumes work-intake (Triage) + lifecycle-dispositions (Exit) +
durable-state UNCHANGED. Edits live ONLY in scheduling.

Behaviours covered:

  1. Pure POOL predicate
     `_work_remains(work_items, triage_memory, backoff_ledger, threshold, now)`:
       - a NEW (not-in-memory) TRIAGE-acceptable non-blocked item -> True
       - all items done/deferred-AND-unchanged (filtered by triage_memory) -> False
       - a blocked item at/above threshold, unchanged                -> False
       - a blocked item whose updated_at advanced past the pin        -> True
       - an empty pool / only invalid (closed/stale/malformed) items  -> False
     The predicate reuses the §3.5.3 skip-filter (_filter_triage_work_items) so a
     done/deferred-unchanged or persistently-rejected (closed) item never refires.
  2. The EXIT wrapper (make_exit): when the inner outcome is `empty` AND the
     route has an acting agent AND the POOL still holds workable work, it rewrites
     the outcome to `work-remains` so EXIT emits `refire`; otherwise it emits
     `idle`; it NEVER overrides a non-`empty` outcome (restart/fault).
  3. run_tick script path: an acting route that reaches EXIT with a pool-workable
     issue (new/changed, classify-valid, non-blocked) returns `refire`; a route
     whose pool is all done-unchanged returns `idle`. The read-and-idle DEFAULT
     route + any empty-pool tick still IDLE (back-compat).
  4. The shipped tick executor skill documents the refire-loop (run another tick
     immediately on a `refire` final signal, looping until a non-refire signal).

scheduling CONSUMES work-intake + lifecycle-dispositions + durable-state +
adapter-wiring + the loop-core features UNCHANGED via sys.path; it does NOT edit
or fork them.

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
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


# A reference time within the work-intake STALE_WINDOW_DAYS of both fixture
# issues (updated 2026-05-02/03), so the deterministic TRIAGE gate accepts them.
_NOW = datetime(2026, 5, 10, 9, 0, 0, tzinfo=timezone.utc)
# A reference time far past the stale window so every fixture item is STALE
# (rejected by the gate) -> no actionable work.
_NOW_STALE = datetime(2030, 1, 1, 9, 0, 0, tzinfo=timezone.utc)


def _item(number, title="Fix a thing", state="OPEN",
          updated_at="2026-05-03T08:00:00Z"):
    """A WorkItem dict (the work_items slot shape) for one issue."""
    return wi.WorkItem(
        id=f"acme/widget#{number}", number=number, title=title, body="",
        url=f"https://github.com/acme/widget/issues/{number}", state=state,
        labels=[], author="x", created_at="2026-05-01T10:00:00Z",
        updated_at=updated_at).to_dict()


# ==========================================================================
# Behaviour 1 — the pure POOL predicate _work_remains (triage-memory-aware).
# ==========================================================================

def test_work_remains_true_for_new_non_blocked_acceptable_item():
    """A NEW (not-in-memory) classify-valid non-blocked item is workable -> True.
    Empty triage_memory keeps every item a candidate (§3.5.3 skip-filter no-op)."""
    items = [_item(7)]
    assert rt._work_remains(items, {}, {}, threshold=5, now=_NOW) is True


def test_work_remains_false_when_all_done_unchanged():
    """tick-26 case (#282/#275/#212 all done-unchanged): every item is `done` in
    triage_memory at the SAME updated_at -> filtered out by the §3.5.3 skip-filter
    -> no candidates -> idle (no refire)."""
    items = [_item(282), _item(275), _item(212)]
    memory = {it["id"]: {"status": "done", "updated_at": it["updated_at"]}
              for it in items}
    assert rt._work_remains(items, memory, {}, threshold=5, now=_NOW) is False


def test_work_remains_true_for_changed_done_item():
    """A `done` item whose updated_at ADVANCED is NOT filtered (a human touched it)
    -> it re-enters the candidate set -> workable -> True."""
    items = [_item(7, updated_at="2026-05-09T08:00:00Z")]
    memory = {"acme/widget#7": {"status": "done",
                                "updated_at": "2026-05-03T08:00:00Z"}}
    assert rt._work_remains(items, memory, {}, threshold=5, now=_NOW) is True


def test_work_remains_false_for_blocked_unchanged_item():
    """A blocked item at threshold whose updated_at == the deferral pin is
    deferred-and-unchanged -> inert -> NOT actionable."""
    items = [_item(7, updated_at="2026-05-03T08:00:00Z")]
    backoff = {"acme/widget#7": {"blocked_count": 5,
                                 "deferred_at_updated_at": "2026-05-03T08:00:00Z"}}
    assert rt._work_remains(items, {}, backoff, threshold=5, now=_NOW) is False


def test_work_remains_true_for_blocked_but_updated_item():
    """A blocked item whose updated_at ADVANCED past the deferral pin re-enters
    -> actionable -> True (a human touched it)."""
    items = [_item(7, updated_at="2026-05-09T08:00:00Z")]
    backoff = {"acme/widget#7": {"blocked_count": 5,
                                 "deferred_at_updated_at": "2026-05-03T08:00:00Z"}}
    assert rt._work_remains(items, {}, backoff, threshold=5, now=_NOW) is True


def test_work_remains_false_below_threshold_is_still_actionable():
    """A blocked item BELOW the threshold is not deferred -> still retryable ->
    actionable (True). Only a deferred-and-unchanged item is inert."""
    items = [_item(7)]
    backoff = {"acme/widget#7": {"blocked_count": 2,
                                 "deferred_at_updated_at": None}}
    assert rt._work_remains(items, {}, backoff, threshold=5, now=_NOW) is True


def test_work_remains_false_for_empty_pool():
    assert rt._work_remains([], {}, {}, threshold=5, now=_NOW) is False
    assert rt._work_remains(None, {}, {}, threshold=5, now=_NOW) is False


def test_work_remains_false_for_invalid_items():
    """Invalid (closed / stale / malformed) items never count."""
    closed = [_item(7, state="CLOSED")]
    assert rt._work_remains(closed, {}, {}, threshold=5, now=_NOW) is False
    malformed = [_item(7, title="")]
    assert rt._work_remains(malformed, {}, {}, threshold=5, now=_NOW) is False
    stale = [_item(7)]
    assert rt._work_remains(stale, {}, {}, threshold=5, now=_NOW_STALE) is False


# ==========================================================================
# Behaviour 2 — the EXIT wrapper (make_exit) refire/idle + non-override.
# ==========================================================================

def _exit_ctx(state_path, work_items, outcome="empty"):
    """A minimal TickContext with the slots make_exit's POOL EXIT run reads:
    tick_outcome, work_items, state_path. The pool predicate keys off the
    work_items + the durable triage_memory/backoff at state_path — NOT on
    committed work_orders (the old committed-work gate was removed)."""
    ctx = fc.TickContext()
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot(wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
                      version=wi.WORK_ITEMS_SLOT["version"])
    ctx.write("tick_outcome", outcome)
    ctx.write("state_path", state_path)
    ctx.write(wi.WORK_ITEMS_SLOT["name"], work_items)
    return ctx


def _save_triage_memory(state_path, memory):
    """Persist a triage_memory under TRIAGE_MEMORY_KEY so make_exit's pool filter
    drops the done/deferred-unchanged items (§3.5.3)."""
    doc = {"schema_version": ds.SCHEMA_VERSION, "counter": 0,
           rt.TRIAGE_MEMORY_KEY: memory}
    ds.DurableState(state_path).save(doc)


def _exit_runtime(runtime_dir, acting=True, now=_NOW):
    return {"runtime_dir": runtime_dir, "now": now, "governance": {},
            "has_acting_agent": acting}


def test_make_exit_emits_refire_when_actionable_and_acting():
    root = tempfile.mkdtemp(prefix="sched-refire-exit-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    ctx = _exit_ctx(state_path, [_item(7)])
    result = run(ctx)
    assert result.signal == "refire", result.signal
    assert ctx.read("tick_outcome") == "work-remains"
    assert ld.read_disposition(runtime_dir) == ld.Disposition.RUNNING


def test_make_exit_emits_idle_when_no_actionable_work():
    root = tempfile.mkdtemp(prefix="sched-refire-exit-idle-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    ctx = _exit_ctx(state_path, [])  # empty pool -> no actionable work
    result = run(ctx)
    assert result.signal == "idle", result.signal
    assert ctx.read("tick_outcome") == "empty"


def test_make_exit_idle_when_pool_all_done_unchanged():
    """When every pooled work_item is `done`-AND-unchanged in triage_memory, the
    §3.5.3 skip-filter drops them all -> no pool-workable item -> idle (the
    tick-26 all-done case)."""
    root = tempfile.mkdtemp(prefix="sched-refire-exit-done-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    item = _item(7)
    _save_triage_memory(state_path, {
        item["id"]: {"status": "done", "updated_at": item["updated_at"]}})
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    ctx = _exit_ctx(state_path, [item])
    result = run(ctx)
    assert result.signal == "idle", result.signal
    assert ctx.read("tick_outcome") == "empty"


def test_make_exit_refires_on_pool_workable_item_no_committed_order():
    """POOL semantics: an acting route refires when the pool holds a classify-valid
    non-blocked item, EVEN with no committed work_order this tick (the old
    committed-work gate was removed — the refire keys on the next tick's TRIAGE
    having work to judge)."""
    root = tempfile.mkdtemp(prefix="sched-refire-exit-pool-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    ctx = _exit_ctx(state_path, [_item(7)])
    result = run(ctx)
    assert result.signal == "refire", result.signal
    assert ctx.read("tick_outcome") == "work-remains"


def test_make_exit_does_not_refire_without_acting_agent():
    """The read-and-idle spine (no acting agent) keeps IDLING even with
    acceptable work_items present (back-compat)."""
    root = tempfile.mkdtemp(prefix="sched-refire-exit-noact-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=False))
    ctx = _exit_ctx(state_path, [_item(7)])
    result = run(ctx)
    assert result.signal == "idle", result.signal
    assert ctx.read("tick_outcome") == "empty"


def test_make_exit_never_overrides_non_empty_outcome():
    """A non-`empty` outcome (restart/fault) is delegated to the inner EXIT
    UNCHANGED — the refire override applies ONLY to the `empty` outcome."""
    root = tempfile.mkdtemp(prefix="sched-refire-exit-nonempty-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    # `restart` -> break, even though actionable work is present.
    ctx = _exit_ctx(state_path, [_item(7)], outcome="restart")
    result = run(ctx)
    assert result.signal == "break", result.signal
    assert ctx.read("tick_outcome") == "restart"


def test_make_exit_manifest_reads_work_items_and_state_path():
    root = tempfile.mkdtemp(prefix="sched-refire-exit-manifest-")
    manifest, _run = rt.make_exit(_exit_runtime(os.path.join(root, "rt")))
    assert "tick_outcome" in manifest.reads
    assert wi.WORK_ITEMS_SLOT["name"] in manifest.reads
    assert "state_path" in manifest.reads
    assert list(manifest.writes) == ["tick_outcome"], manifest.writes
    for sig in ("refire", "idle", "break", "halt"):
        assert sig in manifest.emits, manifest.emits


def test_make_exit_respects_backoff_deferred_unchanged():
    """When the only acceptable item is deferred-and-unchanged, the acting route
    still IDLES (no actionable work)."""
    root = tempfile.mkdtemp(prefix="sched-refire-exit-backoff-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    os.makedirs(runtime_dir, exist_ok=True)
    doc = {"schema_version": ds.SCHEMA_VERSION, "counter": 0,
           rt.BACKOFF_LEDGER_KEY: {
               "acme/widget#7": {"blocked_count": 5,
                                 "deferred_at_updated_at": "2026-05-03T08:00:00Z"}}}
    ds.DurableState(state_path).save(doc)
    _manifest, run = rt.make_exit(_exit_runtime(runtime_dir, acting=True))
    ctx = _exit_ctx(state_path, [_item(7, updated_at="2026-05-03T08:00:00Z")])
    result = run(ctx)
    assert result.signal == "idle", result.signal


# ==========================================================================
# Behaviour 3 — run_tick script path: refire when actionable work remains.
# ==========================================================================

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

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


# An agent TRIAGE that the canned resume drives, plus an ACTING IMPLEMENT agent.
_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {"subagent_type": "triage-doer", "inputs": ["work_items"],
         "writes": "work_orders", "cardinality": "once",
         "task": "Triage the work_items into accepted work_orders."}
    ],
    "signal": {"rule": "nonempty_else_empty"},
}

_PLANNED_HANDOFF_EXAMPLE = {
    "work_order_id": None, "status": "planned",
    "artifact": {"kind": "none", "ref": None},
    "discovered_work": [], "blocked_reason": None,
}

_IMPLEMENT_ACTING_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["execution_plan"], "writes": ["handoffs"],
                 "emits": ["OK", "BLOCKED"]},
    "dispatch": [
        {"subagent_type": "implement-doer", "inputs": ["execution_plan"],
         "writes": "handoffs",
         "cardinality": {"per_item": "execution_plan.ordered"},
         "task": "Implement one work_order.", "effect": "implement",
         "description": "implement a work order",
         "output_example": _PLANNED_HANDOFF_EXAMPLE}
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


def _setup_agent_project():
    project_dir = tempfile.mkdtemp(prefix="sched-refire-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_AGENT_ROUTE, f)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(_agent_map(), f)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": "propose"}, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, cfg, state_path, journal_path


# A canned TRIAGE output accepting BOTH work_items as work_orders that share the
# SAME target feature (`feature:widget` label). PRIORITIZE serializes them (#214):
# it fans out at most ONE per feature per tick, DEFERRING the other — so IMPLEMENT
# acts on wo-#7 and wo-#9 stays committed-but-un-handled backlog -> refire.
_CANNED_TWO_SAME_FEATURE = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": ["feature:widget"], "decision": "accepted",
     "reason": "", "created_at": ""},
    {"schema_version": "1.0.0", "id": "wo-acme/widget#9",
     "work_item_id": "acme/widget#9", "title": "Add retry knob",
     "body": "", "url": "", "labels": ["feature:widget"], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def _canned_handoff(work_order_id):
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": "opened", "artifact": {"kind": "pr", "ref": "PR#1"},
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


def test_run_tick_refires_when_pool_still_has_workable_issue():
    """HEADLINE (POOL semantics): two work_orders share a feature, so PRIORITIZE
    defers one (#214); the acting route opens a PR for wo-#7 (recorded `done` in
    triage_memory) and leaves #9 un-handled. At EXIT the pool still holds #9 — a
    classify-valid, non-blocked, not-yet-done issue the §3.5.3 skip-filter KEEPS —
    so run_tick returns `refire`, running the next tick immediately instead of
    waiting the heartbeat. This needs NO committed work_order at EXIT: the refire
    keys on the next tick's TRIAGE having work to judge/act on."""
    project_dir, cfg, state_path, journal_path = _setup_agent_project()

    # Step 1: TRIAGE (agent) pauses; canned output accepts BOTH (same feature).
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_NOW)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_TWO_SAME_FEATURE])

    # Step 2: resume -> PRIORITIZE serializes the same-feature orders (defers
    # wo-#9) -> IMPLEMENT (acting) pauses to dispatch ONLY wo-#7.
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), now=_NOW, resume=True)
    assert paused2["status"] == "paused" and paused2["state"] == "IMPLEMENT", \
        paused2
    assert len(paused2["dispatches"]) == 1, paused2
    assert paused2["dispatches"][0]["item"] == "wo-acme/widget#7", paused2

    # Step 3: the implement-doer opens the PR for #7; resume drives to EXIT.
    _write_outputs(paused2, [_canned_handoff("wo-acme/widget#7")])
    signal = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_NOW, resume=True)
    # #7 is now `done` (filtered out); #9 was deferred (never handled this tick)
    # so it is NOT in triage_memory -> the pool still holds a workable issue for
    # next tick's TRIAGE -> refire.
    assert signal == "refire", signal
    assert ld.read_disposition(cfg) == ld.Disposition.RUNNING


# Canned TRIAGE accepting BOTH work_items as DISTINCT-feature work_orders, so
# PRIORITIZE keeps both and IMPLEMENT dispatches both (no deferral).
_CANNED_TWO_DISTINCT_FEATURE = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": ["feature:alpha"], "decision": "accepted",
     "reason": "", "created_at": ""},
    {"schema_version": "1.0.0", "id": "wo-acme/widget#9",
     "work_item_id": "acme/widget#9", "title": "Add retry knob",
     "body": "", "url": "", "labels": ["feature:beta"], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def test_run_tick_idles_when_pool_all_done_unchanged():
    """The SAME acting route where every issue is acted+opened this tick (distinct
    features, both dispatched) -> both recorded `done` in triage_memory at their
    current updated_at -> the §3.5.3 skip-filter drops the whole pool -> no
    pool-workable item -> EXIT idles (no refire)."""
    project_dir, cfg, state_path, journal_path = _setup_agent_project()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_NOW)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_TWO_DISTINCT_FEATURE])
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), now=_NOW, resume=True)
    assert paused2["status"] == "paused" and paused2["state"] == "IMPLEMENT", \
        paused2
    assert len(paused2["dispatches"]) == 2, paused2
    _write_outputs(paused2, [_canned_handoff(d["item"])
                             for d in paused2["dispatches"]])
    signal = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_NOW, resume=True)
    # Both issues acted (opened) -> both `done`-unchanged in triage_memory ->
    # the pool is fully filtered -> idle.
    assert signal == "idle", signal
    assert ld.read_disposition(cfg) == ld.Disposition.IDLE


# ==========================================================================
# Back-compat: the DEFAULT read-and-idle route + an empty-pool tick still IDLE.
# ==========================================================================

def _paths():
    root = tempfile.mkdtemp(prefix="sched-refire-default-")
    return (os.path.join(root, "runtime"), os.path.join(root, "state.json"),
            os.path.join(root, "journal.jsonl"))


def test_default_route_with_acceptable_work_still_idles():
    """Back-compat: the read-and-idle DEFAULT route (no acting agent) idles even
    with acceptable work_items pulled."""
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source(),
                         now=_NOW)
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 2


def test_empty_pool_tick_still_idles():
    """Back-compat: an empty pool idles regardless of route."""
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source("[]"),
                         now=_NOW)
    assert signal == "idle", signal


# ==========================================================================
# Behaviour 4 — /tick runs EXACTLY ONE tick (no auto-loop on refire); /start
# OWNS the drain-loop (§3.3.3). The DRIVER of the repeat moved from /tick to
# /start; EXIT's POOL-based refire emission is UNCHANGED (Behaviours 1-3).
# ==========================================================================

def _ship_skill(name):
    return os.path.join(_FEATURE_DIR, "ship", "skills", name, "SKILL.md")


def _skill_frontmatter_description(name):
    """The `description:` value from the SKILL.md YAML frontmatter block."""
    text = open(_ship_skill(name)).read()
    assert text.startswith("---\n"), name
    block = text[4:text.index("\n---\n", 4)]
    for line in block.splitlines():
        if line.startswith("description:"):
            return line.partition(":")[2].strip()
    raise AssertionError(("no description frontmatter", name))


def test_tick_skill_documents_exactly_one_tick_no_refire_loop():
    """The tick executor skill must document that it runs EXACTLY ONE tick and
    STOPS at the terminal — a `refire` final signal is REPORTED (work remains)
    but the executor does NOT auto-loop into another tick. One /tick = one tick,
    even when the pool is not drained."""
    body = open(_ship_skill("tick")).read()
    lowered = body.lower()
    # refire is still REPORTED (the caller must know work remains).
    assert "refire" in lowered, "tick skill must still report the refire signal"
    # It runs EXACTLY ONE tick and stops.
    assert "exactly one tick" in lowered, \
        "tick skill must state it runs exactly one tick"
    assert "stop" in lowered, "tick skill must state it STOPS at the terminal"
    # It must NOT auto-loop on refire (the old behaviour is removed).
    assert "does not" in lowered and "another tick" in lowered, \
        "tick skill must state it does NOT begin another tick on refire"
    # The retired auto-loop wording must be gone.
    assert "looping until a non-refire" not in lowered, \
        "tick skill must not carry the retired refire auto-loop wording"


def test_tick_skill_frontmatter_description_has_no_auto_loop_language():
    """The tick SKILL frontmatter description must no longer claim it loops on
    refire / drains its backlog — that ownership moved to /start."""
    desc = _skill_frontmatter_description("tick").lower()
    assert "drains its backlog" not in desc, desc
    assert "looping until a non-refire" not in desc, desc


def test_start_skill_owns_the_drain_loop():
    """/start OWNS the drain-loop: after tick #1, and on each heartbeat, when a
    completed tick's final signal is `refire` it fires /auto-maintainer:tick
    AGAIN immediately, repeating until a non-refire signal — so /start drains
    the backlog without waiting for the heartbeat interval."""
    body = open(_ship_skill("start")).read()
    lowered = body.lower()
    assert "refire" in lowered, "start skill must reference the refire signal"
    assert "/auto-maintainer:tick" in body, \
        "start skill must drive ticks via the /auto-maintainer:tick executor"
    # It fires again on refire, repeating until non-refire.
    assert "again" in lowered, \
        "start skill must fire another tick again on refire"
    assert ("until" in lowered and ("non-refire" in lowered
            or "not refire" in lowered)), \
        "start skill must repeat until a non-refire signal"
    assert "drain" in lowered, "start skill must state it drains the backlog"


def test_start_heartbeat_prompt_runs_the_drain_loop():
    """The scheduled heartbeat PROMPT must run the SAME drain-loop (tick, and on
    a refire signal tick again until non-refire) — NOT the old single-tick
    'Run one auto-maintainer tick now' prompt."""
    body = open(_ship_skill("start")).read()
    lowered = body.lower()
    # The retired single-tick heartbeat prompt must be gone.
    assert "run one auto-maintainer tick now" not in lowered, \
        "start skill heartbeat prompt must no longer be the single-tick prompt"
    # The heartbeat prompt drains on refire.
    assert "refire" in lowered and "again" in lowered, \
        "start heartbeat prompt must drain-loop on refire"
