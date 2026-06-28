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
    """Relative paths under root, ignoring Python bytecode caches.

    __pycache__/ dirs and *.pyc files are regenerated whenever the shipped
    modules are imported or compiled in place (e.g. running this suite twice in
    one checkout, or a CI job that compiles then tests). They are gitignored and
    never part of the source tree, so excluding them keeps committed-vs-fresh and
    build-idempotency comparisons from reporting spurious drift.
    """
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # prune bytecode-cache dirs so neither they nor their contents appear
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in dirnames + filenames:
            if name.endswith(".pyc"):
                continue
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
# Spec (#16): the shipped plugin carries its OWN README.md at the plugin root
# (plugins/auto-maintainer/README.md) so a user inspecting the installed plugin
# (or the cache) finds usage docs there too — per the Claude Code plugin docs
# best practice ("Add a README.md with installation and usage instructions").
# It must name the plugin, document the /reload-plugins install step, list ALL
# shipped slash commands (DERIVED from the skills/ dir so none is omitted), and
# state the ACCURATE v1-complete status (NOT the stale "packaging skeleton").
# ---------------------------------------------------------------------------
def test_plugin_internal_readme_present_with_expected_content():
    out_root = _build_into_temp()
    try:
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        rm = os.path.join(plugin_root, "README.md")
        assert os.path.isfile(rm), \
            "plugins/auto-maintainer/README.md must ship inside the plugin"
        with open(rm, encoding="utf-8") as fh:
            body = fh.read()

        assert "auto-maintainer" in body, "plugin README must name the plugin"
        assert "/reload-plugins" in body, \
            "plugin README must document the /reload-plugins install step"
        assert "/auto-maintainer:status" in body, \
            "plugin README must document the /auto-maintainer:status skill"

        # Status must be ACCURATE: v1 complete, full loop live-proven — NOT the
        # stale "packaging skeleton" wording the prior attempt (PR #200) carried.
        assert "v1 complete" in body, \
            "plugin README must state the accurate v1-complete status"
        assert "skeleton" not in body.lower(), \
            "plugin README must not call the plugin a packaging skeleton"
        # the configure row sets the central config.json, not "governance config".
        assert "config.json" in body, \
            "configure row must reference the central config.json"
        assert "governance config" not in body, \
            "configure row must not say 'governance config'"

        # The Commands section must list EVERY shipped slash command — derived
        # from the shipped skills/ dir so it can never omit one.
        skills_dir = os.path.join(plugin_root, "skills")
        shipped = sorted(
            name for name in os.listdir(skills_dir)
            if os.path.isdir(os.path.join(skills_dir, name))
        )
        # sanity: the v0.3.0 configurables overhaul ships these seven commands.
        assert set(shipped) == {
            "start", "stop", "status", "tick", "configure", "route",
            "adapter-map",
        }, f"unexpected shipped skill set: {shipped}"
        for name in shipped:
            assert f"`/auto-maintainer:{name}`" in body, \
                f"Commands table omits shipped command /auto-maintainer:{name}"

        # the README is an asset, not dev infra, so the clean-ship invariant
        # applies: no reference back to the source feature tree.
        assert ".rabbit" not in body, "plugin README leaks .rabbit"
        assert "rabbit-project" not in body, \
            "plugin README references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Spec (#16): the Commands table is GENERATED from the shipped skills/ dir, so a
# newly shipped skill with no curated README description must FAIL the build —
# this is the guard that keeps the shipped README complete as commands are added.
# ---------------------------------------------------------------------------
def test_build_fails_when_a_shipped_skill_has_no_readme_description():
    mod = _load_build()
    out_root = tempfile.mkdtemp(prefix="pkgcfg-build-")
    try:
        mod.build(repo_root=_REPO_ROOT, out_root=out_root)
        plugin_root = os.path.join(out_root, "plugins", "auto-maintainer")
        # simulate a freshly shipped skill the description map doesn't cover.
        os.makedirs(os.path.join(plugin_root, "skills", "brand-new-cmd"))
        try:
            mod._render_readme(plugin_root)
        except RuntimeError as exc:
            assert "brand-new-cmd" in str(exc), \
                "the build error must name the undocumented skill"
        else:
            raise AssertionError(
                "rendering the README for a skill with no curated description "
                "must raise so the shipped README stays complete"
            )
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
# Release (v0.7.9, file-referenced dispatch prompts release): version bumped
# to 0.7.9 in BOTH plugin.json and marketplace.json, and the two are consistent.
# v0.7.9 is the release that DEPLOYS #304 (file-referenced dispatch prompts) into
# the installed plugin: scheduling's run_tick now writes each dispatch's rendered
# invocation envelope to a `prompt_path` file and hands the executor only the
# path, and the shipped tick skill (v0.6.0) documents the file-referenced
# dispatch protocol (no inline prompt). It regenerates the committed plugin tree
# from CURRENT src. (Supersedes the v0.7.8 work_own_filings opt-out release.)
# ---------------------------------------------------------------------------
def test_version_bumped_to_0_7_9_and_consistent():
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
        assert pdata.get("version") == "0.7.9", \
            f"plugin.json version must be 0.7.9, got {pdata.get('version')!r}"
        assert mdata["plugins"][0].get("version") == "0.7.9", \
            "marketplace.json plugin entry version must be 0.7.9"
        assert pdata["version"] == mdata["plugins"][0]["version"], \
            "plugin.json and marketplace.json versions must be consistent"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship #123 (budget-window-on-agent-tick): the whole point of the 0.2.17
