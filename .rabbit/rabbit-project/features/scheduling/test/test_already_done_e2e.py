#!/usr/bin/env python3
"""End-to-end conformance tests for IMPLEMENT `already_done` -> terminal-resolved
skip (convergence, §3.5.3 extension).

When run_tick collects an acting-state (IMPLEMENT) handoff whose `status` is
`already_done` (implement's v1.2.0 terminal already-satisfied outcome — the fix
is already on `main`), it records
`triage_memory[work_item_id] = {updated_at, status: "already_done"}` so the NEXT
tick's skip-unchanged filter excludes the unchanged item — the grind stops on the
FIRST detection. An `already_done` handoff is TERMINAL, NOT a retryable block:
run_tick does NOT increment the backoff ledger for it (contrast a genuine
`blocked` handoff, which still increments backoff, §3.8.5), and it is NOT recorded
in the acted-ledger (it opened no PR). `already_done` is in the single
skip-unchanged status set both `_filter_triage_work_items` (TRIAGE dispatch) and
`_work_remains` (refire) consume.

Behaviours covered:
  1. an `already_done` IMPLEMENT handoff records triage_memory status
     'already_done' with the item's current (unchanged) updated_at.
  2. it does NOT increment the backoff ledger.
  3. it does NOT record an acted-ledger entry.
  4. the next tick's _filter_triage_work_items excludes the unchanged item.
  5. _work_remains returns False for an already_done-unchanged pool.
  6. a genuine `blocked` handoff still increments backoff (regression).
  7. `already_done` is in the skip-unchanged status set.

scheduling CONSUMES its sibling features UNCHANGED via sys.path; edits live ONLY
in scheduling (run_tick.py).

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

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)

_UPDATED_7 = "2026-05-02T11:30:00Z"
_UPDATED_8 = "2026-05-03T09:00:00Z"


def _gh_fixture(updated_7=_UPDATED_7, with_8=False):
    issues = [
        {
            "number": 7, "title": "Crash on empty config", "body": "...",
            "url": "https://github.com/acme/widget/issues/7", "state": "OPEN",
            "labels": [{"name": "bug"}], "author": {"login": "octocat"},
            "createdAt": "2026-05-01T10:00:00Z", "updatedAt": updated_7,
        },
    ]
    if with_8:
        issues.append({
            "number": 8, "title": "Add a flag", "body": "...",
            "url": "https://github.com/acme/widget/issues/8", "state": "OPEN",
            "labels": [{"name": "enhancement"}], "author": {"login": "octocat"},
            "createdAt": "2026-05-03T08:00:00Z", "updatedAt": _UPDATED_8,
        })
    return json.dumps(issues)


def _stub_source(json_text=None):
    items = wi.parse_gh_issues(json_text or _gh_fixture())

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "triage-doer", "inputs": ["work_items"],
            "writes": "work_orders", "cardinality": "once",
            "task": "Triage the work_items into accepted work_orders.",
        }
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
        {
            "subagent_type": "implement-doer", "inputs": ["execution_plan"],
            "writes": "handoffs",
            "cardinality": {"per_item": "execution_plan.ordered"},
            "task": "Implement one work_order.", "effect": "implement",
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


def _write_cfg(project_dir, name, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(payload, f)


def _setup_agent_project(mode="propose", backoff_threshold=3):
    project_dir = tempfile.mkdtemp(prefix="sched-alreadydone-")
    _write_cfg(project_dir, "route.json", _AGENT_ROUTE)
    _write_cfg(project_dir, "adapter-map.json", _agent_map())
    _write_cfg(project_dir, "governance.json",
               {"mode": mode, "backoff": {"threshold": backoff_threshold}})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


_CANNED_WORK_ORDERS_7 = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def _handoff(work_order_id, status="already_done", ref="abc123",
             blocked_reason=None):
    if status == "already_done":
        artifact = {"kind": "already-on-main", "ref": ref}
    elif status in ("opened", "closed"):
        artifact = {"kind": "pr", "ref": ref}
    else:
        artifact = {"kind": "none", "ref": None}
    return json.dumps({
        "schema_version": "1.2.0", "work_order_id": work_order_id,
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
    with open(dispatch["prompt_path"]) as f:
        return f.read()


def _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                   now=_DAY1, source=None):
    src = source or _stub_source()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=now)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS_7])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=now, resume=True)


class _AlreadyDoneSink:
    def __init__(self):
        self.calls = []

    def __call__(self, issue_ref, repo=None, reason="", **kwargs):
        self.calls.append((issue_ref, repo, reason))


def _implement_once(project_dir, runtime_dir, state_path, journal_path,
                    status="already_done", now=_DAY1, source=None,
                    blocked_reason=None, ref="abc123", already_done_sink=None):
    """Run ONE full tick: TRIAGE -> IMPLEMENT -> resume with the given handoff
    status for #7 -> DONE. Returns the final signal."""
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=now, source=source)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", paused
    _write_outputs(paused, [_handoff(paused["dispatches"][0]["item"],
                                     status=status, ref=ref,
                                     blocked_reason=blocked_reason)])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=source or _stub_source(), now=now, resume=True,
                       already_done_sink=already_done_sink)


