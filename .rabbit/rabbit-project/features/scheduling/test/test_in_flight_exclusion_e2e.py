#!/usr/bin/env python3
"""End-to-end conformance tests for IN-FLIGHT EXCLUSION wiring (convergence):
PULL honors `in_flight_issue_refs`.

An issue already being worked by an OPEN auto-maintainer PR must NOT be
re-triaged / re-implemented — the open PR's own lifecycle resolves it. scheduling
computes the set of issue refs that already have an OPEN loop PR (from the durable
acted-ledger `opened` entries, live-confirmed OPEN via the EXISTING injectable
gh_pr_state_source seam) and threads it into work-intake's `Pull` as
`in_flight_issue_refs=<set>`. work-intake + safety-governance are consumed
UNCHANGED; the edit lives ONLY in `make_pull` + the new `_in_flight_issue_refs`
helper.

Behaviours covered:
  1. _in_flight_issue_refs returns the canonical owner/repo#N issue refs for
     `opened` ledger entries whose PR the fake source reports OPEN (both the
     `-wo` suffix and legacy `wo-` prefix work_order_id forms canonicalize).
  2. merged / closed PRs are EXCLUDED (only OPEN loop PRs are in flight).
  3. a raising / malformed PR-state read for one entry is tolerated (skipped),
     never crashing.
  4. an empty acted-ledger yields the empty set (non-breaking).
  5. make_pull threads the set into wi.Pull (asserted via pull._in_flight_issue_refs).
  6. e2e run_tick: an in-flight issue is EXCLUDED from the persisted work_items,
     while a non-in-flight issue flows through PULL normally.

scheduling CONSUMES its sibling features UNCHANGED via sys.path; edits live ONLY
in scheduling (run_tick.py).

Owner: changyu87
"""

import json
import os
import sys
import tempfile

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


_PR7 = "https://github.com/acme/widget/pull/42"
_PR8 = "https://github.com/acme/widget/pull/43"


def _pr_state(mapping):
    """A deterministic injectable PR-state source: `mapping` maps pr_ref ->
    {"state","merged"}. An unknown ref returns OPEN (so a test can default)."""
    def source(pr_ref, repo=None):
        return mapping.get(pr_ref, {"state": "OPEN", "merged": False})
    return source


def _seed_ledger(state_path, ledger):
    doc = ds.DurableState(state_path).load()
    doc[rt.ACTED_LEDGER_KEY] = ledger
    ds.DurableState(state_path).save(doc)


def _tmp_state():
    root = tempfile.mkdtemp(prefix="sched-inflight-")
    return os.path.join(root, "state.json")


# ==========================================================================
# Behaviour 1 — opened + OPEN PR -> canonical issue ref in the set (both
# work_order_id affix forms).
# ==========================================================================

def test_in_flight_refs_opened_open_pr_canonical_refs():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
        "wo-acme/widget#8": {"outcome": "opened", "ref": _PR8,
                             "acted_at_updated_at": "t1"},
    })
    src = _pr_state({_PR7: {"state": "OPEN", "merged": False},
                     _PR8: {"state": "OPEN", "merged": False}})
    refs = rt._in_flight_issue_refs(state_path, src)
    assert refs == {"acme/widget#7", "acme/widget#8"}, refs


# ==========================================================================
# Behaviour 2 — merged / closed PRs are excluded.
# ==========================================================================

def test_in_flight_refs_excludes_merged_and_closed():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
        "acme/widget#8-wo": {"outcome": "opened", "ref": _PR8,
                             "acted_at_updated_at": "t1"},
    })
    # #7's PR is merged, #8's PR is closed-unmerged -> neither is in flight.
    src = _pr_state({_PR7: {"state": "MERGED", "merged": True},
                     _PR8: {"state": "CLOSED", "merged": False}})
    assert rt._in_flight_issue_refs(state_path, src) == set()


def test_in_flight_refs_ignores_non_opened_outcomes():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        # a 'closed'/'merged' outcome is not an open loop PR -> never in flight,
        # and its PR is not even queried.
        "acme/widget#7-wo": {"outcome": "closed", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
        "acme/widget#8-wo": {"outcome": "merged", "ref": _PR8,
                             "acted_at_updated_at": "t1"},
    })
    queried = []

    def src(pr_ref, repo=None):
        queried.append(pr_ref)
        return {"state": "OPEN", "merged": False}
    assert rt._in_flight_issue_refs(state_path, src) == set()
    assert queried == [], queried


# ==========================================================================
# Behaviour 3 — a raising / malformed PR-state read for one entry is tolerated.
# ==========================================================================

