#!/usr/bin/env python3
"""Version-monotonicity-on-content-change guard for the shipped plugin lib (#355).

The existing build-drift guard (test_build_plugin.test_committed_plugin_tree_
matches_fresh_build) enforces committed == fresh build. This module adds the
COMPLEMENTARY release invariant: a change to the committed shipped
`plugins/auto-maintainer/lib` bytes MUST advance `_PLUGIN_VERSION`. Without it a
same-version content change is marketplace-invisible — `/plugin marketplace
update` serves by version, so an existing install never receives content that
changed at the same version (the #350 drift: the #342 default_src token was
regenerated into the shipped lib at 0.8.0 without a bump, and a hand-cut 0.8.1
was needed to make it release-clean).

The invariant is anchored deterministically by test/release_lib_baseline.json,
which records the last RELEASED `version` and the `lib_digest` (a deterministic
sha256 over the committed lib/ tree) at that release:

  - shipped lib UNCHANGED since the baseline (current digest == baseline digest):
    any version is fine (docs-only / test-only changes are unaffected);
  - shipped lib CHANGED (current digest != baseline digest): `_PLUGIN_VERSION`
    MUST differ from the baseline `version` (it must have advanced).

An operator cutting a release bumps `_PLUGIN_VERSION` AND re-anchors both
baseline fields (version -> new version, lib_digest -> the new committed-lib
digest), exactly as the doc/e2e baselines are re-anchored.

Owner: changyu87
"""

import hashlib
import importlib.util
import json
import os

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))
_FEATURE_DIR = os.path.dirname(_TEST_DIR)
_SRC = os.path.join(_FEATURE_DIR, "src", "build_plugin.py")
# .rabbit/rabbit-project/features/packaging-config -> up 4 dirs = worktree root
_REPO_ROOT = os.path.abspath(
    os.path.join(_FEATURE_DIR, "..", "..", "..", "..")
)
_BASELINE_PATH = os.path.join(_TEST_DIR, "release_lib_baseline.json")
_COMMITTED_LIB = os.path.join(
    _REPO_ROOT, "plugins", "auto-maintainer", "lib"
)


def _load_build():
    spec = importlib.util.spec_from_file_location("build_plugin", _SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lib_digest(lib_dir):
    """Deterministic sha256 over a plugin lib/ tree.

    Every file under lib/ (recursively) contributes its lib-relative path +
    NUL + bytes + NUL, in sorted relative-path order; bytecode caches (*.pyc,
    including everything under __pycache__/) are excluded (they are gitignored
    and never part of the committed tree). Walking recursively means a byte
    change to any shipped lib file — including one nested in a subdirectory —
    changes the digest, so the version-monotonicity guard cannot be evaded by
    nesting lib content in a subdir. For the flat lib committed today, each
    relative path is just the file's basename, so the digest is unchanged.
    """
    h = hashlib.sha256()
    entries = []
    for root, dirs, files in os.walk(lib_dir):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".pyc"):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, lib_dir)
            entries.append((rel, full))
    for rel, full in sorted(entries):
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        with open(full, "rb") as fh:
            h.update(fh.read())
        h.update(b"\0")
    return h.hexdigest()


def _load_baseline():
    with open(_BASELINE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# The baseline fixture is present and well-formed: it records the last released
# `version` and its `lib_digest` so the guard has a deterministic anchor.
# ---------------------------------------------------------------------------
def test_release_lib_baseline_present_and_wellformed():
    assert os.path.isfile(_BASELINE_PATH), \
        f"release_lib_baseline.json must exist at {_BASELINE_PATH}"
    baseline = _load_baseline()
    version = baseline.get("version", "")
    parts = version.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts), \
        f"baseline version must be explicit semver, got {version!r}"
    digest = baseline.get("lib_digest", "")
    assert isinstance(digest, str) and len(digest) == 64, \
        "baseline lib_digest must be a sha256 hex digest"


# ---------------------------------------------------------------------------
# The #355 invariant: shipped-lib bytes changed ⇒ _PLUGIN_VERSION must advance.
#
#   - lib UNCHANGED since the baseline: any version passes (docs/test-only edits
#     are unaffected — they leave the lib digest identical);
#   - lib CHANGED: _PLUGIN_VERSION must differ from the baseline version (a
#     same-version content change fails, catching the #350 marketplace-invisible
#     drift).
# ---------------------------------------------------------------------------
def test_shipped_lib_change_requires_version_bump():
    assert os.path.isdir(_COMMITTED_LIB), \
        f"committed plugin lib/ must exist at {_COMMITTED_LIB}"
    mod = _load_build()
    baseline = _load_baseline()

    current_digest = _lib_digest(_COMMITTED_LIB)
    baseline_digest = baseline["lib_digest"]
    baseline_version = baseline["version"]
    current_version = mod._PLUGIN_VERSION

    if current_digest == baseline_digest:
        # Shipped lib unchanged since the last release — nothing to enforce
        # (docs-only / test-only changes leave the digest identical).
        return

    # Shipped lib changed: the version MUST have advanced past the baseline so
    # the marketplace serves the new content to existing installs.
    assert current_version != baseline_version, (
        "shipped plugin lib bytes changed relative to the last released "
        f"version ({baseline_version}) but _PLUGIN_VERSION is still "
        f"{current_version!r}. A same-version content change is "
        "marketplace-invisible (/plugin marketplace update serves by version). "
        "Bump _PLUGIN_VERSION in build_plugin.py AND re-anchor "
        "test/release_lib_baseline.json (version + lib_digest) for the release."
    )


# ---------------------------------------------------------------------------
# The two guards agree: the committed lib is exactly a fresh build (guaranteed
# by the build-drift guard), so the digest this module records for a release is
# the digest of what build_plugin produces from current src — the two invariants
# operate on the same shipped bytes.
# ---------------------------------------------------------------------------
def test_baseline_digest_covers_the_fresh_build_lib():
    import shutil
    import tempfile

    mod = _load_build()
    out_root = tempfile.mkdtemp(prefix="pkgcfg-relbase-")
    try:
        mod.build(repo_root=_REPO_ROOT, out_root=out_root)
        fresh_lib = os.path.join(
            out_root, "plugins", "auto-maintainer", "lib"
        )
        assert _lib_digest(fresh_lib) == _lib_digest(_COMMITTED_LIB), (
            "the committed lib digest must equal a fresh build's lib digest "
            "(the build-drift guard enforces this) — regenerate the plugin tree"
        )
    finally:
        shutil.rmtree(out_root, ignore_errors=True)
