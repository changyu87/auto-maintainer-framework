#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling: wiring VERIFY/REVIEW/INTEGRATE/
CLEANUP under the REDESIGNED loop (FT-C/D, the loop redesign).

This cycle (FT-E) reconciles scheduling with the redesigned verify-integrate
contract so the full close-the-loop route
GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->VERIFY->REVIEW->INTEGRATE->
CLEANUP->PERSIST->EXIT wires by a pure route.json edit (NO code change). It
consumes verify-integrate + work-intake + safety-governance UNCHANGED (DESIGN
§3.7); edits live ONLY in scheduling:

  - VERIFY (verify_integrate.Verify): reads the `cross_cutting_risk` slot (the
    open-PR set is sourced live from gh, injectable), writes the `verdicts` +
    `cross_check` slots, emits OK | EMPTY.
  - REVIEW (verify_integrate.REVIEW): ADVISORY (DESIGN §3.7.7). It reads
    `verdicts` and writes the advisory `review_findings` slot (a list of
    DiscoveredIssue-conforming records). REVIEW is NO LONGER a merge gate.
  - INTEGRATE (verify_integrate.Integrate): a THIN merge. Reads ONLY `verdicts`,
    writes `integration_result`, emits OK. Merges each `ok` verdict's PR WITHOUT
    any review-approval read — the merge rests on IMPLEMENT's deterministic gate
    + VERIFY + merge_guardrails + the trust ladder. Merges ONLY at auto-merge
    (the factory binds the loaded governance `mode`); propose merges nothing
    (NO-OP intent). Consumes sg.permits + sg.merge_guardrails.
  - CLEANUP (verify_integrate.Cleanup): reads `integration_result`, writes
    nothing, emits OK (v1-thin pass-through).

Behaviours exercised here:

  1. DEFAULT_ADAPTER_MAP maps VERIFY/REVIEW/INTEGRATE/CLEANUP to their built-in
     factories (resolvable even though DEFAULT_ROUTE omits them — the
     ports-and-adapters promise).
  2. DEFAULT_ROUTE is unchanged (still the read-and-idle spine).
  3. The factories wrap the verify-integrate states: make_verify ->
     VERIFY_MANIFEST + a verify run; make_review -> REVIEW_MANIFEST + a no-op
     run writing EMPTY review_findings; make_integrate -> INTEGRATE_MANIFEST + an
     integrate run bound to the governance mode; make_cleanup -> CLEANUP_MANIFEST
     + a cleanup run.
  4. The close-the-loop route resolves + validates via build_loop and runs e2e.
  5. mode=propose: an ok verdict is NOT merged (NO-OP intent); the sink is never
     called.
  6. mode=auto-merge: an ok verdict IS merged via the injected sink WITHOUT any
     review approval (REVIEW is advisory); the sink is called once.
  7. verdicts/integration_result/review_findings are #64-style per-tick read
     products: seeded empty when their states are routed.
  8. Pure-script existing routes are byte-identical (the default-route tick path
     is unchanged).

scheduling CONSUMES verify-integrate + work-intake + safety-governance UNCHANGED
via sys.path; it does NOT edit or fork them.

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

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


# A single OPEN, mergeable PR on the default branch — VERIFY derives an `ok`
# verdict for it (mergeable AND base == default_branch; CI recorded, not gating).
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

    def _merge(pr_ref, repo=None, base_branch=None):
        if merge_calls is not None:
            merge_calls.append(pr_ref)
        return {"pr_ref": pr_ref, "url": "", "auto_enabled": False}

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
# VERIFY -> REVIEW -> INTEGRATE -> CLEANUP between PULL and PERSIST. REVIEW is
# now ADVISORY (NOT a merge gate); INTEGRATE merges ok verdicts WITHOUT reading
# review output (the loop redesign, FT-C/D).
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
# Behaviour 1 + 2 — DEFAULT_ADAPTER_MAP maps the four states; DEFAULT_ROUTE
# unchanged.
# --------------------------------------------------------------------------

