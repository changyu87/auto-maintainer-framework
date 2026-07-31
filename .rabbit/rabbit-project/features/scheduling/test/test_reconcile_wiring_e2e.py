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
        self.open_pr_closing = []


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
        "open_pr_closing": vi.gh_open_pr_closing_issue_source,
    }

    def _branch(repo=None):
        return "main"

    def _open_pr_closing(repo=None):
        # The (C) same-issue dedup source. make_reconcile now threads it as an
        # injectable seam; stub it (no live open loop-PR set) so the dedup pass is
        # a hermetic no-op and never shells to the real `gh pr list`.
        caps.open_pr_closing.append(repo)
        return []

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
    vi.gh_open_pr_closing_issue_source = _open_pr_closing

    def restore():
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_state_source = saved["pr_state"]
        vi.gh_issue_state_source = saved["issue_state"]
        vi.gh_issue_close_sink = saved["issue_close"]
        vi.gh_pr_close_sink = saved["pr_close"]
        vi.gh_issue_comment_sink = saved["comment"]
        vi.reconcile_rebase_worktree = saved["worktree"]
        vi.gh_open_pr_closing_issue_source = saved["open_pr_closing"]
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
    # skipped inside Reconcile; the issue-close sink is NEVER called (no GitHub
    # mutation at propose).
    assert caps.closes == [], caps.closes
    # The PR is factually MERGED, so RECONCILE records it under auto_merged
    # UNCONDITIONALLY (a pure observability record, mode-independent). scheduling
    # stamps the entry TERMINAL outcome='merged' regardless of mode — the local
    # ledger stamp is not a GitHub mutation, and stamping terminal is what
    # guarantees the completion is surfaced in tick_end.auto_merged EXACTLY ONCE
    # (gating it by mode would re-seed + re-detect it every propose tick).
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "merged", ledger


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


# ==========================================================================
# prior_verdicts seeding (race-breaker for verify-integrate's RECONCILE (B)
# ladder): make_reconcile SEEDS Reconcile's OPTIONAL `prior_verdicts` slot from
# the durable persisted `verdicts` read-product (the PREVIOUS tick's VERIFY
# output), VERBATIM (same List-of-verdict-dicts shape), seeding [] when absent.
# The prior_verdicts ctx slot is registered alongside acted_ledger.
# ==========================================================================

_PRIOR_VERDICTS = [
    {"pr_ref": _PR_REF, "mergeable": "CONFLICTING", "ok": False,
     "reasons": ["merge conflict with base"]},
]


class _FakeReconcile:
    """Captures the ctx `prior_verdicts` slot make_reconcile seeds, then returns
    a minimal empty ReconcileResult (no closed_issues / relanded), so the run
    wrapper's _persist_reconcile_outcome is a clean no-op."""

    captured = {}

    def __init__(self, **kwargs):
        pass

    def run(self, ctx):
        _FakeReconcile.captured["prior_verdicts"] = ctx.read("prior_verdicts")
        _FakeReconcile.captured["acted_ledger"] = ctx.read("acted_ledger")

        class _R:
            writes = {"reconcile_result": vi.ReconcileResult().to_dict()}

        return _R()


def _make_reconcile_capture(state_path):
    """Build the ctx (via _seed_context on the RECONCILE route), run
    make_reconcile's bound wrapper against a fake Reconcile that captures the
    seeded slots. Returns _FakeReconcile.captured."""
    _FakeReconcile.captured = {}
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _RECONCILE_ROUTE)
    restore, _caps = _patch_reconcile_seams({"merged": False, "state": "OPEN"})
    saved_reconcile = vi.Reconcile
    vi.Reconcile = _FakeReconcile
    try:
        runtime = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
                   "source": None, "now": None,
                   "governance": {"mode": "auto-merge"}}
        _manifest, run = rt.make_reconcile(runtime)
        run(ctx)
    finally:
        vi.Reconcile = saved_reconcile
        restore()
    return _FakeReconcile.captured


