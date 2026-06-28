#!/usr/bin/env python3
"""End-to-end conformance tests for skip-unchanged re-triage (§3.5.3).

The triager re-judges every open issue every tick — wasteful when an issue is
already handled and unchanged. This cycle adds a durable triage memory so the
loop only re-triages NEW or CHANGED issues; handled-and-unchanged (done/deferred)
issues are filtered from the TRIAGE dispatch. Edits live ONLY in scheduling
(run_tick.py).

Behaviours covered:
  0. The triage-memory key + helper exist (TRIAGE_MEMORY_KEY="triage_memory";
     persisted_triage_memory default {}).
  1. Memory recorded at acting-state resume: status='done' for opened/closed,
     keyed on work_item_id with the item's current updated_at.
  2. Memory records status='deferred' for an item that became deferred this
     resume (backoff threshold reached).
  3. A done-AND-unchanged work_item is filtered from the TRIAGE dispatch (the
     triager is asked to judge fewer items).
  4. A CHANGED work_item (advanced updated_at) is NOT filtered — re-triaged.
  5. An active (accepted-not-done) item is NOT filtered (never starved).
  6. Empty triage memory filters nothing (byte-identical to before).
  7. The persisted work_items read product stays the FULL PULL set.
  8. The trace surfaces a triaged=<judged>/<pulled> token.

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
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)

_UPDATED_7_T1 = "2026-05-02T11:30:00Z"
_UPDATED_7_T2 = "2026-05-09T08:00:00Z"
_UPDATED_8 = "2026-05-03T09:00:00Z"


def _gh_fixture(updated_7=_UPDATED_7_T1, with_8=False):
    issues = [
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
    ]
    if with_8:
        issues.append({
            "number": 8,
            "title": "Add a flag",
            "body": "Feature request ...",
            "url": "https://github.com/acme/widget/issues/8",
            "state": "OPEN",
            "labels": [{"name": "enhancement"}],
            "author": {"login": "octocat"},
            "createdAt": "2026-05-03T08:00:00Z",
            "updatedAt": _UPDATED_8,
        })
    return json.dumps(issues)


def _stub_source(json_text=None):
    items = wi.parse_gh_issues(json_text or _gh_fixture())

    def source(repo=None):
        return list(items)
    return source


# --------------------------------------------------------------------------
# Agent-adapter fixtures: TRIAGE non-acting agent (reads work_items); IMPLEMENT
# acting agent (effect=implement, per_item over execution_plan.ordered).
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


def _setup_agent_project(mode="propose", backoff_threshold=3):
    project_dir = tempfile.mkdtemp(prefix="sched-skip-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    # The backoff threshold is config-driven (default 5). These triage-memory
    # tests reach the deferral via 3 blocks, so pin it to 3 here.
    _write_governance(
        project_dir,
        {"mode": mode, "backoff": {"threshold": backoff_threshold}})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# One work_order for issue #7.
_CANNED_WORK_ORDERS_7 = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])

# One work_order for issue #8 (used after #7 is filtered from TRIAGE).
_CANNED_WORK_ORDERS_8 = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#8",
     "work_item_id": "acme/widget#8", "title": "Add a flag",
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


def _prompt_text(dispatch):
    """The rendered envelope a dispatch is delivered by file reference: read the
    file at prompt_path (the inline prompt is no longer carried in the rec)."""
    with open(dispatch["prompt_path"]) as f:
        return f.read()


def _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                   now=None, source=None, work_orders=None):
    """Step TRIAGE (non-acting agent) past its pause; return the SECOND return
    (the IMPLEMENT pause / acting-state branch result)."""
    src = source or _stub_source()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=now)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [work_orders or _CANNED_WORK_ORDERS_7])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=now, resume=True)


def _complete_once(project_dir, runtime_dir, state_path, journal_path,
                   status="opened", now=_DAY1, source=None):
    """Run ONE full tick: TRIAGE -> IMPLEMENT -> resume with a completed handoff
    for #7 -> DONE. Returns the final signal."""
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=now, source=source)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_canned_handoff(paused["dispatches"][0]["item"],
                                            status=status, ref="PR#1")])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=source or _stub_source(), now=now, resume=True)


def _block_once(project_dir, runtime_dir, state_path, journal_path,
                blocked_reason="dep missing", now=_DAY1, source=None,
                escalate_sink=None):
    """Run ONE full tick that blocks #7's work order and resumes to DONE."""
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
# Behaviour 0 — the triage-memory key + helper exist.
# ==========================================================================

