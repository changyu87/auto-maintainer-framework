#!/usr/bin/env python3
"""End-to-end tests for the richer + prettier /auto-maintainer:status surface.

status.py now exposes a machine-first ``status_data()`` (a dict of every
surfaced field, incl. ``plugin_version`` and the active ``route``) and a DERIVED
human view ``render_status(data)`` (philosophy §1: the pretty view is produced
FROM the machine artifact, never authored alongside it). The CLI prints the
human view by default, ``--json`` prints ``status_data()``, and ``--line``
prints the retained byte-identical legacy ``status_line()``.

The active route (states + happy-path chain) is resolved via a SHARED run_tick
helper (``resolved_route`` + ``route_happy_chain``) the SAME way the tick
resolves it, so status never diverges from what the loop runs; it is listed
EVEN WHEN it is the default. ``plugin_version`` is read from
``<lib_dir>/../.claude-plugin/plugin.json`` (null when absent, e.g. the source
tree). status stays NON-mutating (never creates the runtime dir).

scheduling CONSUMES adapter-wiring + the loop-core / work-intake features
UNCHANGED via sys.path; run_tick gains only ADDITIVE helpers.

Owner: changyu87
"""

import json
import os
import subprocess
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

import run_tick as rt  # noqa: E402
import status as st  # noqa: E402


_ACTING_ROUTE = {
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
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
        {"state": "PRIORITIZE", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "IMPLEMENT", "signal": "OK", "next": "PERSIST"},
        {"state": "IMPLEMENT", "signal": "EMPTY", "next": "PERSIST"},
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


def _with_project_dir(project_dir, fn):
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old


# --------------------------------------------------------------------------
# route_happy_chain — the pure GUARD->...->terminal walk (excludes terminals).
# --------------------------------------------------------------------------

def test_route_happy_chain_default_route():
    """DEFAULT_ROUTE's happy path is GUARD->DRAIN->PULL->PERSIST->EXIT (no
    terminal), following the OK edge from each state."""
    chain = rt.route_happy_chain(rt.DEFAULT_ROUTE)
    assert chain == ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"], chain
    # Terminals are excluded from the chain.
    assert "DONE" not in chain and "HALTED" not in chain, chain


def test_route_happy_chain_acting_route():
    """A full acting route walks GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->
    IMPLEMENT->PERSIST->EXIT via OK edges (else EMPTY), stopping at the
    terminal."""
    chain = rt.route_happy_chain(_ACTING_ROUTE)
    assert chain == ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                     "IMPLEMENT", "PERSIST", "EXIT"], chain


# --------------------------------------------------------------------------
# resolved_route — the SAME shipped-else-embedded-then-override resolution the
# tick uses, WITHOUT running/mutating a tick.
# --------------------------------------------------------------------------

def test_resolved_route_default_when_no_override():
    """With no project-local route.json, resolved_route returns the default
    route (embedded DEFAULT_ROUTE in the source tree)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    route = rt.resolved_route(project_dir)
    assert route["states"] == rt.DEFAULT_ROUTE["states"], route["states"]


def test_resolved_route_reflects_override():
    """A project-local route.json override is reflected by resolved_route — the
    SAME override adapter-wiring's loader reads."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _write_project_route(project_dir, _ACTING_ROUTE)
    route = rt.resolved_route(project_dir)
    assert "IMPLEMENT" in route["states"], route["states"]
    assert route["states"] == _ACTING_ROUTE["states"], route["states"]