# ==========================================================================
# Behaviour 7 — already_done is in the skip-unchanged status set.
# ==========================================================================

def test_already_done_in_triage_skip_statuses():
    assert "already_done" in rt._TRIAGE_SKIP_STATUSES


# ==========================================================================
# Behaviour 1 — already_done records triage_memory status 'already_done'.
# ==========================================================================

def test_already_done_records_triage_memory():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    signal = _implement_once(project_dir, runtime_dir, state_path, journal_path,
                             status="already_done")
    assert signal in ("idle", "refire"), signal
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "already_done", mem
    assert mem["acme/widget#7"]["updated_at"] == _UPDATED_7, mem


# ==========================================================================
# Behaviour 2 — already_done does NOT increment the backoff ledger.
# ==========================================================================

def test_already_done_does_not_increment_backoff():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _implement_once(project_dir, runtime_dir, state_path, journal_path,
                    status="already_done")
    backoff = rt.persisted_backoff_ledger(state_path)
    # No backoff entry (or a zero count) for #7 — it is terminal, not retryable.
    assert backoff.get("acme/widget#7", {}).get("blocked_count", 0) == 0, backoff


# ==========================================================================
# Behaviour 3 — already_done does NOT record an acted-ledger entry.
# ==========================================================================

def test_already_done_not_in_acted_ledger():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _implement_once(project_dir, runtime_dir, state_path, journal_path,
                    status="already_done")
    ledger = rt.persisted_acted_ledger(state_path)
    assert "wo-acme/widget#7" not in ledger, ledger


# ==========================================================================
# Behaviour 4 — the next tick's _filter_triage_work_items excludes the
# already_done-unchanged item.
# ==========================================================================

def test_already_done_unchanged_filtered_next_tick():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    src = _stub_source(_gh_fixture(with_8=True))
    # Tick 1: TRIAGE #7 -> IMPLEMENT already_done for #7.
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS_7])
    impl = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=_DAY1, resume=True)
    assert impl["state"] == "IMPLEMENT", impl
    _write_outputs(impl, [_handoff(impl["dispatches"][0]["item"],
                                   status="already_done")])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=src, now=_DAY1, resume=True)
    assert rt.persisted_triage_memory(state_path).get(
        "acme/widget#7", {}).get("status") == "already_done"

    # Tick 2: #7 is already_done-and-unchanged -> TRIAGE dispatch fed ONLY #8.
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=src, now=_DAY1)
    assert paused2["state"] == "TRIAGE", paused2
    prompt = _prompt_text(paused2["dispatches"][0])
    assert "acme/widget#8" in prompt, prompt
    assert "acme/widget#7" not in prompt, prompt


def test_filter_triage_work_items_drops_already_done_unchanged():
    items = [
        {"id": "acme/widget#7", "updated_at": _UPDATED_7},
        {"id": "acme/widget#8", "updated_at": _UPDATED_8},
    ]
    memory = {"acme/widget#7": {"status": "already_done",
                                "updated_at": _UPDATED_7}}
    kept = rt._filter_triage_work_items(items, memory)
    assert [it["id"] for it in kept] == ["acme/widget#8"], kept

    # An already_done item whose updated_at ADVANCED is re-triaged.
    changed = {"id": "acme/widget#7", "updated_at": "2026-06-01T00:00:00Z"}
    kept2 = rt._filter_triage_work_items([changed], memory)
    assert [it["id"] for it in kept2] == ["acme/widget#7"], kept2


