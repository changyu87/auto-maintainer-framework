#!/usr/bin/env python3
"""End-to-end conformance tests proving the self-deploy PACKAGE flush is GONE.

Design decision: the auto-maintainer is NOT self-deployable. It files issues
about itself but never self-evolves its own deploy/release pipeline; releases are
cut by humans. This cycle removed scheduling's self-deploy CONSUMER surface from
run_tick.py — the out-of-band PACKAGE flush and all its machinery.

Behaviours covered (every one has an e2e test, per the E2E TEST RULE):

  A. The self-deploy symbols no longer exist on run_tick: _flush_package,
     _self_deploy_repo_root, git_commit_sink, gh_pr_files_source,
     DEFAULT_PACKAGE_COMMIT_SINK, DEFAULT_PR_FILES_SOURCE, and the
     package_commit_sink / pr_files_source run_tick parameters.
  B. The build_plugin (bp) import and the sg.self_deploy reference are gone from
     the run_tick source.
  C. A tick whose INTEGRATE MERGED a PR that touched SHIPPED feature source
     performs NO package regenerate / commit / push (any git invocation during
     the tick fails the test) and still completes with the correct
     disposition/signal.
  D. The tick-end trace carries no deploy=<...> token and the tick_end event
     detail carries no self_deployed / deployed_version / deploy_skip_reason
     field. The non-deploy terminal surface (merged, refire, reported, …) is
     preserved.

scheduling CONSUMES verify-integrate + observability + the loop-core / work-intake
/ prioritize / implement / adapter-wiring / agent-dispatch / safety-governance /
durable-state features UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import inspect
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stdout

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402,F401
import work_intake as wi  # noqa: E402
import verify_integrate as vi  # noqa: E402
import observability as ob  # noqa: E402
import run_tick as rt  # noqa: E402


# ==========================================================================
# Behaviour A — the self-deploy symbols + run_tick params are GONE.
# ==========================================================================

def test_self_deploy_symbols_removed():
    for name in ("_flush_package", "_self_deploy_repo_root", "git_commit_sink",
                 "gh_pr_files_source", "DEFAULT_PACKAGE_COMMIT_SINK",
                 "DEFAULT_PR_FILES_SOURCE"):
        assert not hasattr(rt, name), f"run_tick still exposes {name}"


def test_run_tick_params_removed():
    params = inspect.signature(rt.run_tick).parameters
    assert "package_commit_sink" not in params, params
    assert "pr_files_source" not in params, params
    # The non-self-deploy pr_state_source seam (acted-ledger re-entry) STAYS.
    assert "pr_state_source" in params, params


def test_pr_state_source_seam_preserved():
    """gh_pr_files_source was self-deploy-only and is removed; gh_pr_state_source
    (the §3.8.5 acted-ledger re-entry seam) is NOT self-deploy and STAYS."""
    assert hasattr(rt, "gh_pr_state_source")
    assert rt.DEFAULT_PR_STATE_SOURCE is rt.gh_pr_state_source


# ==========================================================================
# Behaviour B — the build_plugin import + sg.self_deploy reference are gone.
# ==========================================================================

def test_run_tick_source_has_no_self_deploy_refs():
    src = inspect.getsource(rt)
    assert "import build_plugin" not in src, "build_plugin import still present"
    assert "self_deploy" not in src, "self_deploy reference still present"
    assert "_flush_package" not in src, "_flush_package reference still present"


# ==========================================================================
# A merged shipped-src PR tick: NO package/commit/push, tick still completes.
# ==========================================================================

GH_JSON_FIXTURE = """[
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


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


_OK_PR = {
    "number": 42,
    "url": "https://github.com/acme/widget/pull/42",
    "headRefName": "auto-maintainer/fix-42",
    "baseRefName": "main",
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
    ],
}
_DEFAULT_BRANCH = "main"


def _patch_vi_seams(merge_calls=None):
    saved = {
        "open": vi.gh_open_pr_source,
        "branch": vi.gh_default_branch_source,
        "merge": vi.gh_pr_merge_sink,
    }

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return [_OK_PR]

    def _branch(repo=None):
        return _DEFAULT_BRANCH

    def _merge(pr_ref, repo=None):
        if merge_calls is not None:
            merge_calls.append(pr_ref)
        return {"pr_ref": pr_ref, "url": vi._pr_url(pr_ref, repo)}

    vi.gh_open_pr_source = _open
    vi.gh_default_branch_source = _branch
    vi.gh_pr_merge_sink = _merge

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


class _NoGitCommitPush:
    """Fail loudly if the tick shells out to `git commit`/`git push` (the
    self-deploy commit/push must be GONE). Other subprocess calls pass through."""

    def __init__(self):
        self._real = subprocess.run
        self.violations = []

    def __enter__(self):
        def _guard(cmd, *a, **k):
            argv = cmd if isinstance(cmd, (list, tuple)) else [cmd]
            argv = [str(x) for x in argv]
            if argv and argv[0] == "git" and any(
                    sub in argv for sub in ("commit", "push")):
                self.violations.append(argv)
                raise AssertionError(
                    "self-deploy git commit/push must not run: " + " ".join(argv))
            return self._real(cmd, *a, **k)
        subprocess.run = _guard
        return self

    def __exit__(self, *exc):
        subprocess.run = self._real
        return False


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


def _setup_project(mode="auto-merge", route=None):
    project_dir = tempfile.mkdtemp(prefix="sched-no-deploy-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route or _CLOSE_ROUTE, f)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": mode}, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, cfg, state_path, journal_path


def _tick_end(runtime_dir):
    events = ob.EventLog(os.path.join(runtime_dir, "events.jsonl")).read()
    return next(e for e in events if e["kind"] == "tick_end")


# ==========================================================================
# Behaviour C — a merged shipped-src PR triggers NO package/commit/push.
# ==========================================================================

def test_merged_shipped_src_triggers_no_package_commit_push():
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    merge_calls = []
    restore = _patch_vi_seams(merge_calls=merge_calls)
    guard = _NoGitCommitPush()
    try:
        with guard:
            signal = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                                 state_path=state_path,
                                 journal_path=journal_path,
                                 source=_stub_source())
    finally:
        restore()
    # The merge happened (the close route is intact)…
    assert merge_calls == ["acme/widget#42"], merge_calls
    # …but NO git commit/push (the self-deploy flush is gone).
    assert guard.violations == [], guard.violations
    # The tick still completes with the correct disposition/signal (idle: no
    # acting agent-state in this pure-script close route).
    assert signal == "idle", signal


# ==========================================================================
# Behaviour D — the trace + tick_end carry no self_deployed / deploy fields,
# and the non-deploy terminal surface is preserved.
# ==========================================================================

def test_trace_and_tick_end_have_no_deploy_fields():
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    restore = _patch_vi_seams()
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
        trace = buf.getvalue()
    finally:
        restore()
    # No deploy token in the one-line trace.
    assert "deploy=" not in trace, trace
    detail = _tick_end(cfg)["detail"]
    for key in ("self_deployed", "deployed_version", "deploy_skip_reason"):
        assert key not in detail, (key, detail)
    # The non-deploy terminal surface stays intact.
    for key in ("work_items", "work_orders", "merged", "merged_refs",
                "reported_filed", "refire"):
        assert key in detail, (key, detail)


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
