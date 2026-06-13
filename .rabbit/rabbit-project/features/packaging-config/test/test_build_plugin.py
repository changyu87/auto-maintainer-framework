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
# Slice 2 spec (re-ship #?, prioritize.py + implement.py): all TWELVE control
# libs ship under lib/ — the four pure libs, scheduling's run_tick.py,
# scheduling's script-backed status.py + stop.py, work-intake's work_intake.py
# (the GitHub-Issues PULL adapter run_tick imports), scheduling's start.py (the
# deterministic fresh-start starter the /auto-maintainer:start skill invokes for
# tick #1), adapter-wiring's adapter_wiring.py (the route-as-data + adapter
# wiring mechanism run_tick imports), AND prioritize.py + implement.py (the two
# new deterministic adapter libs run_tick now imports so the installed plugin
# runs the PRIORITIZE/IMPLEMENT adapters self-contained).
# ---------------------------------------------------------------------------
def test_all_twelve_control_libs_present():
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
            "work_intake.py",
            "start.py",
            "adapter_wiring.py",
            "prioritize.py",
            "implement.py",
        ):
            assert os.path.isfile(os.path.join(lib, fname)), \
                f"lib/{fname} must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (self-containment, critical): the shipped run_tick.py, status.py,
# stop.py, work_intake.py, start.py, and adapter_wiring.py each import sibling
# libs that must resolve with ONLY the plugin tree on sys.path. Claude copies
# just the plugin dir into its cache, so the shipped control libs must NOT depend
# on the feature src/ dirs. work_intake imports fsm_contracts, start imports
# run_tick + lifecycle_dispositions, and adapter_wiring imports fsm_contracts +
# tick_orchestrator (all flat siblings in lib/), so each gets the same self-path
# bootstrap run_tick/status/stop get. Prove each in a subprocess whose sys.path
# is restricted to the shipped lib/ dir alone (PYTHONPATH cleared, cwd =
# out_root).
# ---------------------------------------------------------------------------
def test_shipped_control_libs_are_self_contained():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for mod, attr in (("run_tick", "run_tick"),
                          ("status", "status_line"),
                          ("stop", "stop"),
                          ("work_intake", "Pull"),
                          ("start", "start"),
                          ("adapter_wiring", "build_loop"),
                          ("prioritize", "PRIORITIZE_MANIFEST"),
                          ("implement", "IMPLEMENT_MANIFEST")):
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
# Re-ship (#109 DRAIN-crash fix): version bumped to 0.2.14 in BOTH plugin.json
# and marketplace.json, and the two are consistent. The spec permits a patch
# bump on each re-ship of the plugin tree (0.2.14 re-normalizes scheduling's
# run_tick.py to carry the #109 fix — the agent-dispatch pause no longer
# journals a counter-less intent that poisoned the next tick's DRAIN).
# ---------------------------------------------------------------------------
def test_version_bumped_to_0_2_14_and_consistent():
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
        assert pdata.get("version") == "0.2.14", \
            f"plugin.json version must be 0.2.14, got {pdata.get('version')!r}"
        assert mdata["plugins"][0].get("version") == "0.2.14", \
            "marketplace.json plugin entry version must be 0.2.14"
        assert pdata["version"] == mdata["plugins"][0]["version"], \
            "plugin.json and marketplace.json versions must be consistent"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship #59, route source): the whole point of the 0.2.6
# re-ship is that the installed plugin reports the loaded route source. The
# shipped run_tick.py emits `route=<src>` in the tick trace and the shipped
# status.py emits `route=<src>` in the status line, both via run_tick's
# route_source helper. Assert the shipped (rebuilt) libs carry that reporting —
# without it the re-ship would silently NOT deliver the #59 fix.
# ---------------------------------------------------------------------------
def test_shipped_libs_report_route_source():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        rt = os.path.join(lib, "run_tick.py")
        st = os.path.join(lib, "status.py")
        with open(rt, encoding="utf-8") as fh:
            rt_body = fh.read()
        with open(st, encoding="utf-8") as fh:
            st_body = fh.read()
        # run_tick exposes the route_source helper and emits route= in the trace
        assert "def route_source(" in rt_body, \
            "shipped run_tick must define the route_source helper (#59)"
        assert "route=" in rt_body, \
            "shipped run_tick must emit route= in the tick trace (#59)"
        # status reuses run_tick's route source and emits route= in its line
        assert "route_source_label" in st_body, \
            "shipped status must reuse run_tick.route_source_label (#59)"
        assert "route=" in st_body, \
            "shipped status must emit route= in the status line (#59)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (observability milestone, scheduling start rework v0.2.0): the #24