# ==========================================================================
# Behaviour 5 — _work_remains excludes an already_done-unchanged pool.
# ==========================================================================

def test_work_remains_false_for_already_done_unchanged():
    items = [{"id": "acme/widget#7", "number": 7, "state": "OPEN",
              "title": "x", "body": "...",
              "url": "https://github.com/acme/widget/issues/7",
              "author": "octocat", "updated_at": _UPDATED_7,
              "created_at": "2026-05-01T10:00:00Z", "labels": []}]
    memory = {"acme/widget#7": {"status": "already_done",
                                "updated_at": _UPDATED_7}}
    assert rt._work_remains(items, memory, {}, 5, _DAY1) is False
    # A NEW (not-remembered) item still refires.
    assert rt._work_remains(items, {}, {}, 5, _DAY1) is True


# ==========================================================================
# Behaviour 6 — a genuine `blocked` handoff still increments backoff.
# ==========================================================================

def test_blocked_still_increments_backoff():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _implement_once(project_dir, runtime_dir, state_path, journal_path,
                    status="blocked", blocked_reason="dep missing")
    backoff = rt.persisted_backoff_ledger(state_path)
    assert backoff.get("acme/widget#7", {}).get("blocked_count") == 1, backoff


# ==========================================================================
# already_done ON-ISSUE enactment + strong-reason guard.
# ==========================================================================

def test_already_done_enacted_at_propose():
    """An already_done handoff with a valid evidence commit at propose calls the
    injected already-done sink with a reason naming the commit AND records
    triage_memory already_done (issue left OPEN — the sink never closes)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    sink = _AlreadyDoneSink()
    signal = _implement_once(project_dir, runtime_dir, state_path, journal_path,
                             status="already_done", ref="deadbeef",
                             already_done_sink=sink)
    assert signal in ("idle", "refire"), signal
    assert len(sink.calls) == 1, sink.calls
    issue_ref, _repo, reason = sink.calls[0]
    assert issue_ref == "https://github.com/acme/widget/issues/7", issue_ref
    assert "deadbeef" in reason, reason
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "already_done", mem


def test_already_done_dry_run_no_sink_but_records():
    """At dry-run the file effect is not permitted: the enactment logs the intent
    and does NOT call the sink, but the durable triage_memory already_done skip is
    still recorded (the on-issue write is gated; the convergence skip is not).

    Exercised directly against _record_triage_memory because the dry-run route's
    acting IMPLEMENT is inert (never dispatches), so no already_done handoff can
    flow through the full route at dry-run."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="dry-run")
    sink = _AlreadyDoneSink()
    handoffs = [{"work_order_id": "wo-acme/widget#7", "status": "already_done",
                 "artifact": {"kind": "already-on-main", "ref": "abc123"}}]
    wo_to_wi = {"wo-acme/widget#7": "acme/widget#7"}
    wi_updated_at = {"acme/widget#7": _UPDATED_7}
    work_items = [{"id": "acme/widget#7", "updated_at": _UPDATED_7,
                   "url": "https://github.com/acme/widget/issues/7"}]
    rt._record_triage_memory(state_path, handoffs, wo_to_wi, wi_updated_at,
                             work_items, "dry-run", sink)
    assert sink.calls == [], sink.calls
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "already_done", mem


def test_already_done_no_evidence_not_enacted_not_recorded():
    """An already_done handoff with NO artifact.ref evidence yields no strong
    reason: the enactment does NOT call the sink and does NOT record already_done,
    so the item re-works next tick."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    sink = _AlreadyDoneSink()
    _implement_once(project_dir, runtime_dir, state_path, journal_path,
                    status="already_done", ref=None, already_done_sink=sink)
    assert sink.calls == [], sink.calls
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") != "already_done", mem