def test_default_adapter_map_includes_verify_integrate_cleanup():
    amap = rt.DEFAULT_ADAPTER_MAP
    assert "VERIFY" in amap, amap
    assert "REVIEW" in amap, amap
    assert "INTEGRATE" in amap, amap
    assert "CLEANUP" in amap, amap
    assert amap["VERIFY"].split(":")[1] == "make_verify", amap["VERIFY"]
    assert amap["REVIEW"].split(":")[1] == "make_review", amap["REVIEW"]
    assert amap["INTEGRATE"].split(":")[1] == "make_integrate", amap["INTEGRATE"]
    assert amap["CLEANUP"].split(":")[1] == "make_cleanup", amap["CLEANUP"]


def test_default_route_omits_verify_integrate_cleanup():
    route = rt.DEFAULT_ROUTE
    for s in ("VERIFY", "REVIEW", "INTEGRATE", "CLEANUP"):
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


def _seed_verify_ctx(risk_dict=None):
    """A TickContext seeded with the slots VERIFY reads/writes, optionally with a
    `cross_cutting_risk` verdict written (the TRIAGE cross-cutting signal §3.5.9)."""
    ctx = fc.TickContext()
    ctx.register_slot("cross_cutting_risk", {"type": "object"}, version="1.0.0")
    ctx.register_slot("verdicts", {"type": "array"}, version="1.0.0")
    ctx.register_slot("cross_check", {"type": "object"}, version="1.0.0")
    if risk_dict is not None:
        ctx.write("cross_cutting_risk", risk_dict)
    return ctx


def _write_feature_run_py(features_root, feature, returncode):
    """Write a minimal <features_root>/<feature>/test/run.py that exits with
    `returncode` — the real default_complement_runner shells this via subprocess."""
    test_dir = os.path.join(features_root, feature, "test")
    os.makedirs(test_dir, exist_ok=True)
    with open(os.path.join(test_dir, "run.py"), "w") as f:
        f.write("import sys\n")
        f.write("print('all 1 passed' if %d == 0 else '1 failure(s)')\n"
                % returncode)
        f.write("sys.exit(%d)\n" % returncode)


def test_make_verify_unconfigured_features_root_gates_flagged_tick():
    """Unconfigured features_root (the null config default) stays CONSERVATIVE: a
    risk=True tick cannot resolve the at-risk features, so VERIFY gates every
    verdict with the unverifiable reason and records cross_check ran=False."""
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "auto-merge", "features_root": None}}
    restore = _patch_vi_seams(None)
    try:
        manifest, run = rt.make_verify(rti)
        assert manifest is vi.VERIFY_MANIFEST
        ctx = _seed_verify_ctx({"risk": True, "features": ["scheduling"],
                                "reason": "shared route schema"})
        result = run(ctx)
    finally:
        restore()
    # The complement could NOT run; cross_check records ran=False + the reason.
    cc = result.writes["cross_check"]
    assert cc["ran"] is False, cc
    assert cc["reason"] == vi._UNVERIFIABLE_REASON, cc
    # Every verdict is conservatively gated ok=False with that reason.
    verdicts = result.writes["verdicts"]
    assert verdicts and all(not v["ok"] for v in verdicts), verdicts
    assert all(vi._UNVERIFIABLE_REASON in v["reasons"] for v in verdicts), verdicts