# plugin-root anchoring of run_tick now lives in the EXECUTOR (the tick skill),
# which owns the run_tick invocation. The reworked start skill (v0.2.0) no longer
# calls run_tick directly — it delegates the first tick to /auto-maintainer:tick
# — so the tick skill must reference run_tick via ${CLAUDE_PLUGIN_ROOT}/lib/
# run_tick.py and carry no bare src/run_tick.py path; the start skill must carry
# no bare src/run_tick.py path either. The shipped plugin carries only its own
# dir, so the dev-time bare src/ path would not resolve once installed.
# ---------------------------------------------------------------------------
def test_shipped_executor_uses_plugin_root_run_tick():
    out_root = _build_into_temp()
    try:
        tick = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        start = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "start", "SKILL.md",
        )
        assert os.path.isfile(tick), "skills/tick/SKILL.md must ship"
        assert os.path.isfile(start), "skills/start/SKILL.md must ship"
        with open(tick, encoding="utf-8") as fh:
            tick_body = fh.read()
        with open(start, encoding="utf-8") as fh:
            start_body = fh.read()
        assert "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py" in tick_body, \
            "shipped tick skill must reference " \
            "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py"
        assert "src/run_tick.py" not in tick_body, \
            "shipped tick skill must not carry a bare src/run_tick.py path"
        assert "src/run_tick.py" not in start_body, \
            "shipped start skill must not carry a bare src/run_tick.py path"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship #44): the shipped start skill invokes start.py via the
