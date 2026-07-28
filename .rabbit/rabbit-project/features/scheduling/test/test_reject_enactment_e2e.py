#!/usr/bin/env python3
"""End-to-end tests for reject-disposition enactment at TRIAGE (Wave-2 consumer).

At the TRIAGE resume, after the triager writes `work_orders`, scheduling enacts
the disposition of every `decision: rejected` order deterministically: it calls
work_intake.reject_dispositions(work_orders) and, for each, invokes
work_intake.gh_issue_reject_sink(issue_ref, repo, reason) (comment + apply
REJECTED_LABEL, NEVER close), then records
triage_memory[work_item_id] = {updated_at, status: 'rejected'} so the item is
skipped from re-triage while unchanged. The enactment is trust-gated by
sg.permits('file', mode) (same rung as REPORT filing — at dry-run the intent is
logged, not written).

Behaviours asserted end to end (TRIAGE wired as an agent that pauses/resumes; the
reject sink injected via run_tick(reject_sink=...) so no network):

  1. A rejected work order -> gh_issue_reject_sink called with the reason at
     propose, and triage_memory records status='rejected' + the issue updated_at.
  2. At dry-run (permits('file') False) the sink is NOT called and no rejected
     status is recorded (intent logged, not written).
  3. The triage skip-set includes 'rejected'; a rejected-AND-unchanged item is
     filtered from the next TRIAGE dispatch (so the second tick does NOT pause at
     TRIAGE), while a CHANGED (advanced updated_at) item is re-admitted.

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
import run_tick as rt  # noqa: E402


_UPDATED_T1 = "2026-05-02T11:30:00Z"
_UPDATED_T2 = "2026-06-10T09:00:00Z"


def _gh_fixture(updated_at=_UPDATED_T1):
    return json.dumps([{
        "number": 7,
        "title": "Crash on empty config",
        "body": "Steps to reproduce ...",
        "url": "https://github.com/acme/widget/issues/7",
        "state": "OPEN",
        "labels": [{"name": "bug"}],
        "author": {"login": "octocat"},
        "createdAt": "2026-05-01T10:00:00Z",
        "updatedAt": updated_at,
    }])


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
            "subagent_type": "triage-doer",
            "inputs": ["work_items"],
            "writes": "work_orders",
            "cardinality": "once",
            "task": "Triage the work_items into work_orders.",
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}


_TRIAGE_ROUTE = {
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


# One REJECTED work order for issue #7.
_REJECTED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "https://github.com/acme/widget/issues/7",
     "labels": [], "decision": "rejected",
     "reason": "not actionable — cannot reproduce", "created_at": ""},
])


class _RejectSink:
    def __init__(self):
        self.calls = []

    def __call__(self, issue_ref, repo=None, reason="", **kwargs):
        self.calls.append((issue_ref, repo, reason))


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


def _write_governance(project_dir, mode):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": mode}, f)


def _agent_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    return amap


def _setup(mode="propose"):
    project_dir = tempfile.mkdtemp(prefix="sched-reject-")
    _write_project_route(project_dir, _TRIAGE_ROUTE)
    _write_project_map(project_dir, _agent_map())
    _write_governance(project_dir, mode)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _tick_with_reject(project_dir, runtime_dir, state_path, journal_path,
                      reject_sink, source, work_orders=_REJECTED_WORK_ORDERS):
    """Run ONE full agent tick: pause at TRIAGE, write a rejected work order,
    resume (which enacts the reject). Returns the resume return value."""
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=source)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [work_orders])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=source, resume=True, reject_sink=reject_sink)


# ==========================================================================
# Behaviour 1 — a rejected work order is enacted at propose: sink called +
# triage_memory records rejected.
# ==========================================================================

def test_rejected_order_enacted_at_propose():
    project_dir, runtime_dir, state_path, journal_path = _setup("propose")
    sink = _RejectSink()
    src = _stub_source()
    sig = _tick_with_reject(project_dir, runtime_dir, state_path, journal_path,
                            sink, src)
    assert sig == "idle", sig
    # The reject sink was invoked with the issue ref + the reason (comment+label,
    # never close — that is the sink's contract, exercised in work-intake).
    assert len(sink.calls) == 1, sink.calls
    issue_ref, _repo, reason = sink.calls[0]
    assert issue_ref == "https://github.com/acme/widget/issues/7", issue_ref
    assert "not actionable" in reason, reason
    # triage_memory records status='rejected' + the issue's current updated_at.
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") == "rejected", mem
    assert mem["acme/widget#7"]["updated_at"] == _UPDATED_T1, mem


# ==========================================================================
# Behaviour 2 — at dry-run the reject is NOT written (permits('file') False).
# ==========================================================================

def test_rejected_order_not_enacted_at_dry_run():
    project_dir, runtime_dir, state_path, journal_path = _setup("dry-run")
    sink = _RejectSink()
    src = _stub_source()
    _tick_with_reject(project_dir, runtime_dir, state_path, journal_path,
                      sink, src)
    # dry-run does not permit the `file` effect: the sink is never called and no
    # rejected status is recorded (intent logged, not written).
    assert sink.calls == [], sink.calls
    mem = rt.persisted_triage_memory(state_path)
    assert mem.get("acme/widget#7", {}).get("status") != "rejected", mem


# ==========================================================================
# Behaviour 3 — 'rejected' is in the triage skip-set; a rejected-AND-unchanged
# item is filtered from the next TRIAGE dispatch; a CHANGED item re-admits.
# ==========================================================================

def test_rejected_in_triage_skip_statuses():
    assert "rejected" in rt._TRIAGE_SKIP_STATUSES, rt._TRIAGE_SKIP_STATUSES


def test_rejected_unchanged_filtered_changed_readmitted():
    memory = {"acme/widget#7": {"updated_at": _UPDATED_T1, "status": "rejected"}}
    # unchanged -> dropped
    unchanged = [{"id": "acme/widget#7", "updated_at": _UPDATED_T1}]
    assert rt._filter_triage_work_items(unchanged, memory) == [], \
        rt._filter_triage_work_items(unchanged, memory)
    # changed (advanced updated_at) -> re-admitted
    changed = [{"id": "acme/widget#7", "updated_at": _UPDATED_T2}]
    assert rt._filter_triage_work_items(changed, memory) == changed, \
        rt._filter_triage_work_items(changed, memory)


def test_rejected_unchanged_item_skips_second_tick_triage():
    project_dir, runtime_dir, state_path, journal_path = _setup("propose")
    sink = _RejectSink()
    src = _stub_source()
    # Tick 1: reject #7 (records triage_memory rejected).
    _tick_with_reject(project_dir, runtime_dir, state_path, journal_path,
                      sink, src)
    assert rt.persisted_triage_memory(state_path).get(
        "acme/widget#7", {}).get("status") == "rejected"
    # Tick 2: #7 is rejected-AND-unchanged -> the TRIAGE dispatch input is fully
    # filtered, so the empty-skip path emits EMPTY WITHOUT pausing. The tick runs
    # straight to a disposition (a signal string), never a TRIAGE pause dict.
    sig = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                      state_path=state_path, journal_path=journal_path,
                      source=src, reject_sink=sink)
    assert sig == "idle", sig
    # No new reject enactment (nothing was re-dispatched).
    assert len(sink.calls) == 1, sink.calls
