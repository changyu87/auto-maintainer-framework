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
import fsm_contracts as fc  # noqa: E402
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

    def source(repo=None, issue_filter=None):
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


def _patch_vi_seams(open_prs=None, merge_calls=None, raise_merge=False,
                    auto_enabled=False):
    """Override verify-integrate's gh seams so VERIFY/INTEGRATE touch no network.
    Returns a restore callable. The merge sink mirrors the production
    `gh_pr_merge_sink(pr_ref, repo=None, base_branch=None)` signature and returns an
    `auto_enabled` flag: when True the entry is recorded under
    integration_result.auto_merge_enabled (GitHub native auto-merge queued — a
    PENDING success), else under merged (merged now)."""
    saved = {
        "open": vi.gh_open_pr_source,
        "branch": vi.gh_default_branch_source,
        "merge": vi.gh_pr_merge_sink,
    }

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return list(open_prs if open_prs is not None else [_OK_PR])

    def _branch(repo=None):
        return _DEFAULT_BRANCH

    def _merge(pr_ref, repo=None, base_branch=None):
        if merge_calls is not None:
            merge_calls.append(pr_ref)
        if raise_merge:
            raise RuntimeError("merge sink failed for " + pr_ref)
        return {"pr_ref": pr_ref, "url": vi._pr_url(pr_ref, repo),
                "auto_enabled": auto_enabled}

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


# ==========================================================================
# Behaviour E — auto_merge_enabled (a PENDING success) is surfaced in the
# tick_end detail (always) + the one-line trace (only when >0), distinct from
# integrate_errored.
# ==========================================================================

def test_auto_merge_enabled_tick_surfaces_count_in_detail_and_trace():
    """When INTEGRATE enables GitHub native auto-merge on a PR (the merge sink
    returns auto_enabled=True), the integration_result.auto_merge_enabled list
    carries it. tick_end.detail.auto_merge_enabled == that count, and the
    one-line trace shows auto_merge_enabled=<n>. The PR is a PENDING success — it
    is NOT counted under merged (merged=0 / merged_refs=[]) and NOT an error."""
    project_dir, cfg, state_path, journal_path = _setup_project("auto-merge")
    restore = _patch_vi_seams(auto_enabled=True)
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
    assert detail.get("auto_merge_enabled") == 1, detail
    # Pending, not merged and not an error.
    assert detail.get("merged") == 0, detail
    assert detail.get("merged_refs") == [], detail
    assert detail.get("integrate_errored") == 0, detail
    # The trace shows the token when >0.
    assert "auto_merge_enabled=1" in trace, trace


def test_no_auto_merge_enabled_shows_zero_and_omits_trace_token():
    """A route with no INTEGRATE (or an integration_result lacking the key) yields
    auto_merge_enabled=0 via the .get default, and the trace omits the token
    (mirroring the integrate_errored=<n>-only-when-positive convention)."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "propose", route=rt.DEFAULT_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    trace = buf.getvalue()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("auto_merge_enabled") == 0, detail
    assert "auto_merge_enabled=" not in trace, trace


# ==========================================================================
# Behaviour F — RECONCILE's `deduped` count (same-issue duplicate loop PRs
# RECONCILE closed this tick) is surfaced in the tick_end detail (always) + the
# one-line trace (only when >0), mirroring auto_merge_enabled. verify-integrate's
# ReconcileResult.deduped is consumed UNCHANGED.
# ==========================================================================

# A RECONCILE route (pure-script): RECONCILE runs BEFORE PULL.
_RECONCILE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "RECONCILE", "PULL", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "RECONCILE"},
        {"state": "RECONCILE", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "PERSIST"},
        {"state": "PULL", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


class _DedupReconcile:
    """A fake vi.Reconcile whose run writes a reconcile_result carrying a
    non-empty `deduped` list (one duplicate same-issue loop PR closed). Replaces
    vi.Reconcile so make_reconcile constructs it at factory-call time — no
    network, no git, no dependence on the def-time dedup source default."""

    _DEDUPED = [{"pr_ref": "acme/widget#43", "issue_ref": "acme/widget#7",
                 "kept_pr_ref": "acme/widget#44"}]

    def __init__(self, *a, **k):
        pass

    def run(self, ctx):
        return fc.StateResult(signal="OK", writes={"reconcile_result": {
            "schema_version": vi.RECONCILE_RESULT_SCHEMA_VERSION,
            "closed_issues": [], "rebased": [], "relanded": [],
            "deduped": [dict(e) for e in self._DEDUPED],
            "skipped": [], "errors": [],
        }})


def _patch_reconcile_dedup():
    saved = {"R": vi.Reconcile, "branch": vi.gh_default_branch_source}
    vi.Reconcile = _DedupReconcile
    vi.gh_default_branch_source = lambda repo=None: "main"

    def restore():
        vi.Reconcile = saved["R"]
        vi.gh_default_branch_source = saved["branch"]
    return restore


def test_reconcile_deduped_surfaced_in_tick_end_and_trace():
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)
    restore = _patch_reconcile_dedup()
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
    assert signal == "idle", signal
    detail = _tick_end(cfg)["detail"]
    assert detail.get("deduped") == 1, detail
    # The trace shows the deduped token when >0.
    assert "deduped=1" in trace, trace


def test_route_without_reconcile_shows_deduped_zero_no_trace_token():
    """A route with no RECONCILE state shows deduped=0 in the tick_end detail and
    omits the trace token (mirroring integrate_errored/auto_merge_enabled)."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "propose", route=rt.DEFAULT_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    trace = buf.getvalue()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("deduped") == 0, detail
    assert "deduped=" not in trace, trace


