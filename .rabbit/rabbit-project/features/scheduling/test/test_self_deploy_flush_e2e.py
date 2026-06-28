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
    """An injectable git commit sink: records
    (repo_root, paths, message, default_branch)."""

    def __init__(self, order=None):
        self.calls = []
        self.order = order if order is not None else []

    def __call__(self, repo_root, paths, message, default_branch):
        self.calls.append((repo_root, list(paths), message, default_branch))
        self.order.append("commit")
        return "abc1234"


class _SyncSink:
    """An injectable git sync sink (#313): records (repo_root, default_branch)
    and, when given a shared `order` list, appends a "sync" marker so tests can
    assert the checkout was synced BEFORE the build ran."""

    def __init__(self, order=None):
        self.calls = []
        self.order = order if order is not None else []

    def __call__(self, repo_root, default_branch):
        self.calls.append((repo_root, default_branch))
        self.order.append("sync")
        return "def5678"


# A monkeypatch context that swaps bp.bump_version + bp.build for no-network fakes
# so the gate-pass path is exercised without rewriting source or assembling a tree.
class _FakeBuild:
    def __init__(self, order=None):
        self.bump_calls = []
        self.build_calls = []
        self.order = order if order is not None else []

    def __enter__(self):
        self._real_bump = bp.bump_version
        self._real_build = bp.build
        self._real_branch = rt.vi.gh_default_branch_source

        def _bump(repo_root):
            self.bump_calls.append(repo_root)
            self.order.append("bump")
            return "9.9.9"

        def _build(repo_root, out_root=None):
            self.build_calls.append(repo_root)
            self.order.append("build")
            return os.path.join(repo_root, "plugins", "auto-maintainer")

        bp.bump_version = _bump
        bp.build = _build
        # Stub the default-branch resolver so the (gate-pass) sync step touches
        # no network in the unit suite.
        rt.vi.gh_default_branch_source = lambda repo=None: "main"
        return self

    def __exit__(self, *exc):
        bp.bump_version = self._real_bump
        bp.build = self._real_build
        rt.vi.gh_default_branch_source = self._real_branch
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
    assert rt.DEFAULT_PACKAGE_SYNC_SINK is rt.git_sync_sink
    assert callable(rt._flush_package)


# ==========================================================================
# Behaviour 1 — the happy path: self_deploy on + auto-merge + a merged PR
# touching shipped src + the framework checkout -> bump + build + ONE commit.
# ==========================================================================

def test_flush_package_deploys_on_shipped_src_merge():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    order = []
    sink = _CommitSink(order)
    sync = _SyncSink(order)
    # project_dir is the framework's own checkout root (carries the marker).
    project_dir = _FEATURES_repo_root()
    with _FakeBuild(order) as fb:
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#42"), "auto-merge",
            _gov(True), project_dir, None, files, sink, sync)
    assert deployed is True, (deployed, reason)
    assert version == "9.9.9", version
    assert reason is None, reason
    # The bump + build ran against the resolved repo root.
    assert len(fb.bump_calls) == 1 and len(fb.build_calls) == 1
    # The checkout was SYNCED to the merged remote base (#313) BEFORE the build,
    # against the resolved repo root + default branch.
    assert len(sync.calls) == 1, sync.calls
    sync_root, sync_branch = sync.calls[0]
    assert sync_root == fb.build_calls[0], (sync_root, fb.build_calls)
    assert sync_branch == "main", sync_branch
    assert order == ["sync", "bump", "build", "commit"], order
    # Exactly ONE commit, staging the build_plugin-owned commit paths, with a
    # message naming the bumped version + #309.
    assert len(sink.calls) == 1, sink.calls
    repo_root, paths, message, default_branch = sink.calls[0]
    assert paths == bp.package_commit_paths(), paths
    assert "v9.9.9" in message and "#309" in message, message
    # The flush threads the resolved default branch to the commit sink so the
    # publish push (#320) names the remote dest branch explicitly.
    assert default_branch == "main", default_branch


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
    sync = _SyncSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "auto-merge",
        _gov(False), _FEATURES_repo_root(), None, files, sink, sync)
    assert deployed is False and version is None
    assert reason == "gated", reason
    # No sync, no build, no commit, and the files source was never even queried.
    assert sink.calls == [] and files.calls == [] and sync.calls == []


