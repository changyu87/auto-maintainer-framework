#!/usr/bin/env python3
"""End-to-end conformance tests for the OUTBOUND REPORT flush in run_tick: the
tick's discoveries (handoffs[].discovered_work + an optional ctx `discoveries`
slot) are normalized to DiscoveredIssue and FILED through work-intake's REPORT
port at the tick TERMINAL (out-of-band — NOT a routed state), trust-gated by
sg.permits('file', mode), with a durable REPORT_LEDGER_KEY for idempotency.

REPORT is out-of-band: the flush runs at the `done` path AFTER the route completed
and the read products are known, on BOTH the pure-script and agent-driver done
paths. It consumes work_intake (DiscoveredIssue / file_discoveries /
gh_issue_file_sink) and safety_governance UNCHANGED.

Behaviours under test (mirroring test_doer_ledger_budget_e2e style):
  - propose mode + a handoff carrying discovered_work -> files via a STUB sink,
    report_ledger records the dedup_key, reported=1/0 in the trace.
  - a SECOND tick re-surfacing the SAME discovery -> SKIPPED (ledger), the sink
    is NOT called, reported=0/1.
  - dry-run mode -> the sink is NOT called, the ledger is UNTOUCHED, and the
    intent is logged (reported=0/<would-file>).
  - a no-discovery tick -> reported=0/0, otherwise byte-identical.
  - the derived dedup_key is stable across ticks for the same title+body.

scheduling CONSUMES work-intake + safety-governance + the loop-core features
UNCHANGED via sys.path; edits live ONLY in scheduling (run_tick.py / status.py).

Owner: changyu87
"""

import io
import contextlib
import json
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import safety_governance as sg  # noqa: E402,F401
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)


GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}, {"name": "p1"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  },
  {
    "number": 9,
    "title": "Add retry knob",
    "body": "",
    "url": "https://github.com/acme/widget/issues/9",
    "state": "OPEN",
    "labels": [],
    "author": {"login": "hubber"},
    "createdAt": "2026-05-03T08:00:00Z",
    "updatedAt": "2026-05-03T08:00:00Z"
  }
]"""


def _stub_source(json_text=GH_JSON_FIXTURE):
    items = wi.parse_gh_issues(json_text)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


class _RecordingSink:
    """A stub REPORT sink: records every (discovery, repo) it is called with and
    returns a synthetic {tracker_ref, url}. No network."""

    def __init__(self):
        self.calls = []

    def __call__(self, discovery, repo=None):
        self.calls.append((discovery, repo))
        n = len(self.calls)
        return {"tracker_ref": f"acme/widget#{100 + n}",
                "url": f"https://github.com/acme/widget/issues/{100 + n}"}


class _RaisingSink:
    """A stub REPORT sink that RAISES for every discovery (reproduces the live
    silent-failure class — e.g. a missing tracker label). file_discoveries
    catches the exception into ReportResult.errors so the batch never aborts."""

    def __init__(self, reason="missing tracker label: needs-triage"):
        self.calls = []
        self.reason = reason

    def __call__(self, discovery, repo=None):
        self.calls.append((discovery, repo))
        raise RuntimeError(self.reason)


# --------------------------------------------------------------------------
# Agent-adapter fixtures (mirror test_doer_ledger_budget_e2e.py): an ACTING
# IMPLEMENT agent whose handoff carries discovered_work, plus a non-acting TRIAGE
# agent so the route reaches IMPLEMENT.
# --------------------------------------------------------------------------

_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "triage-doer",
            "inputs": ["work_items"],
            "writes": "work_orders",
            "cardinality": "once",
            "task": "Triage the work_items into accepted work_orders.",
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}

_PLANNED_HANDOFF_EXAMPLE = {
    "work_order_id": None,
    "status": "planned",
    "artifact": {"kind": "none", "ref": None},
    "discovered_work": [],
    "blocked_reason": None,
}

_IMPLEMENT_ACTING_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["execution_plan"], "writes": ["handoffs"],
                 "emits": ["OK", "BLOCKED"]},
    "dispatch": [
        {
            "subagent_type": "implement-doer",
            "inputs": ["execution_plan"],
            "writes": "handoffs",
            "cardinality": {"per_item": "execution_plan.ordered"},
            "task": "Implement one work_order.",
            "effect": "implement",
            "description": "implement a work order in an isolated worktree",
            "output_example": _PLANNED_HANDOFF_EXAMPLE,
        }
    ],
    "signal": {"rule": "blocked_if_any"},
}


_AGENT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "PERSIST", "EXIT", "DONE", "HALTED"],
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
        {"state": "IMPLEMENT", "signal": "OK", "next": "PERSIST"},
        {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def _agent_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    amap["IMPLEMENT"] = dict(_IMPLEMENT_ACTING_AGENT)
    return amap


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route, f)


def _write_project_map(project_dir, amap):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)


def _write_governance(project_dir, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump(payload, f)


def _setup_agent_project(mode="propose", budget=None):
    project_dir = tempfile.mkdtemp(prefix="sched-report-")
    _write_project_route(project_dir, _AGENT_ROUTE)
    _write_project_map(project_dir, _agent_map())
    gov = {"mode": mode}
    if budget is not None:
        gov["budget"] = budget
    _write_governance(project_dir, gov)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


_CANNED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])

_DISCOVERY = {"title": "Found a flaky test", "body": "test_foo is flaky",
              "kind": "task", "severity": "low"}


def _canned_handoff_with_discovery(work_order_id, discovered_work):
    """A canned REAL handoff a dispatched implement-doer would write that ALSO
    surfaces follow-on discovered_work the REPORT flush must file."""
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": "opened", "artifact": {"kind": "pr", "ref": "PR#1"},
        "discovered_work": discovered_work, "blocked_reason": None,
    })


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _setup_pure_project(mode="propose"):
    """A project dir running the PURE-SCRIPT default route (no agent states), so a
    discovery injected via the ctx `discoveries` param flows straight to the
    terminal REPORT flush. Returns (project_dir, runtime_dir, state_path,
    journal_path)."""
    project_dir = tempfile.mkdtemp(prefix="sched-report-pure-")
    _write_governance(project_dir, {"mode": mode})
    root = tempfile.mkdtemp(prefix="sched-report-pure-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


def _drive_pure_with_ctx_discovery(project_dir, runtime_dir, state_path,
                                   journal_path, sink, discoveries, now=_DAY1):
    """Run the PURE-SCRIPT default route, injecting `discoveries` via the optional
    ctx discoveries param, and capture the done trace string."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=now, report_sink=sink,
                             discoveries=discoveries)
    assert signal == "idle", signal
    return buf.getvalue()