def test_reconcile_empty_dedup_shows_deduped_zero_no_trace_token():
    """A RECONCILE route that dedups NOTHING shows deduped=0 and omits the token
    (the count is len(reconcile_result.deduped), 0 for an empty list)."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)

    class _NoDedup(_DedupReconcile):
        _DEDUPED = []

    saved = {"R": vi.Reconcile, "branch": vi.gh_default_branch_source}
    vi.Reconcile = _NoDedup
    vi.gh_default_branch_source = lambda repo=None: "main"
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
        trace = buf.getvalue()
    finally:
        vi.Reconcile = saved["R"]
        vi.gh_default_branch_source = saved["branch"]
    detail = _tick_end(cfg)["detail"]
    assert detail.get("deduped") == 0, detail
    assert "deduped=" not in trace, trace


# ==========================================================================
# Behaviour G — RECONCILE's `auto_merged` count + refs (acted_ledger PRs
# RECONCILE detected MERGED this tick — an auto-merge GitHub completed
# asynchronously BETWEEN ticks) are surfaced in the tick_end detail
# (auto_merged=<n> always + auto_merged_refs) and the one-line trace
# (auto_merged=<n> only when >0), mirroring deduped. verify-integrate's
# ReconcileResult.auto_merged is consumed UNCHANGED.
# ==========================================================================

_AUTO_MERGED = [{"pr_ref": "acme/widget#42", "issue_ref": "acme/widget#7"}]


class _AutoMergedReconcile:
    """A fake vi.Reconcile whose run writes a reconcile_result carrying a
    non-empty `auto_merged` list (one acted_ledger PR detected merged this tick).
    No network, no git, no dependence on the def-time seams."""

    _AM = _AUTO_MERGED

    def __init__(self, *a, **k):
        pass

    def run(self, ctx):
        return fc.StateResult(signal="OK", writes={"reconcile_result": {
            "schema_version": vi.RECONCILE_RESULT_SCHEMA_VERSION,
            "closed_issues": [], "rebased": [], "relanded": [], "deduped": [],
            "auto_merged": [dict(e) for e in self._AM],
            "skipped": [], "errors": [],
        }})


def _patch_reconcile_auto_merged(cls):
    saved = {"R": vi.Reconcile, "branch": vi.gh_default_branch_source}
    vi.Reconcile = cls
    vi.gh_default_branch_source = lambda repo=None: "main"

    def restore():
        vi.Reconcile = saved["R"]
        vi.gh_default_branch_source = saved["branch"]
    return restore


def test_reconcile_auto_merged_surfaced_in_tick_end_and_trace():
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)
    restore = _patch_reconcile_auto_merged(_AutoMergedReconcile)
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
    assert signal == "idle", signal
    detail = _tick_end(cfg)["detail"]
    assert detail.get("auto_merged") == 1, detail
    assert detail.get("auto_merged_refs") == ["acme/widget#42"], detail
    # The trace shows the auto_merged token when >0.
    assert "auto_merged=1" in trace, trace


def test_route_without_reconcile_shows_auto_merged_zero_no_trace_token():
    """A route with no RECONCILE state shows auto_merged=0 + auto_merged_refs=[]
    in the tick_end detail and omits the trace token (mirroring deduped)."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "propose", route=rt.DEFAULT_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    trace = buf.getvalue()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("auto_merged") == 0, detail
    assert detail.get("auto_merged_refs") == [], detail
    assert "auto_merged=" not in trace, trace