def test_resolved_route_non_mutating():
    """resolved_route must NOT create the runtime dir (status is read-only)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    rt.resolved_route(project_dir)
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer")), \
        "resolved_route created the runtime dir"


# --------------------------------------------------------------------------
# _plugin_version — read from <lib_dir>/../.claude-plugin/plugin.json.
# --------------------------------------------------------------------------

def test_plugin_version_from_temp_plugin_json():
    """_plugin_version reads the 'version' from
    <lib_dir>/../.claude-plugin/plugin.json (installed-plugin context)."""
    root = tempfile.mkdtemp(prefix="sched-plugin-")
    lib_dir = os.path.join(root, "lib")
    os.makedirs(lib_dir)
    pdir = os.path.join(root, ".claude-plugin")
    os.makedirs(pdir)
    with open(os.path.join(pdir, "plugin.json"), "w") as f:
        json.dump({"name": "auto-maintainer", "version": "9.9.9"}, f)
    assert st._plugin_version(lib_dir) == "9.9.9"


def test_plugin_version_none_when_absent():
    """No plugin.json (e.g. the source tree) -> None."""
    root = tempfile.mkdtemp(prefix="sched-plugin-")
    lib_dir = os.path.join(root, "lib")
    os.makedirs(lib_dir)
    assert st._plugin_version(lib_dir) is None
    # And the default (source-tree) lookup is also None: no plugin.json ships
    # alongside src/ in the feature tree.
    assert st._plugin_version() is None


# --------------------------------------------------------------------------
# status_data — the machine-first dict of every surfaced field.
# --------------------------------------------------------------------------

def test_status_data_has_every_field():
    """status_data() returns a dict with EVERY surfaced field, including the
    route sub-dict {source, states, chain}."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    data = _with_project_dir(project_dir, st.status_data)
    for key in ("plugin_version", "disposition", "awaiting", "mode",
                "budget", "work_items", "work_orders", "execution_plan",
                "handoffs", "reported", "route", "runtime_dir"):
        assert key in data, (key, sorted(data))
    # Nested shapes.
    for bkey in ("spent", "ceiling", "window", "paused"):
        assert bkey in data["budget"], (bkey, data["budget"])
    for rkey in ("filed", "skipped"):
        assert rkey in data["reported"], (rkey, data["reported"])
    for rkey in ("source", "states", "chain"):
        assert rkey in data["route"], (rkey, data["route"])


def test_status_data_route_listed_when_default():
    """The route is listed EVEN WHEN it is the default (no override)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    data = _with_project_dir(project_dir, st.status_data)
    assert data["route"]["source"] == "default", data["route"]
    assert data["route"]["states"] == rt.DEFAULT_ROUTE["states"]
    assert data["route"]["chain"] == ["GUARD", "DRAIN", "PULL", "PERSIST",
                                      "EXIT"], data["route"]["chain"]


def test_status_data_route_reflects_override():
    """With a project-local override, status_data's route reflects it and the
    source is override:<path>."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _write_project_route(project_dir, _ACTING_ROUTE)
    data = _with_project_dir(project_dir, st.status_data)
    assert data["route"]["source"].startswith("override:"), data["route"]
    assert "IMPLEMENT" in data["route"]["states"], data["route"]["states"]
    assert "IMPLEMENT" in data["route"]["chain"], data["route"]["chain"]


def test_status_data_non_mutating():
    """status_data must NOT create the runtime dir."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _with_project_dir(project_dir, st.status_data)
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer")), \
        "status_data created the runtime dir"


def _write_project_config(project_dir, config):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    path = os.path.join(cfg, "config.json")
    with open(path, "w") as f:
        json.dump(config, f)
    return path


def test_status_data_heartbeat_interval_default():
    """status_data() surfaces heartbeat_interval_minutes; with no project config
    it is the shipped default 10 (same source start.py's
    heartbeat_interval_minutes() reads)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    data = _with_project_dir(project_dir, st.status_data)
    assert "heartbeat_interval_minutes" in data, sorted(data)
    assert data["heartbeat_interval_minutes"] == 10, data


def test_status_data_heartbeat_interval_reflects_config():
    """A NON-default configured heartbeat.interval_minutes is reflected by
    status_data (it reads config, not a hardcoded constant)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _write_project_config(project_dir, {"heartbeat": {"interval_minutes": 30}})
    data = _with_project_dir(project_dir, st.status_data)
    assert data["heartbeat_interval_minutes"] == 30, data


def test_render_status_shows_heartbeat_line():
    """render_status renders a `heartbeat <n> min` line reflecting the configured
    interval (a NON-default value proves it is config-driven)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _write_project_config(project_dir, {"heartbeat": {"interval_minutes": 30}})
    data = _with_project_dir(project_dir, st.status_data)
    out = st.render_status(data)
    assert "heartbeat" in out, out
    # The configured value (30) and the `min` unit appear on the heartbeat line.
    hb_line = [ln for ln in out.splitlines() if "heartbeat" in ln]
    assert hb_line, out
    assert "30" in hb_line[0] and "min" in hb_line[0], hb_line


