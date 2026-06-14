#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling: wiring VERIFY/INTEGRATE/CLEANUP.

This cycle WIRES verify-integrate's three act-side CLOSE states into the
route-as-data loop so the full close-the-loop route
GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->VERIFY->INTEGRATE->CLEANUP->
PERSIST->EXIT wires by a pure route.json edit (NO code change). It consumes
verify-integrate + safety-governance UNCHANGED (DESIGN §3.7); edits live ONLY in
scheduling:

  - VERIFY (verify_integrate.Verify): reads NOTHING (open PRs sourced live from
    gh, injectable), writes the `verdicts` slot, emits OK | EMPTY.
  - INTEGRATE (verify_integrate.Integrate): reads `verdicts`, writes
    `integration_result`, emits OK. Merges ONLY at gated-merge — the factory
    binds the loaded governance `mode` so propose merges nothing (NO-OP intent),
    gated-merge merges via the injectable sink. Consumes sg.permits +
    sg.merge_guardrails.
  - CLEANUP (verify_integrate.Cleanup): reads `integration_result`, writes
    nothing, emits OK (v1-thin pass-through).

Behaviours exercised here:

  1. DEFAULT_ADAPTER_MAP maps VERIFY/INTEGRATE/CLEANUP to their built-in
     factories (resolvable even though DEFAULT_ROUTE omits them — the
     ports-and-adapters promise).
  2. DEFAULT_ROUTE is unchanged (still the read-and-idle spine, no VERIFY/
     INTEGRATE/CLEANUP).
  3. The factories wrap the verify-integrate states: make_verify ->
     VERIFY_MANIFEST + a verify run; make_integrate -> INTEGRATE_MANIFEST + an
     integrate run bound to the governance mode; make_cleanup -> CLEANUP_MANIFEST
     + a cleanup run.
  4. The close-the-loop route with VERIFY/INTEGRATE/CLEANUP resolves + validates
     via build_loop and runs end-to-end (data-readiness satisfied: VERIFY writes
     verdicts, INTEGRATE reads verdicts, CLEANUP reads integration_result).
  5. mode=propose: an ok verdict is NOT merged (the would-merge intent is
     recorded in integration_result.skipped); the sink is never called.
  6. mode=gated-merge: an ok verdict IS merged via the injected sink (recorded
     in integration_result.merged); the sink is called once.
  7. verdicts/integration_result are #64-style per-tick read products: seeded
     empty when their states are routed.
  8. Pure-script existing routes are byte-identical (the default-route tick path
     is unchanged — no verdicts/integration_result slots).

scheduling CONSUMES verify-integrate + safety-governance UNCHANGED via sys.path;
it does NOT edit or fork them.

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

import fsm_contracts as fc  # noqa: E402,F401
import durable_state as ds  # noqa: E402,F401
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
    "labels": [{"name": "bug"}, {"name": "p1"}],
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


# A single OPEN, mergeable PR on the default branch — VERIFY derives an `ok`
# verdict for it (CI passing AND mergeable AND base == default_branch).
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


def _patch_vi_seams(monkeypatch_state, open_prs=None, merge_calls=None):
    """Override the verify-integrate gh module seams so VERIFY/INTEGRATE touch no
    network. Verify/Integrate default their source/sink/default_branch to these
    module-level functions, and the factories construct them with no args, so
    patching the module seam is the injection point. Returns a restore callable."""
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
        return {"pr_ref": pr_ref, "url": ""}

    vi.gh_open_pr_source = _open
    vi.gh_default_branch_source = _branch
    vi.gh_pr_merge_sink = _merge

    def restore():
        vi.gh_open_pr_source = saved["open"]
        vi.gh_default_branch_source = saved["branch"]
        vi.gh_pr_merge_sink = saved["merge"]
    return restore


def _paths():
    root = tempfile.mkdtemp(prefix="sched-vi-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# The full close-the-loop override route: TRIAGE -> PRIORITIZE -> IMPLEMENT ->
# VERIFY -> INTEGRATE -> CLEANUP between PULL and PERSIST.
_CLOSE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "VERIFY", "INTEGRATE", "CLEANUP", "PERSIST", "EXIT",
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
        {"state": "VERIFY", "signal": "OK", "next": "INTEGRATE"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "INTEGRATE"},
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


# --------------------------------------------------------------------------
# Behaviour 1 + 2 — DEFAULT_ADAPTER_MAP maps the three states; DEFAULT_ROUTE
# unchanged.
# --------------------------------------------------------------------------

def test_default_adapter_map_includes_verify_integrate_cleanup():
    amap = rt.DEFAULT_ADAPTER_MAP
    assert "VERIFY" in amap, amap
    assert "INTEGRATE" in amap, amap
    assert "CLEANUP" in amap, amap
    assert amap["VERIFY"].split(":")[1] == "make_verify", amap["VERIFY"]
    assert amap["INTEGRATE"].split(":")[1] == "make_integrate", amap["INTEGRATE"]
    assert amap["CLEANUP"].split(":")[1] == "make_cleanup", amap["CLEANUP"]


def test_default_route_omits_verify_integrate_cleanup():
    route = rt.DEFAULT_ROUTE
    for s in ("VERIFY", "INTEGRATE", "CLEANUP"):
        assert s not in route["states"], route["states"]


# --------------------------------------------------------------------------
# Behaviour 3 — the factories wrap the verify-integrate states.
# --------------------------------------------------------------------------

def test_verify_factory_wraps_sibling_state():
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "propose"}}
    manifest, run = rt.make_verify(rti)
    assert manifest is vi.VERIFY_MANIFEST
    assert callable(run)


