#!/usr/bin/env python3
"""End-to-end conformance tests for the ACTED-LEDGER RE-ENTRY (§3.8.5-symmetric
leak fix, #204): re-attempt a still-valid issue when its auto-PR is closed.

The durable acted-ledger records every work order the loop acts on as `opened`
(PR opened) / `closed`, and the IMPLEMENT per_item filter skips an already-acted
work order forever (idempotency — no duplicate PR). But when a human CLOSES an
auto-PR (rejecting the work) and leaves the issue OPEN, the loop must NOT abandon
the still-valid issue forever. This cycle adds a re-entry rule symmetric with the
§3.8.5 backoff re-entry:

  An already-`opened` work order RE-ENTERS the dispatch set (its acted-ledger
  entry cleared, the item re-dispatched) when BOTH:
    - its PR `ref` is CLOSED-AND-NOT-MERGED, AND
    - the issue's current updated_at has ADVANCED past acted_at_updated_at.
  It stays LOCKED otherwise: merged (done), still-open PR (pending review), or
  closed-but-issue-unchanged (the human closed it without a redo — no thrash).

Behaviours covered:
  1. The `opened` ledger entry now records acted_at_updated_at (the issue's
     updated_at at act time).
  2. An injectable PR-state seam (gh_pr_state_source / DEFAULT_PR_STATE_SOURCE),
     mirroring verify-integrate's gh_open_pr_source, makes the closed/merged check
     deterministic + unit-testable; the PR is queried ONLY for entries whose
     updated_at advanced (bounds the gh calls to changed issues).
  3. closed-unmerged PR + advanced updated_at -> re-enters (ledger entry cleared,
     re-dispatched).
  4. merged PR -> stays locked.
  5. still-open PR -> stays locked.
  6. closed-unmerged PR + unchanged updated_at -> stays locked (no thrash).

scheduling CONSUMES its sibling features UNCHANGED via sys.path; edits live ONLY
in scheduling (run_tick.py).

Owner: changyu87
"""

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
import run_tick as rt  # noqa: E402


_TZ = timezone(timedelta(hours=-5))
_DAY1 = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)

# Issue #7's updated_at at act time, and an "advanced" value (a human commented /
# reopened the issue after the auto-PR was closed).
_UPDATED_T1 = "2026-05-02T11:30:00Z"
_UPDATED_T2 = "2026-05-09T08:00:00Z"

_PR_REF = "https://github.com/acme/widget/pull/42"


def _gh_fixture(updated_7=_UPDATED_T1):
    return json.dumps([
        {
            "number": 7,
            "title": "Crash on empty config",
            "body": "Steps to reproduce ...",
            "url": "https://github.com/acme/widget/issues/7",
            "state": "OPEN",
            "labels": [{"name": "bug"}],
            "author": {"login": "octocat"},
            "createdAt": "2026-05-01T10:00:00Z",
            "updatedAt": updated_7,
        },
    ])


def _stub_source(json_text=None):
    items = wi.parse_gh_issues(json_text or _gh_fixture())

    def source(repo=None):
        return list(items)
    return source


# --------------------------------------------------------------------------
# Agent-adapter fixtures: TRIAGE non-acting agent; IMPLEMENT acting agent
# (effect=implement, per_item over execution_plan.ordered). One work_item (#7).
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


def _write_cfg(project_dir, name, payload):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(payload, f)


def _setup_agent_project(mode="propose"):
    project_dir = tempfile.mkdtemp(prefix="sched-reenter-")
    _write_cfg(project_dir, "route.json", _AGENT_ROUTE)
    _write_cfg(project_dir, "adapter-map.json", _agent_map())
    _write_cfg(project_dir, "governance.json", {"mode": mode})
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


def _canned_handoff(work_order_id, status="opened", ref=_PR_REF):
    artifact = ({"kind": "pr", "ref": ref} if status in ("opened", "closed")
                else {"kind": "none", "ref": None})
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": status, "artifact": artifact,
        "discovered_work": [], "blocked_reason": None,
    })


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                   now=_DAY1, source=None, pr_state_source=None):
    """Step TRIAGE (non-acting agent) past its pause; return the SECOND return
    (the IMPLEMENT pause / acting-state branch result)."""
    src = source or _stub_source()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=src, now=now, pr_state_source=pr_state_source)
    assert paused["status"] == "paused" and paused["state"] == "TRIAGE", paused
    _write_outputs(paused, [_CANNED_WORK_ORDERS])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=src, now=now, resume=True,
                       pr_state_source=pr_state_source)


def _open_pr_once(project_dir, runtime_dir, state_path, journal_path,
                  now=_DAY1, source=None):
    """Run ONE full tick that reaches IMPLEMENT, opens a PR for the single work
    order, and resumes to DONE. Returns the final signal."""
    paused = _resume_triage(project_dir, runtime_dir, state_path, journal_path,
                            now=now, source=source)
    assert paused["status"] == "paused" and paused["state"] == "IMPLEMENT", \
        paused
    _write_outputs(paused, [_canned_handoff(
        paused["dispatches"][0]["item"], status="opened")])
    return rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                       state_path=state_path, journal_path=journal_path,
                       source=source or _stub_source(), now=now, resume=True)


