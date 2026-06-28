#!/usr/bin/env python3
"""Unit tests for packaging-config's release-detection helpers: the shipped-src
change detector (touches_shipped_src) and the dev-tree marker (SELF_DEPLOY_MARKER)
that scheduling's `release_needed` operator signal uses to surface when a merged
PR's diff changed shipped bytes.

The dead self-deploy BUILD helpers — the deterministic PATCH version bump
(bump_version / _bump_patch) and the same-process disk version-read (_read_version
/ _VERSION_RE) — have been REMOVED: with the self-deploy ACTION gone (scheduling
#324) and the self_deploy knob gone (safety-governance #325), those helpers had
zero callers. build() now stamps the version from the in-memory _PLUGIN_VERSION
constant directly. touches_shipped_src + SELF_DEPLOY_MARKER are KEPT (still used by
release_needed): touches_shipped_src decides whether a merged PR's diff requires a
rebuild at all (only a change to a shipped lib source or a ship/ / plugin_assets
tree does; docs/test-only changes do not, so the version never churns needlessly).

Owner: changyu87
"""

import importlib.util
import os

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_TEST_DIR)
_SRC = os.path.join(_FEATURE_DIR, "src", "build_plugin.py")
# .rabbit/rabbit-project/features/packaging-config -> up 4 dirs = repo root
_REPO_ROOT = os.path.abspath(
    os.path.join(_FEATURE_DIR, "..", "..", "..", "..")
)
_FEATURES_REL = os.path.join(".rabbit", "rabbit-project", "features")


def _load_build():
    spec = importlib.util.spec_from_file_location("build_plugin", _SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# Dead self-deploy BUILD helpers are REMOVED; release-detection helpers KEEP.
# ==========================================================================

def test_dead_self_deploy_build_helpers_removed():
    """bump_version, package_commit_paths, _read_version, _bump_patch, _VERSION_RE,
    and _VERSION_ASSIGN are gone — they served the removed self-deploy build
    rewrite (zero callers after #324/#325)."""
    bp = _load_build()
    for name in (
        "bump_version", "package_commit_paths", "_read_version",
        "_bump_patch", "_VERSION_RE", "_VERSION_ASSIGN",
    ):
        assert not hasattr(bp, name), \
            f"dead self-deploy build helper {name!r} must be removed"


def test_release_detection_helpers_kept():
    """touches_shipped_src + SELF_DEPLOY_MARKER stay — scheduling's release_needed
    operator signal still uses them."""
    bp = _load_build()
    assert hasattr(bp, "touches_shipped_src"), \
        "touches_shipped_src must be kept (release_needed uses it)"
    assert hasattr(bp, "SELF_DEPLOY_MARKER"), \
        "SELF_DEPLOY_MARKER must be kept (release_needed walks up to find it)"
    assert callable(bp.touches_shipped_src)
    # SELF_DEPLOY_MARKER points at this build source's repo-root-relative path.
    assert bp.SELF_DEPLOY_MARKER == os.path.join(
        _FEATURES_REL, "packaging-config", "src", "build_plugin.py")


def test_build_stamps_version_from_in_memory_constant():
    """With the disk version-read removed, build() stamps plugin.json +
    marketplace.json from the in-memory _PLUGIN_VERSION constant directly."""
    import json
    import shutil
    import tempfile
    bp = _load_build()
    out_root = tempfile.mkdtemp(prefix="pkg-build-const-")
    try:
        bp.build(repo_root=_REPO_ROOT, out_root=out_root)
        pdata = json.load(open(os.path.join(
            out_root, "plugins", "auto-maintainer",
            ".claude-plugin", "plugin.json")))
        mdata = json.load(open(os.path.join(
            out_root, ".claude-plugin", "marketplace.json")))
        assert pdata["version"] == bp.PLUGIN_VERSION, pdata["version"]
        assert mdata["plugins"][0]["version"] == bp.PLUGIN_VERSION
        assert bp.PLUGIN_VERSION == bp._PLUGIN_VERSION
    finally:
        shutil.rmtree(out_root, ignore_errors=True)


# ==========================================================================
# touches_shipped_src — the rebuild trigger over a merged PR's changed files.
# ==========================================================================

def test_touches_shipped_src_true_for_a_copied_lib():
    bp = _load_build()
    changed = [os.path.join(
        _FEATURES_REL, "scheduling", "src", "run_tick.py").replace(os.sep, "/")]
    assert bp.touches_shipped_src(changed) is True


def test_touches_shipped_src_true_for_a_ship_tree_file():
    bp = _load_build()
    changed = [
        f"{_FEATURES_REL}/scheduling/ship/skills/start/SKILL.md".replace(
            os.sep, "/")]
    assert bp.touches_shipped_src(changed) is True


def test_touches_shipped_src_true_for_plugin_assets():
    bp = _load_build()
    changed = [
        f"{_FEATURES_REL}/packaging-config/src/plugin_assets/hooks/x.py".replace(
            os.sep, "/")]
    assert bp.touches_shipped_src(changed) is True


def test_touches_shipped_src_false_for_docs_and_tests_only():
    bp = _load_build()
    changed = [
        f"{_FEATURES_REL}/scheduling/docs/spec.md".replace(os.sep, "/"),
        f"{_FEATURES_REL}/scheduling/test/test_x.py".replace(os.sep, "/"),
        f"{_FEATURES_REL}/scheduling/feature.json".replace(os.sep, "/"),
        "docs/ROADMAP.md",
        "README.md",
    ]
    assert bp.touches_shipped_src(changed) is False


def test_touches_shipped_src_false_for_empty_and_none():
    bp = _load_build()
    assert bp.touches_shipped_src([]) is False
    assert bp.touches_shipped_src(None) is False


def test_touches_shipped_src_tolerates_leading_dot_slash():
    bp = _load_build()
    changed = ["./" + os.path.join(
        _FEATURES_REL, "implement", "src", "test_gate.py").replace(os.sep, "/")]
    assert bp.touches_shipped_src(changed) is True


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} failure(s)")
        raise SystemExit(1)
    print(f"\nall {len(fns)} passed")