def test_cleanup_factory_wraps_sibling_state():
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "propose"}}
    manifest, run = rt.make_cleanup(rti)
    assert manifest is vi.CLEANUP_MANIFEST
    assert callable(run)


def test_integrate_factory_binds_governance_mode():
    """make_integrate binds the loaded governance mode so INTEGRATE merges only
    at gated-merge."""
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "gated-merge"}}
    manifest, run = rt.make_integrate(rti)
    assert manifest is vi.INTEGRATE_MANIFEST
    assert callable(run)
    # The bound run, invoked over a ctx with one ok verdict, merges via the sink
    # at gated-merge (mode binding proven through behaviour).
    merge_calls = []
    restore = _patch_vi_seams(None, merge_calls=merge_calls)
    try:
        ctx = fc.TickContext()
        ctx.register_slot("verdicts", {"type": "array"}, version="1.0.0")
        ctx.register_slot("integration_result", {"type": "object"},
                          version="1.0.0")
        ctx.write("verdicts", [vi.derive_verdict(_OK_PR, _DEFAULT_BRANCH)
                               .to_dict()])
        result = run(ctx)
    finally:
        restore()
    assert result.signal == "OK", result
    ir = result.writes["integration_result"]
    assert len(ir["merged"]) == 1, ir
    assert merge_calls == ["acme/widget#42"], merge_calls


# --------------------------------------------------------------------------
# Behaviour 4 — the close-the-loop route resolves + validates + runs e2e.
# --------------------------------------------------------------------------

def test_close_route_resolves_and_validates_via_build_loop():
    runtime = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
               "source": None, "now": None,
               "governance": {"mode": "propose"}}
    route, states = aw.build_loop(
        _CLOSE_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=rt._INITIAL_SLOTS)
    for s in ("VERIFY", "INTEGRATE", "CLEANUP"):
        assert s in states, (s, list(states))
    # VERIFY/INTEGRATE/CLEANUP are SCRIPT states (run callables, not AgentState).
    for s in ("VERIFY", "INTEGRATE", "CLEANUP"):
        assert not isinstance(states[s][1], aw.AgentState), s


def test_close_route_runs_end_to_end_propose():
    project_dir = tempfile.mkdtemp(prefix="sched-viproj-")
    _write_project_route(project_dir, _CLOSE_ROUTE)
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    restore = _patch_vi_seams(None)
    try:
        result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), return_run_result=True)
    finally:
        restore()
    assert result.path[:9] == ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                               "IMPLEMENT", "VERIFY", "INTEGRATE",
                               "CLEANUP"], result.path
    assert result.final_state == "DONE", result.path


# --------------------------------------------------------------------------
# Behaviour 5 — mode=propose: an ok verdict is NOT merged (NO-OP intent).
# --------------------------------------------------------------------------

def test_propose_does_not_merge():
    project_dir = tempfile.mkdtemp(prefix="sched-vipropose-")
    _write_project_route(project_dir, _CLOSE_ROUTE)
    _write_governance(project_dir, "propose")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    merge_calls = []
    restore = _patch_vi_seams(None, merge_calls=merge_calls)
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    finally:
        restore()
    # The sink was NEVER called (no merge at propose).
    assert merge_calls == [], merge_calls
    ir = ds.DurableState(state_path).load().get("integration_result", {})
    assert ir.get("merged", []) == [], ir
    # The would-merge intent is recorded under skipped.
    assert len(ir.get("skipped", [])) == 1, ir


# --------------------------------------------------------------------------
# Behaviour 6 — mode=gated-merge: an ok verdict IS merged via the sink.
# --------------------------------------------------------------------------

def test_gated_merge_merges_via_sink():
    project_dir = tempfile.mkdtemp(prefix="sched-vigated-")
    _write_project_route(project_dir, _CLOSE_ROUTE)
    _write_governance(project_dir, "gated-merge")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    merge_calls = []
    restore = _patch_vi_seams(None, merge_calls=merge_calls)
    try:
        rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                    state_path=state_path, journal_path=journal_path,
                    source=_stub_source())
    finally:
        restore()
    # The sink was called exactly once for the ok PR.
    assert merge_calls == ["acme/widget#42"], merge_calls
    ir = ds.DurableState(state_path).load().get("integration_result", {})
    assert len(ir.get("merged", [])) == 1, ir


# --------------------------------------------------------------------------
# Behaviour 7 — verdicts/integration_result seeded as #64 per-tick read products
# when their states are routed.
# --------------------------------------------------------------------------

def test_seed_context_registers_verdicts_and_integration_result_when_routed():
    project_dir = tempfile.mkdtemp(prefix="sched-viseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _CLOSE_ROUTE)
    assert "verdicts" in ctx.registered_slots(), ctx.registered_slots()
    assert "integration_result" in ctx.registered_slots(), ctx.registered_slots()


def test_default_route_does_not_seed_verdicts():
    project_dir = tempfile.mkdtemp(prefix="sched-vinoseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", rt.DEFAULT_ROUTE)
    assert "verdicts" not in ctx.registered_slots(), ctx.registered_slots()
    assert "integration_result" not in ctx.registered_slots(), \
        ctx.registered_slots()


# --------------------------------------------------------------------------
# Behaviour 8 — pure-script default-route tick is byte-identical (unchanged).
# --------------------------------------------------------------------------

def test_default_route_tick_unchanged():
    runtime_dir, state_path, journal_path = _paths()
    sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                      journal_path=journal_path, source=_stub_source())
    assert sig == "idle", sig
    # No verdicts / integration_result persisted on the default route.
    doc = ds.DurableState(state_path).load()
    assert "integration_result" not in doc or doc.get("integration_result") in (
        {}, None), doc
