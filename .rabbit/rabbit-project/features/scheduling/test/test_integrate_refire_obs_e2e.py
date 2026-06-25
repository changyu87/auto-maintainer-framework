#!/usr/bin/env python3
"""End-to-end conformance tests for INTEGRATE + refire OBSERVABILITY.

Dogfood finding: the tick_end event + one-line trace surfaced
work_items/work_orders/handoffs/reported_* but NOT INTEGRATE's merge results, so
a successful merge (and the refire/idle decision) was INVISIBLE — a working merge
looked broken. This cycle makes both legible (edits ONLY in scheduling
run_tick.py); it consumes verify-integrate's integration_result + observability
UNCHANGED.

Behaviours covered (every one has an e2e test, per the E2E TEST RULE):

  A. A tick whose INTEGRATE MERGED a PR (auto-merge) emits a tick_end event whose
     detail carries merged=<n>, integrate_skipped, integrate_errored, and
     merged_refs (the list of merged pr_refs); the one-line trace shows a
     merged=<n> token. The signal is idle (pure-script close route) with
     refire:false in the detail.
  B. A no-work / no-merge tick shows merged=0 in both the trace and the tick_end
     detail, merged_refs=[], and refire:false.
  C. The pre-existing tick_end detail keys (work_items/work_orders/
     execution_plan/handoffs/reported_*) are PRESERVED alongside the new keys.
  D. integrate_errored is surfaced (>0) when the merge sink raises for a PR; the
     trace appends integrate_errored=<n> only when >0.

scheduling CONSUMES verify-integrate + observability + the loop-core / work-intake
/ prioritize / implement / adapter-wiring / agent-dispatch / safety-governance /
durable-state features UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import io
import json
import os
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

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import verify_integrate as vi  # noqa: E402
import observability as ob  # noqa: E402
import run_tick as rt  # noqa: E402


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


# A single OPEN, mergeable PR on the default branch -> VERIFY derives an `ok`
# verdict; INTEGRATE merges it at auto-merge.
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


def _patch_vi_seams(open_prs=None, merge_calls=None, raise_merge=False):
    """Override verify-integrate's gh seams so VERIFY/INTEGRATE touch no network.
    Returns a restore callable."""
    saved = {
        "open": vi.gh_open_pr_source,
        "branch": vi.gh_default_branch_source,
        "merge": vi.gh_pr_merge_sink,
    }

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return list(open_prs if open_prs is not None else [_OK_PR])

    def _branch(repo=None):
        return _DEFAULT_BRANCH

    def _merge(pr_ref, repo=None):
        if merge_calls is not None:
            merge_calls.append(pr_ref)
        if raise_merge:
            raise RuntimeError("merge sink failed for " + pr_ref)
        return {"pr_ref": pr_ref, "url": vi._pr_url(pr_ref, repo)}

    vi.gh_open_pr_source = _open
    vi.gh_default_branch_source = _branch
    vi.gh_pr_merge_sink = _merge

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


# The close-the-loop route (pure-script with the DEFAULT map): TRIAGE ->
# PRIORITIZE -> IMPLEMENT -> VERIFY -> REVIEW -> INTEGRATE -> CLEANUP. No
# agent-state -> has_acting_agent is False -> EXIT idles (refire:false).
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
    project_dir = tempfile.mkdtemp(prefix="sched-int-obs-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route or _CLOSE_ROUTE, f)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": mode}, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, cfg, state_path, journal_path


def _events(runtime_dir):
    return ob.EventLog(os.path.join(runtime_dir, "events.jsonl")).read()


def _tick_end(runtime_dir):
    return next(e for e in _events(runtime_dir) if e["kind"] == "tick_end")


# ==========================================================================
# Behaviour A — a merged tick surfaces merged/merged_refs in detail + trace.
# ==========================================================================

def test_merged_tick_emits_tick_end_merged_detail_and_refs():
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    merge_calls = []
    restore = _patch_vi_seams(merge_calls=merge_calls)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            signal = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                                 state_path=state_path,
                                 journal_path=journal_path,
                                 source=_stub_source())
        trace = buf.getvalue()
    finally:
        restore()
    assert merge_calls == ["acme/widget#42"], merge_calls
    detail = _tick_end(cfg)["detail"]
    assert detail.get("merged") == 1, detail
    assert detail.get("integrate_skipped") == 0, detail
    assert detail.get("integrate_errored") == 0, detail
    assert detail.get("merged_refs") == ["acme/widget#42"], detail
    # The one-line trace shows the merged token.
    assert "merged=1" in trace, trace
    # No agent-state -> idle, refire:false.
    assert signal == "idle", signal
    assert detail.get("refire") is False, detail


# ==========================================================================
# Behaviour B — a no-merge tick shows merged=0 / merged_refs=[] / refire:false.
# ==========================================================================

def test_no_merge_tick_shows_merged_zero_and_refire_false():
    # Default read-and-idle route: no INTEGRATE at all -> merged=0.
    project_dir, cfg, state_path, journal_path = _setup_project(
        "propose", route=rt.DEFAULT_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source())
    trace = buf.getvalue()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("merged") == 0, detail
    assert detail.get("merged_refs") == [], detail
    assert detail.get("integrate_skipped") == 0, detail
    assert detail.get("integrate_errored") == 0, detail
    assert detail.get("refire") is False, detail
    assert "merged=0" in trace, trace
    assert signal == "idle", signal


def test_propose_skipped_merge_surfaces_integrate_skipped():
    """At propose, an ok verdict is NOT merged (a would-merge intent recorded under
    skipped). The tick_end detail shows merged=0 + integrate_skipped=1, and the
    trace token is merged=0."""
    project_dir, cfg, state_path, journal_path = _setup_project("propose")
    merge_calls = []
    restore = _patch_vi_seams(merge_calls=merge_calls)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
        trace = buf.getvalue()
    finally:
        restore()
    assert merge_calls == [], merge_calls
    detail = _tick_end(cfg)["detail"]
    assert detail.get("merged") == 0, detail
    assert detail.get("integrate_skipped") == 1, detail
    assert detail.get("merged_refs") == [], detail
    assert "merged=0" in trace, trace


# ==========================================================================
# Behaviour C — existing tick_end detail keys are preserved.
# ==========================================================================

def test_existing_tick_end_detail_keys_preserved():
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    restore = _patch_vi_seams()
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    finally:
        restore()
    detail = _tick_end(cfg)["detail"]
    for key in ("work_items", "work_orders", "execution_plan", "handoffs",
                "reported_filed", "reported_skipped", "reported_errored"):
        assert key in detail, (key, detail)
    # And the new INTEGRATE/refire keys are present alongside them.
    for key in ("merged", "integrate_skipped", "integrate_errored",
                "merged_refs", "refire"):
        assert key in detail, (key, detail)


def test_tick_end_kind_still_in_event_vocabulary():
    """The new detail keys are NOT a new event kind — tick_end stays in the closed
    EVENT_KINDS vocabulary."""
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    restore = _patch_vi_seams()
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    finally:
        restore()
    for e in _events(cfg):
        assert e["kind"] in ob.EVENT_KINDS, e["kind"]


# ==========================================================================
# Behaviour D — a merge-sink error is surfaced (integrate_errored>0 + trace).
# ==========================================================================

def test_merge_error_surfaces_integrate_errored_in_detail_and_trace():
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    restore = _patch_vi_seams(raise_merge=True)
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
        trace = buf.getvalue()
    finally:
        restore()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("merged") == 0, detail
    assert detail.get("integrate_errored") == 1, detail
    assert detail.get("merged_refs") == [], detail
    # The trace appends integrate_errored=<n> when >0.
    assert "integrate_errored=1" in trace, trace


def test_no_error_tick_omits_integrate_errored_token_from_trace():
    """integrate_errored=<n> is appended to the trace ONLY when >0; a clean merge
    tick shows merged=1 but no integrate_errored token."""
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
    assert "merged=1" in trace, trace
    assert "integrate_errored=" not in trace, trace


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
    print(f"\n{len(fns) - failures}/{len(fns)} passed")
    sys.exit(1 if failures else 0)