def _drive_to_done_with_discovery(project_dir, runtime_dir, state_path,
                                  journal_path, sink, discovered_work,
                                  now=_DAY1):
    """Step the agent route TRIAGE -> IMPLEMENT, writing a handoff carrying
    `discovered_work`, resume to DONE with the injected REPORT `sink`. Returns the
    captured trace string of the final (done) resume."""
    # TRIAGE pause + resume.
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(), now=now)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    paused2 = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                          state_path=state_path, journal_path=journal_path,
                          source=_stub_source(), now=now, resume=True)
    assert paused2["status"] == "paused" and paused2["state"] == "IMPLEMENT", \
        paused2
    _write_outputs(paused2, [_canned_handoff_with_discovery(
        d["item"], discovered_work) for d in paused2["dispatches"]])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=now, resume=True,
                             report_sink=sink)
    # POOL-based refire (§3.3.3): the canned TRIAGE accepted only #7 (opened ->
    # recorded `done`), but the PULL pool also holds the open, classify-valid #9
    # which was never made a work_order — it stays pool-workable, and the propose
    # route can act, so EXIT refires. (REPORT still flushes at the terminal first.)
    assert signal == "refire", signal
    return buf.getvalue()


# ==========================================================================
# Behaviour 0 — the REPORT ledger key + helper + sink seam exist.
# ==========================================================================

def test_report_ledger_key_and_helper_and_sink_exist():
    assert rt.REPORT_LEDGER_KEY == "report_ledger"
    assert rt.DEFAULT_REPORT_SINK is wi.gh_issue_file_sink
    root = tempfile.mkdtemp(prefix="sched-report-empty-")
    state_path = os.path.join(root, "state.json")
    assert rt.persisted_report_ledger(state_path) == {}


# ==========================================================================
# Behaviour 1 — propose mode, a handoff carries discovered_work -> the flush
# FILES it via the stub sink, the ledger records the dedup_key, reported=1/0.
# ==========================================================================

