#!/usr/bin/env python3
"""Unit tests for packaging-config's self-deploy helpers (#309): the deterministic
PATCH version bump and the shipped-src change detector that gate the loop's
self-deployment (scheduling's out-of-band PACKAGE flush).

The version-bump policy lives in packaging-config (the build owner): a self-deploy
always increments PATCH, deterministically, by rewriting the _PLUGIN_VERSION
constant in build_plugin.py so the new constant is the verbatim source of truth the
next build() reads. touches_shipped_src decides whether a merged PR's diff requires
a rebuild at all (only a change to a shipped lib source or a ship/ / plugin_assets
tree does; docs/test-only changes do not, so the version never churns needlessly).

Owner: changyu87
"""

import importlib.util
import os
import shutil
import tempfile

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
# _bump_patch — deterministic PATCH increment, malformed input rejected.
# ==========================================================================

def test_bump_patch_increments_only_patch():
    bp = _load_build()
    assert bp._bump_patch("0.7.10") == "0.7.11"
    assert bp._bump_patch("1.0.0") == "1.0.1"
    assert bp._bump_patch("2.9.99") == "2.9.100"


def test_bump_patch_is_deterministic():
    bp = _load_build()
    assert bp._bump_patch("3.4.5") == bp._bump_patch("3.4.5")


def test_bump_patch_rejects_malformed_version():
    bp = _load_build()
    for bad in ("1.2", "1.2.3.4", "1.2.x", "v1.2.3", ""):
        try:
            bp._bump_patch(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass


# ==========================================================================
# bump_version — rewrites the constant in a COPY of the repo, returns the new
# version, and the new constant becomes what a fresh import reads.
# ==========================================================================

def _staged_repo_with_build_source():
    """A temp repo root carrying ONLY the packaging-config build source at the
    real relative path, so bump_version can rewrite it without touching the real
    checkout."""
    root = tempfile.mkdtemp(prefix="pkg-bump-")
    dest_dir = os.path.join(root, _FEATURES_REL, "packaging-config", "src")
    os.makedirs(dest_dir)
    shutil.copyfile(_SRC, os.path.join(dest_dir, "build_plugin.py"))
    return root


def test_bump_version_rewrites_constant_and_returns_next_patch():
    bp = _load_build()
    root = _staged_repo_with_build_source()
    expected = bp._bump_patch(bp.PLUGIN_VERSION)
    new_version = bp.bump_version(root)
    assert new_version == expected, (new_version, expected)
    # The rewritten source now carries the bumped constant verbatim.
    staged = os.path.join(
        root, _FEATURES_REL, "packaging-config", "src", "build_plugin.py")
    body = open(staged).read()
    assert f'_PLUGIN_VERSION = "{new_version}"' in body, body[:200]
    assert f'_PLUGIN_VERSION = "{bp.PLUGIN_VERSION}"' not in body


# ==========================================================================
# build() reads the version from the (rewritten) source on disk, NOT the
# in-memory constant frozen at import (#311). The self-deploy flush calls
# bump_version(repo_root) then build(repo_root) in the SAME process, so build
# must stamp plugin.json + marketplace.json with the BUMPED version, otherwise
# the committed tree mismatches its source and the build-drift guard goes RED.
# ==========================================================================

def test_read_version_returns_bumped_value_in_same_process():
    bp = _load_build()
    root = _staged_repo_with_build_source()
    # Before the bump, _read_version reflects the current source-of-truth.
    assert bp._read_version(root) == bp.PLUGIN_VERSION
    new_version = bp.bump_version(root)
    # After the bump rewrites the disk source, _read_version sees the NEW
    # version even though the in-memory _PLUGIN_VERSION constant is unchanged.
    assert bp._read_version(root) == new_version
    assert new_version != bp.PLUGIN_VERSION


def test_read_version_falls_back_to_constant_when_source_absent():
    bp = _load_build()
    empty = tempfile.mkdtemp(prefix="pkg-readver-empty-")
    try:
        assert bp._read_version(empty) == bp.PLUGIN_VERSION
    finally:
        shutil.rmtree(empty, ignore_errors=True)


def test_build_stamps_bumped_version_after_bump_in_same_process():
    """The real bump(disk) -> build(reads disk) interaction the e2e _FakeBuild
    cannot exercise (#311): a bump followed by a build in the same process must
    produce plugin.json + marketplace.json carrying the BUMPED version."""
    import json
    bp = _load_build()
    bumped = bp._bump_patch(bp.PLUGIN_VERSION)
    # Rewrite the version constant in the REAL repo's source on disk, then
    # restore it so the working tree is left untouched.
    src = os.path.join(
        _REPO_ROOT, _FEATURES_REL, "packaging-config", "src", "build_plugin.py")
    original = open(src, encoding="utf-8").read()
    out_root = tempfile.mkdtemp(prefix="pkg-build-bumped-")
    try:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(original.replace(
                f'_PLUGIN_VERSION = "{bp.PLUGIN_VERSION}"',
                f'_PLUGIN_VERSION = "{bumped}"', 1))
        bp.build(repo_root=_REPO_ROOT, out_root=out_root)
        pdata = json.load(open(os.path.join(
            out_root, "plugins", "auto-maintainer",
            ".claude-plugin", "plugin.json")))
        mdata = json.load(open(os.path.join(
            out_root, ".claude-plugin", "marketplace.json")))
        assert pdata["version"] == bumped, pdata["version"]
        assert mdata["plugins"][0]["version"] == bumped
    finally:
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(original)
        shutil.rmtree(out_root, ignore_errors=True)


def test_bump_version_missing_source_raises():
    bp = _load_build()
    empty = tempfile.mkdtemp(prefix="pkg-bump-empty-")
    try:
        bp.bump_version(empty)
        assert False, "expected RuntimeError when build source is absent"
    except (RuntimeError, FileNotFoundError):
        pass


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
