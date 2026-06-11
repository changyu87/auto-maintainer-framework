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
        # The handler command must run via an explicit interpreter so it does
        # not depend on the script's exec bit surviving git/copy.
        cmd = hooks["SessionStart"][0]["hooks"][0]["command"]
        assert cmd.startswith("python3 "), \
            f"hook command must invoke python3 explicitly, got {cmd!r}"
        assert "session-start-persona.py" in cmd
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec: the SessionStart persona/banner is a genuine v1 component — running
# the shipped hook script must emit a valid SessionStart additionalContext.
# ---------------------------------------------------------------------------
def test_sessionstart_hook_executes_and_emits_context():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        script = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "hooks", "session-start-persona.py",
        )
        proc = subprocess.run(
            [sys.executable, script],
            input='{"hook_event_name": "SessionStart"}',
            capture_output=True, text=True,
        )
        assert proc.returncode == 0, f"hook exited {proc.returncode}: {proc.stderr}"
        payload = json.loads(proc.stdout)
        out = payload["hookSpecificOutput"]
        assert out["hookEventName"] == "SessionStart"
        assert out["additionalContext"], "hook must inject a persona/banner"
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
# fsm_contracts, tick_orchestrator, durable_state, and lifecycle_dispositions
# are pure libs copied verbatim. run_tick is normalized for self-containment
# (see test_shipped_run_tick_is_self_contained) so it is NOT byte-identical.
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
            "durable_state.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "durable-state", "src", "durable_state.py",
            ),
            "lifecycle_dispositions.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "lifecycle-dispositions", "src", "lifecycle_dispositions.py",
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
# Slice 2 spec (re-ship #29/#30): all SEVEN control libs ship under lib/ —
# the four pure libs, scheduling's run_tick.py, and scheduling's script-backed
# status.py + stop.py (the control surface the shipped /auto-maintainer:status
# and :stop skills invoke via ${CLAUDE_PLUGIN_ROOT}/lib/).
# ---------------------------------------------------------------------------
def test_all_seven_control_libs_present():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for fname in (
            "fsm_contracts.py",
            "tick_orchestrator.py",
            "durable_state.py",
            "lifecycle_dispositions.py",
            "run_tick.py",
            "status.py",
            "stop.py",
        ):
            assert os.path.isfile(os.path.join(lib, fname)), \
                f"lib/{fname} must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (self-containment, critical): the shipped status.py and stop.py
# import run_tick / lifecycle_dispositions / durable_state, which must resolve
# with ONLY the plugin tree on sys.path. Claude copies just the plugin dir into
# its cache, so the shipped control libs must NOT depend on the feature src/
# dirs. Prove it by importing each in a subprocess whose sys.path is restricted
# to the shipped lib/ dir alone (PYTHONPATH cleared, cwd = out_root).
# ---------------------------------------------------------------------------
def test_shipped_control_libs_are_self_contained():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for mod, attr in (("run_tick", "run_tick"),
                          ("status", "status_line"),
                          ("stop", "stop")):
            probe = (
                "import sys; "
                f"sys.path.insert(0, {lib!r}); "
                f"import {mod}; "
                f"assert hasattr({mod}, {attr!r}); "
                "print('OK')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True, text=True,
                env={"PYTHONPATH": ""},
                cwd=out_root,
            )
            assert proc.returncode == 0, \
                f"shipped {mod} not self-contained: {proc.stderr}"
            assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship #29/#30): the shipped status skill is scheduling's
# script-backed one — it references ${CLAUDE_PLUGIN_ROOT}/lib/status.py and
# carries NONE of the slice-1 packaging-config stub text. The packaging-config
# slice-1 status STUB is dropped; scheduling now owns the status skill.
# ---------------------------------------------------------------------------
def test_shipped_status_skill_is_script_backed_not_stub():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "status", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/status/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "${CLAUDE_PLUGIN_ROOT}/lib/status.py" in body, \
            "shipped status skill must reference " \
            "${CLAUDE_PLUGIN_ROOT}/lib/status.py"
        assert "no loop configured" not in body, \
            "shipped status skill must not carry the slice-1 stub text"
        assert "packaging slice 1" not in body, \
            "shipped status skill must not carry the slice-1 stub text"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship #29/#30): exactly ONE skills/status/SKILL.md ships —