def test_propose_files_discovery_records_ledger_reported_1_0():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    sink = _RecordingSink()
    trace = _drive_to_done_with_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink,
        [dict(_DISCOVERY)])
    # The sink was called exactly once (one discovery filed).
    assert len(sink.calls) == 1, sink.calls
    filed_discovery, repo = sink.calls[0]
    assert isinstance(filed_discovery, wi.DiscoveredIssue), filed_discovery
    assert filed_discovery.filed_by == "autonomous-maintainer", filed_discovery
    assert filed_discovery.target == "project", filed_discovery
    # project target -> the gh default repo (None).
    assert repo is None, repo
    # The ledger records the filed dedup_key with its tracker_ref + url.
    ledger = rt.persisted_report_ledger(state_path)
    assert len(ledger) == 1, ledger
    (dedup_key, rec), = ledger.items()
    assert rec["tracker_ref"].startswith("acme/widget#"), rec
    assert rec["url"].startswith("https://"), rec
    # The trace surfaces reported=1/0 with NO report_errors token (errored==0).
    assert "reported=1/0" in trace, trace
    assert "report_errors" not in trace, trace


# ==========================================================================
# Behaviour 2 — a SECOND tick re-surfacing the SAME discovery -> SKIPPED (the
# ledger already has the dedup_key); the sink is NOT called; reported=0/1.
# ==========================================================================

def test_second_tick_same_discovery_skipped_reported_0_1():
    project_dir, runtime_dir, state_path, journal_path = _setup_pure_project(
        mode="propose")
    sink1 = _RecordingSink()
    trace1 = _drive_pure_with_ctx_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink1,
        [dict(_DISCOVERY)])
    assert len(sink1.calls) == 1, sink1.calls
    assert "reported=1/0" in trace1, trace1
    ledger_after_t1 = rt.persisted_report_ledger(state_path)

    # Tick 2: the SAME discovery re-surfaces. It is already in the ledger -> the
    # flush SKIPS it and does NOT call the sink.
    sink2 = _RecordingSink()
    trace = _drive_pure_with_ctx_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink2,
        [dict(_DISCOVERY)])
    assert sink2.calls == [], sink2.calls
    assert "reported=0/1" in trace, trace
    # The ledger is unchanged (idempotent).
    assert rt.persisted_report_ledger(state_path) == ledger_after_t1


# ==========================================================================
# Behaviour 3 — dry-run mode -> filing is NOT permitted: the sink is NOT called,
# the ledger is UNTOUCHED, and the intent is logged (reported=0/<would-file>).
# ==========================================================================

def test_dry_run_does_not_file_leaves_ledger_untouched():
    project_dir, runtime_dir, state_path, journal_path = _setup_pure_project(
        mode="dry-run")
    sink = _RecordingSink()
    # The flush is gated by permits('file', 'dry-run') == False; drive a discovery
    # through the optional ctx `discoveries` slot — it must NOT be filed.
    trace = _drive_pure_with_ctx_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink,
        [dict(_DISCOVERY)])
    # Filing was not permitted -> the sink was never called.
    assert sink.calls == [], sink.calls
    # The ledger is UNTOUCHED so a later armed tick can file it.
    assert rt.persisted_report_ledger(state_path) == {}
    # The would-file count is logged: reported=0/1.
    assert "reported=0/1" in trace, trace


# ==========================================================================
# Behaviour 4 — a no-discovery tick -> reported=0/0, otherwise byte-identical to
# the pre-REPORT pure-script trace; the sink is NEVER called.
# ==========================================================================