# plugin-root token ${CLAUDE_PLUGIN_ROOT}/lib/start.py (the deterministic
# starter that clears a latched STOPPED before tick #1) and carries NO inline
# python — disposition handling is never prompt-tier (spec-rules §1). The
# shipped plugin carries only its own dir, so the path must be plugin-root
# anchored, never a bare src/ path.
# ---------------------------------------------------------------------------
def test_shipped_start_skill_invokes_start_py_no_inline_python():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "start", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/start/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "${CLAUDE_PLUGIN_ROOT}/lib/start.py" in body, \
            "shipped start skill must reference " \
            "${CLAUDE_PLUGIN_ROOT}/lib/start.py"
        assert "src/start.py" not in body, \
            "shipped start skill must not carry a bare src/start.py path"
        # no inline python -c blocks: the starter owns the latch-clear decision
        assert "python3 -c" not in body and "python -c" not in body, \
            "shipped start skill must not hand-roll inline python"
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
# Re-ship #64 (ephemeral per-tick read products): the whole point of the 0.2.7
# re-ship is that the installed plugin's run_tick carries the #64 fix —
# work_items/work_orders are EPHEMERAL per tick, reset to reflect only what THIS
# tick's route produced (PULL writes work_items; TRIAGE writes work_orders) and
# NOT a stale count carried forward. Prove the SHIPPED run_tick carries the
# fixed logic AND is byte-identical to the build's own normalization of the
# scheduling source — so the rebuilt tree genuinely ships the merged fix.
# Without this, the version bump could land while the shipped run_tick still
# carried the OLD logic.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_64_ephemeral_fix():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        # The #64 fix gates each read-product on its producing state being in
        # the route, resetting to [] otherwise (ephemeral per tick).
        assert "#64" in shipped, \
            "shipped run_tick must carry the #64 ephemeral read-product fix"
        assert 'if "PULL" in route["states"] else []' in shipped, \
            "shipped run_tick must reset work_items to [] when PULL not routed"
        assert 'if "TRIAGE" in route["states"] else []' in shipped, \
            "shipped run_tick must reset work_orders to [] when TRIAGE not routed"
        # And it must be byte-identical to the build's own normalization of the
        # CURRENT scheduling source — proving the merged fix actually shipped.
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "scheduling", "src", "run_tick.py",
        )
        dst_name, (src_rel, anchor, bootstrap) = "run_tick.py", \
            mod._NORMALIZED_LIBS["run_tick.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped run_tick is not the normalized scheduling source bytes"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship #109 (DRAIN-crash fix): the whole point of the 0.2.14 re-ship is that
# the installed plugin's run_tick carries the #109 fix — the agent-dispatch
# PAUSE is deliberately NOT journaled (a counter-less agent-dispatch intent would
# poison the NEXT tick's DRAIN with KeyError 'target_counter'). Prove the SHIPPED
# run_tick carries the fixed pause logic (the #109 marker, and NO journal.record
# in the agent-state pause branch) AND is byte-identical to the build's own
# normalization of the CURRENT scheduling source — so the rebuilt tree genuinely
# ships the merged fix rather than the old crashing logic.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_109_drain_crash_fix():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        # The #109 fix is carried by the source's pause-branch comment explaining
        # why the agent dispatch is NOT journaled.
        assert "(#109)" in shipped, \
            "shipped run_tick must carry the #109 DRAIN-crash fix"
        assert "The pause is deliberately NOT" in shipped, \
            "shipped run_tick pause must document the no-journal #109 fix"
        # And it must be byte-identical to the build's own normalization of the
        # CURRENT scheduling source — proving the merged fix actually shipped.
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "scheduling", "src", "run_tick.py",
        )
        _dst_name, (_src_rel, anchor, bootstrap) = "run_tick.py", \
            mod._NORMALIZED_LIBS["run_tick.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped run_tick is not the normalized scheduling source bytes"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship #69 (status always reports work_orders): the whole point of the 0.2.8
# re-ship is that the installed plugin's status.py carries the #69 fix —
# work_orders is ALWAYS reported in the status line (including work_orders=0),
# matching the tick trace's unconditional work_orders=N field rather than
# omitting it when TRIAGE was not routed. Prove the SHIPPED status.py carries the
# fixed logic AND is byte-identical to the build's own normalization of the
# scheduling source — so the rebuilt tree genuinely ships the merged fix.
# Without this, the version bump could land while the shipped status.py still
# carried the OLD logic.
# ---------------------------------------------------------------------------
def test_shipped_status_carries_69_workorders_fix():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        st = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "status.py"
        )
        with open(st, encoding="utf-8") as fh:
            shipped = fh.read()
        # The #69 fix reads work_orders and emits it unconditionally in the line.
        assert "#69" in shipped, \
            "shipped status must carry the #69 always-report-work_orders fix"
        assert "persisted_work_orders_count" in shipped, \
            "shipped status must read the persisted work_orders count (#69)"
        assert "work_orders=" in shipped, \
            "shipped status must emit work_orders= in the status line (#69)"
        # And it must be byte-identical to the build's own normalization of the
        # CURRENT scheduling source — proving the merged fix actually shipped.
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "scheduling", "src", "status.py",
        )
        _dst_name, (_src_rel, anchor, bootstrap) = "status.py", \
            mod._NORMALIZED_LIBS["status.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped status is not the normalized scheduling source bytes"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship #44): the shipped start.py (another file the build
# rewrites for self-containment) must not leak a path back into the source
# feature tree — the headline clean-ship invariant applies to it too.
# ---------------------------------------------------------------------------
def test_shipped_start_py_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        st = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "start.py"
        )
        with open(st, encoding="utf-8") as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped start.py leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped start.py references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice 2 spec (re-ship, adapter_wiring): the shipped adapter_wiring.py (another
# file the build rewrites for self-containment — it imports sibling
# fsm_contracts + tick_orchestrator) must not leak a path back into the source
# feature tree — the headline clean-ship invariant applies to it too.
# ---------------------------------------------------------------------------
def test_shipped_adapter_wiring_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        aw = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "adapter_wiring.py"
        )
        with open(aw, encoding="utf-8") as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped adapter_wiring leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped adapter_wiring references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (prioritize.py + implement.py): the shipped prioritize.py and
# implement.py must be byte-identical to the build's OWN normalization of their
# scheduling-sibling sources. Each imports `import fsm_contracts as fc` and does
# NOT import os/sys at module top (identical shape to work_intake.py), so each
# is normalized via the work_intake-style with-imports bootstrap. Prove the
# rebuilt tree genuinely ships the normalized source bytes — mirroring the
# run_tick/status normalization-equality assertions.
# ---------------------------------------------------------------------------
def test_shipped_prioritize_implement_are_normalized_source_bytes():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        mapping = {
            "prioritize.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "prioritize", "src", "prioritize.py",
            ),
            "implement.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "implement", "src", "implement.py",
            ),
        }
        for fname, src in mapping.items():
            dst = os.path.join(lib, fname)
            assert os.path.isfile(dst), f"missing shipped lib: {dst}"
            with open(dst, encoding="utf-8") as fh:
                shipped = fh.read()
            _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS[fname]
            expected = mod._normalize_lib(src, anchor, bootstrap)
            assert shipped == expected, \
                f"shipped {fname} is not the normalized source bytes"
            # the with-imports bootstrap must be present (the file imports no
            # os/sys at top, so the bootstrap supplies them before the insert)
            assert "import os  # noqa: E402" in shipped, \
                f"shipped {fname} must carry the with-imports bootstrap"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (prioritize.py + implement.py): the two newly-shipped, build-rewritten
