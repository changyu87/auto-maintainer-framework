#!/usr/bin/env python3
"""End-to-end conformance tests for the OUTBOUND PACKAGE flush in run_tick (the
self-deployment step, #309): after the route completes, when THIS tick merged a
PR whose diff touched SHIPPED feature source AND the self_deploy gate is on AND
the mode permits merge, run_tick regenerates the committed plugin tree via
packaging-config's build_plugin.build, deterministically PATCH-bumps the version,
and commits the regenerated artifacts + the version source in ONE commit so main
never drifts.

PACKAGE is out-of-band — NOT a routed state. The flush runs at the `done` path
AFTER the route's INTEGRATE merge surface is known. It is GATED, in order, on:
self_deploy (default off), build_plugin importable, permits(merge), a merged PR
touching shipped src, and the project being the framework's own checkout.

These tests exercise _flush_package directly with INJECTED files/commit seams
(no network, no real git, no real build) over a synthetic integration_result, so
the gate logic + commit invocation are deterministically verified. The real
build()/git only run in the live framework checkout.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability", "verify-integrate", "packaging-config"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import safety_governance as sg  # noqa: E402,F401
import run_tick as rt  # noqa: E402
import build_plugin as bp  # noqa: E402


_SHIPPED_FILE = os.path.join(
    ".rabbit", "rabbit-project", "features", "scheduling", "src",
    "run_tick.py").replace(os.sep, "/")
_DOCS_FILE = os.path.join(
    ".rabbit", "rabbit-project", "features", "scheduling", "docs",
    "spec.md").replace(os.sep, "/")


def _merged_result(*pr_refs):
    return {"merged": [{"pr_ref": r, "url": ""} for r in pr_refs],
            "skipped": [], "errors": []}


class _FilesSource:
    """An injectable per-PR files source mapping pr_ref -> changed paths."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, pr_ref, repo=None):
        self.calls.append(pr_ref)
        return self.mapping.get(pr_ref, [])


class _CommitSink:
    """An injectable git commit sink: records (repo_root, paths, message)."""

    def __init__(self):
        self.calls = []

    def __call__(self, repo_root, paths, message):
        self.calls.append((repo_root, list(paths), message))
        return "abc1234"


# A monkeypatch context that swaps bp.bump_version + bp.build for no-network fakes
# so the gate-pass path is exercised without rewriting source or assembling a tree.
class _FakeBuild:
    def __init__(self):
        self.bump_calls = []
        self.build_calls = []

    def __enter__(self):
        self._real_bump = bp.bump_version
        self._real_build = bp.build

        def _bump(repo_root):
            self.bump_calls.append(repo_root)
            return "9.9.9"

        def _build(repo_root, out_root=None):
            self.build_calls.append(repo_root)
            return os.path.join(repo_root, "plugins", "auto-maintainer")

        bp.bump_version = _bump
        bp.build = _build
        return self

    def __exit__(self, *exc):
        bp.bump_version = self._real_bump
        bp.build = self._real_build
        return False


def _gov(self_deploy):
    g = sg._copy_defaults()
    g["self_deploy"] = self_deploy
    return g


# ==========================================================================
# Behaviour 0 — the self-deploy seams + helpers exist.
# ==========================================================================

def test_self_deploy_seams_exist():
    assert rt.DEFAULT_PR_FILES_SOURCE is rt.gh_pr_files_source
    assert rt.DEFAULT_PACKAGE_COMMIT_SINK is rt.git_commit_sink
    assert callable(rt._flush_package)


# ==========================================================================
# Behaviour 1 — the happy path: self_deploy on + auto-merge + a merged PR
# touching shipped src + the framework checkout -> bump + build + ONE commit.
# ==========================================================================

def test_flush_package_deploys_on_shipped_src_merge():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    sink = _CommitSink()
    # project_dir is the framework's own checkout root (carries the marker).
    project_dir = _FEATURES_repo_root()
    with _FakeBuild() as fb:
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#42"), "auto-merge",
            _gov(True), project_dir, None, files, sink)
    assert deployed is True, (deployed, reason)
    assert version == "9.9.9", version
    assert reason is None, reason
    # The bump + build ran against the resolved repo root.
    assert len(fb.bump_calls) == 1 and len(fb.build_calls) == 1
    # Exactly ONE commit, staging the build_plugin-owned commit paths, with a
    # message naming the bumped version + #309.
    assert len(sink.calls) == 1, sink.calls
    repo_root, paths, message = sink.calls[0]
    assert paths == bp.package_commit_paths(), paths
    assert "v9.9.9" in message and "#309" in message, message