def test_make_verify_configured_features_root_runs_real_complement():
    """A configured features_root makes a risk=True tick RUN each named feature's
    run.py (the real subprocess complement) and gate only on a real failure: a
    passing sibling leaves verdicts ok, a failing sibling flips them ok=False."""
    features_root = tempfile.mkdtemp(prefix="sched-feat-root-")
    _write_feature_run_py(features_root, "good-feature", returncode=0)
    _write_feature_run_py(features_root, "bad-feature", returncode=1)
    base_rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
                "source": None, "now": None,
                "governance": {"mode": "auto-merge",
                               "features_root": features_root}}

    restore = _patch_vi_seams(None)
    try:
        # A passing at-risk sibling: the complement RAN and verdicts stay ok.
        _, run = rt.make_verify(base_rti)
        ok_result = run(_seed_verify_ctx(
            {"risk": True, "features": ["good-feature"], "reason": "shared"}))
        # A failing at-risk sibling: the complement RAN and gates every verdict.
        _, run = rt.make_verify(base_rti)
        bad_result = run(_seed_verify_ctx(
            {"risk": True, "features": ["bad-feature"], "reason": "shared"}))
    finally:
        restore()

    ok_cc = ok_result.writes["cross_check"]
    assert ok_cc["ran"] is True, ok_cc
    assert all(r["passed"] for r in ok_cc["results"]), ok_cc
    assert all(v["ok"] for v in ok_result.writes["verdicts"]), ok_result.writes

    bad_cc = bad_result.writes["cross_check"]
    assert bad_cc["ran"] is True, bad_cc
    assert any(not r["passed"] for r in bad_cc["results"]), bad_cc
    bad_verdicts = bad_result.writes["verdicts"]
    assert bad_verdicts and all(not v["ok"] for v in bad_verdicts), bad_verdicts
    assert any("cross-feature break" in reason
               for v in bad_verdicts for reason in v["reasons"]), bad_verdicts


def test_cleanup_factory_wraps_sibling_state():
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "propose"}}
    manifest, run = rt.make_cleanup(rti)
    assert manifest is vi.CLEANUP_MANIFEST
    assert callable(run)


def test_make_review_writes_empty_review_findings():
    """The deterministic default make_review is a no-op ADVISORY reviewer: it
    reads the verdicts slot and writes an EMPTY review_findings list. Because
    REVIEW is ADVISORY (never a merge gate), its no-op ALWAYS continues to
    INTEGRATE — it emits OK (always_ok), not EMPTY. It no longer writes
    review_verdicts."""
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None, "governance": {"mode": "auto-merge"}}
    manifest, run = rt.make_review(rti)
    assert manifest is vi.REVIEW_MANIFEST
    assert list(manifest.writes) == ["review_findings"], manifest.writes
    ctx = fc.TickContext()
    ctx.register_slot("verdicts", {"type": "array"}, version="1.0.0")
    ctx.register_slot("review_findings", {"type": "array"}, version="1.0.0")
    ctx.write("verdicts", [vi.derive_verdict(_OK_PR, _DEFAULT_BRANCH).to_dict()])
    result = run(ctx)
    assert result.signal == "OK", result
    assert result.writes == {"review_findings": []}, result.writes


def test_integrate_factory_binds_governance_mode_no_review_read():
    """make_integrate binds the loaded governance mode so INTEGRATE merges only
    at auto-merge — and merges an ok verdict WITHOUT any review-approval read
    (the loop redesign, FT-C/D: INTEGRATE reads ONLY verdicts)."""
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "auto-merge"}}
    merge_calls = []
    restore = _patch_vi_seams(None, merge_calls=merge_calls)
    try:
        manifest, run = rt.make_integrate(rti)
        assert manifest is vi.INTEGRATE_MANIFEST
        # The manifest reads ONLY verdicts (no review_verdicts coupling).
        assert list(manifest.reads) == ["verdicts"], manifest.reads
        assert callable(run)
        # The bound run, invoked over a ctx with one ok verdict and NO
        # review_verdicts slot at all, merges via the sink at auto-merge.
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
    for s in ("VERIFY", "REVIEW", "INTEGRATE", "CLEANUP"):
        assert s in states, (s, list(states))
    # With the DEFAULT adapter-map, REVIEW resolves to make_review's deterministic
    # no-op (a SCRIPT state, not AgentState — the agent reviewer is wired by an
    # adapter-map override). VERIFY/INTEGRATE/CLEANUP are SCRIPT states too.
    for s in ("VERIFY", "REVIEW", "INTEGRATE", "CLEANUP"):
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
    assert result.path[:10] == ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE",
                                "IMPLEMENT", "VERIFY", "REVIEW", "INTEGRATE",
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
    # The would-merge intent is recorded under skipped (the propose NO-OP).
    assert len(ir.get("skipped", [])) == 1, ir
    assert "propose" in ir["skipped"][0]["reason"], ir


