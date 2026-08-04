#!/usr/bin/env python3
"""End-to-end tests for status.py's LOCAL-CONFIG presence warning.

status WARNS when the project-local ``config.json`` is MISSING or EMPTY, so an
operator immediately sees a wrong-anchor / unconfigured run (the trap where
``run_tick`` silently falls back to shipped-default governance with an EMPTY
``issue_filter`` + aggressive ``auto-merge`` and pulls ALL open issues).

``status.py`` resolves ``config_path`` = ``<runtime_dir>/config.json`` (the SAME
path ``load_config`` reads) and computes ``local_config_present``: True ONLY when
that file exists AND parses to a NON-EMPTY JSON object — an absent file, an
unreadable/invalid file, or an empty ``{}`` all yield False. The check NEVER
crashes status (a read/parse error is treated as absent, not raised).
``status_data()`` exposes ``config_path`` (absolute) and ``local_config_present``.
``render_status`` shows, when False, a LOUD warning naming ``config_path`` + that
shipped defaults / no scope filter / aggressive auto-merge are in effect; when
True, a quiet ``config  <config_path>`` confirmation. ``--json`` emits the new
fields; the ``--line`` legacy one-liner is unchanged.

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
_DEPS = ("fsm-contracts", "tick-orchestrator", "durable-state",
         "lifecycle-dispositions", "work-intake", "adapter-wiring",
         "prioritize", "implement", "safety-governance", "agent-dispatch",
         "observability", "verify-integrate")
for _dep in _DEPS:
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


def _write_config(project_dir, content, raw=None):
    """Write the project-local config.json. ``raw`` writes verbatim text (used
    for the invalid-JSON case); otherwise ``content`` is json-dumped."""
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    path = os.path.join(cfg, "config.json")
    with open(path, "w") as f:
        if raw is not None:
            f.write(raw)
        else:
            json.dump(content, f)
    return path


# ==========================================================================
# _local_config_present — True only for an existing NON-EMPTY dict file.
# ==========================================================================

def test_local_config_present_true_for_nonempty():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    path = _write_config(project_dir, {"mode": "auto-merge"})
    assert st._local_config_present(path) is True


def test_local_config_present_false_when_absent():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    path = os.path.join(project_dir, ".auto-maintainer", "config.json")
    assert st._local_config_present(path) is False


def test_local_config_present_false_for_empty_object():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    path = _write_config(project_dir, {})
    assert st._local_config_present(path) is False


def test_local_config_present_false_for_invalid_json():
    """An unreadable/invalid file is treated as absent — never raises."""
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    path = _write_config(project_dir, None, raw="{ this is not json ")
    assert st._local_config_present(path) is False


def test_local_config_present_false_for_non_dict():
    """A valid JSON that is not an object (e.g. a list) is not a config."""
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    path = _write_config(project_dir, [1, 2, 3])
    assert st._local_config_present(path) is False


# ==========================================================================
# status_data — exposes config_path (absolute) + local_config_present.
# ==========================================================================

def test_status_data_exposes_config_fields_present():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    _write_config(project_dir, {"mode": "propose"})
    data = _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))
    assert "config_path" in data, sorted(data)
    assert "local_config_present" in data, sorted(data)
    assert os.path.isabs(data["config_path"]), data["config_path"]
    # config_path is <runtime_dir>/config.json (the SAME path load_config reads).
    assert data["config_path"] == os.path.join(
        data["runtime_dir"], "config.json"), data
    assert data["local_config_present"] is True, data


def test_status_data_config_absent_is_false():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    data = _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))
    assert data["local_config_present"] is False, data
    assert data["config_path"].endswith(
        os.path.join(".auto-maintainer", "config.json")), data


def test_status_data_config_check_non_mutating():
    """Computing local_config_present must NOT create the runtime dir."""
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer")), \
        "status_data created the runtime dir"


# ==========================================================================
# render_status — loud warning when absent, quiet confirmation when present.
# ==========================================================================

def _base_data(project_dir):
    return _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))


def test_render_status_warns_when_config_absent():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    data = _base_data(project_dir)
    assert data["local_config_present"] is False, data
    out = st.render_status(data)
    low = out.lower()
    # Names the config_path + surfaces the shipped-defaults / auto-merge trap.
    assert data["config_path"] in out, out
    assert "warning" in low, out
    assert "shipped default" in low, out
    assert "auto-merge" in low, out
    # No emojis / non-ASCII beyond the arrow/rule chars (coding-rules §5).
    for ch in out:
        assert ord(ch) < 128 or ch in "→─", repr(ch)


def test_render_status_quiet_confirmation_when_present():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    _write_config(project_dir, {"mode": "auto-merge"})
    data = _base_data(project_dir)
    assert data["local_config_present"] is True, data
    out = st.render_status(data)
    low = out.lower()
    # A quiet `config <path>` confirmation, NOT the loud warning.
    assert data["config_path"] in out, out
    assert "warning" not in low, out
    assert "shipped default" not in low, out


# ==========================================================================
# CLI — --json emits the new fields; --line legacy one-liner unchanged.
# ==========================================================================

def _run_cli(args, project_dir):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [os.path.join(_FEATURES, d, "src") for d in _DEPS]
        + [env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, os.path.join(_SRC, "status.py")] + args,
        capture_output=True, text=True, env=env)


def test_cli_json_emits_config_fields():
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    proc = _run_cli(["--json"], project_dir)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "config_path" in data, sorted(data)
    assert "local_config_present" in data, sorted(data)


def test_cli_line_unchanged_no_config_fields():
    """The legacy --line one-liner is unchanged: it carries no config_path /
    local_config_present tokens."""
    project_dir = tempfile.mkdtemp(prefix="sched-cfg-")
    proc = _run_cli(["--line"], project_dir)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("[status] disposition="), proc.stdout
    assert "local_config_present" not in proc.stdout, proc.stdout
    assert "config_path" not in proc.stdout, proc.stdout