def test_no_discovery_tick_reported_0_0():
    project_dir = tempfile.mkdtemp(prefix="sched-report-nd-")
    _write_governance(project_dir, {"mode": "propose"})
    root = tempfile.mkdtemp(prefix="sched-report-nd-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    sink = _RecordingSink()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        signal = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1, report_sink=sink)
    assert signal == "idle", signal
    line = buf.getvalue()
    # The default pure-script path is unchanged except the reported token.
    assert "[tick] path=GUARD->DRAIN->PULL->PERSIST->EXIT->DONE" in line, line
    assert "reported=0/0" in line, line
    # errored==0 -> NO report_errors token in the trace (default, no error).
    assert "report_errors" not in line, line
    # The sink was never called (no discoveries).
    assert sink.calls == [], sink.calls
    # No report ledger key was written (nothing filed).
    doc = ds.DurableState(state_path).load()
    assert rt.REPORT_LEDGER_KEY not in doc, doc
    # The tick_end event detail carries reported_errored defaulting to 0.
    end = next(e for e in _read_events(runtime_dir) if e["kind"] == "tick_end")
    assert end["detail"]["reported_errored"] == 0, end


# ==========================================================================
# Behaviour 5 — the derived dedup_key is STABLE: the same title+body yields the
# same key across two separate normalizations (so a refire never double-files).
# ==========================================================================

def test_derived_dedup_key_is_stable():
    raw = {"title": "Found a flaky test", "body": "test_foo is flaky"}
    d1 = rt._normalize_discovery(dict(raw))
    d2 = rt._normalize_discovery(dict(raw))
    assert d1.dedup_key == d2.dedup_key, (d1.dedup_key, d2.dedup_key)
    assert d1.dedup_key, d1.dedup_key
    # A different body yields a DIFFERENT key.
    d3 = rt._normalize_discovery({"title": "Found a flaky test",
                                  "body": "something else"})
    assert d3.dedup_key != d1.dedup_key, d3.dedup_key
    # A work_order_id prefixes the key (still stable).
    d4 = rt._normalize_discovery(dict(raw), fallback_wo_id="wo-1")
    d5 = rt._normalize_discovery(dict(raw), fallback_wo_id="wo-1")
    assert d4.dedup_key == d5.dedup_key, (d4.dedup_key, d5.dedup_key)
    assert d4.dedup_key != d1.dedup_key, (d4.dedup_key, d1.dedup_key)


# ==========================================================================
# Behaviour 6 — recording the report ledger preserves all OTHER durable keys.
# ==========================================================================

def test_report_ledger_record_preserves_other_durable_keys():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    sink = _RecordingSink()
    _drive_to_done_with_discovery(project_dir, runtime_dir, state_path,
                                  journal_path, sink, [dict(_DISCOVERY)])
    doc = ds.DurableState(state_path).load()
    assert rt.REPORT_LEDGER_KEY in doc
    # The durable cross-tick facts + read products survive alongside the ledger.
    assert rt.BUDGET_KEY in doc
    assert rt.ACTED_LEDGER_KEY in doc
    assert "counter" in doc
    assert rt.HANDOFFS_KEY in doc


# ==========================================================================
# Behaviour 7 — a maintainer-self target resolves its repo to the FIXED
# safety_governance.MAINTAINER_REPO constant (§3.11.6), NEVER the project repo,
# with NO fallback. The former governance.maintainer_repo config field is gone:
# _repo_for_target ignores config entirely for maintainer-self and always routes
# to the upstream maintainer repo.
# ==========================================================================

def test_maintainer_self_target_routes_to_fixed_maintainer_repo():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project(
        mode="propose")
    sink = _RecordingSink()
    disc = dict(_DISCOVERY)
    disc["target"] = "maintainer-self"
    _drive_to_done_with_discovery(project_dir, runtime_dir, state_path,
                                  journal_path, sink, [disc])
    assert len(sink.calls) == 1, sink.calls
    filed, repo = sink.calls[0]
    assert filed.target == "maintainer-self", filed
    # Fixed MAINTAINER_REPO, never the project repo, no fallback.
    assert repo == sg.MAINTAINER_REPO, (repo, sg.MAINTAINER_REPO)


def test_repo_for_target_maintainer_self_ignores_config_no_fallback():
    """_repo_for_target(maintainer-self) is the FIXED constant regardless of the
    governance dict (a stale maintainer_repo config field is ignored — gone)."""
    # An empty gov, a gov with a (now-removed) maintainer_repo, and a real loaded
    # config all route maintainer-self to the SAME fixed MAINTAINER_REPO.
    assert rt._repo_for_target("maintainer-self", {}) == sg.MAINTAINER_REPO
    assert rt._repo_for_target(
        "maintainer-self", {"maintainer_repo": "someone/else"}
    ) == sg.MAINTAINER_REPO
    # A `project` target is still the gh default (None).
    assert rt._repo_for_target("project", {}) is None


def _read_events(runtime_dir):
    path = os.path.join(runtime_dir, "events.jsonl")
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


# ==========================================================================
# Behaviour 8 — REPORT filing ERRORS are surfaced, never silent. A sink that
# RAISES for a discovery -> file_discoveries records it in errors ->
# _flush_report returns errored=1 -> the trace shows reported=0/0 report_errors=1
# and the tick_end event detail carries reported_errored=1. This is the fix for a
# real silent failure: a filing error (missing tracker label) was caught but the
# surface only showed reported=0/0, looking like "no discoveries".
# ==========================================================================

def test_filing_error_is_surfaced_report_errors_and_tick_end_detail():
    project_dir, runtime_dir, state_path, journal_path = _setup_pure_project(
        mode="propose")
    sink = _RaisingSink()
    trace = _drive_pure_with_ctx_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink,
        [dict(_DISCOVERY)])
    # The sink WAS called (filing was attempted) but raised.
    assert len(sink.calls) == 1, sink.calls
    # Nothing filed, nothing skipped -> reported=0/0, but the error is visible.
    assert "reported=0/0" in trace, trace
    assert "report_errors=1" in trace, trace
    # The error did NOT enter the ledger (it never filed) so a later armed tick
    # retries it.
    assert rt.persisted_report_ledger(state_path) == {}
    # The tick_end event detail carries reported_errored=1.
    end = next(e for e in _read_events(runtime_dir) if e["kind"] == "tick_end")
    assert end["detail"]["reported_errored"] == 1, end