def test_reconcile_empty_auto_merged_shows_zero_no_trace_token():
    """A RECONCILE route with an empty auto_merged shows auto_merged=0 +
    auto_merged_refs=[] and omits the trace token."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)

    class _NoAutoMerged(_AutoMergedReconcile):
        _AM = []

    restore = _patch_reconcile_auto_merged(_NoAutoMerged)
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
    assert detail.get("auto_merged") == 0, detail
    assert detail.get("auto_merged_refs") == [], detail
    assert "auto_merged=" not in trace, trace


# ==========================================================================
# Behaviour H — RECONCILE's RECOVERY outcome (rebased / relanded /
# reconcile_errors counts + rebased_refs/relanded_refs) is surfaced in the
# tick_end detail (always) + the one-line trace (each token only when >0),
# mirroring deduped/auto_merged. This closes the observability gap where a
# RECONCILE that silently errored on every conflicting PR (the fixed-worktree
# wedge) was invisible in the trace. verify-integrate's ReconcileResult
# rebased/relanded/errors fields are consumed UNCHANGED.
# ==========================================================================

_REBASED = [{"pr_ref": "acme/widget#43", "issue_ref": "acme/widget#7"}]
_RELANDED = [{"pr_ref": "acme/widget#44", "issue_ref": "acme/widget#8"}]
_ERRORS = [{"ref": "acme/widget#45", "reason": "worktree wedged"}]


class _RecoveryReconcile:
    """A fake vi.Reconcile whose run writes a reconcile_result carrying non-empty
    `rebased`, `relanded`, and `errors` lists (one PR each). No network, no git,
    no dependence on the def-time seams."""

    _RB = _REBASED
    _RL = _RELANDED
    _ER = _ERRORS

    def __init__(self, *a, **k):
        pass

    def run(self, ctx):
        return fc.StateResult(signal="OK", writes={"reconcile_result": {
            "schema_version": vi.RECONCILE_RESULT_SCHEMA_VERSION,
            "closed_issues": [], "deduped": [], "auto_merged": [],
            "rebased": [dict(e) for e in self._RB],
            "relanded": [dict(e) for e in self._RL],
            "skipped": [], "errors": [dict(e) for e in self._ER],
        }})


def _patch_reconcile_recovery(cls):
    saved = {"R": vi.Reconcile, "branch": vi.gh_default_branch_source}
    vi.Reconcile = cls
    vi.gh_default_branch_source = lambda repo=None: "main"

    def restore():
        vi.Reconcile = saved["R"]
        vi.gh_default_branch_source = saved["branch"]
    return restore


def test_reconcile_recovery_surfaced_in_tick_end_and_trace():
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)
    restore = _patch_reconcile_recovery(_RecoveryReconcile)
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
    assert signal == "idle", signal
    detail = _tick_end(cfg)["detail"]
    assert detail.get("rebased") == 1, detail
    assert detail.get("rebased_refs") == ["acme/widget#43"], detail
    assert detail.get("relanded") == 1, detail
    assert detail.get("relanded_refs") == ["acme/widget#44"], detail
    assert detail.get("reconcile_errors") == 1, detail
    # Each token appears in the trace when >0.
    assert "rebased=1" in trace, trace
    assert "relanded=1" in trace, trace
    assert "reconcile_errors=1" in trace, trace


def test_route_without_reconcile_shows_recovery_zero_no_trace_tokens():
    """A route with no RECONCILE state shows rebased=0/relanded=0/
    reconcile_errors=0 + empty ref lists in the tick_end detail and omits the
    trace tokens (mirroring deduped/auto_merged)."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "propose", route=rt.DEFAULT_ROUTE)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rt.run_tick(project_dir=project_dir, runtime_dir=cfg,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    trace = buf.getvalue()
    detail = _tick_end(cfg)["detail"]
    assert detail.get("rebased") == 0, detail
    assert detail.get("rebased_refs") == [], detail
    assert detail.get("relanded") == 0, detail
    assert detail.get("relanded_refs") == [], detail
    assert detail.get("reconcile_errors") == 0, detail
    assert "rebased=" not in trace, trace
    assert "relanded=" not in trace, trace
    assert "reconcile_errors=" not in trace, trace


def test_reconcile_empty_recovery_shows_zero_no_trace_tokens():
    """A RECONCILE route whose result carries empty rebased/relanded/errors
    shows all counts 0 + empty ref lists and omits the trace tokens."""
    project_dir, cfg, state_path, journal_path = _setup_project(
        "auto-merge", route=_RECONCILE_ROUTE)

    class _NoRecovery(_RecoveryReconcile):
        _RB = []
        _RL = []
        _ER = []

    restore = _patch_reconcile_recovery(_NoRecovery)
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
    assert detail.get("rebased") == 0, detail
    assert detail.get("rebased_refs") == [], detail
    assert detail.get("relanded") == 0, detail
    assert detail.get("relanded_refs") == [], detail
    assert detail.get("reconcile_errors") == 0, detail
    assert "rebased=" not in trace, trace
    assert "relanded=" not in trace, trace
    assert "reconcile_errors=" not in trace, trace


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
