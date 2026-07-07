#!/usr/bin/env python3
"""#337 runtime override-else-default resolution for load_config.

load_config resolves, in order: the project-local override
${project_dir}/.auto-maintainer/config.json (backfilled), else the legacy
governance.json (migrated once), else the shipped read-only default at
${CLAUDE_PLUGIN_ROOT}/config/default/config.json (backfilled), else the code
DEFAULT_GOVERNANCE. The three acceptance cases for the shipped-default layer are
exercised here.
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
_LD_SRC = os.path.join(_FEATURES_DIR, "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)
_FSM_SRC = os.path.join(_FEATURES_DIR, "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import safety_governance as sg  # noqa: E402


def _write_shipped_default(plugin_root, obj):
    d = os.path.join(plugin_root, "config", "default")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(obj, f)


def _write_override(project_dir, obj):
    d = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "config.json"), "w") as f:
        json.dump(obj, f)


def _with_plugin_root(plugin_root, fn):
    prev = os.environ.get("CLAUDE_PLUGIN_ROOT")
    os.environ["CLAUDE_PLUGIN_ROOT"] = plugin_root
    try:
        return fn()
    finally:
        if prev is None:
            os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        else:
            os.environ["CLAUDE_PLUGIN_ROOT"] = prev


def test_absent_override_resolves_to_shipped_default():
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, {"mode": "auto-merge"})
        cfg = _with_plugin_root(plugin, lambda: sg.load_config(proj))
        # The shipped default's mode is surfaced (backfilled from DEFAULT).
        assert cfg["mode"] == "auto-merge"
        # And the code default (dry-run) is NOT what was used.
        assert cfg["mode"] != sg.DEFAULT_GOVERNANCE["mode"]


def test_present_override_wins_over_shipped_default():
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, {"mode": "auto-merge"})
        _write_override(proj, {"mode": "propose"})
        cfg = _with_plugin_root(plugin, lambda: sg.load_config(proj))
        assert cfg["mode"] == "propose"


def test_release_changed_default_reaches_unoverridden_install():
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, {"heartbeat": {"interval_minutes": 3}})
        cfg1 = _with_plugin_root(plugin, lambda: sg.load_config(proj))
        assert cfg1["heartbeat"]["interval_minutes"] == 3
        # A "release" rewrites the shipped default; the unoverridden install
        # resolves the new value with no re-seed.
        _write_shipped_default(plugin, {"heartbeat": {"interval_minutes": 7}})
        cfg2 = _with_plugin_root(plugin, lambda: sg.load_config(proj))
        assert cfg2["heartbeat"]["interval_minutes"] == 7


def test_falls_through_to_code_default_when_no_files():
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        prev = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        try:
            cfg = sg.load_config(proj)
        finally:
            if prev is not None:
                os.environ["CLAUDE_PLUGIN_ROOT"] = prev
        assert cfg["mode"] == sg.DEFAULT_GOVERNANCE["mode"]