def _pr_state(state, merged=False):
    """A deterministic injectable PR-state source returning a fixed state."""
    def source(pr_ref, repo=None):
        return {"state": state, "merged": merged}
    return source


def _counting_pr_state(state, merged=False):
    """A PR-state source that records every pr_ref it was queried for (so a test
    can assert the query is bounded to changed issues)."""
    calls = []

    def source(pr_ref, repo=None):
        calls.append(pr_ref)
        return {"state": state, "merged": merged}
    return source, calls


# ==========================================================================
# Behaviour 1 — the `opened` ledger entry records acted_at_updated_at.
# ==========================================================================

def test_opened_entry_records_acted_at_updated_at():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    signal = _open_pr_once(project_dir, runtime_dir, state_path, journal_path)
    assert signal in ("idle", "refire"), signal
    ledger = rt.persisted_acted_ledger(state_path)
    entry = ledger["wo-acme/widget#7"]
    assert entry["outcome"] == "opened", entry
    assert entry["ref"] == _PR_REF, entry
    assert entry["acted_at_updated_at"] == _UPDATED_T1, entry


# ==========================================================================
# Behaviour 2 — the injectable PR-state seam exists + mirrors gh_open_pr_source.
# ==========================================================================

def test_pr_state_seam_exists_and_is_injectable():
    assert rt.DEFAULT_PR_STATE_SOURCE is rt.gh_pr_state_source

    captured = {}

    def fake_runner(cmd, capture_output, text, check):
        captured["cmd"] = cmd

        class _R:
            stdout = json.dumps({"state": "CLOSED", "mergedAt": None})
        return _R()

    out = rt.gh_pr_state_source(_PR_REF, runner=fake_runner)
    assert out == {"state": "CLOSED", "merged": False}, out
    # It shells `gh pr view <ref>` for the closed/merged fields.
    assert captured["cmd"][:3] == ["gh", "pr", "view"], captured["cmd"]
    assert _PR_REF in captured["cmd"], captured["cmd"]

    # A non-null mergedAt is a merged PR.
    def merged_runner(cmd, capture_output, text, check):
        class _R:
            stdout = json.dumps({"state": "MERGED",
                                 "mergedAt": "2026-05-03T00:00:00Z"})
        return _R()
    out = rt.gh_pr_state_source(_PR_REF, runner=merged_runner)
    assert out == {"state": "MERGED", "merged": True}, out


# ==========================================================================
# Behaviour 3 — closed-unmerged PR + advanced updated_at -> re-enters.
# ==========================================================================

def test_closed_unmerged_advanced_reenters():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _open_pr_once(project_dir, runtime_dir, state_path, journal_path)
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path)

    # A human closed the auto-PR (not merged) and reopened/updated the issue
    # (updated_at advanced).
    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    result = _resume_triage(
        project_dir, runtime_dir, state_path, journal_path,
        source=advanced_src, pr_state_source=_pr_state("CLOSED", merged=False))
    # The item re-enters: IMPLEMENT pauses to dispatch it again.
    assert result["status"] == "paused", result
    assert result["state"] == "IMPLEMENT", result
    assert len(result["dispatches"]) == 1, result
    assert result["dispatches"][0]["item"] == "wo-acme/widget#7", result
    # Its acted-ledger entry was cleared on re-entry.
    assert "wo-acme/widget#7" not in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)


# ==========================================================================
# Behaviour 4 — merged PR -> stays locked (done, no re-attempt).
# ==========================================================================

def test_merged_pr_stays_locked():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _open_pr_once(project_dir, runtime_dir, state_path, journal_path)

    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    result = _resume_triage(
        project_dir, runtime_dir, state_path, journal_path,
        source=advanced_src, pr_state_source=_pr_state("MERGED", merged=True))
    # No item remains -> IMPLEMENT does NOT pause; drives to DONE.
    assert result in ("idle", "refire", "break"), result
    # The acted-ledger entry is preserved (still done).
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)


# ==========================================================================
# Behaviour 5 — still-open PR -> stays locked (pending review, no thrash).
# ==========================================================================

def test_open_pr_stays_locked():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _open_pr_once(project_dir, runtime_dir, state_path, journal_path)

    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    result = _resume_triage(
        project_dir, runtime_dir, state_path, journal_path,
        source=advanced_src, pr_state_source=_pr_state("OPEN", merged=False))
    assert result in ("idle", "refire", "break"), result
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)


# ==========================================================================
# Behaviour 6 — closed-unmerged PR + UNCHANGED updated_at -> stays locked.
# The human closed the auto-PR without asking for a redo (issue untouched): the
# loop must respect it and NOT re-attempt (no thrash). The PR must NOT even be
# queried (the query is bounded to changed issues).
# ==========================================================================

