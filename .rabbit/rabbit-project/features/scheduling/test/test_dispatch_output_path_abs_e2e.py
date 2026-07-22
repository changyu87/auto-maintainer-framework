#!/usr/bin/env python3
"""End-to-end conformance tests for the ABSOLUTE dispatch output_path
(auto-maintainer-framework#143).

run_tick MUST absolutize the agent-dispatch output_dir
(`output_dir = os.path.abspath(os.path.join(runtime_dir, "dispatch-out"))`) so
EVERY paused dispatch's `output_path` is an absolute path.

The bug: a relative `output_path` is resolved against the *subagent's* cwd. An
acting agent-state dispatched with `isolation: "worktree"` runs with its cwd
INSIDE the worktree, so a relative path would land at
`<worktree>/.auto-maintainer/dispatch-out/...` — invisible to the orchestrator
(cwd = main workspace), which then reads a MISSING file and re-dispatches,
re-running the act (a second PR / a second issue-close). An absolute
`output_path` makes the subagent write its handoff to the SHARED main-workspace
dispatch-out regardless of its cwd.

Behaviours exercised (E2E TEST RULE):

  A. A fresh agent-route tick PAUSES and EVERY dispatch's output_path is
     absolute (os.path.isabs True), at the first PAUSE (TRIAGE, cardinality
     once) and at the second PAUSE (IMPLEMENT, per_item — multiple dispatches).
  B. THE #143 regression: even when run_tick is invoked with a RELATIVE
     runtime_dir / project_dir, the emitted output_path is still absolute.
  C. The persisted checkpoint's output_dir is absolute, so a crash-safety
     re-emit and a worktree-isolated resume read the same absolute path.

scheduling CONSUMES the loop-core / work-intake / prioritize / implement /
adapter-wiring / agent-dispatch features UNCHANGED via sys.path.

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
             "prioritize", "implement", "agent-dispatch", "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


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


# An agent TRIAGE (cardinality once) + agent IMPLEMENT (per_item) route, so we
# exercise both the single-dispatch and the multi-dispatch PAUSE shapes.
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


def _setup_agent_project():
    project_dir = tempfile.mkdtemp(prefix="sched-abs-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
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


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


# ==========================================================================
# Behaviour A — every dispatch output_path at every PAUSE is absolute.
# ==========================================================================

def test_first_pause_dispatch_output_path_is_absolute():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    assert paused["status"] == "paused", paused
    assert paused["state"] == "TRIAGE", paused
    for d in paused["dispatches"]:
        assert os.path.isabs(d["output_path"]), d["output_path"]
        # The rendered envelope (delivered by FILE REFERENCE at prompt_path)
        # names the same absolute output_path.
        with open(d["prompt_path"]) as _f:
            rendered = _f.read()
        assert d["output_path"] in rendered, rendered


def test_per_item_pause_every_dispatch_output_path_is_absolute():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    paused1 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source())
    _write_outputs(paused1, [_CANNED_WORK_ORDERS])
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), resume=True)
    assert paused2["status"] == "paused", paused2
    assert paused2["state"] == "IMPLEMENT", paused2
    assert len(paused2["dispatches"]) == 2, paused2
    for d in paused2["dispatches"]:
        assert os.path.isabs(d["output_path"]), d["output_path"]


# ==========================================================================
# Behaviour B (THE #143 regression) — a RELATIVE runtime_dir/project_dir still
# yields an ABSOLUTE output_path.
# ==========================================================================

def test_relative_runtime_dir_still_yields_absolute_output_path():
    """A relative runtime_dir/project_dir is the worktree-isolation hazard: with
    a relative output_dir the dispatched subagent (cwd inside its worktree)
    would write to a worktree-local path invisible to the orchestrator. run_tick
    MUST absolutize, so the emitted output_path is absolute regardless."""
    project_dir = tempfile.mkdtemp(prefix="sched-absrel-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    # Drive run_tick with cwd == project_dir and RELATIVE path arguments.
    prev_cwd = os.getcwd()
    os.chdir(project_dir)
    try:
        rel_runtime = ".auto-maintainer"
        rel_state = os.path.join(rel_runtime, "durable-state.json")
        rel_journal = os.path.join(rel_runtime, "tick-journal.jsonl")
        assert not os.path.isabs(rel_runtime)
        paused = rt.run_tick(project_dir=".", runtime_dir=rel_runtime,
                             state_path=rel_state, journal_path=rel_journal,
                             source=_stub_source())
    finally:
        os.chdir(prev_cwd)
    assert paused["status"] == "paused", paused
    for d in paused["dispatches"]:
        assert os.path.isabs(d["output_path"]), \
            ("relative input must still yield an absolute output_path",
             d["output_path"])


# ==========================================================================
# Behaviour C — the persisted checkpoint's output_dir is absolute.
# ==========================================================================

def test_checkpoint_output_dir_is_absolute():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    doc = ds.DurableState(state_path).load()
    cp = doc.get(rt.TICK_CHECKPOINT_KEY)
    assert cp is not None, doc
    assert os.path.isabs(cp["output_dir"]), cp["output_dir"]
    for d in cp["pending"]["dispatches"]:
        assert os.path.isabs(d["output_path"]), d["output_path"]