def test_triage_memory_key_helper_exist():
    assert rt.TRIAGE_MEMORY_KEY == "triage_memory"
    root = tempfile.mkdtemp(prefix="sched-skip-empty-")
    state_path = os.path.join(root, "state.json")
    assert rt.persisted_triage_memory(state_path) == {}


# ==========================================================================
# Behaviour 1 — at resume, a completed (opened) outcome records the work_item in
# triage memory with status='done' + the issue's current updated_at.
# ==========================================================================

def test_opened_records_done_in_triage_memory():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    signal = _complete_once(project_dir, runtime_dir, state_path, journal_path,
                            status="opened")
    assert signal == "idle", signal
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "done", mem
    assert mem["acme/widget#7"]["updated_at"] == _UPDATED_7_T1, mem


def test_closed_records_done_in_triage_memory():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _complete_once(project_dir, runtime_dir, state_path, journal_path,
                   status="closed")
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "done", mem


# ==========================================================================
# Behaviour 2 — an item that became deferred this resume (backoff threshold) is
# recorded status='deferred'.
# ==========================================================================

def test_deferred_item_records_deferred_in_triage_memory():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Block 3 times -> #7 becomes deferred on the 3rd.
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    mem = rt.persisted_triage_memory(state_path)
    # Below threshold: NOT yet deferred or done (still active, must keep being
    # triaged) — so #7 is NOT marked done/deferred yet.
    assert mem.get("acme/widget#7", {}).get("status") != "done", mem
    assert mem.get("acme/widget#7", {}).get("status") != "deferred", mem

    _block_once(project_dir, runtime_dir, state_path, journal_path)
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff["acme/widget#7"]["deferred_at_updated_at"] == _UPDATED_7_T1
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "deferred", mem
    assert mem["acme/widget#7"]["updated_at"] == _UPDATED_7_T1, mem


# ==========================================================================
# Behaviour 3 — a done-AND-unchanged work_item is filtered from the TRIAGE
# dispatch (the triager is asked to judge fewer items).
# ==========================================================================

def test_done_unchanged_item_filtered_from_triage_dispatch():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Tick 1: #7 completed (opened) -> recorded done in triage memory.
    src = _stub_source(_gh_fixture(with_8=True))
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    # TRIAGE dispatch sees BOTH issues on the first tick (empty memory).
    _write_outputs(paused, [_CANNED_WORK_ORDERS_7])
    impl_paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                              state_path=state_path, journal_path=journal_path,
                              source=src, now=_DAY1, resume=True)
    assert impl_paused["state"] == "IMPLEMENT", impl_paused
    _write_outputs(impl_paused, [_canned_handoff(
        impl_paused["dispatches"][0]["item"], status="opened", ref="PR#1")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=src, now=_DAY1, resume=True)
    assert rt.persisted_triage_memory(state_path).get(
        "acme/widget#7", {}).get("status") == "done"

    # Tick 2: PULL re-pulls BOTH issues, but #7 is done-and-unchanged, so the
    # TRIAGE dispatch is fed ONLY #8.
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=src, now=_DAY1)
    assert paused2["state"] == "TRIAGE", paused2
    prompt = _prompt_text(paused2["dispatches"][0])
    assert "acme/widget#8" in prompt, prompt
    assert "acme/widget#7" not in prompt, prompt


# ==========================================================================
# Behaviour 4 — a CHANGED work_item (advanced updated_at) is NOT filtered.
# ==========================================================================

def test_changed_item_not_filtered():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Tick 1: #7 done at T1.
    _complete_once(project_dir, runtime_dir, state_path, journal_path,
                   status="opened")
    assert rt.persisted_triage_memory(state_path)["acme/widget#7"][
        "status"] == "done"

    # Tick 2: #7's updated_at has ADVANCED (human commented) -> re-triaged.
    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_7_T2))
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=advanced_src, now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    assert "acme/widget#7" in _prompt_text(paused["dispatches"][0]), paused


# ==========================================================================
# Behaviour 5 — an active (accepted-not-done) item is NOT filtered. A blocked
# (but not yet deferred) item stays active and keeps being triaged.
# ==========================================================================