def test_closed_unmerged_unchanged_stays_locked_and_not_queried():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _open_pr_once(project_dir, runtime_dir, state_path, journal_path)

    # The issue updated_at is UNCHANGED (same fixture value as act time). #7 was
    # recorded done-AND-unchanged in triage_memory at the act, so TRIAGE
    # empty-skips (#306): no subagent dispatch, no TRIAGE pause — the tick runs
    # straight through to idle in ONE invocation. IMPLEMENT (where the bounded PR
    # query lives) is reached over an empty plan, so the PR is still never
    # queried.
    same_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T1))
    source, calls = _counting_pr_state("CLOSED", merged=False)
    result = rt.run_tick(
        project_dir=project_dir, runtime_dir=runtime_dir,
        state_path=state_path, journal_path=journal_path,
        source=same_src, now=_DAY1, pr_state_source=source)
    assert result in ("idle", "refire", "break"), result
    # Stays locked: the acted-ledger entry is preserved.
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)
    # And the PR was NEVER queried (the gh call is bounded to changed issues).
    assert calls == [], calls


# ==========================================================================
# Behaviour 7 — a pre-#204 ledger entry (no acted_at_updated_at) never re-enters
# and never triggers a PR query (back-compatible; the null pin can't advance).
# ==========================================================================

def test_legacy_entry_without_pin_stays_locked():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    # Pre-seed a legacy `opened` entry with NO acted_at_updated_at.
    doc = ds.DurableState(state_path).load()
    doc[rt.ACTED_LEDGER_KEY] = {
        "wo-acme/widget#7": {"outcome": "opened", "ref": _PR_REF},
    }
    ds.DurableState(state_path).save(doc)

    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    source, calls = _counting_pr_state("CLOSED", merged=False)
    result = _resume_triage(
        project_dir, runtime_dir, state_path, journal_path,
        source=advanced_src, pr_state_source=source)
    assert result in ("idle", "refire", "break"), result
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)
    assert calls == [], calls


# ==========================================================================
# Behaviour 8 — a raising / malformed PR-state source never crashes the tick and
# never re-enters (stays locked, no thrash).
# ==========================================================================

def test_raising_pr_state_source_does_not_crash_or_reenter():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    _open_pr_once(project_dir, runtime_dir, state_path, journal_path)

    def boom(pr_ref, repo=None):
        raise RuntimeError("gh unavailable")

    advanced_src = _stub_source(_gh_fixture(updated_7=_UPDATED_T2))
    result = _resume_triage(
        project_dir, runtime_dir, state_path, journal_path,
        source=advanced_src, pr_state_source=boom)
    # The tick still reached its terminal despite the source failure.
    assert result in ("idle", "refire", "break"), result
    # Stays locked (the failed query can't confirm closed-unmerged).
    assert "wo-acme/widget#7" in rt.persisted_acted_ledger(state_path), \
        rt.persisted_acted_ledger(state_path)


# ==========================================================================
# Behaviour 9 — direct unit coverage of _acted_reentry_wo_ids decision matrix.
# ==========================================================================

def test_acted_reentry_wo_ids_decision_matrix():
    collection = ["wo-acme/widget#7"]
    wo_to_wi = {"wo-acme/widget#7": "acme/widget#7"}
    advanced = {"acme/widget#7": _UPDATED_T2}
    unchanged = {"acme/widget#7": _UPDATED_T1}
    opened = {"wo-acme/widget#7": {"outcome": "opened", "ref": _PR_REF,
                                   "acted_at_updated_at": _UPDATED_T1}}

    # closed-unmerged + advanced -> re-enters.
    assert rt._acted_reentry_wo_ids(
        collection, opened, wo_to_wi, advanced,
        _pr_state("CLOSED", merged=False)) == {"wo-acme/widget#7"}
    # merged + advanced -> locked.
    assert rt._acted_reentry_wo_ids(
        collection, opened, wo_to_wi, advanced,
        _pr_state("MERGED", merged=True)) == set()
    # open + advanced -> locked.
    assert rt._acted_reentry_wo_ids(
        collection, opened, wo_to_wi, advanced,
        _pr_state("OPEN", merged=False)) == set()
    # closed-unmerged + UNCHANGED -> locked.
    assert rt._acted_reentry_wo_ids(
        collection, opened, wo_to_wi, unchanged,
        _pr_state("CLOSED", merged=False)) == set()
    # a `closed` outcome (issue itself closed by the doer) never re-enters.
    closed = {"wo-acme/widget#7": {"outcome": "closed", "ref": None,
                                   "acted_at_updated_at": _UPDATED_T1}}
    assert rt._acted_reentry_wo_ids(
        collection, closed, wo_to_wi, advanced,
        _pr_state("CLOSED", merged=False)) == set()
    # a None source never re-enters (defensive).
    assert rt._acted_reentry_wo_ids(
        collection, opened, wo_to_wi, advanced, None) == set()