# --------------------------------------------------------------------------
# Behaviour 6 — mode=auto-merge: an ok verdict IS merged via the sink WITHOUT
# any review approval (REVIEW is advisory; INTEGRATE reads only verdicts).
# --------------------------------------------------------------------------

def test_auto_merge_merges_via_sink_without_review_approval():
    project_dir = tempfile.mkdtemp(prefix="sched-vigated-")
    _write_project_route(project_dir, _CLOSE_ROUTE)
    _write_governance(project_dir, "auto-merge")
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
    # The sink was called exactly once for the ok PR — the default make_review
    # writes EMPTY review_findings (advisory only), yet the PR STILL merges.
    assert merge_calls == ["acme/widget#42"], merge_calls
    doc = ds.DurableState(state_path).load()
    ir = doc.get("integration_result", {})
    assert len(ir.get("merged", [])) == 1, ir
    # REVIEW ran but wrote EMPTY review_findings (no model reviewer wired).
    assert doc.get("review_findings", []) == [], doc


# --------------------------------------------------------------------------
# Behaviour 7 — verdicts/integration_result/review_findings seeded as #64
# per-tick read products when their states are routed; review_verdicts is gone.
# --------------------------------------------------------------------------

def test_seed_context_registers_verdicts_review_findings_integration_result():
    project_dir = tempfile.mkdtemp(prefix="sched-viseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _CLOSE_ROUTE)
    slots = ctx.registered_slots()
    assert "verdicts" in slots, slots
    assert "cross_check" in slots, slots
    assert "cross_cutting_risk" in slots, slots
    assert "review_findings" in slots, slots
    assert "integration_result" in slots, slots
    # The retired review_verdicts slot is NO LONGER seeded.
    assert "review_verdicts" not in slots, slots


def test_default_route_does_not_seed_verify_slots():
    project_dir = tempfile.mkdtemp(prefix="sched-vinoseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", rt.DEFAULT_ROUTE)
    slots = ctx.registered_slots()
    for s in ("verdicts", "cross_check", "cross_cutting_risk",
              "review_findings", "integration_result", "review_verdicts"):
        assert s not in slots, (s, slots)


# --------------------------------------------------------------------------
# Behaviour 8 — pure-script default-route tick is byte-identical (unchanged).
# --------------------------------------------------------------------------

def test_default_route_tick_unchanged():
    runtime_dir, state_path, journal_path = _paths()
    sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                      journal_path=journal_path, source=_stub_source())
    assert sig == "idle", sig
    doc = ds.DurableState(state_path).load()
    assert "integration_result" not in doc or doc.get("integration_result") in (
        {}, None), doc


# --------------------------------------------------------------------------
# GATE wiring — the cumulative regression GATE (verify-integrate §2.2 [v2]).
# scheduling wires GATE by (a) mapping GATE -> run_tick:make_gate in
# DEFAULT_ADAPTER_MAP (pre-mapped, resolvable), (b) delegating make_gate to
# verify_integrate.make_gate (reads verdicts, writes gate_results), and (c)
# seeding the gate_results read-product slot EMPTY when GATE is routed. GATE
# only appears in the shipped aggressive route (packaging); the conservative
# code DEFAULT_ROUTE is unchanged.
# --------------------------------------------------------------------------

# The close-the-loop route WITH GATE between REVIEW and INTEGRATE (the cumulative
# regression gate). REVIEW -> GATE -> INTEGRATE (GATE reads verdicts, writes
# gate_results, emits OK).
_GATE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "VERIFY", "REVIEW", "GATE", "INTEGRATE", "CLEANUP", "PERSIST",
               "EXIT", "DONE", "HALTED"],
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
        {"state": "REVIEW", "signal": "OK", "next": "GATE"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "GATE"},
        {"state": "GATE", "signal": "OK", "next": "INTEGRATE"},
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