# libs must not leak a path back into the source feature tree — the headline
# clean-ship invariant applies to them too.
# ---------------------------------------------------------------------------
def test_shipped_prioritize_implement_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for fname in ("prioritize.py", "implement.py"):
            with open(os.path.join(lib, fname), encoding="utf-8") as fh:
                body = fh.read()
            assert ".rabbit" not in body, f"shipped {fname} leaks .rabbit"
            assert "rabbit-project" not in body, \
                f"shipped {fname} references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (prioritize.py + implement.py), critical self-containment: the shipped
# run_tick.py imports `import prioritize as pr` and `import implement as im`, so
# importing the shipped run_tick with ONLY the plugin lib/ on sys.path must
# resolve those two new siblings (and their fsm_contracts sibling) from lib/
# alone. Claude copies just the plugin dir into its cache, so a shipped run_tick
# that could not find prioritize/implement in lib/ would fail to import once
# installed. Prove it by importing the shipped run_tick in a subprocess whose
# sys.path is restricted to the shipped lib/ dir alone, with NOTHING else.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_imports_prioritize_implement_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import run_tick; "
            "import prioritize, implement; "
            "assert run_tick.pr is prioritize; "
            "assert run_tick.im is implement; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped run_tick cannot resolve prioritize/implement: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (safety_governance.py): the new safety-governance lib ships under
# lib/ — run_tick now imports it (`import safety_governance as sg`), so the
# installed plugin must carry it alongside the other control libs.
# ---------------------------------------------------------------------------
def test_safety_governance_lib_present():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        assert os.path.isfile(os.path.join(lib, "safety_governance.py")), \
            "lib/safety_governance.py must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (safety_governance.py): the shipped safety_governance.py must be
# byte-identical to the build's OWN normalization of its source. It imports
# `import lifecycle_dispositions as ld` and imports os (not sys) at module top
# — the SAME shape as adapter_wiring.py — so it is normalized via the
# adapter_wiring-style with-imports bootstrap. Prove the rebuilt tree genuinely
# ships the normalized source bytes — mirroring the run_tick/status/adapter
# normalization-equality assertions.
# ---------------------------------------------------------------------------
def test_shipped_safety_governance_is_normalized_source_bytes():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "safety_governance.py")
        assert os.path.isfile(dst), f"missing shipped lib: {dst}"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "safety-governance", "src", "safety_governance.py",
        )
        with open(dst, encoding="utf-8") as fh:
            shipped = fh.read()
        _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["safety_governance.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped safety_governance is not the normalized source bytes"
        # the with-imports bootstrap must be present (the file imports os but
        # not sys at top, so the bootstrap supplies sys before the insert; the
        # re-import of os is harmless — identical to adapter_wiring)
        assert "import sys  # noqa: E402" in shipped, \
            "shipped safety_governance must carry the with-imports bootstrap"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (safety_governance.py): the newly-shipped, build-rewritten lib must
# not leak a path back into the source feature tree — the headline clean-ship
# invariant applies to it too.
# ---------------------------------------------------------------------------
def test_shipped_safety_governance_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        with open(
            os.path.join(lib, "safety_governance.py"), encoding="utf-8"
        ) as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped safety_governance leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped safety_governance references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (safety_governance.py), critical self-containment: the shipped
# run_tick.py imports `import safety_governance as sg`, so importing the shipped
# run_tick with ONLY the plugin lib/ on sys.path must resolve safety_governance
# (and IT must resolve its lifecycle_dispositions sibling) from lib/ alone.
# Claude copies just the plugin dir into its cache, so a shipped run_tick that
# could not find safety_governance in lib/ would fail to import once installed.
# Prove it by importing the shipped run_tick (and safety_governance directly) in
# a subprocess whose sys.path is restricted to the shipped lib/ dir alone, with
# NOTHING else.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_imports_safety_governance_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import run_tick; "
            "import safety_governance, lifecycle_dispositions; "
            "assert run_tick.sg is safety_governance; "
            "assert safety_governance.ld is lifecycle_dispositions; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped run_tick cannot resolve safety_governance: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (safety_governance.py): v0.2.11 ships the updated governance lib whose
# DEFAULT_GOVERNANCE per_day_tokens is now null / no-limit (was 200000). Import
# the SHIPPED lib from lib/ alone and assert the default daily budget ceiling is
# None — the behaviour this re-ship exists to carry into the installed plugin.
# ---------------------------------------------------------------------------
def test_shipped_safety_governance_default_per_day_tokens_is_none():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import safety_governance as sg; "
            "assert sg.DEFAULT_GOVERNANCE['budget']['per_day_tokens'] is None, "
            "sg.DEFAULT_GOVERNANCE['budget']['per_day_tokens']; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped safety_governance default per_day_tokens not None: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (agent-adapter milestone): the new agent_dispatch.py lib ships under
# lib/. It is PURE stdlib (imports only json, no sibling libs), so it is copied
# byte-for-byte (like the four pure libs) — NOT normalized. run_tick and
# adapter_wiring both import it (`import agent_dispatch as ad`), so the installed
# plugin must carry it alongside the other control libs.
# ---------------------------------------------------------------------------
def test_agent_dispatch_lib_present_and_byte_identical():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "agent_dispatch.py")
        assert os.path.isfile(dst), \
            "lib/agent_dispatch.py must ship in the plugin tree"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "agent-dispatch", "src", "agent_dispatch.py",
        )
        with open(src, "rb") as a, open(dst, "rb") as b:
            assert a.read() == b.read(), \
                "agent_dispatch.py is not byte-identical to its source"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (agent-adapter milestone), critical self-containment: the shipped