# ==========================================================================
# Behaviour 9 — _flush_report returns the (filed, skipped, errored) triple. The
# errored count is len(ReportResult.errors).
# ==========================================================================

def test_flush_report_returns_errored_triple():
    root = tempfile.mkdtemp(prefix="sched-report-triple-")
    state_path = os.path.join(root, "state.json")
    handoff = {"work_order_id": "wo-1", "status": "opened",
               "artifact": {"kind": "pr", "ref": "PR#1"},
               "discovered_work": [dict(_DISCOVERY)], "blocked_reason": None}
    gov = sg.load_governance(tempfile.mkdtemp(prefix="sched-report-triple-pd-"))
    filed, skipped, errored = rt._flush_report(
        state_path, [handoff], [], "propose", gov, _RaisingSink())
    assert (filed, skipped, errored) == (0, 0, 1), (filed, skipped, errored)


# ==========================================================================
# Behaviour 10 — dedup-vs-open (#224): a discovery whose subject DUPLICATES an
# already-OPEN work_item is NOT filed (the sink is never called); it folds into
# the skipped count so reported reflects it. A genuinely-new discovery is filed.
# This is the REPORT-side of DESIGN §3.5.4 "dedup vs open": the flush now passes
# the tick's PULLed open work_items to work-intake as known_open.
# ==========================================================================

def _open_work_item(number, title):
    """A minimal open work_item dict (the machine-first shape persisted under
    WORK_ITEMS_KEY that _flush_report passes as known_open)."""
    return {"schema_version": "1.1.0", "id": f"acme/widget#{number}",
            "number": number, "title": title, "body": "",
            "url": f"https://github.com/acme/widget/issues/{number}",
            "state": "OPEN", "labels": []}


def test_flush_report_skips_discovery_matching_open_work_item():
    root = tempfile.mkdtemp(prefix="sched-report-vsopen-")
    state_path = os.path.join(root, "state.json")
    gov = sg.load_governance(tempfile.mkdtemp(prefix="sched-report-vsopen-pd-"))
    # The discovery's subject matches an already-open issue -> must NOT be filed.
    dup = {"title": "Serialize same-feature work orders",
           "body": "they collide on shared metadata", "kind": "task",
           "severity": "low"}
    handoff = {"work_order_id": "wo-1", "status": "blocked",
               "artifact": {"kind": "none", "ref": None},
               "discovered_work": [dup], "blocked_reason": "x"}
    open_items = [_open_work_item(214, "Serialize the same-feature work-orders")]
    sink = _RecordingSink()
    filed, skipped, errored = rt._flush_report(
        state_path, [handoff], [], "propose", gov, sink,
        work_items=open_items)
    # NOT filed, the sink was never called; the skip folds into skipped count.
    assert sink.calls == [], sink.calls
    assert (filed, skipped, errored) == (0, 1, 0), (filed, skipped, errored)
    # Nothing entered the report ledger (it was never filed).
    assert rt.persisted_report_ledger(state_path) == {}