# re-ship is that the installed plugin's run_tick carries the #123 fix — an
# agent-route tick pauses at the first agent-state and returns BEFORE the
# terminal budget-persist block, so run_tick now persists the durable budget
# window BEFORE returning the pause outcome (load-modify-save only BUDGET_KEY)
# and a resume reuses the persisted window without re-rolling. Prove the SHIPPED
# run_tick carries the fixed logic (the #123 marker, the pre-return budget
# persist, and the resume carry-forward) AND is byte-identical to the build's own
# normalization of the CURRENT scheduling source — so the rebuilt tree genuinely
# ships the merged fix rather than the old logic that lost the window.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_123_budget_window_fix():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        # The #123 fix persists the durable budget window before returning the
        # agent-route pause outcome, and carries the persisted window on resume.
        assert "(#123)" in shipped, \
            "shipped run_tick must carry the #123 budget-window-on-agent fix"
        assert "doc[BUDGET_KEY] = new_budget_state" in shipped, \
            "shipped run_tick must persist the budget window before the agent " \
            "pause returns (#123)"
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
# Re-ship #119 (handoff hardening): the whole point of the 0.2.16 re-ship is that
# the installed plugin's agent_dispatch.py carries the #119 fix — render() frames
# the embedded output_contract as a CONCRETE example to mimic, and
# validate_agent_adapter REJECTS a JSON-Schema descriptor in the
# output_example / output_schema slot (the descriptor guard). Prove the SHIPPED
# agent_dispatch carries the #119 markers AND, importing it from lib/ alone,
# enforces the descriptor guard at runtime — so the rebuilt tree genuinely ships
# the merged fix rather than the old pre-#119 logic.
# ---------------------------------------------------------------------------
def test_shipped_agent_dispatch_carries_119_handoff_hardening():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        ad = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "agent_dispatch.py"
        )
        with open(ad, encoding="utf-8") as fh:
            shipped = fh.read()
        # the #119 markers: descriptor guard + output_example field/alias
        assert "#119" in shipped, \
            "shipped agent_dispatch must carry the #119 handoff-hardening fix"
        assert "output_example" in shipped, \
            "shipped agent_dispatch must support the output_example field (#119)"
        assert "output_schema" in shipped, \
            "shipped agent_dispatch must keep the output_schema back-compat alias"
        assert "_is_schema_descriptor" in shipped, \
            "shipped agent_dispatch must carry the JSON-Schema descriptor guard"
        # runtime: validate_agent_adapter must REJECT a JSON-Schema descriptor in
        # the output_example slot, importing the shipped lib from lib/ alone. The
        # adapter is otherwise fully valid, so the descriptor is the SOLE
        # violation — proving the #119 guard (not some unrelated check) fires.
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import copy, sys\n"
            f"sys.path.insert(0, {lib!r})\n"
            "import agent_dispatch as ad\n"
            "base={'kind':'agent',"
            "'manifest':{'reads':['a'],'writes':['b'],'emits':['c']},"
            "'dispatch':[{'subagent_type':'t','inputs':[],"
            "'cardinality':'once','writes':'w'}],"
            "'signal':{'rule':'always_ok'}}\n"
            "ad.validate_agent_adapter(copy.deepcopy(base))\n"
            "bad=copy.deepcopy(base)\n"
            "bad['dispatch'][0]['output_example']="
            "{'type':'array','items':{'type':'object'}}\n"
            "raised=False\n"
            "try:\n"
            "    ad.validate_agent_adapter(bad)\n"
            "except ValueError:\n"
            "    raised=True\n"
            "assert raised, 'descriptor must be rejected'\n"
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped agent_dispatch descriptor guard not enforced: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
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
# Re-ship (protocol-free-subagent context isolation): the auto-maintainer-echo
# subagent ships at agents/auto-maintainer-echo.md via the ship/ collection
# convention (it lives in scheduling's ship/agents/). It is the executor-proof
# triager the tick skill dispatches at the TRIAGE agent-state. The reworked
# v2.0.0 agent is PROTOCOL-FREE: it carries no baked-in output schema or
# output_path (the invocation-envelope prompt's ## Handoff is the sole source),
# and it has a `Write` tool so it writes its own output file. Assert it ships,
# its name is `auto-maintainer-echo`, its version is 2.0.0, its tools include
# Write, and it carries no baked-in schema/output_path.
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
        assert "\nversion: 2.0.0\n" in body, \
            "echo agent frontmatter version must be 2.0.0"
        assert "Write" in body, \
            "echo agent tools must include Write (it writes its own output file)"
        # protocol-free: the prompt's ## Handoff is the sole source of the schema
        # and the output path, so the agent body bakes in neither.
        assert "output_path" not in body, \
            "protocol-free echo agent must not bake in an output_path"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (0.2.18, auto-maintainer-triager): the new triager subagent ships at
# agents/auto-maintainer-triager.md via the ship/ collection convention (it
# lives in work-intake's ship/agents/). It is the TRIAGE-state judge the tick
# skill dispatches by subagent_type. The build walks EVERY feature's ship/ dir,
# so it lands alongside the existing auto-maintainer-echo agent with NO build
# change beyond the version bump. Assert it ships, its frontmatter name is
# `auto-maintainer-triager`, and it is byte-identical to the work-intake source.
# ---------------------------------------------------------------------------
def test_ship_collection_triager_agent_present():
    out_root = _build_into_temp()
    try:
        agents = os.path.join(
            out_root, "plugins", "auto-maintainer", "agents",
        )
        ag = os.path.join(agents, "auto-maintainer-triager.md")
        assert os.path.isfile(ag), \
            "ship/ collection must place agents/auto-maintainer-triager.md"
        # it ships ALONGSIDE the existing echo agent (both via ship/ collection)
        assert os.path.isfile(
            os.path.join(agents, "auto-maintainer-echo.md")
        ), "echo agent must still ship alongside the triager agent"
        with open(ag, encoding="utf-8") as fh:
            body = fh.read()
        assert body.lstrip().startswith("---"), \
            "agents/auto-maintainer-triager.md must carry YAML frontmatter"
        assert "\nname: auto-maintainer-triager\n" in body, \
            "triager agent frontmatter name must be `auto-maintainer-triager`"
        # byte-identical to the work-intake source (the ship/ collection copies
        # it verbatim — no build-time normalization for shipped agents).
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "work-intake", "ship", "agents", "auto-maintainer-triager.md",
        )
        with open(src, "rb") as a, open(ag, "rb") as b:
            assert a.read() == b.read(), \
                "shipped triager agent is not byte-identical to its source"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (#209, auto-maintainer-reviewer): the model-backed REVIEW-gate reviewer
# subagent ships at agents/auto-maintainer-reviewer.md via the ship/ collection
# convention (it lives in verify-integrate's ship/agents/). It is the REVIEW-state
# judge the tick skill dispatches by subagent_type. The build walks EVERY
# feature's ship/ dir, so it lands alongside the other agents with NO build change.
# Assert it ships, its frontmatter name is `auto-maintainer-reviewer`, and it is
# byte-identical to the verify-integrate source.
# ---------------------------------------------------------------------------
def test_ship_collection_reviewer_agent_present():
    out_root = _build_into_temp()
    try:
        agents = os.path.join(
            out_root, "plugins", "auto-maintainer", "agents",
        )
        ag = os.path.join(agents, "auto-maintainer-reviewer.md")
        assert os.path.isfile(ag), \
            "ship/ collection must place agents/auto-maintainer-reviewer.md"
        with open(ag, encoding="utf-8") as fh:
            body = fh.read()
        assert body.lstrip().startswith("---"), \
            "agents/auto-maintainer-reviewer.md must carry YAML frontmatter"
        assert "\nname: auto-maintainer-reviewer\n" in body, \
            "reviewer agent frontmatter name must be `auto-maintainer-reviewer`"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "verify-integrate", "ship", "agents",
            "auto-maintainer-reviewer.md",
        )
        with open(src, "rb") as a, open(ag, "rb") as b:
            assert a.read() == b.read(), \
                "shipped reviewer agent is not byte-identical to its source"
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
# Minor (v0.3.0, configurables overhaul): the reworked start skill (scheduling
# v0.3.0, collected via ship/) ships at skills/start/SKILL.md. Assert its
# frontmatter version is 0.3.0, the body references the deterministic latch-clear
# flag `--clear-only`, drives the first tick through the executor
# `/auto-maintainer:tick`, and schedules a CONFIG-DRIVEN heartbeat interval
# (heartbeat.interval_minutes, read via start.py --print-interval) rather than a
# hardcoded cadence.
# ---------------------------------------------------------------------------
def test_shipped_start_skill_is_v0_3_0_clear_only_executor_config_interval():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "start", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/start/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "\nversion: 0.3.0\n" in body, \
            "shipped start skill frontmatter version must be 0.3.0"
        assert "--clear-only" in body, \
            "shipped start skill must reference the --clear-only latch-clear flag"
        assert "/auto-maintainer:tick" in body, \
            "shipped start skill must drive the first tick through the executor"
        assert "heartbeat.interval_minutes" in body, \
            "shipped start skill must read the config-driven heartbeat interval"
        assert "--print-interval" in body, \
            "shipped start skill must read the interval via start.py --print-interval"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Re-ship (v0.7.9, file-referenced dispatch prompts, #304): the tick executor
# skill (scheduling v0.6.0, collected via ship/) ships at skills/tick/SKILL.md
# with the file-referenced dispatch protocol. The runner writes each dispatch's
# rendered invocation envelope to a `prompt_path` FILE and hands the executor
# only the path; the executor points each subagent at its prompt_path file rather
# than passing an inline prompt body. The dispatched subagent still writes its
# OWN output file (the prompt's ## Handoff names the path); the executor never
# marshals the dispatch result. So the skill references `run_tick.py --resume`
# and `prompt_path`, and it does NOT reference the old dispatch-result.json
# marshalling path.
# ---------------------------------------------------------------------------
def test_shipped_tick_skill_is_v0_6_0_file_referenced_dispatch():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        assert os.path.isfile(sk), "skills/tick/SKILL.md must ship"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert "\nversion: 0.6.0\n" in body, \
            "shipped tick skill frontmatter version must be 0.6.0 " \
            "(#304 file-referenced dispatch prompts documented)"
        assert "run_tick.py --resume" in body, \
            "shipped tick skill must reference run_tick.py --resume"
        assert "prompt_path" in body, \
            "shipped tick skill must document the #304 file-referenced dispatch " \
            "(point each subagent at the runner-named prompt_path file)"
        assert "dispatch-result.json" not in body, \
            "shipped tick skill must NOT reference the old dispatch-result.json " \
            "marshalling path (the subagent writes its own output file)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19): safety-governance's configure.py — the
# governance-config WRITER — ships under lib/ as a normalized control lib. It
# imports its sibling `safety_governance` (the reader/decider), so the build
# inserts the plain self-path bootstrap (configure.py already imports os/sys at
# module top) before its first sibling import, making the shipped copy resolve
# safety_governance from the co-located lib/ alone.
# ---------------------------------------------------------------------------
def test_configure_lib_present():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        assert os.path.isfile(os.path.join(lib, "configure.py")), \
            "lib/configure.py must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19): the shipped configure.py must be byte-identical
# to the build's OWN normalization of its source. It imports
# `import safety_governance as sg` and already imports os/sys at module top, so it
# is normalized via the PLAIN self-path bootstrap (no with-imports variant).
# ---------------------------------------------------------------------------
def test_shipped_configure_is_normalized_source_bytes():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "configure.py")
        assert os.path.isfile(dst), f"missing shipped lib: {dst}"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "safety-governance", "src", "configure.py",
        )
        with open(dst, encoding="utf-8") as fh:
            shipped = fh.read()
        _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["configure.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped configure is not the normalized source bytes"
        # the PLAIN bootstrap (sys.path insert) must be present, anchored before
        # its first sibling import; configure already imports os/sys at top, so
        # the with-imports variant is NOT used.
        assert "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))" \
            in shipped, \
            "shipped configure must carry the self-path bootstrap"
        assert "import safety_governance as sg" in shipped, \
            "shipped configure must import its safety_governance sibling"
        assert "import os  # noqa: E402" not in shipped, \
            "shipped configure must use the PLAIN bootstrap (no with-imports)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19): the build-rewritten configure lib must not
# leak a path back into the source feature tree — the headline clean-ship
# invariant applies to it too.
# ---------------------------------------------------------------------------
def test_shipped_configure_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        with open(
            os.path.join(lib, "configure.py"), encoding="utf-8"
        ) as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped configure leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped configure references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19), critical self-containment: the shipped
