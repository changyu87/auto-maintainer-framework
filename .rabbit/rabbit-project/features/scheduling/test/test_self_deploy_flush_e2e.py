#!/usr/bin/env python3
"""End-to-end conformance tests for the release_needed DETECTION in run_tick.

The auto-maintainer is NOT self-deployable: it does NOT regenerate / commit /
push the plugin tree itself. It DOES keep the operator signal `release_needed`
— the human-release-owed flag (#319). When THIS tick merged >=1 PR whose diff
touched SHIPPED feature source AND the maintained project is the framework's own
checkout, a human release is owed to keep the plugin version 1:1 with the shipped
bytes. The loop merges PRs server-side but performs NO build/commit/push, so the
committed plugin tree drifts from the bumped version a release would carry;
`release_needed` surfaces that explicitly so the operator knows a release is owed.

`_release_needed` fires PURELY on the merged shipped-src change + the framework
checkout — it is INDEPENDENT of any self_deploy knob (nothing self-deploys now).
These tests exercise `_release_needed` directly with an INJECTED files source (no
network) over a synthetic integration_result, plus an e2e tick proving a merged
shipped-src change does NO commit / push / regenerate yet still sets
release_needed=True on the trace + tick_end event.

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

import json  # noqa: E402
import safety_governance as sg  # noqa: E402,F401
import run_tick as rt  # noqa: E402
import build_plugin as bp  # noqa: E402,F401


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


def _FEATURES_repo_root():
    """The framework checkout root (parent of .rabbit/) — carries the marker, so
    _self_deploy_repo_root resolves it."""
    # _FEATURES = .../.rabbit/rabbit-project/features -> up 3 = repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(_FEATURES)))


# ==========================================================================
# Behaviour 0 — the kept detection seams + helper exist; the self-deploy ACTION
# seams (the flush + the git commit/sync sinks) are GONE.
# ==========================================================================

def test_release_detection_seams_exist():
    # The PR-files source seam (shared by the detector) is KEPT.
    assert rt.DEFAULT_PR_FILES_SOURCE is rt.gh_pr_files_source
    # The own-checkout detector is KEPT.
    assert callable(rt._self_deploy_repo_root)
    # The detector itself is KEPT.
    assert callable(rt._release_needed)


def test_self_deploy_action_seams_removed():
    # The self-deploy ACTION (build/commit/push/sync) is removed wholesale —
    # the auto-maintainer is not self-deployable.
    for gone in ("_flush_package", "git_commit_sink", "git_sync_sink",
                 "DEFAULT_PACKAGE_COMMIT_SINK", "DEFAULT_PACKAGE_SYNC_SINK"):
        assert not hasattr(rt, gone), gone


# ==========================================================================
# Behaviour 1 — release_needed is TRUE when a merged PR touched shipped src in
# the framework's own checkout (a human release is owed). It fires on the merged
# shipped-src change alone — no package_deployed param, no self_deploy knob.
# ==========================================================================

def test_release_needed_true_when_shipped_src_merged():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files, None) is True
    assert files.calls == ["acme/widget#42"], files.calls


# ==========================================================================
# Behaviour 2 — release_needed is INDEPENDENT of any self_deploy governance knob:
# whether self_deploy is on or off (the knob is dead now), the signal fires the
# same on a merged shipped-src change. _release_needed takes no gov / no knob.
# ==========================================================================

def test_release_needed_independent_of_self_deploy_knob():
    files_a = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    files_b = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    # Same args (no gov passed) -> same True result; the function signature has
    # no governance/knob input at all.
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files_a, None) is True
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files_b, None) is True


# ==========================================================================
# Behaviour 3 — a docs/test-only merge owes NO release; False.
# ==========================================================================

def test_release_needed_false_for_docs_only_merge():
    files = _FilesSource({"acme/widget#42": [_DOCS_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files, None) is False


# ==========================================================================
# Behaviour 4 — a tick that merged NOTHING owes no release; False (files source
# never queried).
# ==========================================================================

def test_release_needed_false_when_nothing_merged():
    files = _FilesSource({})
    assert rt._release_needed(
        {"merged": [], "skipped": [], "errors": []},
        _FEATURES_repo_root(), files, None) is False
    assert files.calls == []


# ==========================================================================
# Behaviour 5 — a maintained project that is NOT the framework checkout owes no
# self-release; False (files source never queried — no extra network).
# ==========================================================================

def test_release_needed_false_when_not_framework_checkout():
    import tempfile
    not_framework = tempfile.mkdtemp(prefix="sched-rel-not-fw-")
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), not_framework,
        files, None) is False
    assert files.calls == []


# ==========================================================================
# Behaviour 6 — multiple merged PRs: ANY one touching shipped src owes a release.
# ==========================================================================

def test_release_needed_true_when_any_merged_pr_touches_shipped_src():
    files = _FilesSource({
        "acme/widget#1": [_DOCS_FILE],
        "acme/widget#2": [_SHIPPED_FILE],
    })
    assert rt._release_needed(
        _merged_result("acme/widget#1", "acme/widget#2"),
        _FEATURES_repo_root(), files, None) is True


# ==========================================================================
# Behaviour 7 (E2E) — a full tick whose INTEGRATE merged a shipped-src change
# does NO commit / push / regenerate (the loop is not self-deployable) yet sets
# release_needed=True on the one-line trace AND the tick_end event. This proves
# the detection survives while the action is gone: no git/build seam is ever
# touched.
# ==========================================================================

import io  # noqa: E402
import json as _json  # noqa: E402,F811
import tempfile  # noqa: E402
from contextlib import redirect_stdout  # noqa: E402

import work_intake as wi  # noqa: E402
import verify_integrate as vi  # noqa: E402
import observability as ob  # noqa: E402


_GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  }
]"""