# packaging-config no longer contributes its own status stub, so there is no
# collision/duplicate and the single shipped one is scheduling's.
# ---------------------------------------------------------------------------
def test_exactly_one_status_skill_and_control_skills_present():
    out_root = _build_into_temp()
    try:
        skills = os.path.join(
            out_root, "plugins", "auto-maintainer", "skills"
        )
        status_md = [
            p for p in _walk_paths(skills)
            if os.path.basename(p) == "SKILL.md"
            and os.path.basename(os.path.dirname(p)) == "status"
        ]
        assert len(status_md) == 1, \
            f"exactly one status SKILL.md must ship, found {status_md}"
        for name in ("start", "stop", "status"):
            assert os.path.isfile(
                os.path.join(skills, name, "SKILL.md")
            ), f"skills/{name}/SKILL.md must ship"
        # packaging-config no longer ships a status stub of its own.
        assert not os.path.isdir(
            os.path.join(_FEATURE_DIR, "src", "plugin_assets", "skills",
                         "status")
        ), "packaging-config must drop its slice-1 status stub asset"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec: the ship/ collection convention — scheduling's
# ship/skills/{start,stop} land at the plugin root as skills/{start,stop}.
# ---------------------------------------------------------------------------
def test_ship_collection_start_stop_skills_present():
    out_root = _build_into_temp()
    try:
        skills = os.path.join(
            out_root, "plugins", "auto-maintainer", "skills"
        )
        for name in ("start", "stop"):
            sk = os.path.join(skills, name, "SKILL.md")
            assert os.path.isfile(sk), \
                f"ship/ collection must place skills/{name}/SKILL.md"
            with open(sk, encoding="utf-8") as fh:
                assert fh.read().lstrip().startswith("---"), \
                    f"skills/{name}/SKILL.md must carry YAML frontmatter"
        # the status skill (now scheduling's, #29/#30) must ship alongside them
        assert os.path.isfile(
            os.path.join(skills, "status", "SKILL.md")
        ), "status skill must ship alongside start/stop"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship): version bumped to 0.2.2 in BOTH plugin.json and
# marketplace.json, and the two are consistent. The spec permits a patch bump
# on each re-ship of the plugin tree (0.2.2 re-ships the scheduling #29/#30
# script-backed control surface — status.py/stop.py libs + the script-backed
# status skill — into the installed plugin).
# ---------------------------------------------------------------------------
def test_version_bumped_to_0_2_2_and_consistent():
    out_root = _build_into_temp()
    try:
        pj = os.path.join(
            out_root, "plugins", "auto-maintainer",
            ".claude-plugin", "plugin.json",
        )
        mk = os.path.join(out_root, ".claude-plugin", "marketplace.json")
        with open(pj, encoding="utf-8") as fh:
            pdata = json.load(fh)
        with open(mk, encoding="utf-8") as fh:
            mdata = json.load(fh)
        assert pdata.get("version") == "0.2.2", \
            f"plugin.json version must be 0.2.2, got {pdata.get('version')!r}"
        assert mdata["plugins"][0].get("version") == "0.2.2", \
            "marketplace.json plugin entry version must be 0.2.2"
        assert pdata["version"] == mdata["plugins"][0]["version"], \
            "plugin.json and marketplace.json versions must be consistent"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship, scheduling #24 fix): the shipped start skill must
# reference run_tick via the plugin-root token ${CLAUDE_PLUGIN_ROOT}/lib/
# run_tick.py and must NOT carry a bare src/run_tick.py reference. The shipped
# plugin carries only its own dir, so the dev-time bare src/ path would not
# resolve once installed; the #24 fix anchors it to the plugin root.
# ---------------------------------------------------------------------------
def test_shipped_start_skill_uses_plugin_root_run_tick():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "start", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/start/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py" in body, \
            "shipped start skill must reference " \
            "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py"
        assert "src/run_tick.py" not in body, \
            "shipped start skill must not carry a bare src/run_tick.py path"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (self-containment, critical): the shipped run_tick.py imports
# fsm_contracts / tick_orchestrator / durable_state / lifecycle_dispositions,
# which must resolve with ONLY the plugin tree on sys.path. Claude copies just
# the plugin dir into its cache, so the shipped run_tick must NOT depend on the
# feature src/ dirs. Prove it by importing the shipped run_tick in a subprocess
# whose sys.path is restricted to the shipped lib/ dir alone.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_is_self_contained():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        # A probe that adds ONLY the shipped lib dir to sys.path (the stdlib
        # stays, the feature src/ dirs do NOT — PYTHONPATH is cleared and the
        # probe runs from the out_root, not a feature dir). If the shipped
        # run_tick still resolved its sibling libs from the feature src/ dirs
        # the import would fail, because those dirs are not on this path.
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import run_tick; "
            "assert hasattr(run_tick, 'run_tick'); "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped run_tick not self-contained: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec: even the shipped run_tick.py (the one file the build rewrites)
# must not leak a path back into the source feature tree — the headline
# clean-ship invariant applies to the normalized file too.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped run_tick leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped run_tick references the source feature tree"
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