def test_default_adapter_map_includes_gate():
    """GATE is pre-mapped to run_tick:make_gate (resolvable even though the
    conservative DEFAULT_ROUTE omits it — the ports-and-adapters promise)."""
    amap = rt.DEFAULT_ADAPTER_MAP
    assert "GATE" in amap, amap
    assert amap["GATE"] == "run_tick:make_gate", amap["GATE"]


def test_gate_factory_delegates_to_verify_integrate():
    """make_gate returns (vi.GATE_MANIFEST, callable) — it delegates to
    verify-integrate's cumulative GATE factory (reads verdicts, writes
    gate_results)."""
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None,
           "governance": {"mode": "auto-merge"}}
    restore = _patch_vi_seams(None)
    try:
        manifest, run = rt.make_gate(rti)
    finally:
        restore()
    assert manifest is vi.GATE_MANIFEST, manifest
    assert list(manifest.reads) == ["verdicts"], manifest.reads
    assert list(manifest.writes) == ["gate_results"], manifest.writes
    assert callable(run)


def test_gate_route_resolves_and_validates_via_build_loop():
    """A route REVIEW -> GATE -> INTEGRATE resolves + validates via build_loop
    with NO WiringError; GATE resolves to a SCRIPT state (not AgentState)."""
    runtime = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
               "source": None, "now": None,
               "governance": {"mode": "propose"}}
    route, states = aw.build_loop(
        _GATE_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=rt._INITIAL_SLOTS)
    assert "GATE" in states, list(states)
    assert not isinstance(states["GATE"][1], aw.AgentState), states["GATE"]


def test_seed_context_seeds_gate_results_empty_when_gate_routed():
    """gate_results is REGISTERED and seeded EMPTY ([]) when GATE is routed, so
    INTEGRATE reading it and a GATE-skipped route both stay crash-free."""
    project_dir = tempfile.mkdtemp(prefix="sched-gateseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    ctx = rt._seed_context(state_path, "/tmp/j.jsonl", _GATE_ROUTE)
    slots = ctx.registered_slots()
    assert "gate_results" in slots, slots
    assert ctx.read("gate_results") == [], ctx.read("gate_results")


def test_seed_context_omits_gate_results_when_gate_not_routed():
    """gate_results is NOT seeded when GATE is absent from the route (the
    close-the-loop route without GATE, and the conservative default route)."""
    project_dir = tempfile.mkdtemp(prefix="sched-gatenoseed-")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    state_path = os.path.join(runtime_dir, "durable-state.json")
    ds.DurableState(state_path).save(
        {"schema_version": ds.SCHEMA_VERSION, "counter": 0})
    for route in (_CLOSE_ROUTE, rt.DEFAULT_ROUTE):
        ctx = rt._seed_context(state_path, "/tmp/j.jsonl", route)
        assert "gate_results" not in ctx.registered_slots(), route["states"]


def test_gate_route_runs_end_to_end_auto_merge():
    """The REVIEW -> GATE -> INTEGRATE route runs end-to-end at auto-merge: GATE
    runs between REVIEW and INTEGRATE, gate_results is persisted, and the ok PR
    still merges (GATE reports the partition; it does not block the merge)."""
    project_dir = tempfile.mkdtemp(prefix="sched-gaterun-")
    _write_project_route(project_dir, _GATE_ROUTE)
    _write_governance(project_dir, "auto-merge")
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")

    merge_calls = []
    restore = _patch_vi_seams(None, merge_calls=merge_calls)
    try:
        result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source(), return_run_result=True)
    finally:
        restore()
    assert "GATE" in result.path, result.path
    gate_i = result.path.index("GATE")
    assert result.path[gate_i - 1] == "REVIEW", result.path
    assert result.path[gate_i + 1] == "INTEGRATE", result.path
    assert result.final_state == "DONE", result.path


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