def test_flush_report_files_new_discovery_with_open_work_items_present():
    root = tempfile.mkdtemp(prefix="sched-report-vsopen-new-")
    state_path = os.path.join(root, "state.json")
    gov = sg.load_governance(
        tempfile.mkdtemp(prefix="sched-report-vsopen-new-pd-"))
    # A genuinely-new subject: no open issue is about it -> filed normally even
    # though unrelated open work_items are present.
    handoff = {"work_order_id": "wo-1", "status": "opened",
               "artifact": {"kind": "pr", "ref": "PR#1"},
               "discovered_work": [dict(_DISCOVERY)], "blocked_reason": None}
    open_items = [_open_work_item(214, "Serialize the same-feature work-orders")]
    sink = _RecordingSink()
    filed, skipped, errored = rt._flush_report(
        state_path, [handoff], [], "propose", gov, sink,
        work_items=open_items)
    assert len(sink.calls) == 1, sink.calls
    assert (filed, skipped, errored) == (1, 0, 0), (filed, skipped, errored)


def test_flush_report_dry_run_would_file_excludes_open_duplicates():
    root = tempfile.mkdtemp(prefix="sched-report-vsopen-dry-")
    state_path = os.path.join(root, "state.json")
    gov = sg.load_governance(
        tempfile.mkdtemp(prefix="sched-report-vsopen-dry-pd-"))
    dup = {"title": "Serialize same-feature work orders", "body": "x",
           "kind": "task", "severity": "low"}
    new = dict(_DISCOVERY)
    handoff = {"work_order_id": "wo-1", "status": "opened",
               "artifact": {"kind": "pr", "ref": "PR#1"},
               "discovered_work": [dup, new], "blocked_reason": None}
    open_items = [_open_work_item(214, "Serialize the same-feature work-orders")]
    sink = _RecordingSink()
    # dry-run: filing not permitted; the would-file count must exclude the open
    # duplicate (1 new would-file, not 2).
    filed, skipped, errored = rt._flush_report(
        state_path, [handoff], [], "dry-run", gov, sink,
        work_items=open_items)
    assert sink.calls == [], sink.calls
    assert (filed, skipped, errored) == (0, 1, 0), (filed, skipped, errored)


# ==========================================================================
# Behaviour 11 (FT-E) — REVIEW's advisory review_findings flush through the
# injected REPORT sink. REVIEW is a NON-acting agent-state writing
# review_findings (DiscoveredIssue-conforming records with a stable dedup_key).
# At the terminal the REPORT flush gathers them as an ADDITIONAL discoveries
# source and files them via the SAME journaled-idempotency + dedup-vs-open
# (known_open=work_items) path as handoffs[].discovered_work — NOT a parallel
# filing mechanism. dedup-vs-open is honored.
# ==========================================================================

import verify_integrate as vi  # noqa: E402
import adapter_map_config as amc  # noqa: E402


# A route with REVIEW as the agent gate between VERIFY and INTEGRATE, no
# IMPLEMENT/TRIAGE agent so the FIRST (and only) pause is REVIEW:
# GUARD->DRAIN->PULL->VERIFY->REVIEW->INTEGRATE->CLEANUP->PERSIST->EXIT.
_REVIEW_AGENT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "VERIFY", "REVIEW", "INTEGRATE",
               "CLEANUP", "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "VERIFY"},
        {"state": "PULL", "signal": "EMPTY", "next": "VERIFY"},
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

_OK_PR = {
    "number": 42,
    "url": "https://github.com/acme/widget/pull/42",
    "headRefName": "auto-maintainer/fix-42",
    "baseRefName": "main",
    "mergeable": "MERGEABLE",
    "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
}


def _patch_vi_seams():
    saved = {"open": vi.gh_open_pr_source,
             "branch": vi.gh_default_branch_source,
             "merge": vi.gh_pr_merge_sink}

    def _open(repo=None, label=vi.LOOP_PR_LABEL):
        return [_OK_PR]

    def _branch(repo=None):
        return "main"

    def _merge(pr_ref, repo=None, base_branch=None):
        return {"pr_ref": pr_ref, "url": "", "auto_enabled": False}

    vi.gh_open_pr_source = _open
    vi.gh_default_branch_source = _branch
    vi.gh_pr_merge_sink = _merge

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


def _write_review_agent_map(project_dir):
    entry = amc._build_agent_entry(
        "REVIEW", "auto-maintainer:auto-maintainer-reviewer")
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["REVIEW"] = entry
    _write_project_map(project_dir, amap)


