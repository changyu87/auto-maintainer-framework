#!/usr/bin/env python3
"""End-to-end conformance for #342: surface the default-config SOURCE in the trace.

As of #337 run_tick resolves the active default handed to build_loop by reading
the shipped ``default-config/{route,adapter-map}.json`` FRESH when present, else
the embedded conservative ``DEFAULT_ROUTE`` / ``DEFAULT_ADAPTER_MAP`` constant.
The existing trace reports the route SOURCE (default vs override) but not whether
the default itself came from the shipped default-config/ or the embedded
fallback. This module exercises the observability-only fix: the tick trace now
carries a ``default_src`` token —

  1. shipped default-config/ present -> trace shows default_src=shipped-default-config.
  2. no shipped default-config dir -> trace shows default_src=embedded-constant.
  3. the _default_source_label helper maps a shipped route/map value ->
     "shipped-default-config" and both-None -> "embedded-constant".

The resolution logic itself is UNCHANGED; only the machine-visible token is added.

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
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


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


def _paths():
    root = tempfile.mkdtemp(prefix="sched-defsrc-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _write_shipped_default_config(route=None, adapter_map=None):
    """Create a temp <plugin_root>/default-config/ dir with the given shipped
    files and return the dir (point rt.DEFAULT_CONFIG_DIR at it)."""
    d = tempfile.mkdtemp(prefix="sched-shipped-")
    cfg = os.path.join(d, "default-config")
    os.makedirs(cfg)
    if route is not None:
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(route, f)
    if adapter_map is not None:
        with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
            json.dump(adapter_map, f)
    return cfg


# --------------------------------------------------------------------------
# Behaviour 1 — a shipped default-config/ present -> shipped-default-config token.
# --------------------------------------------------------------------------

def test_trace_reports_shipped_default_config_when_present():
    """With a shipped default-config/ dir present, the tick trace carries
    default_src=shipped-default-config (the fresh shipped default is in effect)."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = _write_shipped_default_config(
            route=rt.DEFAULT_ROUTE, adapter_map=rt.DEFAULT_ADAPTER_MAP)
        runtime_dir, state_path, journal_path = _paths()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                        journal_path=journal_path, source=_stub_source())
        out = buf.getvalue()
        assert "default_src=shipped-default-config" in out, out
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 2 — no shipped default-config dir -> embedded-constant token.
# --------------------------------------------------------------------------

def test_trace_reports_embedded_constant_when_no_shipped_dir():
    """With DEFAULT_CONFIG_DIR pointing at a NONEXISTENT dir, the tick trace
    carries default_src=embedded-constant (the conservative fallback is in
    effect)."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = os.path.join(
            tempfile.mkdtemp(prefix="sched-noship-"), "does-not-exist")
        runtime_dir, state_path, journal_path = _paths()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                        journal_path=journal_path, source=_stub_source())
        out = buf.getvalue()
        assert "default_src=embedded-constant" in out, out
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 3 — the _default_source_label helper: shipped value vs both-None.
# --------------------------------------------------------------------------

def test_default_source_label_helper():
    assert rt._default_source_label({"x": 1}, None) == "shipped-default-config"
    assert rt._default_source_label(None, {"y": 2}) == "shipped-default-config"
    assert rt._default_source_label({"x": 1}, {"y": 2}) \
        == "shipped-default-config"
    assert rt._default_source_label(None, None) == "embedded-constant"
