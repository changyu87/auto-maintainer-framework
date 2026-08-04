#!/usr/bin/env python3
"""End-to-end tests for the tick_start version+file PROVENANCE block (debug
enhancement).

run_tick now enriches the ``tick_start`` event ``detail`` with a machine-first
provenance block so an operator can see EXACTLY which code + config a given tick
ran with:

  - ``plugin_version`` — the shipped plugin version, read the SAME way status.py
    does (``<lib_dir>/../.claude-plugin/plugin.json``; ``null`` in the source
    tree). Also surfaced as a compact ``plugin_version=<v>`` token on the
    one-line trace.
  - ``lib_dir`` — the ABSOLUTE directory of the running run_tick.py.
  - ``runtime_dir`` — the resolved runtime dir.
  - ``config_path`` / ``route_path`` / ``adapter_map_path`` (+ each ``*_source``)
    — the resolved config / route / adapter-map FILE PATHS actually in use this
    tick, each with its default-vs-override source, reusing run_tick's EXISTING
    resolution.

The block is PURELY ADDITIVE: observability.EVENT_KINDS is unchanged (this is
tick_start.detail, a scheduling-owned opaque dict), the existing detail fields
(``source`` / ``mode``) are preserved, and a source-tree run with no shipped
plugin.json degrades gracefully (plugin_version=null, no crash).

scheduling CONSUMES observability + the loop-core / work-intake / adapter-wiring
/ agent-dispatch / safety-governance features UNCHANGED via sys.path.

Owner: changyu87
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime, timezone

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
import observability as ob  # noqa: E402
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
  }
]"""

_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=timezone.utc)


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="sched-prov-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _events(runtime_dir):
    return ob.EventLog(os.path.join(runtime_dir, "events.jsonl")).read()


def _tick_start(runtime_dir):
    return next(e for e in _events(runtime_dir) if e["kind"] == "tick_start")


# ==========================================================================
# Provenance block presence + shape on a pure-script DEFAULT tick.
# ==========================================================================

def test_tick_start_detail_carries_provenance_block():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    detail = _tick_start(runtime_dir)["detail"]
    assert detail is not None, detail
    # Existing fields preserved (additive change).
    assert detail.get("source") == "default", detail
    assert "mode" in detail, detail
    prov = detail.get("provenance")
    assert prov is not None, detail
    for key in ("plugin_version", "lib_dir", "runtime_dir",
                "config_path", "config_source",
                "route_path", "route_source",
                "adapter_map_path", "adapter_map_source"):
        assert key in prov, (key, prov)


def test_provenance_lib_dir_is_run_tick_abspath():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert prov["lib_dir"] == os.path.dirname(
        os.path.abspath(rt.__file__)), prov["lib_dir"]
    assert os.path.isabs(prov["lib_dir"]), prov["lib_dir"]


def test_provenance_runtime_dir_matches():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert prov["runtime_dir"] == runtime_dir, prov


def test_provenance_plugin_version_null_in_source_tree():
    """No plugin.json ships alongside src/ in the feature tree, so
    plugin_version degrades to null (None) gracefully — no crash."""
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert prov["plugin_version"] is None, prov["plugin_version"]


def test_provenance_config_sources_embedded_when_no_override():
    """With no project-local override and no shipped default-config dir (the
    source tree), each artifact resolves to the embedded constant."""
    project_dir = tempfile.mkdtemp(prefix="sched-prov-proj-")
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert prov["route_source"] == "embedded-constant", prov
    assert prov["route_path"] is None, prov
    assert prov["adapter_map_source"] == "embedded-constant", prov
    assert prov["config_source"] == "embedded-constant", prov


def test_provenance_route_source_reflects_override():
    """A project-local route.json override is reflected in the provenance
    route_source (override) + route_path (the override file)."""
    project_dir = tempfile.mkdtemp(prefix="sched-prov-proj-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    route_path = os.path.join(cfg, "route.json")
    with open(route_path, "w") as f:
        json.dump(rt.DEFAULT_ROUTE, f)
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert prov["route_source"] == "override", prov
    assert prov["route_path"] == route_path, prov


# ==========================================================================
# The compact plugin_version=<v> token on the one-line trace.
# ==========================================================================

def test_trace_carries_plugin_version_token_null_in_source_tree():
    runtime_dir, state_path, journal_path = _paths()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, source=_stub_source(), now=_NOW)
    trace = buf.getvalue()
    assert "plugin_version=null" in trace, trace


# ==========================================================================
# EVENT_KINDS unchanged (provenance is tick_start.detail, not a new kind).
# ==========================================================================

def test_provenance_does_not_add_new_event_kinds():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source(), now=_NOW)
    for e in _events(runtime_dir):
        assert e["kind"] in ob.EVENT_KINDS, e["kind"]


# ==========================================================================
# Agent route: the fresh --step tick_start also carries the provenance block.
# ==========================================================================

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


def test_agent_route_tick_start_carries_provenance():
    project_dir = tempfile.mkdtemp(prefix="sched-prov-agent-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_AGENT_ROUTE, f)
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)
    runtime_dir = cfg
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source(), now=_NOW)
    prov = _tick_start(runtime_dir)["detail"]["provenance"]
    assert "plugin_version" in prov, prov
    assert prov["lib_dir"] == os.path.dirname(
        os.path.abspath(rt.__file__)), prov
    # The route override is reflected in the agent-route provenance too.
    assert prov["route_source"] == "override", prov
