#!/usr/bin/env python3
"""#337 runtime override-else-default resolution for load_route / load_adapter_map.

The config readers resolve, per file: a project-local override in
${project_dir}/.auto-maintainer/<name> if present, else the shipped read-only
default at ${CLAUDE_PLUGIN_ROOT}/config/default/<name>, else the caller's code
fallback. The three acceptance cases are exercised here:
  (a) absent override resolves to the shipped default;
  (b) present override wins over the shipped default;
  (c) a release-changed shipped default reaches an unoverridden install.
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
for _dep in ("fsm-contracts", "tick-orchestrator", "agent-dispatch"):
    _dep_src = os.path.join(_FEATURES_DIR, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402


# The conservative code fallback the caller supplies (never the shipped default).
_CODE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "EXIT"],
    "edges": [{"state": "GUARD", "signal": "GO", "next": "EXIT"}],
    "terminal": ["EXIT"],
}
_CODE_MAP = {"GUARD": "run_tick:make_guard"}


def _valid_route(states_tail):
    return {
        "schema_version": "1.0.0",
        "states": ["GUARD"] + states_tail + ["EXIT"],
        "edges": (
            [{"state": "GUARD", "signal": "GO",
              "next": (states_tail[0] if states_tail else "EXIT")}]
            + [{"state": s, "signal": "GO",
                "next": (states_tail[i + 1] if i + 1 < len(states_tail)
                         else "EXIT")}
               for i, s in enumerate(states_tail)]
        ),
        "terminal": ["EXIT"],
    }


def _write_shipped_default(plugin_root, name, obj):
    d = os.path.join(plugin_root, "config", "default")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as f:
        json.dump(obj, f)


def _write_override(project_dir, name, obj):
    d = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "w") as f:
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
    shipped = _valid_route(["SHIP"])
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, "route.json", shipped)
        route = _with_plugin_root(
            plugin, lambda: aw.load_route(_CODE_ROUTE, proj))
        assert route == shipped
        assert route != _CODE_ROUTE  # the shipped default, not the code fallback


def test_present_override_wins_over_shipped_default():
    shipped = _valid_route(["SHIP"])
    override = _valid_route(["OWN"])
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, "route.json", shipped)
        _write_override(proj, "route.json", override)
        route = _with_plugin_root(
            plugin, lambda: aw.load_route(_CODE_ROUTE, proj))
        assert route == override
        assert route != shipped


def test_release_changed_default_reaches_unoverridden_install():
    """With no override, load_route reads the shipped default live — so a release
    that changes the shipped default file changes what an install resolves, with
    no re-seed (the #337 staleness fix)."""
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        v1 = _valid_route(["V1"])
        _write_shipped_default(plugin, "route.json", v1)
        got1 = _with_plugin_root(
            plugin, lambda: aw.load_route(_CODE_ROUTE, proj))
        assert got1 == v1
        # A "release" rewrites the shipped default in place; the unoverridden
        # install now resolves the NEW default with no manual step.
        v2 = _valid_route(["V2"])
        _write_shipped_default(plugin, "route.json", v2)
        got2 = _with_plugin_root(
            plugin, lambda: aw.load_route(_CODE_ROUTE, proj))
        assert got2 == v2
        assert got2 != got1


def test_adapter_map_resolves_shipped_default_then_override():
    shipped = {"GUARD": "run_tick:make_guard", "EXIT": "run_tick:make_exit"}
    override = {"GUARD": "run_tick:make_guard"}
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        plugin = os.path.join(t, "plugin")
        _write_shipped_default(plugin, "adapter-map.json", shipped)
        # absent override -> shipped default.
        got = _with_plugin_root(
            plugin, lambda: aw.load_adapter_map(_CODE_MAP, proj))
        assert got == shipped
        # present override wins.
        _write_override(proj, "adapter-map.json", override)
        got2 = _with_plugin_root(
            plugin, lambda: aw.load_adapter_map(_CODE_MAP, proj))
        assert got2 == override


def test_falls_through_to_code_default_when_no_files():
    """No override and no shipped default (CLAUDE_PLUGIN_ROOT unset or the file
    absent) -> the caller's conservative code fallback."""
    with tempfile.TemporaryDirectory() as t:
        proj = os.path.join(t, "proj")
        os.makedirs(proj)
        # Ensure CLAUDE_PLUGIN_ROOT does not leak a real default into the test.
        prev = os.environ.pop("CLAUDE_PLUGIN_ROOT", None)
        try:
            route = aw.load_route(_CODE_ROUTE, proj)
            amap = aw.load_adapter_map(_CODE_MAP, proj)
        finally:
            if prev is not None:
                os.environ["CLAUDE_PLUGIN_ROOT"] = prev
        assert route == _CODE_ROUTE
        assert amap == _CODE_MAP