def _FEATURES_repo_root():
    """The framework checkout root (parent of .rabbit/) — carries the marker, so
    _self_deploy_repo_root resolves it."""
    # _FEATURES = .../.rabbit/rabbit-project/features -> up 3 = repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(_FEATURES)))


# ==========================================================================
# Behaviour 2 — the self_deploy gate is DEFAULT OFF: a merged shipped-src change
# does NOT deploy when self_deploy is false; reason "gated".
# ==========================================================================

def test_flush_package_gated_off_by_default():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    sink = _CommitSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "auto-merge",
        _gov(False), _FEATURES_repo_root(), None, files, sink)
    assert deployed is False and version is None
    assert reason == "gated", reason
    # No build, no commit, and the files source was never even queried.
    assert sink.calls == [] and files.calls == []


# ==========================================================================
# Behaviour 3 — propose mode (permits merge False) never deploys, even with
# self_deploy on; reason "not-permitted".
# ==========================================================================

def test_flush_package_not_permitted_in_propose():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    sink = _CommitSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "propose",
        _gov(True), _FEATURES_repo_root(), None, files, sink)
    assert deployed is False and reason == "not-permitted", reason
    assert sink.calls == []


# ==========================================================================
# Behaviour 4 — a docs/test-only merge does NOT trigger a rebuild (no version
# churn); reason "no-shipped-change". The files source IS queried (to decide).
# ==========================================================================

def test_flush_package_skips_docs_only_merge():
    files = _FilesSource({"acme/widget#42": [_DOCS_FILE]})
    sink = _CommitSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "auto-merge",
        _gov(True), _FEATURES_repo_root(), None, files, sink)
    assert deployed is False and reason == "no-shipped-change", reason
    assert files.calls == ["acme/widget#42"], files.calls
    assert sink.calls == []


# ==========================================================================
# Behaviour 5 — a tick that merged NOTHING does not deploy; reason
# "no-shipped-change" (no merged PR to rebuild for).
# ==========================================================================

def test_flush_package_skips_when_nothing_merged():
    files = _FilesSource({})
    sink = _CommitSink()
    deployed, version, reason = rt._flush_package(
        {"merged": [], "skipped": [], "errors": []}, "auto-merge",
        _gov(True), _FEATURES_repo_root(), None, files, sink)
    assert deployed is False and reason == "no-shipped-change", reason
    assert files.calls == [] and sink.calls == []


# ==========================================================================
# Behaviour 6 — a maintained project that is NOT the framework checkout (no
# marker) does not deploy even when everything else passes; reason
# "not-self-repo".
# ==========================================================================

def test_flush_package_skips_when_not_framework_checkout():
    import tempfile
    not_framework = tempfile.mkdtemp(prefix="sched-not-fw-")
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    sink = _CommitSink()
    with _FakeBuild():
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#42"), "auto-merge",
            _gov(True), not_framework, None, files, sink)
    assert deployed is False and reason == "not-self-repo", reason
    assert sink.calls == []


# ==========================================================================
# Behaviour 7 — multiple merged PRs: ANY one touching shipped src triggers the
# deploy (the first docs-only PR does not block the second shipped-src PR).
# ==========================================================================

def test_flush_package_deploys_when_any_merged_pr_touches_shipped_src():
    files = _FilesSource({
        "acme/widget#1": [_DOCS_FILE],
        "acme/widget#2": [_SHIPPED_FILE],
    })
    sink = _CommitSink()
    with _FakeBuild():
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#1", "acme/widget#2"), "auto-merge",
            _gov(True), _FEATURES_repo_root(), None, files, sink)
    assert deployed is True, reason
    assert len(sink.calls) == 1, sink.calls


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
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
        sys.exit(1)
    print(f"\nall {len(fns)} passed")
