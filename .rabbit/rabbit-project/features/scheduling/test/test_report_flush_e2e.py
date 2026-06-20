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
             "observability"):
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

    def source(repo=None):
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
            "isolation": "worktree",
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
    assert signal == "idle", signal
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
# Behaviour 7 — a maintainer-self target resolves its repo via
# gov.get('maintainer_repo'); with safety-governance consumed UNCHANGED (its
# load_governance does NOT surface a maintainer_repo key) this falls back to the
# project repo (None) per the documented v1 fallback. The flush still files it.
# ==========================================================================

def test_maintainer_self_target_routes_to_project_repo_fallback():
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
    # v1 fallback: no maintainer_repo surfaced by the unchanged lib -> project
    # repo (the gh default, None).
    assert repo is None, repo


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