def _review_finding(slug, title, body):
    """A DiscoveredIssue-conforming review_findings record (vi.ReviewFinding
    shape) with a stable dedup_key."""
    return vi.review_finding_record(
        "acme/widget#42", title, body, "bug", "low", slug)


def test_review_findings_flush_through_report_sink():
    project_dir = tempfile.mkdtemp(prefix="sched-revfind-")
    _write_project_route(project_dir, _REVIEW_AGENT_ROUTE)
    _write_review_agent_map(project_dir)
    _write_governance(project_dir, {"mode": "auto-merge"})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    sink = _RecordingSink()
    restore = _patch_vi_seams()
    try:
        paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
        assert paused["status"] == "paused", paused
        assert paused["state"] == "REVIEW", paused
        d = paused["dispatches"][0]
        assert d["writes"] == "review_findings", d
        findings = [_review_finding("over-built", "PR over-builds the fix",
                                    "adds unused config")]
        with open(d["output_path"], "w") as f:
            json.dump(findings, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sig = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                              state_path=state_path, journal_path=journal_path,
                              source=_stub_source(), now=_DAY1, resume=True,
                              report_sink=sink)
        trace = buf.getvalue()
    finally:
        restore()
    assert sig == "idle", sig
    # The review finding flowed through the SAME REPORT sink as handoff
    # discoveries (one finding filed).
    assert len(sink.calls) == 1, sink.calls
    filed, repo = sink.calls[0]
    assert isinstance(filed, wi.DiscoveredIssue), filed
    assert filed.title == "PR over-builds the fix", filed
    # The finding's own stable dedup_key (review:<pr>:<slug>) is preserved.
    assert filed.dedup_key == "review:acme/widget#42:over-built", filed
    assert "reported=1/0" in trace, trace
    # The report ledger recorded the finding's dedup_key (journaled idempotency).
    ledger = rt.persisted_report_ledger(state_path)
    assert "review:acme/widget#42:over-built" in ledger, ledger
    # The advisory finding did NOT block the merge (REVIEW is advisory).
    ir = ds.DurableState(state_path).load().get("integration_result", {})
    assert len(ir.get("merged", [])) == 1, ir


def test_review_finding_matching_open_work_item_is_skipped():
    """A review finding whose subject DUPLICATES an already-open PULLed issue is
    NOT filed (dedup-vs-open honored on the SAME known_open path as handoff
    discoveries)."""
    # The PULL fixture issue #7 title is "Crash on empty config"; a finding with
    # the same subject must fold into skipped, not file a duplicate.
    project_dir = tempfile.mkdtemp(prefix="sched-revfind-dup-")
    _write_project_route(project_dir, _REVIEW_AGENT_ROUTE)
    _write_review_agent_map(project_dir)
    _write_governance(project_dir, {"mode": "auto-merge"})
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    sink = _RecordingSink()
    restore = _patch_vi_seams()
    try:
        paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), now=_DAY1)
        d = paused["dispatches"][0]
        findings = [_review_finding("dup", "Crash on empty config",
                                    "same subject as the open issue")]
        with open(d["output_path"], "w") as f:
            json.dump(findings, f)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source(), now=_DAY1, resume=True,
                        report_sink=sink)
        trace = buf.getvalue()
    finally:
        restore()
    # The finding duplicated an open issue -> NOT filed; folds into skipped.
    assert sink.calls == [], sink.calls
    assert "reported=0/1" in trace, trace
    assert rt.persisted_report_ledger(state_path) == {}


# ==========================================================================
# Behaviour 12 — PULL-visibility labels: the flush resolves
# sg.issue_filter_apply_labels(gov) and threads it as file_discoveries'
# apply_labels, so a project-target loop-filed discovery carries the labels a
# later label-filtered PULL matches. safety-governance + work-intake consumed
# UNCHANGED — the edit is ONLY the resolve+thread in run_tick's flush.
# ==========================================================================

class _LabelRecordingSink:
    """A stub REPORT sink that also captures the apply_labels forwarded to it
    (the new signature work-intake's file_discoveries uses when apply_labels is
    set), so a test can assert the PULL-visibility labels reached a filing."""

    def __init__(self):
        self.calls = []

    def __call__(self, discovery, repo=None, apply_labels=None):
        self.calls.append((discovery, repo, apply_labels))
        n = len(self.calls)
        return {"tracker_ref": f"acme/widget#{100 + n}",
                "url": f"https://github.com/acme/widget/issues/{100 + n}"}