def test_in_flight_refs_tolerates_raising_entry():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
        "acme/widget#8-wo": {"outcome": "opened", "ref": _PR8,
                             "acted_at_updated_at": "t1"},
    })

    def src(pr_ref, repo=None):
        if pr_ref == _PR7:
            raise RuntimeError("gh unavailable")
        return {"state": "OPEN", "merged": False}
    # The raising #7 entry is skipped; #8 still resolves.
    assert rt._in_flight_issue_refs(state_path, src) == {"acme/widget#8"}


def test_in_flight_refs_tolerates_malformed_value():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
    })

    def src(pr_ref, repo=None):
        return "not-a-dict"
    assert rt._in_flight_issue_refs(state_path, src) == set()


# ==========================================================================
# Behaviour 4 — empty acted-ledger / None source yields the empty set.
# ==========================================================================

def test_in_flight_refs_empty_ledger():
    state_path = _tmp_state()
    src = _pr_state({})
    assert rt._in_flight_issue_refs(state_path, src) == set()


def test_in_flight_refs_none_source():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
    })
    assert rt._in_flight_issue_refs(state_path, None) == set()


def test_in_flight_refs_skips_entry_without_ref():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": None,
                             "acted_at_updated_at": "t1"},
    })
    assert rt._in_flight_issue_refs(state_path, _pr_state({})) == set()


# ==========================================================================
# Behaviour 5 — make_pull threads the set into wi.Pull.
# ==========================================================================

def _recording_source(items_json="[]"):
    items = wi.parse_gh_issues(items_json)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def test_make_pull_threads_in_flight_issue_refs():
    state_path = _tmp_state()
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "t1"},
    })
    runtime = {
        "source": _recording_source(),
        "governance": {},
        "state_path": state_path,
        "pr_state_source": _pr_state({_PR7: {"state": "OPEN", "merged": False}}),
    }
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._in_flight_issue_refs == frozenset({"acme/widget#7"}), \
        pull._in_flight_issue_refs


def test_make_pull_default_runtime_binds_empty_in_flight():
    """A runtime with no state_path / pr_state_source (the existing test shape)
    binds the empty set — non-breaking."""
    runtime = {"source": _recording_source(), "governance": {}}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._in_flight_issue_refs == frozenset(), pull._in_flight_issue_refs


# ==========================================================================
# Behaviour 6 — e2e run_tick: an in-flight issue is excluded from work_items.
# ==========================================================================

def _fixture_two_issues():
    return json.dumps([
        {
            "number": 7, "title": "Crash on empty config", "body": "...",
            "url": "https://github.com/acme/widget/issues/7", "state": "OPEN",
            "labels": [{"name": "bug"}], "author": {"login": "octocat"},
            "createdAt": "2026-05-01T10:00:00Z",
            "updatedAt": "2026-05-02T11:30:00Z",
        },
        {
            "number": 8, "title": "Add a flag", "body": "...",
            "url": "https://github.com/acme/widget/issues/8", "state": "OPEN",
            "labels": [{"name": "enhancement"}], "author": {"login": "octocat"},
            "createdAt": "2026-05-03T08:00:00Z",
            "updatedAt": "2026-05-03T09:00:00Z",
        },
    ])


def _paths():
    root = tempfile.mkdtemp(prefix="sched-inflight-e2e-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def test_run_tick_excludes_in_flight_issue_from_work_items():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="sched-inflight-proj-")
    # #7 already has an OPEN loop PR (an `opened` acted-ledger entry).
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "2026-05-02T11:30:00Z"},
    })
    src = _recording_source(_fixture_two_issues())
    signal = rt.run_tick(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir, source=src,
        pr_state_source=_pr_state({_PR7: {"state": "OPEN", "merged": False}}))
    assert signal == "idle", signal
    ids = {it.get("id") for it in rt.persisted_work_items(state_path)}
    # #7 is in flight (OPEN loop PR) -> excluded; #8 flows through normally.
    assert ids == {"acme/widget#8"}, ids


def test_run_tick_merged_loop_pr_not_in_flight():
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="sched-inflight-proj-")
    _seed_ledger(state_path, {
        "acme/widget#7-wo": {"outcome": "opened", "ref": _PR7,
                             "acted_at_updated_at": "2026-05-02T11:30:00Z"},
    })
    src = _recording_source(_fixture_two_issues())
    # #7's loop PR has since MERGED -> #7 is NOT in flight -> pulled normally.
    rt.run_tick(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=project_dir, source=src,
        pr_state_source=_pr_state({_PR7: {"state": "MERGED", "merged": True}}))
    ids = {it.get("id") for it in rt.persisted_work_items(state_path)}
    assert ids == {"acme/widget#7", "acme/widget#8"}, ids