# configure.py imports `import safety_governance as sg`, so importing the shipped
# configure with ONLY the plugin lib/ on sys.path must resolve safety_governance
# (and IT must resolve its lifecycle_dispositions sibling) from lib/ alone.
# Claude copies just the plugin dir into its cache, so a shipped configure that
# could not find safety_governance in lib/ would fail to import once installed.
# Prove it by importing the shipped configure in a subprocess whose sys.path is
# restricted to the shipped lib/ dir alone, with NOTHING else.
# ---------------------------------------------------------------------------
def test_shipped_configure_imports_safety_governance_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import configure; "
            "import safety_governance, lifecycle_dispositions; "
            "assert configure.sg is safety_governance; "
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
            f"shipped configure cannot resolve safety_governance: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19): the /auto-maintainer:configure skill ships at
# skills/configure/SKILL.md via the ship/ convention (safety-governance's
# ship/skills/configure/). It is collected automatically with NO build change.
# ---------------------------------------------------------------------------
def test_ship_collection_configure_skill_present():
    out_root = _build_into_temp()
    try:
        sk = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "configure", "SKILL.md",
        )
        assert os.path.isfile(sk), \
            "ship/ collection must place skills/configure/SKILL.md"
        with open(sk, encoding="utf-8") as fh:
            body = fh.read()
        assert body.startswith("---"), \
            "skills/configure/SKILL.md must carry YAML frontmatter"
        assert "\nname: configure\n" in body, \
            "configure skill frontmatter name must be `configure`"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (configure lib, v0.2.19): the auto-maintainer-implementer subagent ships
# at agents/auto-maintainer-implementer.md via the ship/ convention (implement's
# ship/agents/). It is collected automatically with NO build change.
# ---------------------------------------------------------------------------
def test_ship_collection_implementer_agent_present():
    out_root = _build_into_temp()
    try:
        ag = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "agents", "auto-maintainer-implementer.md",
        )
        assert os.path.isfile(ag), \
            "ship/ collection must place agents/auto-maintainer-implementer.md"
        with open(ag, encoding="utf-8") as fh:
            body = fh.read()
        assert body.startswith("---"), \
            "agents/auto-maintainer-implementer.md must carry YAML frontmatter"
        assert "\nname: auto-maintainer-implementer\n" in body, \
            "implementer agent frontmatter name must be `auto-maintainer-implementer`"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (verify-integrate lib, v0.2.24): the new verify-integrate lib ships under
# lib/ as a normalized control lib. It imports its siblings `fsm_contracts` (the
# FIRST import, the bootstrap anchor) and `safety_governance`, and does NOT import
# os/sys at module top, so it takes the with-imports self-path bootstrap (the same
# as prioritize/implement/work_intake).
# ---------------------------------------------------------------------------
def test_verify_integrate_lib_present():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        assert os.path.isfile(os.path.join(lib, "verify_integrate.py")), \
            "lib/verify_integrate.py must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (verify-integrate lib, v0.2.24): the shipped verify_integrate.py must be
# byte-identical to the build's OWN normalization of its source. It imports
# `import fsm_contracts as fc` (first sibling import, the anchor) and
# `import safety_governance as sg`, and does NOT import os/sys at module top, so
# it is normalized via the with-imports bootstrap inserted before its first
# sibling import — the SAME shape as prioritize/implement/work_intake.
# ---------------------------------------------------------------------------
def test_shipped_verify_integrate_is_normalized_source_bytes():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "verify_integrate.py")
        assert os.path.isfile(dst), f"missing shipped lib: {dst}"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "verify-integrate", "src", "verify_integrate.py",
        )
        with open(dst, encoding="utf-8") as fh:
            shipped = fh.read()
        _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["verify_integrate.py"]
        expected = mod._normalize_lib(src, anchor, bootstrap)
        assert shipped == expected, \
            "shipped verify_integrate is not the normalized source bytes"
        # the with-imports bootstrap must be present (the file imports no os/sys
        # at top, so the bootstrap supplies them before the insert), anchored
        # before the FIRST sibling import (fsm_contracts).
        assert "import os  # noqa: E402" in shipped, \
            "shipped verify_integrate must carry the with-imports bootstrap"
        bootstrap_marker = (
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))"
        )
        assert bootstrap_marker in shipped, \
            "shipped verify_integrate must carry the self-path bootstrap"
        # the bootstrap must precede the fsm_contracts import so the shipped copy
        # resolves its siblings from the co-located lib/ alone.
        assert shipped.index(bootstrap_marker) < \
            shipped.index("import fsm_contracts as fc"), \
            "bootstrap must be inserted before the fsm_contracts import"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (verify-integrate lib, v0.2.24): the build-rewritten verify_integrate lib
# must not leak a path back into the source feature tree — the headline clean-ship
# invariant applies to it too.
# ---------------------------------------------------------------------------
def test_shipped_verify_integrate_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        with open(
            os.path.join(lib, "verify_integrate.py"), encoding="utf-8"
        ) as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped verify_integrate leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped verify_integrate references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Slice (verify-integrate lib, v0.2.24), critical self-containment: the shipped
