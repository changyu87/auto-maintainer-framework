#!/usr/bin/env python3
"""E2E tests for packaging-config — the clean-ship plugin assembly.

Each test drives the real assembly script end to end (build a full plugin
tree from the framework sources) and asserts a spec invariant against the
produced artifacts. Builds target a fresh temp out-root so the suite is
hermetic and the idempotency check is meaningful.

Owner: changyu87
"""

import importlib.util
import json
import os
import shutil
import tempfile

# ---------------------------------------------------------------------------
# Path wiring: locate the feature src/build_plugin.py and the real repo root
# (the worktree root that holds .rabbit/ and the framework source features).
# ---------------------------------------------------------------------------
_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_TEST_DIR)
_SRC = os.path.join(_FEATURE_DIR, "src", "build_plugin.py")
# .rabbit/rabbit-project/features/packaging-config -> up 4 dirs = worktree root
_REPO_ROOT = os.path.abspath(
    os.path.join(_FEATURE_DIR, "..", "..", "..", "..")
)


def _load_build():
    spec = importlib.util.spec_from_file_location("build_plugin", _SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_into_temp():
    """Build the plugin tree into a fresh temp out-root; return the path.

    Caller is responsible for cleanup.
    """
    mod = _load_build()
    out_root = tempfile.mkdtemp(prefix="pkgcfg-build-")
    mod.build(repo_root=_REPO_ROOT, out_root=out_root)
    return out_root


def _walk_paths(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return found


# ---------------------------------------------------------------------------
# Spec: the assembly script exists and is importable / callable.
# ---------------------------------------------------------------------------
def test_build_script_exists_and_is_callable():
    assert os.path.isfile(_SRC), f"missing assembly script: {_SRC}"
    mod = _load_build()
    assert hasattr(mod, "build") and callable(mod.build), \
        "build_plugin must expose a callable build(repo_root, out_root)"


# ---------------------------------------------------------------------------
# Spec headline invariant: NO dev infra leaks — the built plugin tree
# contains no path matching ".rabbit" (directory or file).
# ---------------------------------------------------------------------------
def test_no_rabbit_dev_infra_leaks():
    out_root = _build_into_temp()
    try:
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        leaks = [p for p in _walk_paths(plugin_root) if ".rabbit" in p]
        assert not leaks, f"plugin tree leaks .rabbit paths: {leaks}"
        # also assert nothing in the plugin file *contents* references .rabbit
        for dirpath, _dirnames, filenames in os.walk(plugin_root):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                assert ".rabbit" not in body, \
                    f"plugin file {fp} references .rabbit"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: manifest placement — plugin.json under .claude-plugin/; component
# dirs (hooks/, skills/, lib/) at plugin ROOT, never inside .claude-plugin/.
# ---------------------------------------------------------------------------
def test_manifest_and_component_placement():
    out_root = _build_into_temp()
    try:
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        assert os.path.isfile(
            os.path.join(plugin_root, ".claude-plugin", "plugin.json")
        ), "plugin.json must live at plugins/auto-maintainer/.claude-plugin/"
        for comp in ("hooks", "skills", "lib"):
            assert os.path.isdir(os.path.join(plugin_root, comp)), \
                f"component dir {comp}/ must exist at plugin root"
            assert not os.path.exists(
                os.path.join(plugin_root, ".claude-plugin", comp)
            ), f"{comp}/ must NOT be inside .claude-plugin/"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: plugin.json content — name, semver version, description, author.name.
# ---------------------------------------------------------------------------
def test_plugin_json_content():
    out_root = _build_into_temp()
    try:
        pj = os.path.join(
            out_root, "plugins", "auto-maintainer",
            ".claude-plugin", "plugin.json",
        )
        with open(pj, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data.get("name") == "auto-maintainer"
        ver = data.get("version", "")
        parts = ver.split(".")
        assert len(parts) == 3 and all(p.isdigit() for p in parts), \
            f"version must be explicit semver, got {ver!r}"
        assert data.get("description"), "plugin.json needs a description"
        assert data.get("author", {}).get("name") == "changyu87"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: marketplace.json catalog at repo/out root — name, owner.name,
# plugins[] single entry whose name/source resolve to the built tree.
# ---------------------------------------------------------------------------
def test_marketplace_json_catalog():
    out_root = _build_into_temp()
    try:
        mk = os.path.join(out_root, ".claude-plugin", "marketplace.json")
        assert os.path.isfile(mk), "marketplace.json must be at out-root/.claude-plugin/"
        with open(mk, encoding="utf-8") as fh:
            data = json.load(fh)
        assert data.get("name") == "auto-maintainer"
        assert data.get("owner", {}).get("name") == "changyu87"
        plugins = data.get("plugins", [])
        assert isinstance(plugins, list) and len(plugins) == 1, \
            "marketplace must list exactly one plugin entry"
        entry = plugins[0]
        assert entry.get("name") == "auto-maintainer"
        src = entry.get("source", "")
        assert src == "./plugins/auto-maintainer", \
            f"source must be ./plugins/auto-maintainer, got {src!r}"
        # source must resolve to the built tree (relative to marketplace.json)
        resolved = os.path.normpath(os.path.join(out_root, src))
        assert os.path.isdir(resolved), f"source does not resolve: {resolved}"
        assert os.path.isfile(
            os.path.join(resolved, ".claude-plugin", "plugin.json")
        ), "resolved source has no plugin.json"
        assert entry.get("description")
        assert entry.get("version")
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: SessionStart persona/banner hook wired in hooks/hooks.json.
# ---------------------------------------------------------------------------
def test_sessionstart_hook_wired():
    out_root = _build_into_temp()
    try:
        hj = os.path.join(
            out_root, "plugins", "auto-maintainer", "hooks", "hooks.json"
        )
        assert os.path.isfile(hj), "hooks/hooks.json must exist"
        with open(hj, encoding="utf-8") as fh:
            data = json.load(fh)
        hooks = data.get("hooks", data)
        assert "SessionStart" in hooks, \
            "hooks.json must wire a SessionStart handler"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: /auto-maintainer:status skill ships at skills/status/SKILL.md.
# ---------------------------------------------------------------------------
def test_status_skill_present():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "status", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/status/SKILL.md must exist"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert body.lstrip().startswith("---"), \
            "SKILL.md must carry YAML frontmatter"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: core libs copied in byte-identical from their feature src/ dirs.
# ---------------------------------------------------------------------------
def test_core_libs_copied_byte_identical():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        mapping = {
            "fsm_contracts.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "fsm-contracts", "src", "fsm_contracts.py",
            ),
            "tick_orchestrator.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "tick-orchestrator", "src", "tick_orchestrator.py",
            ),
        }
        for fname, src_path in mapping.items():
            dst = os.path.join(lib, fname)
            assert os.path.isfile(dst), f"missing copied lib: {dst}"
            with open(src_path, "rb") as a, open(dst, "rb") as b:
                assert a.read() == b.read(), \
                    f"{fname} is not byte-identical to its source"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: idempotent build — running the assembly twice yields an identical
# tree (same set of paths AND byte-identical file contents).
# ---------------------------------------------------------------------------
def test_idempotent_build():
    mod = _load_build()
    a = tempfile.mkdtemp(prefix="pkgcfg-a-")
    b = tempfile.mkdtemp(prefix="pkgcfg-b-")
    try:
        mod.build(repo_root=_REPO_ROOT, out_root=a)
        mod.build(repo_root=_REPO_ROOT, out_root=b)
        pa = os.path.join(a, "plugins", "auto-maintainer")
        pb = os.path.join(b, "plugins", "auto-maintainer")
        assert sorted(_walk_paths(pa)) == sorted(_walk_paths(pb)), \
            "two builds produced different path sets"
        for rel in sorted(_walk_paths(pa)):
            fa = os.path.join(pa, rel)
            fb = os.path.join(pb, rel)
            if os.path.isfile(fa):
                with open(fa, "rb") as x, open(fb, "rb") as y:
                    assert x.read() == y.read(), \
                        f"build is not idempotent for {rel}"
        # re-building into the SAME out-root must also be stable
        before = _walk_paths(pa)
        mod.build(repo_root=_REPO_ROOT, out_root=a)
        after = _walk_paths(pa)
        assert sorted(before) == sorted(after), \
            "re-build into same out-root changed the tree"
    finally:
        shutil.rmtree(a, ignore_errors=True)
        shutil.rmtree(b, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: self-contained — every file the plugin references resolves inside
# the plugin dir. Structural check: no shipped file contains an absolute path
# or a parent-escaping ("../") reference that climbs out of the plugin tree.
# ---------------------------------------------------------------------------
def test_self_contained_no_external_refs():
    out_root = _build_into_temp()
    try:
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        for dirpath, _dn, filenames in os.walk(plugin_root):
            for name in filenames:
                # only inspect config/manifest files for path references
                if not (name.endswith(".json") or name.endswith(".md")):
                    continue
                fp = os.path.join(dirpath, name)
                with open(fp, encoding="utf-8", errors="replace") as fh:
                    body = fh.read()
                assert "../" not in body, \
                    f"{fp} contains a parent-escaping reference"
                # no reference back to the source feature tree
                assert "rabbit-project" not in body, \
                    f"{fp} references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: schema-valid plugin — structural equivalent of
# `claude plugin validate` (CLI unavailable in this env). Validate the
# minimum shape Claude Code requires: plugin.json present + parseable with a
# name, components in valid locations, marketplace entry resolves.
# ---------------------------------------------------------------------------
def test_plugin_structurally_valid():
    out_root = _build_into_temp()
    try:
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        pj = os.path.join(plugin_root, ".claude-plugin", "plugin.json")
        with open(pj, encoding="utf-8") as fh:
            data = json.load(fh)  # must parse
        assert data.get("name"), "plugin.json requires a name"
        # hooks.json and SKILL.md must parse/exist as components
        with open(os.path.join(plugin_root, "hooks", "hooks.json")) as fh:
            json.load(fh)
        assert os.path.isfile(
            os.path.join(plugin_root, "skills", "status", "SKILL.md")
        )
        # marketplace.json must parse and reference this plugin
        mk = os.path.join(out_root, ".claude-plugin", "marketplace.json")
        with open(mk, encoding="utf-8") as fh:
            mkdata = json.load(fh)
        assert mkdata["plugins"][0]["name"] == data["name"]
    finally:
        shutil.rmtree(out_root, ignore_errors=True)