def _handoff_with(discovered_work, status="opened", ref="PR#1"):
    return {"work_order_id": "wo-1", "status": status,
            "artifact": {"kind": "pr", "ref": ref},
            "discovered_work": discovered_work, "blocked_reason": None}


def test_flush_threads_issue_filter_apply_labels_to_project_filing():
    root = tempfile.mkdtemp(prefix="sched-report-labels-")
    state_path = os.path.join(root, "state.json")
    gov = {"mode": "propose",
           "issue_filter": {"labels": [["dci-team marketplace"]]}}
    sink = _LabelRecordingSink()
    filed, skipped, errored = rt._flush_report(
        state_path, [_handoff_with([dict(_DISCOVERY)])], [], "propose", gov,
        sink)
    assert (filed, skipped, errored) == (1, 0, 0), (filed, skipped, errored)
    assert len(sink.calls) == 1, sink.calls
    _disc, repo, apply_labels = sink.calls[0]
    # project target -> gh default repo (None) AND the PULL-visibility labels.
    assert repo is None, repo
    assert apply_labels == ["dci-team marketplace"], apply_labels


def test_flush_no_issue_filter_apply_labels_empty_filing_unchanged():
    """With no issue_filter, sg.issue_filter_apply_labels(gov) is [] -> the flush
    invokes the sink EXACTLY as before (the old-style (discovery, repo=None)
    sink still works — filing is byte-unchanged)."""
    root = tempfile.mkdtemp(prefix="sched-report-nolabels-")
    state_path = os.path.join(root, "state.json")
    gov = {"mode": "propose"}
    sink = _RecordingSink()  # old-style: (discovery, repo=None), no apply_labels
    filed, skipped, errored = rt._flush_report(
        state_path, [_handoff_with([dict(_DISCOVERY)])], [], "propose", gov,
        sink)
    assert (filed, skipped, errored) == (1, 0, 0), (filed, skipped, errored)
    assert len(sink.calls) == 1, sink.calls


def test_flush_maintainer_self_filing_gets_no_apply_labels():
    """Even with issue_filter labels configured, a maintainer-self filing gets
    apply_labels=[] (that per-target gate lives in work-intake's file_discoveries;
    the flush just supplies the project labels)."""
    root = tempfile.mkdtemp(prefix="sched-report-labels-ms-")
    state_path = os.path.join(root, "state.json")
    gov = {"mode": "propose",
           "issue_filter": {"labels": [["dci-team marketplace"]]}}
    disc = dict(_DISCOVERY)
    disc["target"] = "maintainer-self"
    sink = _LabelRecordingSink()
    filed, skipped, errored = rt._flush_report(
        state_path, [_handoff_with([disc])], [], "propose", gov, sink)
    assert (filed, skipped, errored) == (1, 0, 0), (filed, skipped, errored)
    _disc, repo, apply_labels = sink.calls[0]
    assert repo == sg.MAINTAINER_REPO, (repo, sg.MAINTAINER_REPO)
    assert apply_labels == [], apply_labels


def test_run_tick_e2e_threads_apply_labels_to_project_filing():
    """End-to-end through run_tick's pure-script route: a project-local config.json
    carrying issue_filter labels -> the terminal REPORT flush stamps them on the
    project-target filing (loop can re-pull work it filed for itself)."""
    project_dir = tempfile.mkdtemp(prefix="sched-report-labels-e2e-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "config.json"), "w") as f:
        json.dump({"mode": "propose",
                   "issue_filter": {"labels": [["dci-team marketplace"]]}}, f)
    root = tempfile.mkdtemp(prefix="sched-report-labels-e2e-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    sink = _LabelRecordingSink()
    trace = _drive_pure_with_ctx_discovery(
        project_dir, runtime_dir, state_path, journal_path, sink,
        [dict(_DISCOVERY)])
    assert "reported=1/0" in trace, trace
    assert len(sink.calls) == 1, sink.calls
    _disc, repo, apply_labels = sink.calls[0]
    assert repo is None, repo
    assert apply_labels == ["dci-team marketplace"], apply_labels


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
        sys.exit(1)
    print(f"\nall {len(fns)} passed")
