#!/usr/bin/env python3
"""End-to-end tests for the RECONCILE state wiring (Wave-2 consumer).

scheduling wires a new built-in factory `make_reconcile(runtime)` that wraps
verify-integrate's `Reconcile` class (mirroring `make_integrate` over
`Integrate`) and registers at `run_tick:make_reconcile` in DEFAULT_ADAPTER_MAP.
RECONCILE runs BEFORE PULL (route GUARD -> DRAIN -> RECONCILE -> PULL -> ...).

Behaviours asserted end to end (all seams injected via monkeypatching the
verify-integrate module attributes — no network, no real git):

  1. DEFAULT_ADAPTER_MAP maps RECONCILE -> run_tick:make_reconcile; the
     conservative DEFAULT_ROUTE OMITS it (a pure route.json edit enables it).
  2. make_reconcile returns (vi.RECONCILE_MANIFEST, callable).
  3. _seed_context registers acted_ledger + reconcile_result when RECONCILE is
     routed, and neither when it is absent.
  4. A RECONCILE route resolves + validates via build_loop.
  5. make_reconcile SEEDS Reconcile's acted_ledger slot from the durable
     ACTED_LEDGER_KEY `opened` entries and, at auto-merge, a MERGED-PR-with-open
     issue is CLOSED (closed_issues) and the acted-ledger entry is STAMPED
     outcome='closed' (idempotency — a later tick's seed excludes it).
  6. A tier-2 reland (conflicting PR, rebase fails) CLOSES the PR + comments the
     issue and CLEARS the acted-ledger entry so the §3.8.5 re-entry gate re-lands
     it next tick.
  7. A tier-1 rebase (clean) records `rebased` and leaves the acted-ledger entry
     untouched (still opened).
  8. At propose/dry-run (merge not permitted) NO sink is called and the
     acted-ledger entry is unchanged (RECONCILE records the would-act intent
     under skipped; a human acts).
  9. RECONCILE is ADVISORY: a raising seam is recorded as an error and the tick
     still completes (never crashes).

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
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import adapter_wiring as aw  # noqa: E402
import verify_integrate as vi  # noqa: E402
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


# RECONCILE runs BEFORE PULL (route GUARD -> DRAIN -> RECONCILE -> PULL -> ...).
# All ports resolve to the built-in SCRIPT factories, so this is a pure-script
# route (runs via tick_orchestrator.run).
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

_PR_REF = "acme/widget#42"
_ISSUE_REF = "acme/widget#7"
_WO_ID = "wo-acme/widget#7"
_UPDATED = "2026-05-02T11:30:00Z"


def _write_project_route(project_dir, route):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(route, f)


def _write_governance(project_dir, mode):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": mode}, f)


def _seed_acted_ledger(state_path, entry=None):
    entry = entry or {"outcome": "opened", "ref": _PR_REF,
                      "acted_at_updated_at": _UPDATED}
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.ACTED_LEDGER_KEY: {_WO_ID: entry},
    })


class _Captures:
    def __init__(self):
        self.pr_state = []
        self.issue_state = []
        self.closes = []
        self.pr_closes = []
        self.comments = []
        self.worktree = []


def _patch_reconcile_seams(pr_state, issue_state=None, worktree=None,
                           raise_pr_state=False):
    """Override the verify-integrate gh module seams Reconcile (and make_reconcile)
    resolve at factory-call time. Returns (restore, captures)."""
    caps = _Captures()
    saved = {
        "branch": vi.gh_default_branch_source,
        "pr_state": vi.gh_pr_state_source,
        "issue_state": vi.gh_issue_state_source,
        "issue_close": vi.gh_issue_close_sink,
        "pr_close": vi.gh_pr_close_sink,
        "comment": vi.gh_issue_comment_sink,
        "worktree": vi.reconcile_rebase_worktree,
    }

    def _branch(repo=None):
        return "main"

    def _pr_state(pr_ref, repo=None):
        caps.pr_state.append((pr_ref, repo))
        if raise_pr_state:
            raise RuntimeError("gh boom")
        return dict(pr_state)

    def _issue_state(issue_ref, repo=None):
        caps.issue_state.append((issue_ref, repo))
        return dict(issue_state or {"state": "OPEN"})

    def _issue_close(issue_ref, repo=None, comment=None):
        caps.closes.append((issue_ref, repo, comment))

    def _pr_close(pr_ref, repo=None):
        caps.pr_closes.append((pr_ref, repo))

    def _comment(issue_ref, body, repo=None):
        caps.comments.append((issue_ref, body, repo))

    def _worktree(pr_ref, default_branch, repo=None):
        caps.worktree.append((pr_ref, default_branch, repo))
        return dict(worktree or {"rebased": True, "summary": ""})

    vi.gh_default_branch_source = _branch
    vi.gh_pr_state_source = _pr_state
    vi.gh_issue_state_source = _issue_state
    vi.gh_issue_close_sink = _issue_close
    vi.gh_pr_close_sink = _pr_close
    vi.gh_issue_comment_sink = _comment
    vi.reconcile_rebase_worktree = _worktree

    def restore():
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_state_source = saved["pr_state"]
        vi.gh_issue_state_source = saved["issue_state"]
        vi.gh_issue_close_sink = saved["issue_close"]
        vi.gh_pr_close_sink = saved["pr_close"]
        vi.gh_issue_comment_sink = saved["comment"]
        vi.reconcile_rebase_worktree = saved["worktree"]
    return restore, caps


def _run(project_dir, state_path, mode, pr_state, issue_state=None,
         worktree=None, raise_pr_state=False):
    _write_project_route(project_dir, _RECONCILE_ROUTE)
    _write_governance(project_dir, mode)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    restore, caps = _patch_reconcile_seams(
        pr_state, issue_state=issue_state, worktree=worktree,
        raise_pr_state=raise_pr_state)
    try:
        result = rt.run_tick(
            project_dir=project_dir, runtime_dir=runtime_dir,
            state_path=state_path, journal_path=journal_path,
            source=_stub_source(), return_run_result=True)
    finally:
        restore()
    return result, caps


# ==========================================================================
# Behaviour 1 + 2 — wiring: DEFAULT_ADAPTER_MAP maps RECONCILE; DEFAULT_ROUTE
# omits it; make_reconcile wraps the sibling state.
# ==========================================================================

def test_default_adapter_map_includes_reconcile():
    amap = rt.DEFAULT_ADAPTER_MAP
    assert "RECONCILE" in amap, amap
    assert amap["RECONCILE"].split(":")[1] == "make_reconcile", amap["RECONCILE"]


def test_default_route_omits_reconcile():
    assert "RECONCILE" not in rt.DEFAULT_ROUTE["states"], rt.DEFAULT_ROUTE


def test_make_reconcile_wraps_sibling_state():
    restore, _caps = _patch_reconcile_seams({"merged": False, "state": "OPEN"})
    try:
        rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
               "source": None, "now": None,
               "governance": {"mode": "auto-merge"}}
        manifest, run = rt.make_reconcile(rti)
    finally:
        restore()
    assert manifest is vi.RECONCILE_MANIFEST, manifest
    assert callable(run)


# ==========================================================================
# Behaviour 3 — _seed_context registers acted_ledger + reconcile_result when
# RECONCILE is routed; neither when it is absent.
# ==========================================================================

def test_seed_context_registers_reconcile_slots_when_routed():
    project_dir = tempfile.mkdtemp(prefix="sched-recseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _RECONCILE_ROUTE)
    slots = ctx.registered_slots()
    assert "acted_ledger" in slots, slots
    assert "reconcile_result" in slots, slots


def test_seed_context_omits_reconcile_slots_when_absent():
    project_dir = tempfile.mkdtemp(prefix="sched-recnoseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", rt.DEFAULT_ROUTE)
    slots = ctx.registered_slots()
    assert "acted_ledger" not in slots, slots
    assert "reconcile_result" not in slots, slots


def test_acted_ledger_in_initial_slots():
    assert "acted_ledger" in rt._INITIAL_SLOTS, rt._INITIAL_SLOTS


# ==========================================================================
# Behaviour 4 — the RECONCILE route resolves + validates via build_loop.
# ==========================================================================

def test_reconcile_route_resolves_and_validates():
    restore, _caps = _patch_reconcile_seams({"merged": False, "state": "OPEN"})
    try:
        runtime = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
                   "source": None, "now": None,
                   "governance": {"mode": "auto-merge"}}
        route, states = aw.build_loop(
            _RECONCILE_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
            start="GUARD", initial=rt._INITIAL_SLOTS)
    finally:
        restore()
    assert "RECONCILE" in states, list(states)
    assert not isinstance(states["RECONCILE"][1], aw.AgentState)


# ==========================================================================
# Behaviour 5 — merged-PR issue-close fallback at auto-merge; the acted-ledger
# entry is stamped outcome='closed'.
# ==========================================================================

def test_merged_pr_closes_issue_and_stamps_ledger():
    project_dir = tempfile.mkdtemp(prefix="sched-recmerged-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
        issue_state={"state": "OPEN"})
    assert result.final_state == "DONE", result.path
    # The merged PR's still-open source issue was closed.
    assert [c[0] for c in caps.closes] == [_ISSUE_REF], caps.closes
    # The acted-ledger entry is stamped outcome='closed' (idempotency): the next
    # tick's seed (opened-only) excludes it, so it is never re-processed.
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "closed", ledger


def test_seed_shapes_issue_ref_and_pr_ref_from_ledger():
    project_dir = tempfile.mkdtemp(prefix="sched-recshape-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    _result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
        issue_state={"state": "OPEN"})
    # Reconcile queried the PR ref (from entry['ref']) and the issue ref (derived
    # from the wo- prefixed work_order_id).
    assert caps.pr_state and caps.pr_state[0][0] == _PR_REF, caps.pr_state
    assert caps.issue_state and caps.issue_state[0][0] == _ISSUE_REF, \
        caps.issue_state


# ==========================================================================
# Behaviour 6 — tier-2 reland (conflicting PR, rebase fails) clears the entry.
# ==========================================================================

def test_reland_clears_ledger_entry():
    project_dir = tempfile.mkdtemp(prefix="sched-recreland-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": False, "state": "OPEN",
                  "mergeable": "CONFLICTING"},
        worktree={"rebased": False, "summary": "conflict"})
    assert result.final_state == "DONE", result.path
    # The conflicting PR was closed + the issue commented to re-land.
    assert [c[0] for c in caps.pr_closes] == [_PR_REF], caps.pr_closes
    assert [c[0] for c in caps.comments] == [_ISSUE_REF], caps.comments
    # The acted-ledger entry is CLEARED so the §3.8.5 re-entry gate re-lands it.
    ledger = rt.persisted_acted_ledger(state_path)
    assert _WO_ID not in ledger, ledger


# ==========================================================================
# Behaviour 7 — tier-1 clean rebase records rebased, leaves the entry untouched.
# ==========================================================================

def test_clean_rebase_leaves_ledger_entry():
    project_dir = tempfile.mkdtemp(prefix="sched-recrebase-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": False, "state": "OPEN",
                  "mergeable": "CONFLICTING"},
        worktree={"rebased": True, "summary": ""})
    assert result.final_state == "DONE", result.path
    # Tier-1: the PR was rebased + force-pushed; NOT closed, issue NOT commented.
    assert caps.worktree and caps.worktree[0][0] == _PR_REF, caps.worktree
    assert caps.pr_closes == [], caps.pr_closes
    # The entry is untouched (still opened — a later tick sees it merged).
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "opened", ledger


# ==========================================================================
# Behaviour 8 — propose (merge not permitted): NO sink called, entry unchanged.
# ==========================================================================

def test_propose_does_not_mutate():
    project_dir = tempfile.mkdtemp(prefix="sched-recpropose-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, caps = _run(
        project_dir, state_path, "propose",
        pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
        issue_state={"state": "OPEN"})
    assert result.final_state == "DONE", result.path
    # merge is not permitted at propose -> the would-close is recorded under
    # skipped inside Reconcile; the issue-close sink is NEVER called.
    assert caps.closes == [], caps.closes
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "opened", ledger


# ==========================================================================
# Behaviour 9 — RECONCILE is ADVISORY: a raising seam never crashes the tick.
# ==========================================================================

def test_raising_seam_does_not_crash_tick():
    project_dir = tempfile.mkdtemp(prefix="sched-recadvisory-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, _caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": True, "state": "MERGED"},
        raise_pr_state=True)
    # The single-entry fault is recorded under errors; the tick still completes.
    assert result.final_state == "DONE", result.path
    # The entry is left untouched (no close happened) — never a crash.
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "opened", ledger


# ==========================================================================
# Empty ledger — a RECONCILE tick with no opened entries is a clean no-op.
# ==========================================================================

def test_empty_ledger_is_noop():
    project_dir = tempfile.mkdtemp(prefix="sched-recempty-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": False, "state": "OPEN"})
    assert result.final_state == "DONE", result.path
    assert caps.pr_state == [], caps.pr_state
    assert caps.closes == [], caps.closes
