#!/usr/bin/env python3
"""End-to-end tests for status.py's injectable, tolerant RELEASE PROBE.

status informs the user when a newer plugin release is available. status.py
gains a deterministic, INJECTABLE release probe (``DEFAULT_RELEASE_PROBE``,
overridable via a ``status_data`` param so tests stub it with no network); the
production probe shells ``gh`` against the FIXED distribution repo
``changyu87/auto-maintainer-framework``. ``status_data()`` exposes:

  - ``latest_version`` — the probe result, or ``None`` when unknown.
  - ``update_available`` — a STRICT semver-greater comparison of
    ``latest_version`` vs the installed ``plugin_version`` — ``False`` whenever
    either is unknown.
  - ``release_check_error`` — a short reason string, or ``None``.

The probe is TOLERANT: ANY failure (no gh / no network / parse error) yields
``latest_version=None``, ``update_available=False``, a non-null
``release_check_error``, and NEVER crashes status. ``render_status`` shows a
clear line: update-available / up-to-date / check-errored.

scheduling CONSUMES run_tick + lifecycle-dispositions UNCHANGED via sys.path.

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

import status as st  # noqa: E402


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


# ==========================================================================
# status_data — the new release-check fields with an injected probe.
# ==========================================================================

def test_status_data_has_release_check_fields():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    data = _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))
    for key in ("latest_version", "update_available", "release_check_error"):
        assert key in data, (key, sorted(data))


def test_update_available_true_when_latest_greater(monkeypatch=None):
    """latest > installed (strict semver-greater) -> update_available True."""
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")

    def _probe():
        return "0.50.0"

    # Force a known installed version by stubbing _plugin_version.
    orig = st._plugin_version
    st._plugin_version = lambda lib_dir=None: "0.49.0"
    try:
        data = _with_project_dir(
            project_dir, lambda: st.status_data(release_probe=_probe))
    finally:
        st._plugin_version = orig
    assert data["latest_version"] == "0.50.0", data
    assert data["update_available"] is True, data
    assert data["release_check_error"] is None, data


def test_update_available_false_when_equal_or_lower():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    orig = st._plugin_version
    st._plugin_version = lambda lib_dir=None: "0.49.0"
    try:
        eq = _with_project_dir(
            project_dir, lambda: st.status_data(release_probe=lambda: "0.49.0"))
        lo = _with_project_dir(
            project_dir, lambda: st.status_data(release_probe=lambda: "0.40.0"))
    finally:
        st._plugin_version = orig
    assert eq["update_available"] is False, eq
    assert lo["update_available"] is False, lo


def test_update_available_false_when_installed_unknown():
    """A None installed plugin_version (source tree) -> update_available False
    even when latest is known (strict guard: false if EITHER is unknown)."""
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    orig = st._plugin_version
    st._plugin_version = lambda lib_dir=None: None
    try:
        data = _with_project_dir(
            project_dir, lambda: st.status_data(release_probe=lambda: "9.9.9"))
    finally:
        st._plugin_version = orig
    assert data["latest_version"] == "9.9.9", data
    assert data["update_available"] is False, data


def test_probe_error_is_tolerated_never_crashes():
    """A raising probe yields latest_version=None, update_available=False, a
    non-null release_check_error, and does NOT crash status_data."""
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")

    def _boom():
        raise RuntimeError("gh: command not found")

    data = _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=_boom))
    assert data["latest_version"] is None, data
    assert data["update_available"] is False, data
    assert data["release_check_error"], data
    assert "gh" in data["release_check_error"], data


def test_default_probe_never_crashes_status():
    """Calling status_data() with the DEFAULT (real gh) probe must never crash —
    it is tolerant regardless of gh/network availability."""
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    data = _with_project_dir(project_dir, st.status_data)
    # Whatever the environment, the fields are present and update_available is a
    # bool (never an exception).
    assert isinstance(data["update_available"], bool), data
    assert "latest_version" in data and "release_check_error" in data, data


# ==========================================================================
# render_status — the update-available / up-to-date / errored line.
# ==========================================================================

def _base_data(project_dir):
    return _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))


def test_render_status_update_available_line():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    data = _base_data(project_dir)
    data["plugin_version"] = "0.49.0"
    data["latest_version"] = "0.50.0"
    data["update_available"] = True
    data["release_check_error"] = None
    out = st.render_status(data)
    assert "0.50.0" in out, out
    assert "update available" in out.lower(), out
    # No emojis / non-ASCII beyond the arrow/rule chars (coding-rules §5).
    for ch in out:
        assert ord(ch) < 128 or ch in "→─", repr(ch)


def test_render_status_up_to_date_line():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    data = _base_data(project_dir)
    data["plugin_version"] = "0.50.0"
    data["latest_version"] = "0.50.0"
    data["update_available"] = False
    data["release_check_error"] = None
    out = st.render_status(data)
    assert "up to date" in out.lower(), out


def test_render_status_check_errored_note():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    data = _base_data(project_dir)
    data["plugin_version"] = "0.50.0"
    data["latest_version"] = None
    data["update_available"] = False
    data["release_check_error"] = "gh: no network"
    out = st.render_status(data)
    low = out.lower()
    assert ("check" in low or "error" in low or "unavailable" in low), out


# ==========================================================================
# semver comparison helper — strict-greater, tolerant of unknowns.
# ==========================================================================

def test_update_available_helper_strict_semver():
    assert st._update_available("0.50.0", "0.49.0") is True
    assert st._update_available("0.49.1", "0.49.0") is True
    assert st._update_available("1.0.0", "0.49.0") is True
    assert st._update_available("0.49.0", "0.49.0") is False
    assert st._update_available("0.48.0", "0.49.0") is False
    # Unknown either side -> False.
    assert st._update_available(None, "0.49.0") is False
    assert st._update_available("0.50.0", None) is False
    assert st._update_available(None, None) is False
    # Unparseable -> False (tolerant).
    assert st._update_available("garbage", "0.49.0") is False


# ==========================================================================
# CLI --json emits the new fields; the default human view stays crash-free.
# ==========================================================================

def _run_cli(args, project_dir):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [os.path.join(_FEATURES, d, "src") for d in (
            "fsm-contracts", "tick-orchestrator", "durable-state",
            "lifecycle-dispositions", "work-intake", "adapter-wiring",
            "prioritize", "implement", "safety-governance", "agent-dispatch",
            "observability", "verify-integrate")]
        + [env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, os.path.join(_SRC, "status.py")] + args,
        capture_output=True, text=True, env=env)


def test_cli_json_emits_release_fields():
    project_dir = tempfile.mkdtemp(prefix="sched-rel-")
    proc = _run_cli(["--json"], project_dir)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    for key in ("latest_version", "update_available", "release_check_error"):
        assert key in data, (key, sorted(data))