# ==========================================================================
# Behaviour 3 — propose mode (permits merge False) never deploys, even with
# self_deploy on; reason "not-permitted".
# ==========================================================================

def test_flush_package_not_permitted_in_propose():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    sink = _CommitSink()
    sync = _SyncSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "propose",
        _gov(True), _FEATURES_repo_root(), None, files, sink, sync)
    assert deployed is False and reason == "not-permitted", reason
    assert sink.calls == [] and sync.calls == []


# ==========================================================================
# Behaviour 4 — a docs/test-only merge does NOT trigger a rebuild (no version
# churn); reason "no-shipped-change". The files source IS queried (to decide).
# ==========================================================================

def test_flush_package_skips_docs_only_merge():
    files = _FilesSource({"acme/widget#42": [_DOCS_FILE]})
    sink = _CommitSink()
    sync = _SyncSink()
    deployed, version, reason = rt._flush_package(
        _merged_result("acme/widget#42"), "auto-merge",
        _gov(True), _FEATURES_repo_root(), None, files, sink, sync)
    assert deployed is False and reason == "no-shipped-change", reason
    assert files.calls == ["acme/widget#42"], files.calls
    assert sink.calls == [] and sync.calls == []


# ==========================================================================
# Behaviour 5 — a tick that merged NOTHING does not deploy; reason
# "no-shipped-change" (no merged PR to rebuild for).
# ==========================================================================

def test_flush_package_skips_when_nothing_merged():
    files = _FilesSource({})
    sink = _CommitSink()
    sync = _SyncSink()
    deployed, version, reason = rt._flush_package(
        {"merged": [], "skipped": [], "errors": []}, "auto-merge",
        _gov(True), _FEATURES_repo_root(), None, files, sink, sync)
    assert deployed is False and reason == "no-shipped-change", reason
    assert files.calls == [] and sink.calls == [] and sync.calls == []


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
    sync = _SyncSink()
    with _FakeBuild():
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#42"), "auto-merge",
            _gov(True), not_framework, None, files, sink, sync)
    assert deployed is False and reason == "not-self-repo", reason
    assert sink.calls == [] and sync.calls == []


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
    sync = _SyncSink()
    with _FakeBuild():
        deployed, version, reason = rt._flush_package(
            _merged_result("acme/widget#1", "acme/widget#2"), "auto-merge",
            _gov(True), _FEATURES_repo_root(), None, files, sink, sync)
    assert deployed is True, reason
    assert len(sink.calls) == 1, sink.calls
    assert len(sync.calls) == 1, sync.calls


# ==========================================================================
# Behaviour 8 — the PRODUCTION git_commit_sink PUBLISHES the self-deploy commit:
# it stages the given paths, commits, and PUSHES to remote `default_branch` (#312)
# so remote main (where CI runs the build-drift guards) actually advances. Without
# the push the bump+commit would land only in the loop's local checkout while
# INTEGRATE merges PRs server-side, leaving remote main drifted/RED. The push uses
# an EXPLICIT `origin HEAD:<default_branch>` refspec (#320) — the preceding sync
# hard-resets the checkout onto origin/<default_branch>, leaving it
# detached/upstream-less, where a bare `git push` would raise and abort the flush.
# ==========================================================================

def test_git_commit_sink_pushes_the_self_deploy_commit():
    class _RecordingRunner:
        def __init__(self):
            self.cmds = []

        def __call__(self, cmd, check=None, capture_output=False, text=False):
            self.cmds.append(list(cmd))

            class _Result:
                stdout = "deadbee\n"
            return _Result()

    runner = _RecordingRunner()
    sha = rt.git_commit_sink("/repo", ["a", "b"], "msg", "main", runner=runner)
    assert sha == "deadbee", sha
    # cmd[0:3] is always `git -C /repo`; cmd[3] is the git verb.
    git_verbs = [c[3] for c in runner.cmds]
    # The sink stages, commits, PUSHES, then reads the sha — the push between the
    # commit and the rev-parse is the #312/#320 fix that publishes to remote main.
    assert git_verbs == ["add", "commit", "push", "rev-parse"], git_verbs
    # The push names the remote AND an explicit HEAD:<default_branch> refspec
    # (#320) so it does not depend on the checkout's branch/upstream state.
    assert runner.cmds[2] == [
        "git", "-C", "/repo", "push", "origin", "HEAD:main"], runner.cmds[2]


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
