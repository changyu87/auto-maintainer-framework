#!/usr/bin/env python3
"""End-to-end conformance tests for the per-issue / per-PR IDENTIFIERS added to
the ``tick_end`` event detail (observability §3.9.1, additive).

In addition to the existing count-only detail (work_items / work_orders /
execution_plan / handoffs, the REPORT counts, the INTEGRATE merge counts +
merged_refs), ``tick_end.detail`` now carries three additive identifier lists,
sourced from read products already in scope at the terminal (no new plumbing):

  - ``handoffs_detail`` — the tick's handoffs as
    ``[{work_order_id, status, ref}]`` (ref = the opened PR's artifact.ref, else
    null); the per-issue -> per-outcome/PR bridge.
  - ``integrated`` — the INTEGRATE result as
    ``{merged: [{pr_ref, url}], skipped: [{pr_ref, reason}],
       errored: [{pr_ref, reason}]}``.
  - ``reported_detail`` — the discoveries filed THIS tick as
    ``[{dedup_key, tracker_ref, url}]`` (the new report_ledger entries).

They are BOUNDED by the tick's own work and are purely ADDITIVE — every
pre-existing count + merged_refs + trace field is unchanged, and a route
producing none shows empty lists. observability's EVENT_KINDS envelope +
EVENT_SCHEMA_VERSION are unchanged (``detail`` is an opaque emitter-owned dict).

scheduling CONSUMES observability + work-intake + verify-integrate +
safety-governance + the loop-core features UNCHANGED via sys.path; edits live
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
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import work_intake as wi  # noqa: E402
import verify_integrate as vi  # noqa: E402
import adapter_map_config as amc  # noqa: E402
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)
_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


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

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


class _RecordingSink:
    """A stub REPORT sink: returns a synthetic {tracker_ref, url}. No network."""

    def __init__(self):
        self.calls = []

    def __call__(self, discovery, repo=None):
        self.calls.append((discovery, repo))
        n = len(self.calls)
        return {"tracker_ref": f"acme/widget#{100 + n}",
                "url": f"https://github.com/acme/widget/issues/{100 + n}"}


def _events(runtime_dir):
    path = os.path.join(runtime_dir, "events.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def _tick_end(runtime_dir):
    return next(e for e in _events(runtime_dir) if e["kind"] == "tick_end")


# --------------------------------------------------------------------------
# Agent-adapter fixtures (mirror test_report_flush_e2e.py): a non-acting TRIAGE
# agent + an ACTING IMPLEMENT agent whose handoff opens a PR and surfaces a
# discovered_work item the REPORT flush files.
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

_IMPLEMENT_ROUTE = {
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


def _write_cfg(project_dir, name, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(payload, f)


def _implement_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    amap["IMPLEMENT"] = dict(_IMPLEMENT_ACTING_AGENT)
    return amap


_CANNED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])

_DISCOVERY = {"title": "Found a flaky test", "body": "test_foo is flaky",
              "kind": "task", "severity": "low"}


def _write_outputs(paused, contents):
    for d, content in zip(paused["dispatches"], contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _canned_handoff(work_order_id):
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": "opened", "artifact": {"kind": "pr", "ref": "PR#1"},
        "discovered_work": [dict(_DISCOVERY)], "blocked_reason": None,
    })


def _setup_project(route, amap, mode="propose"):
    project_dir = tempfile.mkdtemp(prefix="sched-tickid-")
    _write_cfg(project_dir, "route.json", route)
    _write_cfg(project_dir, "adapter-map.json", amap)
    _write_cfg(project_dir, "governance.json", {"mode": mode})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# ==========================================================================
# Behaviour A1 — a route that opens a PR + files a discovery: tick_end.detail
# carries handoffs_detail (work_order_id/status/ref) + reported_detail
# (dedup_key/tracker_ref/url). Both count + identifier surfaces are present.
# ==========================================================================

def test_tick_end_detail_handoffs_and_reported_identifiers():
    project_dir, runtime_dir, state_path, journal_path = _setup_project(
        _IMPLEMENT_ROUTE, _implement_map(), mode="propose")
    sink = _RecordingSink()
    # TRIAGE pause -> resume; IMPLEMENT pause -> resume (opens PR#1 + discovers).
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=_DAY1)
    assert paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), now=_DAY1, resume=True)
    assert paused2["state"] == "IMPLEMENT", paused2
    _write_outputs(paused2, [_canned_handoff(d["item"])
                             for d in paused2["dispatches"]])
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_DAY1, resume=True, report_sink=sink)

    detail = _tick_end(runtime_dir)["detail"]
    # handoffs_detail — the per-issue -> per-PR bridge.
    hd = detail["handoffs_detail"]
    assert isinstance(hd, list) and len(hd) == 1, hd
    assert hd[0]["work_order_id"] == "wo-acme/widget#7", hd
    assert hd[0]["status"] == "opened", hd
    assert hd[0]["ref"] == "PR#1", hd
    # reported_detail — the discovery filed THIS tick, named by its ledger entry.
    rd = detail["reported_detail"]
    assert isinstance(rd, list) and len(rd) == 1, rd
    assert rd[0]["dedup_key"], rd
    assert rd[0]["tracker_ref"] == "acme/widget#101", rd
    assert rd[0]["url"].startswith("https://"), rd
    # The pre-existing count fields are preserved (additive, not replaced).
    assert detail["handoffs"] == 1, detail
    assert detail["reported_filed"] == 1, detail
    # integrated is present (empty — no INTEGRATE stage on this route).
    assert detail["integrated"] == {"merged": [], "skipped": [], "errored": []}, \
        detail


# ==========================================================================
# Behaviour A2 — an INTEGRATE route that merges a PR: tick_end.detail.integrated
# carries {merged: [{pr_ref, url}], skipped, errored}, enriching the count-only
# merged / merged_refs already present.
# ==========================================================================

_REVIEW_AGENT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "VERIFY", "REVIEW", "INTEGRATE",
               "CLEANUP", "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "VERIFY"},
        {"state": "PULL", "signal": "EMPTY", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "REVIEW"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "REVIEW"},
        {"state": "REVIEW", "signal": "OK", "next": "INTEGRATE"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "INTEGRATE"},
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

_OK_PR = {
    "number": 42,
    "url": "https://github.com/acme/widget/pull/42",
    "headRefName": "auto-maintainer/fix-42",
    "baseRefName": "main",
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
}


def _patch_vi_seams():
    saved = {"open": vi.gh_open_pr_source,
             "branch": vi.gh_default_branch_source,
             "merge": vi.gh_pr_merge_sink}

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return [_OK_PR]

    def _branch(repo=None):
        return "main"

    def _merge(pr_ref, repo=None, base_branch=None):
        return {"pr_ref": pr_ref, "url": "https://github.com/acme/widget/pull/42",
                "auto_enabled": False}

    vi.gh_open_pr_source = _open
    vi.gh_default_branch_source = _branch
    vi.gh_pr_merge_sink = _merge

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


def test_tick_end_detail_integrated_carries_merged_pr_identifiers():
    project_dir = tempfile.mkdtemp(prefix="sched-tickid-int-")
    _write_cfg(project_dir, "route.json", _REVIEW_AGENT_ROUTE)
    entry = amc._build_agent_entry(
        "REVIEW", "auto-maintainer:auto-maintainer-reviewer")
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["REVIEW"] = entry
    _write_cfg(project_dir, "adapter-map.json", amap)
    _write_cfg(project_dir, "governance.json", {"mode": "auto-merge"})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    sink = _RecordingSink()
    restore = _patch_vi_seams()
    try:
        paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
        assert paused["state"] == "REVIEW", paused
        # A clean review (zero findings) -> merge proceeds.
        with open(paused["dispatches"][0]["output_path"], "w") as f:
            json.dump([], f)
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source(), now=_DAY1, resume=True,
                    report_sink=sink)
    finally:
        restore()

    detail = _tick_end(runtime_dir)["detail"]
    integrated = detail["integrated"]
    assert len(integrated["merged"]) == 1, integrated
    assert integrated["merged"][0]["pr_ref"], integrated
    assert integrated["merged"][0]["url"].startswith("https://"), integrated
    assert integrated["skipped"] == [], integrated
    assert integrated["errored"] == [], integrated
    # The count-only merged surface is preserved (additive).
    assert detail["merged"] == 1, detail
    assert detail["merged_refs"] == [integrated["merged"][0]["pr_ref"]], detail


# ==========================================================================
# Behaviour A3 — the default read-and-idle tick (no acting/INTEGRATE stage)
# shows the identifiers as EMPTY (additive, non-breaking).
# ==========================================================================

def test_default_tick_identifiers_are_empty():
    root = tempfile.mkdtemp(prefix="sched-tickid-def-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    detail = _tick_end(runtime_dir)["detail"]
    assert detail["handoffs_detail"] == [], detail
    assert detail["reported_detail"] == [], detail
    assert detail["integrated"] == {"merged": [], "skipped": [], "errored": []}, \
        detail
    # The pre-existing counts + merged_refs are all still present.
    assert detail["handoffs"] == 0, detail
    assert detail["merged_refs"] == [], detail


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\nall {len(fns)} passed")