# A single OPEN, mergeable PR -> VERIFY derives an `ok` verdict; INTEGRATE merges
# it at auto-merge.
_OK_PR = {
    "number": 42,
    "url": "https://github.com/acme/widget/pull/42",
    "headRefName": "auto-maintainer/fix-42",
    "baseRefName": "main",
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
}

# The close-the-loop route (pure-script with the DEFAULT map): a real INTEGRATE
# merges the ok PR, so the terminal sees a merged shipped-src change.
_CLOSE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "VERIFY", "REVIEW", "INTEGRATE", "CLEANUP", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PRIORITIZE"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PRIORITIZE"},
        {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
        {"state": "PRIORITIZE", "signal": "EMPTY", "next": "IMPLEMENT"},
        {"state": "IMPLEMENT", "signal": "OK", "next": "VERIFY"},
        {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "REVIEW"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "REVIEW"},
        {"state": "REVIEW", "signal": "OK", "next": "INTEGRATE"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "INTEGRATE"},
        {"state": "INTEGRATE", "signal": "OK", "next": "CLEANUP"},
        {"state": "CLEANUP", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def _stub_source(json_text=_GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _patch_vi_merge():
    """Override verify-integrate's gh seams so VERIFY/INTEGRATE touch no network
    and INTEGRATE merges the ok PR. Returns a restore callable."""
    saved = {"open": vi.gh_open_pr_source,
             "branch": vi.gh_default_branch_source,
             "merge": vi.gh_pr_merge_sink}

    vi.gh_open_pr_source = lambda repo=None, label=vi.LOOP_PR_LABEL: [dict(_OK_PR)]
    vi.gh_default_branch_source = lambda repo=None: "main"
    vi.gh_pr_merge_sink = (
        lambda pr_ref, repo=None: {"pr_ref": pr_ref,
                                   "url": vi._pr_url(pr_ref, repo)})

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


def test_e2e_merged_shipped_src_sets_release_needed_without_acting():
    """A full auto-merge tick whose INTEGRATE merges a shipped-src PR does NO
    commit / push / regenerate (the loop is not self-deployable — those seams no
    longer exist) yet sets release_needed=True on the one-line trace AND the
    tick_end event. The detector touches ONLY the injected files source; no
    git/build seam is reachable."""
    # A temp project dir made into a valid "framework checkout" by planting the
    # SELF_DEPLOY_MARKER file, so _self_deploy_repo_root resolves it AND the tick
    # loads THIS dir's route/governance (config resolves from project_dir).
    project_dir = tempfile.mkdtemp(prefix="sched-rel-e2e-")
    marker = os.path.join(project_dir, bp.SELF_DEPLOY_MARKER)
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w") as f:
        f.write("")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        _json.dump(_CLOSE_ROUTE, f)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        _json.dump({"mode": "auto-merge"}, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")

    # The merged PR's diff touches shipped src -> a human release is owed.
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})

    restore = _patch_vi_merge()
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(runtime_dir=cfg, state_path=state_path,
                        journal_path=journal_path, project_dir=project_dir,
                        source=_stub_source(), pr_files_source=files)
        trace = buf.getvalue()
    finally:
        restore()

    # The one-line trace carries the standalone release_needed token (no deploy=).
    assert "release_needed" in trace, trace
    assert "deploy=" not in trace, trace
    # The tick_end event carries release_needed=True; the removed self-deploy
    # action keys are gone.
    end = next(e for e in ob.EventLog(os.path.join(cfg, "events.jsonl")).read()
               if e["kind"] == "tick_end")
    assert end["detail"].get("release_needed") is True, end
    assert "self_deployed" not in end["detail"], end
    assert "deployed_version" not in end["detail"], end
    assert "deploy_skip_reason" not in end["detail"], end
    # The detector queried the merged PR's files (the only seam it touches).
    assert files.calls == ["acme/widget#42"], files.calls


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