# run_tick.py imports `import agent_dispatch as ad` and `import adapter_wiring
# as aw` (and uses aw.AgentState), so importing the shipped run_tick with ONLY
# the plugin lib/ on sys.path must resolve agent_dispatch + adapter_wiring (and
# AgentState) from lib/ alone. Claude copies just the plugin dir into its cache,
# so a shipped run_tick that could not find agent_dispatch/adapter_wiring in
# lib/ would fail to import once installed. Prove it in a subprocess whose
# sys.path is restricted to the shipped lib/ dir alone, with NOTHING else.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_imports_agent_dispatch_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import run_tick; "
            "import agent_dispatch, adapter_wiring; "
            "assert run_tick.ad is agent_dispatch; "
            "assert run_tick.aw is adapter_wiring; "
            "assert hasattr(adapter_wiring, 'AgentState'); "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped run_tick cannot resolve agent_dispatch: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (agent-adapter milestone), critical self-containment: the shipped
# adapter_wiring.py imports `import agent_dispatch as ad`, so importing the
# shipped adapter_wiring with ONLY the plugin lib/ on sys.path must resolve
# agent_dispatch (and its fsm_contracts + tick_orchestrator siblings) from lib/
# alone. Prove it in a subprocess whose sys.path is restricted to the shipped
# lib/ dir alone.
# ---------------------------------------------------------------------------
def test_shipped_adapter_wiring_imports_agent_dispatch_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import adapter_wiring; "
            "import agent_dispatch; "
            "assert adapter_wiring.ad is agent_dispatch; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped adapter_wiring cannot resolve agent_dispatch: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (agent-adapter milestone): the tick executor skill ships at
# skills/tick/SKILL.md via the ship/ collection convention (it lives in
# scheduling's ship/skills/tick/). It drives the --step/--resume yield/resume
# loop. Assert it ships and its frontmatter name is `tick`.
# ---------------------------------------------------------------------------
def test_ship_collection_tick_skill_present():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        assert os.path.isfile(sk), \
            "ship/ collection must place skills/tick/SKILL.md"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert body.lstrip().startswith("---"), \
            "skills/tick/SKILL.md must carry YAML frontmatter"
        assert "\nname: tick\n" in body, \
            "skills/tick/SKILL.md frontmatter name must be `tick`"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (agent-adapter milestone): the auto-maintainer-echo subagent ships at
