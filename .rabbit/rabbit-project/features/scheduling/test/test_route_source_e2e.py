#!/usr/bin/env python3
"""End-to-end conformance tests for #59: make the loaded route SOURCE observable.

When no project-local route.json exists (or it is misplaced) the loop silently
used the built-in default with NO signal — a misplaced override looked identical
to "no effect", which led to a wrong diagnosis. This module exercises the
observability fix from docs/spec.md: run_tick's tick trace AND status.py both
report the route source — `default` when no project-local route.json exists, or
`override:<abs path>` when it does — via a SINGLE deterministic helper
(`run_tick.route_source`) that resolves the SAME runtime/project dir the loader
(adapter-wiring) actually reads, so the reported source matches the route run.

  1. No project-local route.json -> trace shows route=default.
  2. No project-local route.json -> status shows route=default.
  3. A project-local route.json present -> trace shows route=override:<path>.
  4. A project-local route.json present -> status shows route=override:<path>.
  5. The helper resolves the SAME dir the loader uses: an override that inserts
     TRIAGE both RUNS TRIAGE and REPORTS route=override:<that path>.
  6. status.py and run_tick use the SAME helper (single source of truth — no
     divergent existence logic).

scheduling CONSUMES adapter-wiring + the loop-core / work-intake features
UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402
import status as st  # noqa: E402


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


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    path = os.path.join(cfg, "route.json")
    with open(path, "w") as f:
        json.dump(route, f)
    return path


def _project_paths():
    """A temp project_dir whose runtime dir is the standard ${proj}/.auto-maintainer
    so route.json (override config) and the runtime markers share a dir, exactly
    as the installed plugin lays them out."""
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# --------------------------------------------------------------------------
# The helper itself — single source of truth for the route source.
# --------------------------------------------------------------------------

def test_route_source_default_when_no_project_route():
    """No project-local route.json -> ('default', None)."""
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    label, path = rt.route_source(project_dir)
    assert label == "default", (label, path)
    assert path is None, path


def test_route_source_override_when_project_route_present():
    """A project-local route.json -> ('override', <abs path>)."""
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    written = _write_project_route(project_dir, _TRIAGE_ROUTE)
    label, path = rt.route_source(project_dir)
    assert label == "override", (label, path)
    assert path == written, (path, written)
    assert os.path.isabs(path), path


def test_route_source_resolves_same_path_as_loader():
    """The helper resolves the SAME ${project_dir}/.auto-maintainer/route.json
    that adapter-wiring's loader reads — so the reported source matches the route
    actually run."""
    import adapter_wiring as aw
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    written = _write_project_route(project_dir, _TRIAGE_ROUTE)
    _label, path = rt.route_source(project_dir)
    # adapter-wiring's loader reads exactly this path.
    assert path == aw._config_path(project_dir, "route.json"), path
    assert path == written, path


# --------------------------------------------------------------------------
# Behaviour 1+3 — the tick trace reports the route source.
# --------------------------------------------------------------------------

def test_trace_reports_route_default_when_no_override():
    project_dir, runtime_dir, state_path, journal_path = _project_paths()
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    out = buf.getvalue()
    assert "route=default" in out, out


def test_trace_reports_route_override_path_when_present():
    project_dir, runtime_dir, state_path, journal_path = _project_paths()
    written = _write_project_route(project_dir, _TRIAGE_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    out = buf.getvalue()
    assert f"route=override:{written}" in out, out


# --------------------------------------------------------------------------
# Behaviour 2+4 — status.py reports the route source.
# --------------------------------------------------------------------------

def test_status_reports_route_default_when_no_override():
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "route=default" in line, line


def test_status_reports_route_override_path_when_present():
    project_dir = tempfile.mkdtemp(prefix="sched-routesrc-")
    written = _write_project_route(project_dir, _TRIAGE_ROUTE)
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert f"route=override:{written}" in line, line


# --------------------------------------------------------------------------
# Behaviour 5 — HEADLINE: the helper agrees with the route actually run. An
# override that inserts TRIAGE both RUNS TRIAGE and REPORTS route=override.
# --------------------------------------------------------------------------

def test_override_runs_triage_and_trace_reports_override():
    project_dir, runtime_dir, state_path, journal_path = _project_paths()
    written = _write_project_route(project_dir, _TRIAGE_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), return_run_result=True)
    out = buf.getvalue()
    # The route ACTUALLY run inserted TRIAGE ...
    assert "TRIAGE" in result.path, result.path
    # ... AND the trace reports the override that drove it (matching source).
    assert f"route=override:{written}" in out, out
    assert rt.persisted_work_orders(state_path) == [] or \
        len(rt.persisted_work_orders(state_path)) == 2


# --------------------------------------------------------------------------
# Behaviour 6 — status.py and run_tick use the SAME helper (single source of
# truth — no divergent existence logic).
# --------------------------------------------------------------------------

def test_status_uses_run_tick_route_source_helper():
    """status.py must reuse run_tick.route_source (single source of truth), not
    hand-roll its own route.json existence check."""
    import inspect
    src = inspect.getsource(st)
    assert "route_source" in src, src
    # status must not duplicate the existence logic with its own route.json probe.
    assert "route.json" not in src, src
