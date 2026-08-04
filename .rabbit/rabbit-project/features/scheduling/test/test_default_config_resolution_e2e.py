#!/usr/bin/env python3
"""End-to-end conformance for #337: read the shipped default-config FRESH; no seed.

docs/spec.md "Default-config resolution (read shipped default FRESH, #337)": the
tick-runner RESOLVES the active default handed to adapter_wiring.build_loop by
READING the shipped ``<plugin_root>/default-config/route.json`` /
``adapter-map.json`` FRESH when present, else the embedded conservative
``DEFAULT_ROUTE`` / ``DEFAULT_ADAPTER_MAP`` constant. There is NO seed-once copy:
``start.py`` writes NOTHING into ``.auto-maintainer/`` (``seed_default_config`` is
retired). A project-local ``.auto-maintainer/<file>`` override still WINS over the
shipped default (adapter-wiring's override-else-default, unchanged).

Behaviours exercised:

  1. Shipped default-config/route.json present -> run_tick resolves it FRESH and
     runs the SHIPPED route (a GUARD..PULL..TRIAGE..route that produces
     work_orders), NOT the embedded no-TRIAGE DEFAULT_ROUTE constant.
  2. No shipped default-config dir -> run_tick falls back to the embedded
     DEFAULT_ROUTE constant (read-and-idle, no TRIAGE -> no work_orders).
  3. The _shipped_default helper reads + parses the shipped file when present and
     returns None when the dir is absent or the file is unparsable.
  4. A project-local override still WINS over a shipped default (override-else-
     shipped-else-constant): a project route.json inserting TRIAGE runs even when
     the shipped default is the plain spine.
  5. start() performs NO seed-once copy: it writes NOTHING to .auto-maintainer/
     beyond the runtime markers a normal tick produces (no config.json /
     route.json / adapter-map.json seeded), and seed_default_config is gone.

scheduling CONSUMES adapter-wiring + the loop-core / work-intake features
UNCHANGED via sys.path; it does NOT edit or fork them.

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
             "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402
import start as sa  # noqa: E402


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
    root = tempfile.mkdtemp(prefix="sched-defcfg-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# A shipped-default route that inserts TRIAGE — observably DIFFERENT from the
# embedded no-TRIAGE DEFAULT_ROUTE spine (a TRIAGE tick produces work_orders).
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


def _write_shipped_default_config(route=None, adapter_map=None):
    """Create a temp <plugin_root>/default-config/ dir with the given shipped
    files and point rt.DEFAULT_CONFIG_DIR at it. Returns the dir."""
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
# Behaviour 1 — shipped default-config/route.json is read FRESH and RUNS.
# --------------------------------------------------------------------------

def test_shipped_default_route_is_resolved_fresh_and_runs():
    """With a shipped default-config/route.json inserting TRIAGE (+ a shipped
    adapter-map mapping every port), run_tick runs the SHIPPED route -> it
    produces work_orders, proving the shipped file was read (the embedded
    DEFAULT_ROUTE has no TRIAGE and produces none)."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = _write_shipped_default_config(
            route=_TRIAGE_ROUTE, adapter_map=rt.DEFAULT_ADAPTER_MAP)
        runtime_dir, state_path, journal_path = _paths()
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, source=_stub_source())
        assert rt.persisted_work_orders(state_path), \
            "shipped TRIAGE route should have produced work_orders"
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 2 — no shipped dir -> the embedded constant is the fallback.
# --------------------------------------------------------------------------

def test_falls_back_to_embedded_constant_when_no_shipped_dir():
    """With DEFAULT_CONFIG_DIR pointing at a NONEXISTENT dir, run_tick uses the
    embedded DEFAULT_ROUTE spine (no TRIAGE -> no work_orders)."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = os.path.join(
            tempfile.mkdtemp(prefix="sched-noship-"), "does-not-exist")
        runtime_dir, state_path, journal_path = _paths()
        result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                             journal_path=journal_path, source=_stub_source())
        assert result == "idle", result
        assert rt.persisted_work_orders(state_path) == [], \
            "embedded default spine has no TRIAGE -> no work_orders"
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 3 — the _shipped_default helper: parse-or-None.
# --------------------------------------------------------------------------

def test_shipped_default_helper_reads_parsed_file():
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = _write_shipped_default_config(
            route=_TRIAGE_ROUTE)
        got = rt._shipped_default("route.json")
        assert got == _TRIAGE_ROUTE, got
        # adapter-map.json was not written -> None.
        assert rt._shipped_default("adapter-map.json") is None
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


def test_shipped_default_helper_returns_none_when_dir_absent():
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        rt.DEFAULT_CONFIG_DIR = os.path.join(
            tempfile.mkdtemp(prefix="sched-noship-"), "nope")
        assert rt._shipped_default("route.json") is None
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


def test_shipped_default_helper_returns_none_on_unparsable():
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        cfg = _write_shipped_default_config()
        with open(os.path.join(cfg, "route.json"), "w") as f:
            f.write("{ this is not json")
        rt.DEFAULT_CONFIG_DIR = cfg
        assert rt._shipped_default("route.json") is None
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 4 — project-local override WINS over the shipped default.
# --------------------------------------------------------------------------

def test_project_override_wins_over_shipped_default():
    """A project-local .auto-maintainer/route.json (inserting TRIAGE) runs even
    when the shipped default is the plain no-TRIAGE spine: override beats shipped
    beats constant."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        # Shipped default = the plain spine (no TRIAGE).
        rt.DEFAULT_CONFIG_DIR = _write_shipped_default_config(
            route=rt.DEFAULT_ROUTE, adapter_map=rt.DEFAULT_ADAPTER_MAP)
        project_dir = tempfile.mkdtemp(prefix="sched-override-")
        runtime_dir = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(runtime_dir, exist_ok=True)
        with open(os.path.join(runtime_dir, "route.json"), "w") as f:
            json.dump(_TRIAGE_ROUTE, f)
        state_path = os.path.join(runtime_dir, "durable-state.json")
        journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
        assert rt.persisted_work_orders(state_path), \
            "project-local override route (TRIAGE) must win and produce work_orders"
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


# --------------------------------------------------------------------------
# Behaviour 5 — start() seeds NOTHING (seed_default_config retired, #337).
# --------------------------------------------------------------------------

def test_start_writes_no_seeded_config_into_runtime_dir():
    """start() performs NO seed-once copy: after a fresh start, the runtime dir
    contains NO seeded config.json / route.json / adapter-map.json — only the
    runtime markers a normal tick writes."""
    saved = rt.DEFAULT_CONFIG_DIR
    try:
        # Even with a shipped default-config present, start must not copy it in.
        rt.DEFAULT_CONFIG_DIR = _write_shipped_default_config(
            route=rt.DEFAULT_ROUTE, adapter_map=rt.DEFAULT_ADAPTER_MAP)
        runtime_dir, state_path, journal_path = _paths()
        sa.start(runtime_dir=runtime_dir, state_path=state_path,
                 journal_path=journal_path, source=_stub_source())
        seeded_names = {"config.json", "route.json", "adapter-map.json"}
        present = set(os.listdir(runtime_dir)) if os.path.isdir(runtime_dir) \
            else set()
        leaked = seeded_names & present
        assert not leaked, f"start seeded config files into runtime dir: {leaked}"
    finally:
        rt.DEFAULT_CONFIG_DIR = saved


def test_seed_default_config_is_removed():
    """The retired seed_default_config API is GONE from start (#337)."""
    assert not hasattr(sa, "seed_default_config"), \
        "seed_default_config must be removed (no seed-once copy, #337)"