# --------------------------------------------------------------------------
# render_status — the DERIVED human view.
# --------------------------------------------------------------------------

def test_render_status_contains_version_disposition_chain():
    """render_status emphasizes the version, shows the disposition, and renders
    the happy-path chain with arrows."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    data = _with_project_dir(project_dir, st.status_data)
    data["plugin_version"] = "1.2.3"
    out = st.render_status(data)
    assert "1.2.3" in out, out
    assert data["disposition"] in out, out
    assert "→" in out, out  # the "->" arrow between chain states
    assert "GUARD" in out and "EXIT" in out, out
    # No emojis (coding-rules §5) — restrict to ASCII plus the arrow/rule chars.
    for ch in out:
        assert ord(ch) < 128 or ch in "→─", repr(ch)


def test_render_status_dev_fallback_when_no_version():
    """A None plugin_version (source tree) renders a dev fallback in the
    header, and render_status survives a not-started state."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    data = _with_project_dir(project_dir, st.status_data)
    assert data["plugin_version"] is None, data["plugin_version"]
    out = st.render_status(data)
    assert "dev" in out.lower(), out
    # Not-started state still renders (IDLE disposition, zero counts).
    assert data["disposition"] in out, out


# --------------------------------------------------------------------------
# CLI — default=human view, --json=dict, --line=byte-identical legacy line.
# --------------------------------------------------------------------------

def _run_cli(args, project_dir):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    # Ensure the deps are importable in the subprocess.
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [os.path.join(_FEATURES, d, "src") for d in (
            "fsm-contracts", "tick-orchestrator", "durable-state",
            "lifecycle-dispositions", "work-intake", "adapter-wiring",
            "prioritize", "implement", "safety-governance", "agent-dispatch",
            "observability", "verify-integrate")]
        + [env.get("PYTHONPATH", "")])
    proc = subprocess.run(
        [sys.executable, os.path.join(_SRC, "status.py")] + args,
        capture_output=True, text=True, env=env)
    return proc


def test_cli_json_is_valid_json():
    """--json prints status_data() as valid JSON."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    proc = _run_cli(["--json"], project_dir)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "plugin_version" in data and "route" in data, data


def test_cli_line_is_byte_identical_legacy():
    """--line prints the exact legacy status_line() output."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    proc = _run_cli(["--line"], project_dir)
    assert proc.returncode == 0, proc.stderr
    expected = _with_project_dir(project_dir, st.status_line)
    assert proc.stdout == expected + "\n", (proc.stdout, expected)
    assert proc.stdout.startswith("[status] disposition="), proc.stdout


def test_cli_default_is_human_view():
    """No flag -> the rendered human view (render_status)."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    proc = _run_cli([], project_dir)
    assert proc.returncode == 0, proc.stderr
    # The human view carries the header and a Route section, not the legacy line.
    assert "auto-maintainer" in proc.stdout, proc.stdout
    assert "Route" in proc.stdout, proc.stdout
    assert not proc.stdout.startswith("[status] disposition="), proc.stdout


def test_cli_default_does_not_create_runtime_dir():
    """The default human-view CLI stays non-mutating."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    _run_cli([], project_dir)
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer")), \
        "status CLI created the runtime dir"


# --------------------------------------------------------------------------
# status_line stays UNCHANGED (byte-identical legacy back-compat).
# --------------------------------------------------------------------------

def test_status_line_unchanged_shape():
    """status_line() still emits the exact legacy one-line shape."""
    project_dir = tempfile.mkdtemp(prefix="sched-status-")
    line = _with_project_dir(project_dir, st.status_line)
    assert line.startswith("[status] disposition="), line
    assert "route=default" in line, line
    assert "work_items=" in line and "awaiting=" in line, line