# verify_integrate.py imports `import fsm_contracts as fc` and
# `import safety_governance as sg`, so importing the shipped verify_integrate with
# ONLY the plugin lib/ on sys.path must resolve both siblings (and
# safety_governance's lifecycle_dispositions sibling) from lib/ alone. Claude
# copies just the plugin dir into its cache, so a shipped verify_integrate that
# could not find its siblings in lib/ would fail to import once installed. Prove
# it in a subprocess whose sys.path is restricted to the shipped lib/ dir alone.
# ---------------------------------------------------------------------------
def test_shipped_verify_integrate_imports_siblings_from_lib_alone():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import verify_integrate; "
            "import fsm_contracts, safety_governance; "
            "assert verify_integrate.fc is fsm_contracts; "
            "assert verify_integrate.sg is safety_governance; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped verify_integrate cannot resolve siblings: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.3.0, configurables overhaul): the two new wiring-config CLIs —
# route_config.py + adapter_map_config.py — ship under lib/ as normalized
# control libs. Each imports its sibling adapter_wiring (its FIRST sibling
# import, the bootstrap anchor) plus run_tick (and adapter_map_config also
# agent_dispatch/work_intake/implement), and each already imports os/sys at
# module top, so each takes the PLAIN self-path bootstrap (the same as
# run_tick/status/start/configure).
# ---------------------------------------------------------------------------
def test_route_and_adapter_map_config_libs_present():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for fname in ("route_config.py", "adapter_map_config.py"):
            assert os.path.isfile(os.path.join(lib, fname)), \
                f"lib/{fname} must ship in the plugin tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.3.0): the shipped route_config.py + adapter_map_config.py must be
# byte-identical to the build's OWN normalization of their scheduling sources.
# Each imports `import adapter_wiring as aw` (first sibling import, the anchor)
# and already imports os/sys at module top, so each is normalized via the PLAIN
# self-path bootstrap (no with-imports variant) inserted before that anchor —
# the SAME shape as run_tick/status/start/configure.
# ---------------------------------------------------------------------------
def test_shipped_route_adapter_map_config_are_normalized_source_bytes():
    mod = _load_build()
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        mapping = {
            "route_config.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "scheduling", "src", "route_config.py",
            ),
            "adapter_map_config.py": os.path.join(
                _REPO_ROOT, ".rabbit", "rabbit-project", "features",
                "scheduling", "src", "adapter_map_config.py",
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
            # the PLAIN bootstrap (the file already imports os/sys at top, so the
            # with-imports variant is NOT used), anchored before the first
            # sibling import (adapter_wiring).
            bootstrap_marker = (
                "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))"
            )
            assert bootstrap_marker in shipped, \
                f"shipped {fname} must carry the self-path bootstrap"
            assert "import os  # noqa: E402" not in shipped, \
                f"shipped {fname} must use the PLAIN bootstrap (no with-imports)"
            assert shipped.index(bootstrap_marker) < \
                shipped.index("import adapter_wiring as aw"), \
                f"bootstrap must precede the adapter_wiring import in {fname}"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.3.0): the build-rewritten route_config + adapter_map_config libs
# must not leak a path back into the source feature tree — the headline
# clean-ship invariant applies to them too.
# ---------------------------------------------------------------------------
def test_shipped_route_adapter_map_config_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for fname in ("route_config.py", "adapter_map_config.py"):
            with open(os.path.join(lib, fname), encoding="utf-8") as fh:
                body = fh.read()
            assert ".rabbit" not in body, f"shipped {fname} leaks .rabbit"
            assert "rabbit-project" not in body, \
                f"shipped {fname} references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.3.0), critical self-containment: the shipped route_config.py imports
# `import adapter_wiring as aw` + `import run_tick as rt`, and
# adapter_map_config.py imports adapter_wiring + agent_dispatch + work_intake +
# implement + run_tick — all flat siblings in lib/. Importing each with ONLY the
# plugin lib/ on sys.path must resolve those siblings from lib/ alone. Claude
# copies just the plugin dir into its cache, so a shipped wiring CLI that could
# not find its siblings in lib/ would fail to import once installed. Prove it in
# a subprocess whose sys.path is restricted to the shipped lib/ dir alone.
# ---------------------------------------------------------------------------
def test_shipped_route_adapter_map_config_are_self_contained():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for mod_name in ("route_config", "adapter_map_config"):
            probe = (
                "import sys; "
                f"sys.path.insert(0, {lib!r}); "
                f"import {mod_name}; "
                f"import adapter_wiring, run_tick; "
                f"assert {mod_name}.aw is adapter_wiring; "
                f"assert {mod_name}.rt is run_tick; "
                "print('OK')"
            )
            proc = subprocess.run(
                [sys.executable, "-c", probe],
                capture_output=True, text=True,
                env={"PYTHONPATH": ""},
                cwd=out_root,
            )
            assert proc.returncode == 0, \
                f"shipped {mod_name} not self-contained: {proc.stderr}"
            assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.3.0): the new wiring skills /auto-maintainer:route and