# agents/auto-maintainer-echo.md via the ship/ collection convention (it lives
# in scheduling's ship/agents/). It is the executor-proof triager the tick skill
# dispatches at the TRIAGE agent-state. Assert it ships and its frontmatter name
# is `auto-maintainer-echo`.
# ---------------------------------------------------------------------------
def test_ship_collection_echo_agent_present():
    out_root = _build_into_temp()
    try:
        ag = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "agents", "auto-maintainer-echo.md",
        )
        assert os.path.isfile(ag), \
            "ship/ collection must place agents/auto-maintainer-echo.md"
        with open(ag, encoding="utf-8") as fh:
            body = fh.read()
        assert body.lstrip().startswith("---"), \
            "agents/auto-maintainer-echo.md must carry YAML frontmatter"
        assert "\nname: auto-maintainer-echo\n" in body, \
            "echo agent frontmatter name must be `auto-maintainer-echo`"
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


# ---------------------------------------------------------------------------
# Re-ship (observability milestone): the new observability.py lib ships under
# lib/. It is PURE stdlib (imports only json + os, no sibling libs), so it is
# copied byte-for-byte (like the other pure libs) — NOT normalized. run_tick now
# imports it (`import observability as ob`) and emits structured tick events, so
# the installed plugin must carry it alongside the other control libs.
# ---------------------------------------------------------------------------
def test_observability_lib_present_and_byte_identical():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "observability.py")
        assert os.path.isfile(dst), \
            "lib/observability.py must ship in the plugin tree"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "observability", "src", "observability.py",
        )
        with open(src, "rb") as a, open(dst, "rb") as b:
            assert a.read() == b.read(), \
                "observability.py is not byte-identical to its source"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (observability milestone): the newly-shipped observability.py must not
# leak a path back into the source feature tree — the headline clean-ship
# invariant applies to it too (it is pure stdlib, so this is a sanity guard).
# ---------------------------------------------------------------------------
def test_shipped_observability_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        with open(
            os.path.join(lib, "observability.py"), encoding="utf-8"
        ) as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped observability leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped observability references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (observability milestone), critical self-containment: the shipped
# run_tick.py imports `import observability as ob` (alongside agent_dispatch +
# adapter_wiring), so importing the shipped run_tick with ONLY the plugin lib/ on
# sys.path must resolve observability + agent_dispatch + adapter_wiring from lib/
# alone. Claude copies just the plugin dir into its cache, so a shipped run_tick
# that could not find observability in lib/ would fail to import once installed.
# Prove it in a subprocess whose sys.path is restricted to the shipped lib/ dir
# alone, with NOTHING else.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_imports_observability_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import run_tick; "
            "import observability, agent_dispatch, adapter_wiring; "
            "assert run_tick.ob is observability; "
            "assert run_tick.ad is agent_dispatch; "
            "assert run_tick.aw is adapter_wiring; "
            "assert hasattr(observability, 'EventLog'); "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped run_tick cannot resolve observability: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (observability milestone): the reworked start skill (scheduling v0.2.0,
# collected via ship/) ships at skills/start/SKILL.md. Assert its frontmatter
# version is 0.2.0 and the body references the deterministic latch-clear flag
# `--clear-only` and drives the first tick through the executor
# `/auto-maintainer:tick`.
# ---------------------------------------------------------------------------
def test_shipped_start_skill_is_v0_2_0_clear_only_and_executor():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "start", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/start/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "\nversion: 0.2.0\n" in body, \
            "shipped start skill frontmatter version must be 0.2.0"
        assert "--clear-only" in body, \
            "shipped start skill must reference the --clear-only latch-clear flag"
        assert "/auto-maintainer:tick" in body, \
            "shipped start skill must drive the first tick through the executor"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (observability milestone): the hardened tick skill (scheduling v0.1.1,
# collected via ship/) ships at skills/tick/SKILL.md. Assert its frontmatter
# version is 0.1.1 and the body instructs writing the dispatch result with the
# `Write` tool to the ABSOLUTE dispatch-result.json path under
# ${CLAUDE_PROJECT_DIR}/.auto-maintainer/.
# ---------------------------------------------------------------------------
def test_shipped_tick_skill_is_v0_1_1_write_tool_absolute_path():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/tick/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "\nversion: 0.1.1\n" in body, \
            "shipped tick skill frontmatter version must be 0.1.1"
        assert "`Write` tool" in body, \
            "shipped tick skill must instruct using the `Write` tool"
        assert "${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json" \
            in body, \
            "shipped tick skill must reference the absolute dispatch-result path"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)