def test_make_reconcile_seeds_prior_verdicts_from_snapshot():
    project_dir = tempfile.mkdtemp(prefix="sched-recpv-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    # make_reconcile now seeds prior_verdicts from the DURABLE PRIOR_VERDICTS_KEY
    # snapshot (written at tick end), NOT the ephemeral VERDICTS_KEY read product
    # (which the fresh-tick reset wipes before RECONCILE runs). Seed ONLY the
    # snapshot key and assert it is read VERBATIM.
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.PRIOR_VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    captured = _make_reconcile_capture(state_path)
    assert captured["prior_verdicts"] == _PRIOR_VERDICTS, captured


def test_make_reconcile_ignores_live_verdicts_slot():
    """make_reconcile must NOT read the ephemeral VERDICTS_KEY read product: with
    ONLY the live verdicts key populated (and no snapshot), prior_verdicts is []."""
    project_dir = tempfile.mkdtemp(prefix="sched-recpvlive-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    captured = _make_reconcile_capture(state_path)
    assert captured["prior_verdicts"] == [], captured


def test_make_reconcile_seeds_prior_verdicts_empty_when_absent():
    project_dir = tempfile.mkdtemp(prefix="sched-recpvempty-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    # No PRIOR_VERDICTS_KEY persisted (first tick / none).
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    captured = _make_reconcile_capture(state_path)
    assert captured["prior_verdicts"] == [], captured


def test_persisted_verdicts_reads_durable_read_product():
    project_dir = tempfile.mkdtemp(prefix="sched-pvaccessor-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    assert rt.persisted_verdicts(state_path) == _PRIOR_VERDICTS
    # Absent -> [] (non-breaking default).
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    assert rt.persisted_verdicts(state_path) == []


def test_seed_context_registers_prior_verdicts_when_routed():
    project_dir = tempfile.mkdtemp(prefix="sched-recpvseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _RECONCILE_ROUTE)
    slots = ctx.registered_slots()
    assert "prior_verdicts" in slots, slots
    # Seeded EMPTY so a route reads it without a ContractError.
    assert ctx.read("prior_verdicts") == []


def test_seed_context_omits_prior_verdicts_when_reconcile_absent():
    project_dir = tempfile.mkdtemp(prefix="sched-recpvnoseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", rt.DEFAULT_ROUTE)
    assert "prior_verdicts" not in ctx.registered_slots(), \
        ctx.registered_slots()


def test_prior_verdicts_in_initial_slots():
    assert "prior_verdicts" in rt._INITIAL_SLOTS, rt._INITIAL_SLOTS


def test_reconcile_route_with_prior_verdicts_resolves_and_validates():
    # data-readiness: RECONCILE reads acted_ledger + prior_verdicts and runs
    # BEFORE PULL, so both must be satisfied by the initial set for build_loop.
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


# ==========================================================================
# auto_merged ledger-stamp (report-once): scheduling stamps each acted-ledger
# entry named in reconcile_result.auto_merged to a TERMINAL outcome='merged' so
# _reconcile_ledger_seed (seeds only outcome=='opened') never re-seeds it — an
# async auto-merge completion is surfaced in EXACTLY ONE tick's auto_merged.
# The real Reconcile records auto_merged for EVERY merged acted_ledger PR seen;
# a merged PR whose source issue is ALREADY CLOSED lands in auto_merged but NOT
# closed_issues, isolating the merged-stamp path.
# ==========================================================================

def test_auto_merged_stamps_ledger_terminal_merged():
    project_dir = tempfile.mkdtemp(prefix="sched-recautomerged-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    result, caps = _run(
        project_dir, state_path, "auto-merge",
        pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
        issue_state={"state": "CLOSED"})
    assert result.final_state == "DONE", result.path
    # The merged PR's issue was already closed -> auto_merged captured it, but
    # closed_issues did NOT (never re-touch a closed issue).
    assert caps.closes == [], caps.closes
    # The acted-ledger entry is stamped outcome='merged' (a terminal, non-'opened'
    # outcome) so _reconcile_ledger_seed never re-seeds it.
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] == "merged", ledger


def test_auto_merged_report_once_seed_excludes_stamped_entry():
    """After the auto_merged stamp, _reconcile_ledger_seed (opened-only) no longer
    includes the entry — the completion is reported in EXACTLY ONE tick."""
    project_dir = tempfile.mkdtemp(prefix="sched-recreportonce-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    _run(project_dir, state_path, "auto-merge",
         pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
         issue_state={"state": "CLOSED"})
    ledger = rt.persisted_acted_ledger(state_path)
    seed = rt._reconcile_ledger_seed(ledger)
    assert all(e["work_order_id"] != _WO_ID for e in seed), seed


def test_auto_merged_with_open_issue_ends_terminal():
    """A merged PR whose issue is still OPEN lands in BOTH auto_merged and
    closed_issues; the entry ends TERMINAL either way (idempotent), so the
    report-once seed still excludes it next tick."""
    project_dir = tempfile.mkdtemp(prefix="sched-recboth-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    _seed_acted_ledger(state_path)
    _run(project_dir, state_path, "auto-merge",
         pr_state={"merged": True, "state": "MERGED", "mergeable": "MERGEABLE"},
         issue_state={"state": "OPEN"})
    ledger = rt.persisted_acted_ledger(state_path)
    assert ledger[_WO_ID]["outcome"] in ("closed", "merged"), ledger
    seed = rt._reconcile_ledger_seed(ledger)
    assert all(e["work_order_id"] != _WO_ID for e in seed), seed


# ==========================================================================
# _reconcile_ledger_seed canonical pr_ref (tier-1 crash fix): the acted-ledger
# stores each PR ref as a full GitHub URL; the seed must derive the canonical
# owner/repo#N form so verify-integrate's tier-1 _pr_number never crashes on
# int(<url>) and the prior_by_ref verdict keys match. An already-canonical ref
# is unchanged.
# ==========================================================================

_PR_URL = "https://github.com/acme/widget/pull/42"


def test_reconcile_ledger_seed_converts_url_ref_to_canonical():
    ledger = {_WO_ID: {"outcome": "opened", "ref": _PR_URL,
                       "acted_at_updated_at": _UPDATED}}
    seed = rt._reconcile_ledger_seed(ledger)
    assert len(seed) == 1, seed
    assert seed[0]["pr_ref"] == "acme/widget#42", seed
    # issue_ref / repo derivation is unchanged.
    assert seed[0]["issue_ref"] == _ISSUE_REF, seed
    assert seed[0]["repo"] == "acme/widget", seed


def test_reconcile_ledger_seed_leaves_canonical_ref_unchanged():
    ledger = {_WO_ID: {"outcome": "opened", "ref": _PR_REF,
                       "acted_at_updated_at": _UPDATED}}
    seed = rt._reconcile_ledger_seed(ledger)
    assert seed[0]["pr_ref"] == _PR_REF, seed


def test_reconcile_ledger_seed_url_ref_none_stays_none():
    ledger = {_WO_ID: {"outcome": "opened", "ref": None,
                       "acted_at_updated_at": _UPDATED}}
    seed = rt._reconcile_ledger_seed(ledger)
    assert seed[0]["pr_ref"] is None, seed


# ==========================================================================
# Durable PRIOR_VERDICTS_KEY snapshot (race-breaker fix): written at tick end,
# EXEMPT from _reset_ephemeral_read_products, verdict pr_refs normalized to
# owner/repo#N. make_reconcile seeds prior_verdicts from THIS snapshot, so a
# PR confirmed CONFLICTING by tick N's VERIFY reaches tick N+1's RECONCILE even
# after the fresh-tick reset wipes the live `verdicts` slot.
# ==========================================================================

def test_prior_verdicts_key_defined_and_distinct_from_verdicts():
    assert rt.PRIOR_VERDICTS_KEY, rt.PRIOR_VERDICTS_KEY
    assert rt.PRIOR_VERDICTS_KEY != rt.VERDICTS_KEY, rt.PRIOR_VERDICTS_KEY
    # It is a durable CROSS-TICK fact, NOT an ephemeral read product.
    assert rt.PRIOR_VERDICTS_KEY not in rt.EPHEMERAL_READ_PRODUCT_DEFAULTS


def test_persisted_prior_verdicts_reads_snapshot_key():
    project_dir = tempfile.mkdtemp(prefix="sched-ppvaccessor-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.PRIOR_VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    assert rt.persisted_prior_verdicts(state_path) == _PRIOR_VERDICTS
    # Absent -> [] (non-breaking default).
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    assert rt.persisted_prior_verdicts(state_path) == []


def test_reset_ephemeral_read_products_does_not_clear_snapshot():
    project_dir = tempfile.mkdtemp(prefix="sched-resetpv-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.VERDICTS_KEY: _PRIOR_VERDICTS,
        rt.PRIOR_VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    rt._reset_ephemeral_read_products(state_path)
    doc = ds.DurableState(state_path).load()
    # The ephemeral live verdicts slot is wiped ...
    assert doc[rt.VERDICTS_KEY] == [], doc
    # ... but the durable prior-verdicts snapshot survives.
    assert doc[rt.PRIOR_VERDICTS_KEY] == _PRIOR_VERDICTS, doc


def test_snapshot_normalizes_pr_refs_to_canonical():
    url_verdicts = [
        {"pr_ref": _PR_URL, "mergeable": "CONFLICTING", "ok": False,
         "reasons": ["merge conflict with base"]},
    ]
    snap = rt._snapshot_prior_verdicts(url_verdicts)
    assert snap[0]["pr_ref"] == "acme/widget#42", snap
    # Non-pr_ref fields preserved verbatim.
    assert snap[0]["mergeable"] == "CONFLICTING", snap
    assert snap[0]["reasons"] == ["merge conflict with base"], snap


def test_tick_end_writes_prior_verdicts_snapshot():
    project_dir = tempfile.mkdtemp(prefix="sched-tickendpv-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    # The RECONCILE route has no VERIFY, so the snapshot is written EMPTY -- but
    # the KEY is written at the tick-end done path either way.
    _run(project_dir, state_path, "auto-merge",
         pr_state={"merged": False, "state": "OPEN"})
    doc = ds.DurableState(state_path).load()
    assert rt.PRIOR_VERDICTS_KEY in doc, doc
    assert doc[rt.PRIOR_VERDICTS_KEY] == [], doc


# ==========================================================================
# Cross-tick e2e: the durable snapshot survives the fresh-tick reset that wipes
# the live `verdicts` slot, so tick N+1's make_reconcile seeds the prior tick's
# CONFLICTING verdict into prior_verdicts even though the live verdicts slot was
# reset to [] before RECONCILE ran.
# ==========================================================================

def _run_capture_prior_verdicts(project_dir, state_path):
    """Run a FULL run_tick over the RECONCILE route with vi.Reconcile replaced by
    a capturing fake, so the run exercises _reset_ephemeral_read_products (the
    fresh-tick wipe) before RECONCILE seeds prior_verdicts. Returns the captured
    prior_verdicts slot."""
    _write_project_route(project_dir, _RECONCILE_ROUTE)
    _write_governance(project_dir, "auto-merge")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    _FakeReconcile.captured = {}
    restore, _caps = _patch_reconcile_seams({"merged": False, "state": "OPEN"})
    saved_reconcile = vi.Reconcile
    vi.Reconcile = _FakeReconcile
    try:
        rt.run_tick(
            project_dir=project_dir, runtime_dir=runtime_dir,
            state_path=state_path, journal_path=journal_path,
            source=_stub_source(), return_run_result=True)
    finally:
        vi.Reconcile = saved_reconcile
        restore()
    return _FakeReconcile.captured.get("prior_verdicts")


def test_snapshot_survives_fresh_reset_reaches_next_tick_reconcile():
    project_dir = tempfile.mkdtemp(prefix="sched-pvcrosstick-")
    state_path = os.path.join(project_dir, ".auto-maintainer",
                              "durable-state.json")
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    # Simulate the state AFTER tick N: the durable snapshot holds tick N's
    # CONFLICTING verdict; the ephemeral live `verdicts` slot holds a decoy that
    # the fresh-tick reset will wipe to [] BEFORE RECONCILE runs.
    decoy = [{"pr_ref": "acme/widget#999", "mergeable": "MERGEABLE",
              "ok": True, "reasons": []}]
    ds.DurableState(state_path).save({
        "schema_version": ds.SCHEMA_VERSION,
        "counter": 0,
        rt.VERDICTS_KEY: decoy,
        rt.PRIOR_VERDICTS_KEY: _PRIOR_VERDICTS,
    })
    captured = _run_capture_prior_verdicts(project_dir, state_path)
    # RECONCILE seeded from the DURABLE snapshot (the prior CONFLICTING verdict),
    # NOT the wiped live verdicts slot and NOT the decoy.
    assert captured == _PRIOR_VERDICTS, captured