# /auto-maintainer:adapter-map ship at skills/route/SKILL.md and
# skills/adapter-map/SKILL.md via the ship/ convention (scheduling's
# ship/skills/{route,adapter-map}/). They are collected automatically with NO
# build change.
# ---------------------------------------------------------------------------
def test_ship_collection_route_and_adapter_map_skills_present():
    out_root = _build_into_temp()
    try:
        skills = os.path.join(
            out_root, "plugins", "auto-maintainer", "skills"
        )
        cases = (("route", "route"), ("adapter-map", "adapter-map"))
        for dirname, fm_name in cases:
            sk = os.path.join(skills, dirname, "SKILL.md")
            assert os.path.isfile(sk), \
                f"ship/ collection must place skills/{dirname}/SKILL.md"
            with open(sk, encoding="utf-8") as fh:
                body = fh.read()
            assert body.lstrip().startswith("---"), \
                f"skills/{dirname}/SKILL.md must carry YAML frontmatter"
            assert f"\nname: {fm_name}\n" in body, \
                f"skills/{dirname}/SKILL.md frontmatter name must be `{fm_name}`"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Minor (v0.5.0, #211 aggressive default): the plugin ships a default-config/
# dir with the aggressive seed assets (config.json mode=auto-merge, the full
# acting route incl. REVIEW, the agent-wired adapter-map) that start.py seeds
# into a fresh install's .auto-maintainer/ — the plug-and-play aggressive default.
# ---------------------------------------------------------------------------
def test_default_config_seed_assets_shipped():
    out_root = _build_into_temp()
    try:
        dc = os.path.join(out_root, "plugins", "auto-maintainer", "default-config")
        for name in ("config.json", "route.json", "adapter-map.json"):
            assert os.path.isfile(os.path.join(dc, name)), \
                f"default-config/{name} must ship for fresh-install seeding"
        with open(os.path.join(dc, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        assert cfg["mode"] == "auto-merge", "seed config.json mode must be auto-merge"
        with open(os.path.join(dc, "route.json"), encoding="utf-8") as fh:
            route = json.load(fh)
        assert "REVIEW" in route["states"], "seed route must include the REVIEW gate"
        with open(os.path.join(dc, "adapter-map.json"), encoding="utf-8") as fh:
            amap = json.load(fh)
        agents = [k for k, v in amap.items() if isinstance(v, dict)]
        assert "IMPLEMENT" in agents and "TRIAGE" in agents and "REVIEW" in agents, \
            "seed adapter-map must wire TRIAGE/IMPLEMENT/REVIEW to agents"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.6.0 (#263/#264), BUILD-DRIFT GUARD — the headline release test.
#
# The committed plugin tree (<repo_root>/plugins/auto-maintainer/) is the
# artifact a GitHub clone installs. It is GENERATED from the current framework
# src by build_plugin.build(), but it can silently drift: a merged src fix (a
# new verify_integrate review-evidence gate, a prioritize serialization change)
# does NOT reach the installed plugin until the committed tree is regenerated.
#
# This guard rebuilds the plugin from CURRENT src into a fresh temp out-root and
# asserts it is byte-for-byte identical to the committed tree — both the path set
# AND every file's bytes. If a src fix was merged but the committed tree was
# never re-shipped, this FAILS, locating the drift to a concrete file. It is the
# deterministic gate that this release's whole purpose (deploy merged fixes into
# the shipped tree) actually landed.
# ---------------------------------------------------------------------------
def test_committed_plugin_tree_matches_fresh_build():
    committed = os.path.join(_REPO_ROOT, "plugins", "auto-maintainer")
    assert os.path.isdir(committed), \
        f"committed plugin tree must exist at {committed}"

    out_root = _build_into_temp()
    try:
        fresh = os.path.join(out_root, "plugins", "auto-maintainer")
        committed_paths = sorted(_walk_paths(committed))
        fresh_paths = sorted(_walk_paths(fresh))
        assert committed_paths == fresh_paths, (
            "committed plugin tree drifted from a fresh build (path set "
            "differs) — regenerate plugins/auto-maintainer/ from current src.\n"
            f"only in committed: {sorted(set(committed_paths) - set(fresh_paths))}\n"
            f"only in fresh:     {sorted(set(fresh_paths) - set(committed_paths))}"
        )
        for rel in committed_paths:
            fc = os.path.join(committed, rel)
            ff = os.path.join(fresh, rel)
            if os.path.isfile(fc):
                with open(fc, "rb") as a, open(ff, "rb") as b:
                    assert a.read() == b.read(), (
                        f"committed plugin file {rel} drifted from a fresh "
                        "build — regenerate plugins/auto-maintainer/ from "
                        "current src so the shipped bytes match the merged fix."
                    )
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.6.0 (#263/#264), #255 evidence-gate deploy confirmation: the
# whole point of this release is that the committed (shipped) verify_integrate
# carries the #255 model-review evidence gate (review_evidence_valid /
# batch_is_untrustworthy), which the stale committed tree lacked. Assert the
# COMMITTED lib — the bytes an installed plugin runs — now contains the gate.
# ---------------------------------------------------------------------------
def test_committed_verify_integrate_carries_255_evidence_gate():
    committed_vi = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "verify_integrate.py"
    )
    assert os.path.isfile(committed_vi), \
        "committed lib/verify_integrate.py must ship in the plugin tree"
    with open(committed_vi, encoding="utf-8") as fh:
        body = fh.read()
    assert "review_evidence_valid" in body, \
        "committed verify_integrate must carry the #255 review_evidence_valid gate"
    assert "batch_is_untrustworthy" in body, \
        "committed verify_integrate must carry the #255 batch_is_untrustworthy gate"


# ---------------------------------------------------------------------------
# Release v0.7.1 (#277-#280), dogfood-fix deploy confirmation: the whole point
# of this release is that the committed (shipped) libs carry the three merged
# dogfood fixes — the agent-dispatch empty-schema guard (#277), the
# adapter-wiring build_loop migrate hook (#278), and the scheduling adapter-map
# known-port auto-migration (#279) — which the stale committed tree could lack.
# Assert the COMMITTED libs — the bytes an installed plugin runs — carry each
# fix. These guard against shipping a tree that drifted from the merged src.
# ---------------------------------------------------------------------------
def test_committed_libs_carry_277_278_279_dogfood_fixes():
    lib = os.path.join(_REPO_ROOT, "plugins", "auto-maintainer", "lib")

    # #277: agent_dispatch's _expected_type derives no type for an EMPTY dict
    # (the empty-schema accept-as-is guard) — the non-empty-dict -> "object"
    # branch.
    ad = os.path.join(lib, "agent_dispatch.py")
    assert os.path.isfile(ad), \
        "committed lib/agent_dispatch.py must ship in the plugin tree"
    with open(ad, encoding="utf-8") as fh:
        ad_body = fh.read()
    assert "isinstance(schema, dict) and schema" in ad_body, \
        "committed agent_dispatch must carry the #277 empty-schema guard " \
        "(non-empty dict -> object; empty {} derives no type)"

    # #278: adapter_wiring.build_loop carries the optional migrate hook.
    aw = os.path.join(lib, "adapter_wiring.py")
    assert os.path.isfile(aw), \
        "committed lib/adapter_wiring.py must ship in the plugin tree"
    with open(aw, encoding="utf-8") as fh:
        aw_body = fh.read()
    assert "migrate=None" in aw_body, \
        "committed adapter_wiring must carry the #278 build_loop migrate hook"

    # #279: adapter_map_config exposes migrate_known_port_entries.
    amc = os.path.join(lib, "adapter_map_config.py")
    assert os.path.isfile(amc), \
        "committed lib/adapter_map_config.py must ship in the plugin tree"
    with open(amc, encoding="utf-8") as fh:
        amc_body = fh.read()
    assert "def migrate_known_port_entries(" in amc_body, \
        "committed adapter_map_config must carry the #279 " \
        "migrate_known_port_entries known-port auto-migration"


# ---------------------------------------------------------------------------
# Release v0.7.2 (#283), surgical adapter-map migration deploy confirmation:
# the whole point of this release is that the committed (shipped)
# adapter_map_config carries the #283 surgical migration fix — the migration
# only heals retired-writes entries and preserves valid custom wiring, gated by
# the `valid_writes` set derived from the agent-port templates. Assert the
# COMMITTED lib — the bytes an installed plugin runs — carries the gate, AND
# that it is byte-identical to a fresh normalization of the current scheduling
# source (so the committed tree genuinely shipped the merged #283 fix).
# ---------------------------------------------------------------------------
def test_committed_adapter_map_config_carries_283_surgical_migration():
    mod = _load_build()
    committed_amc = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib",
        "adapter_map_config.py",
    )
    assert os.path.isfile(committed_amc), \
        "committed lib/adapter_map_config.py must ship in the plugin tree"
    with open(committed_amc, encoding="utf-8") as fh:
        committed = fh.read()
    assert "valid_writes" in committed, \
        "committed adapter_map_config must carry the #283 surgical " \
        "migration gate (valid_writes set preserves valid custom wiring)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #283 fix.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "adapter_map_config.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["adapter_map_config.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed adapter_map_config drifted from a fresh normalization of " \
        "the current scheduling source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.5 (#290), advisory-REVIEW merge-fix deploy confirmation: the
# whole point of this release is that the committed (shipped)
# adapter_map_config carries the #290 advisory-REVIEW fix — the REVIEW
# adapter-map template's signal is `always_ok`, so a clean (zero-finding)
# review emits OK and ALWAYS continues to INTEGRATE instead of EMPTY-branching
# past the merge. Assert the COMMITTED lib — the bytes an installed plugin runs
# — carries the always_ok signal_rule on the REVIEW template, AND that it is
# byte-identical to a fresh normalization of the current scheduling source (so
# the committed tree genuinely shipped the merged #290 fix).
# ---------------------------------------------------------------------------
def test_committed_adapter_map_config_carries_290_review_always_ok():
    mod = _load_build()
    committed_amc = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib",
        "adapter_map_config.py",
    )
    assert os.path.isfile(committed_amc), \
        "committed lib/adapter_map_config.py must ship in the plugin tree"
    with open(committed_amc, encoding="utf-8") as fh:
        committed = fh.read()
    assert '"signal_rule": "always_ok"' in committed, \
        "committed adapter_map_config must carry the #290 advisory-REVIEW " \
        "fix (the REVIEW template's signal_rule is always_ok)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #290 fix.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "adapter_map_config.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["adapter_map_config.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed adapter_map_config drifted from a fresh normalization of " \
        "the current scheduling source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.6 (#292), immediate-refire enhancement deploy confirmation: the
# whole point of this release is that the committed (shipped) run_tick carries
# the #292 immediate-refire predicate — scheduling's EXIT anchor is wrapped so
# a completed tick with remaining actionable work signals refire and the loop
# runs the next tick immediately instead of waiting for the heartbeat (the
# `_work_remains` predicate over the remaining work + the durable backoff
# ledger) — AND that the shipped tick skill documents the refire loop. Assert
# the COMMITTED lib — the bytes an installed plugin runs — carries the
# predicate, that it is byte-identical to a fresh normalization of the current
# scheduling source (so the committed tree genuinely shipped the merged #292
# enhancement), and that the committed tick skill documents the refire loop.
# ---------------------------------------------------------------------------
def test_committed_run_tick_carries_292_immediate_refire():
    mod = _load_build()
    committed_rt = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "run_tick.py",
    )
    assert os.path.isfile(committed_rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(committed_rt, encoding="utf-8") as fh:
        committed = fh.read()
    assert "_work_remains" in committed, \
        "committed run_tick must carry the #292 immediate-refire predicate " \
        "(_work_remains over the remaining work + the durable backoff ledger)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #292 enhancement.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "run_tick.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["run_tick.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed run_tick drifted from a fresh normalization of the " \
        "current scheduling source — regenerate the plugin tree"

    # The shipped tick skill documents the refire loop (#292).
    committed_skill = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "skills", "tick", "SKILL.md",
    )
    assert os.path.isfile(committed_skill), \
        "committed skills/tick/SKILL.md must ship in the plugin tree"
    with open(committed_skill, encoding="utf-8") as fh:
        skill = fh.read()
    assert "refire" in skill, \
        "committed tick SKILL.md must document the #292 refire loop"


# ---------------------------------------------------------------------------
# Release v0.7.6 (#292), shipped-build immediate-refire: the freshly built
# run_tick (the bytes the regenerated tree ships) carries the #292
# immediate-refire predicate, and the freshly built tick skill documents the
# refire loop. This proves the build's normalization of the current scheduling
# source delivers the #292 enhancement into the plugin tree.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_immediate_refire():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        assert "_work_remains" in shipped, \
            "shipped run_tick must carry the #292 immediate-refire predicate " \
            "(_work_remains)"
        skill = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        with open(skill, encoding="utf-8") as fh:
            skill_body = fh.read()
        assert "refire" in skill_body, \
            "shipped tick SKILL.md must document the #292 refire loop"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.3 (#285), stale-checkpoint discard deploy confirmation: the
# whole point of this release is that the committed (shipped) run_tick carries
# the #285 stale-checkpoint discard fix — run_tick discards a persisted PAUSED
# checkpoint that is incompatible with the current route/context (via the
# `_checkpoint_compatible` guard) instead of resuming against it. Assert the
# COMMITTED lib — the bytes an installed plugin runs — carries the guard, AND
# that it is byte-identical to a fresh normalization of the current scheduling
# source (so the committed tree genuinely shipped the merged #285 fix).
# ---------------------------------------------------------------------------
def test_committed_run_tick_carries_285_checkpoint_compat_guard():
    mod = _load_build()
    committed_rt = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "run_tick.py",
    )
    assert os.path.isfile(committed_rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(committed_rt, encoding="utf-8") as fh:
        committed = fh.read()
    assert "_checkpoint_compatible" in committed, \
        "committed run_tick must carry the #285 stale-checkpoint discard " \
        "guard (_checkpoint_compatible)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #285 fix.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "run_tick.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["run_tick.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed run_tick drifted from a fresh normalization of the " \
        "current scheduling source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.3 (#285), shipped-build checkpoint-compat guard: the freshly
# built run_tick (the bytes the regenerated tree ships) carries the
# `_checkpoint_compatible` guard. This proves the build's normalization of the
# current scheduling source delivers the #285 fix into the plugin lib/.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_checkpoint_compat_guard():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        assert "_checkpoint_compatible" in shipped, \
            "shipped run_tick must carry the #285 checkpoint-compat guard " \
            "(_checkpoint_compatible)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.4 (#288), per-item dispatch-description deploy confirmation: the
# whole point of this release is that the committed (shipped) run_tick carries
# the #288 per-item dispatch-description fix — _dispatch_description now branches
# on cardinality (`if "item" in env:`) so a per_item fan-out ALWAYS names the
# item ref (distinct parallel subagents) instead of letting an explicit
# dispatch_entry['description'] win verbatim. Assert the COMMITTED lib — the
# bytes an installed plugin runs — carries the branch, AND that it is
# byte-identical to a fresh normalization of the current scheduling source (so
# the committed tree genuinely shipped the merged #288 fix).
# ---------------------------------------------------------------------------
def test_committed_run_tick_carries_288_per_item_dispatch_desc():
    mod = _load_build()
    committed_rt = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "run_tick.py",
    )
    assert os.path.isfile(committed_rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(committed_rt, encoding="utf-8") as fh:
        committed = fh.read()
    assert 'if "item" in env:' in committed, \
        "committed run_tick must carry the #288 per-item dispatch-description " \
        "branch (if \"item\" in env: → always name the item ref)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #288 fix.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "run_tick.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["run_tick.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed run_tick drifted from a fresh normalization of the " \
        "current scheduling source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.4 (#288), shipped-build per-item dispatch-description: the freshly
# built run_tick (the bytes the regenerated tree ships) carries the #288
# per-item dispatch-description branch. This proves the build's normalization of
# the current scheduling source delivers the #288 fix into the plugin lib/.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_per_item_dispatch_desc():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        assert 'if "item" in env:' in shipped, \
            "shipped run_tick must carry the #288 per-item dispatch-description " \
            "branch (if \"item\" in env:)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.0 (loop-redesign final), test_gate.py ship: implement's
# test_gate.py — the IMPLEMENT doer's deterministic test gate — ships under
# lib/. It imports ONLY stdlib (argparse/json/os/subprocess/sys) and NO sibling
# lib, so it is a PURE byte-copied lib (in _LIBS, NOT _NORMALIZED_LIBS) — copied
# verbatim like fsm_contracts/agent_dispatch/observability. Prove the fresh
# build ships lib/test_gate.py byte-identical to implement/src/test_gate.py.
# ---------------------------------------------------------------------------
def test_test_gate_lib_present_and_byte_identical():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        dst = os.path.join(lib, "test_gate.py")
        assert os.path.isfile(dst), \
            "lib/test_gate.py must ship in the plugin tree"
        src = os.path.join(
            _REPO_ROOT, ".rabbit", "rabbit-project", "features",
            "implement", "src", "test_gate.py",
        )
        with open(src, "rb") as a, open(dst, "rb") as b:
            assert a.read() == b.read(), \
                "test_gate.py is not byte-identical to its source"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.0: the byte-copied test_gate.py must not leak a path back into
# the source feature tree — the headline clean-ship invariant applies to it too
# (it is pure stdlib, so this is a sanity guard mirroring the other pure libs).
# ---------------------------------------------------------------------------
def test_shipped_test_gate_no_source_tree_leak():
    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        with open(
            os.path.join(lib, "test_gate.py"), encoding="utf-8"
        ) as fh:
            body = fh.read()
        assert ".rabbit" not in body, "shipped test_gate leaks .rabbit"
        assert "rabbit-project" not in body, \
            "shipped test_gate references the source feature tree"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.7 (#294), merge-sink-url deploy confirmation: the whole point of
# this release is that the committed (shipped) verify_integrate carries the #294
# merge-sink fix — the merge sink records the merged PR url (`_pr_url`) for
# traceability. Assert the COMMITTED lib — the bytes an installed plugin runs —
# carries the `_pr_url` token, AND that it is byte-identical to a fresh
# normalization of the current verify-integrate source (so the committed tree
# genuinely shipped the merged #294 fix).
# ---------------------------------------------------------------------------
def test_committed_verify_integrate_carries_294_merged_pr_url():
    mod = _load_build()
    committed_vi = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "verify_integrate.py"
    )
    assert os.path.isfile(committed_vi), \
        "committed lib/verify_integrate.py must ship in the plugin tree"
    with open(committed_vi, encoding="utf-8") as fh:
        committed = fh.read()
    assert "_pr_url" in committed, \
        "committed verify_integrate must carry the #294 merge-sink PR url " \
        "(_pr_url) the merge sink records for traceability"

    # byte-identical to the build's own normalization of the CURRENT
    # verify-integrate source — proving the committed tree shipped the #294 fix.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "verify-integrate", "src", "verify_integrate.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["verify_integrate.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed verify_integrate drifted from a fresh normalization of the " \
        "current verify-integrate source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.7 (#294), shipped-build merge-sink-url: the freshly built
# verify_integrate (the bytes the regenerated tree ships) carries the #294
# `_pr_url` merge-sink token. This proves the build's normalization of the
# current verify-integrate source delivers the #294 fix into the plugin lib/.
# ---------------------------------------------------------------------------
def test_shipped_verify_integrate_carries_merged_pr_url():
    out_root = _build_into_temp()
    try:
        vi = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib",
            "verify_integrate.py",
        )
        with open(vi, encoding="utf-8") as fh:
            shipped = fh.read()
        assert "_pr_url" in shipped, \
            "shipped verify_integrate must carry the #294 merge-sink PR url " \
            "(_pr_url)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.7 (#295), pool-based-refire + integrate observability deploy
# confirmation: the whole point of this release is that the committed (shipped)
# run_tick carries the #295 pool-based refire — _work_remains is redefined to a
# triage-memory-aware POOL predicate that computes its candidate pool via
# `_filter_triage_work_items` (the SAME §3.5.3 skip-filter TRIAGE applies) — and
# surfaces INTEGRATE merge results in the tick_end detail via `merged_refs`.
# Assert the COMMITTED lib — the bytes an installed plugin runs — carries both
# tokens, AND that it is byte-identical to a fresh normalization of the current
# scheduling source (so the committed tree genuinely shipped the merged #295
# work).
# ---------------------------------------------------------------------------
def test_committed_run_tick_carries_295_pool_refire_and_merged_refs():
    mod = _load_build()
    committed_rt = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "run_tick.py",
    )
    assert os.path.isfile(committed_rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(committed_rt, encoding="utf-8") as fh:
        committed = fh.read()
    # the pool-based refire: _work_remains filters its candidate pool via
    # _filter_triage_work_items (the §3.5.3 skip-filter).
    assert "_filter_triage_work_items" in committed, \
        "committed run_tick must carry the #295 pool-based refire " \
        "(_work_remains computes candidates via _filter_triage_work_items)"
    # the INTEGRATE/refire observability: tick_end detail surfaces merged_refs.
    assert "merged_refs" in committed, \
        "committed run_tick must surface merged_refs in the tick_end detail " \
        "(#295 INTEGRATE/refire observability)"

    # byte-identical to the build's own normalization of the CURRENT scheduling
    # source — proving the committed tree shipped the merged #295 work.
    src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "run_tick.py",
    )
    _src_rel, anchor, bootstrap = mod._NORMALIZED_LIBS["run_tick.py"]
    expected = mod._normalize_lib(src, anchor, bootstrap)
    assert committed == expected, \
        "committed run_tick drifted from a fresh normalization of the " \
        "current scheduling source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.7 (#295), shipped-build pool-refire + merged_refs: the freshly
# built run_tick (the bytes the regenerated tree ships) carries the #295
# pool-based refire (_filter_triage_work_items in _work_remains) and the
# merged_refs tick_end detail. This proves the build's normalization of the
# current scheduling source delivers the #295 work into the plugin lib/.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_pool_refire_and_merged_refs():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        assert "_filter_triage_work_items" in shipped, \
            "shipped run_tick must carry the #295 pool-based refire " \
            "(_filter_triage_work_items)"
        assert "merged_refs" in shipped, \
            "shipped run_tick must surface merged_refs in the tick_end detail " \
            "(#295)"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.8 (#297/#298/#299), work_own_filings knob DISCOVERABILITY: the
# whole point of this release is that the shipped default-config config.json
# surfaces the §3.11.5 work_own_filings opt-out so a user inspecting the seed
# config finds the knob to flip. Assert the freshly built default-config
# config.json carries a top-level "work_own_filings": true at schema_version
# 2.2.0 (matching safety-governance's GOVERNANCE_SCHEMA_VERSION), and that the
# pre-existing keys (mode/features_root/budget/heartbeat/backoff) are unchanged.
# ---------------------------------------------------------------------------
def test_default_config_surfaces_work_own_filings_at_schema_2_2_0():
    out_root = _build_into_temp()
    try:
        dc = os.path.join(
            out_root, "plugins", "auto-maintainer", "default-config"
        )
        with open(os.path.join(dc, "config.json"), encoding="utf-8") as fh:
            cfg = json.load(fh)
        assert cfg.get("schema_version") == "2.2.0", \
            f"seed config schema_version must be 2.2.0, got " \
            f"{cfg.get('schema_version')!r}"
        assert cfg.get("work_own_filings") is True, \
            "seed config must surface the §3.11.5 work_own_filings opt-out " \
            "(default-on true) so users can find the knob"
        # the pre-existing keys must remain unchanged.
        assert cfg["mode"] == "auto-merge"
        assert cfg["features_root"] is None
        assert cfg["budget"] == {"per_day_tokens": None, "window_tz": "local"}
        assert cfg["heartbeat"] == {"interval_minutes": 3}
        assert cfg["backoff"] == {"threshold": 5}
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.8 (#297/#298/#299), COMMITTED default-config knob deploy
# confirmation: the COMMITTED default-config config.json — the seed bytes a
# GitHub clone installs — must carry the work_own_filings:true knob at schema
# 2.2.0. This guards against shipping a tree where the source asset was updated
# but the committed tree was never regenerated.
# ---------------------------------------------------------------------------
def test_committed_default_config_surfaces_work_own_filings():
    committed = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "default-config",
        "config.json",
    )
    assert os.path.isfile(committed), \
        "committed default-config/config.json must ship in the plugin tree"
    with open(committed, encoding="utf-8") as fh:
        cfg = json.load(fh)
    assert cfg.get("schema_version") == "2.2.0", \
        "committed seed config schema_version must be 2.2.0"
    assert cfg.get("work_own_filings") is True, \
        "committed seed config must surface the work_own_filings opt-out"


# ---------------------------------------------------------------------------
# Release v0.7.8 (#297/#298/#299), shipped-libs opt-out deploy confirmation: the
# whole point of this release is that the committed (shipped) libs carry the
# §3.11.5 work_own_filings opt-out — safety_governance's default-true accessor
# (#297), work_intake's Pull honoring the work_own_filings flag (#298), and
# run_tick's make_pull threading it from the loaded config (#299). Assert the
# COMMITTED libs — the bytes an installed plugin runs — carry each, AND that
# each is byte-identical to a fresh normalization of its current source (so the
# committed tree genuinely shipped the merged opt-out).
# ---------------------------------------------------------------------------
def test_committed_libs_carry_work_own_filings_opt_out():
    mod = _load_build()
    lib = os.path.join(_REPO_ROOT, "plugins", "auto-maintainer", "lib")

    # #297: safety_governance exposes the default-true work_own_filings accessor.
    sg = os.path.join(lib, "safety_governance.py")
    assert os.path.isfile(sg), \
        "committed lib/safety_governance.py must ship in the plugin tree"
    with open(sg, encoding="utf-8") as fh:
        sg_body = fh.read()
    assert "def work_own_filings(" in sg_body, \
        "committed safety_governance must carry the #297 work_own_filings " \
        "accessor (the §3.11.5 default-on opt-out reader)"
    sg_src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "safety-governance", "src", "safety_governance.py",
    )
    _r, anchor, bootstrap = mod._NORMALIZED_LIBS["safety_governance.py"]
    assert sg_body == mod._normalize_lib(sg_src, anchor, bootstrap), \
        "committed safety_governance drifted from a fresh normalization of " \
        "the current source — regenerate the plugin tree"

    # #298: work_intake's Pull honors the work_own_filings flag.
    wi = os.path.join(lib, "work_intake.py")
    assert os.path.isfile(wi), \
        "committed lib/work_intake.py must ship in the plugin tree"
    with open(wi, encoding="utf-8") as fh:
        wi_body = fh.read()
    assert "work_own_filings" in wi_body, \
        "committed work_intake must carry the #298 work_own_filings handling " \
        "(Pull honors the opt-out)"
    wi_src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "work-intake", "src", "work_intake.py",
    )
    _r, anchor, bootstrap = mod._NORMALIZED_LIBS["work_intake.py"]
    assert wi_body == mod._normalize_lib(wi_src, anchor, bootstrap), \
        "committed work_intake drifted from a fresh normalization of the " \
        "current source — regenerate the plugin tree"

    # #299: run_tick's make_pull threads work_own_filings from the config.
    rt = os.path.join(lib, "run_tick.py")
    assert os.path.isfile(rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(rt, encoding="utf-8") as fh:
        rt_body = fh.read()
    assert "work_own_filings" in rt_body, \
        "committed run_tick must carry the #299 work_own_filings threading " \
        "(make_pull passes it from the loaded config)"
    rt_src = os.path.join(
        _REPO_ROOT, ".rabbit", "rabbit-project", "features",
        "scheduling", "src", "run_tick.py",
    )
    _r, anchor, bootstrap = mod._NORMALIZED_LIBS["run_tick.py"]
    assert rt_body == mod._normalize_lib(rt_src, anchor, bootstrap), \
        "committed run_tick drifted from a fresh normalization of the " \
        "current source — regenerate the plugin tree"


# ---------------------------------------------------------------------------
# Release v0.7.8 (#297/#298/#299), shipped-build opt-out: the freshly built libs
# (the bytes the regenerated tree ships) carry the §3.11.5 work_own_filings
# opt-out across safety_governance / work_intake / run_tick. This proves the
# build's normalization of the current sources delivers the merged opt-out into
# the plugin lib/, and that work_intake's Pull honors it at runtime when imported
# from lib/ alone.
# ---------------------------------------------------------------------------
def test_shipped_libs_carry_work_own_filings_opt_out():
    import subprocess
    import sys

    out_root = _build_into_temp()
    try:
        lib = os.path.join(out_root, "plugins", "auto-maintainer", "lib")
        for fname, token in (
            ("safety_governance.py", "def work_own_filings("),
            ("work_intake.py", "work_own_filings"),
            ("run_tick.py", "work_own_filings"),
        ):
            with open(os.path.join(lib, fname), encoding="utf-8") as fh:
                assert token in fh.read(), \
                    f"shipped {fname} must carry the work_own_filings opt-out"
        # runtime: work_intake's Pull accepts the work_own_filings flag, imported
        # from lib/ alone — proving the #298 opt-out is honored once installed.
        probe = (
            "import sys; "
            f"sys.path.insert(0, {lib!r}); "
            "import work_intake; "
            "p = work_intake.Pull(work_own_filings=False); "
            "assert p._work_own_filings is False; "
            "import safety_governance as sg; "
            "assert sg.work_own_filings({}) is True; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True, text=True,
            env={"PYTHONPATH": ""},
            cwd=out_root,
        )
        assert proc.returncode == 0, \
            f"shipped work_own_filings opt-out not honored: {proc.stderr}"
        assert proc.stdout.strip() == "OK"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.9 (#304), file-referenced dispatch prompts deploy confirmation:
# the whole point of this release is that the committed (shipped) run_tick carries
# the #304 file-referenced dispatch — scheduling's run_tick now writes each
# dispatch's rendered invocation envelope to a `prompt_path` FILE and hands the
# executor only the path (instead of an inline prompt body) — AND that the
# shipped tick skill (v0.6.0) documents the file-referenced dispatch protocol.
# Assert the SHIPPED bytes (both the freshly built tree and the committed tree)
# carry prompt_path in run_tick and document the file-referenced dispatch in the
# tick skill, with NO inline prompt in run_tick's dispatch entry.
# ---------------------------------------------------------------------------
def test_shipped_run_tick_carries_304_file_referenced_dispatch():
    out_root = _build_into_temp()
    try:
        rt = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib", "run_tick.py"
        )
        with open(rt, encoding="utf-8") as fh:
            shipped = fh.read()
        assert "prompt_path" in shipped, \
            "shipped run_tick must carry the #304 file-referenced dispatch " \
            "(writes the rendered envelope to a prompt_path file)"

        # The shipped tick skill documents the file-referenced dispatch (#304):
        # it points each subagent at the runner-named prompt_path file and does
        # NOT pass an inline prompt body / marshal a dispatch-result file.
        skill = os.path.join(
            out_root, "plugins", "auto-maintainer",
            "skills", "tick", "SKILL.md",
        )
        with open(skill, encoding="utf-8") as fh:
            skill_body = fh.read()
        assert "prompt_path" in skill_body, \
            "shipped tick SKILL.md must document the #304 file-referenced " \
            "dispatch (point each subagent at the runner-named prompt_path file)"
        assert "dispatch-result.json" not in skill_body, \
            "shipped tick SKILL.md must NOT reference the old dispatch-result.json " \
            "marshalling path"
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# Release v0.7.9 (#304), COMMITTED-tree deploy confirmation: the committed
# (shipped) run_tick — the bytes an installed plugin runs — carries the #304
# file-referenced dispatch prompt_path, and the committed tick skill documents
# it. This guards against shipping a tree that drifted from the merged #304 src.
# ---------------------------------------------------------------------------
def test_committed_run_tick_carries_304_file_referenced_dispatch():
    committed_rt = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "lib", "run_tick.py",
    )
    assert os.path.isfile(committed_rt), \
        "committed lib/run_tick.py must ship in the plugin tree"
    with open(committed_rt, encoding="utf-8") as fh:
        committed = fh.read()
    assert "prompt_path" in committed, \
        "committed run_tick must carry the #304 file-referenced dispatch " \
        "(writes the rendered envelope to a prompt_path file)"

    committed_skill = os.path.join(
        _REPO_ROOT, "plugins", "auto-maintainer", "skills", "tick", "SKILL.md",
    )
    assert os.path.isfile(committed_skill), \
        "committed skills/tick/SKILL.md must ship in the plugin tree"
    with open(committed_skill, encoding="utf-8") as fh:
        skill = fh.read()
    assert "prompt_path" in skill, \
        "committed tick SKILL.md must document the #304 file-referenced dispatch"