def test_active_item_not_filtered():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Block once: #7 is blocked but below the deferral threshold -> active.
    _block_once(project_dir, runtime_dir, state_path, journal_path)
    mem = rt.persisted_triage_memory(state_path)
    # #7 not marked done/deferred -> next tick's TRIAGE still sees it.
    assert mem.get("acme/widget#7", {}).get("status") not in ("done",
                                                              "deferred"), mem
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    assert "acme/widget#7" in _prompt_text(paused["dispatches"][0]), paused


# ==========================================================================
# Behaviour 6 — empty triage memory filters nothing (first run sees all items).
# ==========================================================================

def test_empty_memory_filters_nothing():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    src = _stub_source(_gh_fixture(with_8=True))
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    prompt = _prompt_text(paused["dispatches"][0])
    assert "acme/widget#7" in prompt, prompt
    assert "acme/widget#8" in prompt, prompt


# ==========================================================================
# Behaviour 7 — the persisted work_items read product stays the FULL PULL set
# even when the TRIAGE dispatch was filtered.
# ==========================================================================

def test_persisted_work_items_full_pull_after_filter():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    src = _stub_source(_gh_fixture(with_8=True))
    # Tick 1: complete #7.
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    _write_outputs(paused, [_CANNED_WORK_ORDERS_7])
    impl = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=_DAY1, resume=True)
    _write_outputs(impl, [_canned_handoff(impl["dispatches"][0]["item"],
                                          status="opened", ref="PR#1")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=src, now=_DAY1, resume=True)

    # Tick 2: #7 filtered from TRIAGE (only #8 judged), but the persisted
    # work_items read product must STILL be the full PULL set (2), written at the
    # TERMINAL from the full pulled work_items — not the filtered TRIAGE input.
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=src, now=_DAY1)
    assert paused2["state"] == "TRIAGE", paused2
    _write_outputs(paused2, [_CANNED_WORK_ORDERS_8])
    impl2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=src, now=_DAY1, resume=True)
    assert impl2["state"] == "IMPLEMENT", impl2
    _write_outputs(impl2, [_canned_handoff(impl2["dispatches"][0]["item"],
                                           status="opened", ref="PR#2")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=src, now=_DAY1, resume=True)
    assert rt.persisted_work_items_count(state_path) == 2, \
        rt.persisted_work_items(state_path)


# ==========================================================================
# Behaviour 8 — the trace surfaces a triaged=<judged>/<pulled> token.
# ==========================================================================

def test_trace_surfaces_triaged_token():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    src = _stub_source(_gh_fixture(with_8=True))
    # Tick 1: complete #7 so tick 2's TRIAGE filters it.
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    _write_outputs(paused, [_CANNED_WORK_ORDERS_7])
    impl = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=_DAY1, resume=True)
    _write_outputs(impl, [_canned_handoff(impl["dispatches"][0]["item"],
                                          status="opened", ref="PR#1")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=src, now=_DAY1, resume=True)

    # Tick 2: TRIAGE filters #7 (only #8 judged); complete #8 so the tick reaches
    # the terminal and prints the trace with the triaged token.
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=src, now=_DAY1)
    assert paused2["state"] == "TRIAGE", paused2
    _write_outputs(paused2, [_CANNED_WORK_ORDERS_8])
    impl2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=src, now=_DAY1, resume=True)
    assert impl2["state"] == "IMPLEMENT", impl2
    _write_outputs(impl2, [_canned_handoff(impl2["dispatches"][0]["item"],
                                           status="opened", ref="PR#2")])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=src, now=_DAY1, resume=True)
    trace = buf.getvalue()
    # judged=1 (only #8), pulled=2 (the full PULL set).
    assert "triaged=1/2" in trace, trace


# ==========================================================================
# Behaviour 9 — a pure-script DEFAULT route shows triaged=<n>/<n> (no filtering)
# and carries no triage-memory key.
# ==========================================================================

def test_pure_script_route_triaged_token_and_no_memory_key():
    project_dir = tempfile.mkdtemp(prefix="sched-skip-pure-")
    _write_governance(project_dir, {"mode": "propose"})
    root = tempfile.mkdtemp(prefix="sched-skip-pure-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    src = _stub_source(_gh_fixture(with_8=True))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=src, now=_DAY1)
    assert signal == "idle", signal
    trace = buf.getvalue()
    # No TRIAGE agent-state -> nothing filtered -> triaged=2/2.
    assert "triaged=2/2" in trace, trace
    doc = ds.DurableState(state_path).load()
    assert rt.TRIAGE_MEMORY_KEY not in doc, doc
