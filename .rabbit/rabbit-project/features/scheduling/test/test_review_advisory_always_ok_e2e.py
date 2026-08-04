#!/usr/bin/env python3
"""End-to-end conformance tests for the advisory-REVIEW always_ok fix.

DOGFOOD BUG: PRs the loop opens are reviewed but never merged. REVIEW became
ADVISORY in the loop redesign (FT-C/D) — it files findings as issues, it is NOT a
merge gate — but its ``AGENT_PORT_TEMPLATES['REVIEW']`` ``signal_rule`` was still
``nonempty_else_empty`` (left over from the gate era). A clean PR yields ZERO
``review_findings`` -> the rule emits EMPTY -> a route edge
``REVIEW--EMPTY-->PERSIST`` SKIPS INTEGRATE entirely, so ``integration_result``
stays empty every tick and mergeable PRs are never merged.

An advisory state must NEVER branch flow on its findings: REVIEW must ALWAYS
continue to INTEGRATE. The fix sets the REVIEW template ``signal_rule`` to
``always_ok`` (the rule already exists in ``agent_dispatch.compute_signal`` and
returns ``"OK"``) and flips ``make_review``'s deterministic no-op ``_run`` to
emit ``OK`` (was ``EMPTY``) while still writing an EMPTY ``review_findings`` list.

REVIEW's declared emits stay ``[OK, EMPTY]`` (verify-integrate's REVIEW_MANIFEST,
UNCHANGED) so seeded routes that still carry a ``REVIEW--EMPTY-->PERSIST`` edge
keep passing ``validate_signals`` — the EMPTY edge becomes valid-but-dead.

Behaviours exercised (every one has a test; the dogfood case is the e2e per the
E2E TEST RULE):

  1. AGENT_PORT_TEMPLATES['REVIEW']['signal_rule'] == 'always_ok'.
  2. The REVIEW agent dispatch resume computes signal 'OK' when review_findings
     is EMPTY (the dogfood case) AND when non-empty (always_ok).
  3. make_review's deterministic no-op emits 'OK' (not 'EMPTY') and writes
     review_findings [].
  4. A migrated REVIEW entry carries signal_rule == 'always_ok'.
  5. e2e: an agent tick over ...VERIFY->REVIEW->INTEGRATE with open mergeable
     PRs and ZERO review findings REACHES INTEGRATE and merges (the merge sink is
     called / integration_result.merged non-empty), NOT bypassed to PERSIST.

scheduling CONSUMES verify-integrate + work-intake + safety-governance +
agent-dispatch UNCHANGED via sys.path; it does NOT edit or fork them.

Owner: changyu87
"""

import contextlib
import copy
import io
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

import fsm_contracts as fc  # noqa: E402
import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import agent_dispatch as ad  # noqa: E402
import verify_integrate as vi  # noqa: E402
import run_tick as rt  # noqa: E402
import adapter_map_config as amc  # noqa: E402


# A concrete review_findings record (the ADVISORY shape REVIEW produces) used to
# prove always_ok also yields OK for a NON-empty review.
_FINDING_EXAMPLE = {
    "schema_version": vi.REVIEW_FINDINGS_SCHEMA_VERSION,
    "title": "Consider adding a test",
    "body": "The new branch is untested.",
    "dedup_key": "review:pr-42:untested",
    "kind": "task",
    "filed_by": "auto-maintainer-reviewer",
    "target": "project",
    "pr_ref": "acme/widget#42",
}


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


# A single OPEN, mergeable PR on the default branch — VERIFY derives an `ok`
# verdict for it (mergeable AND base == default_branch).
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


def _patch_vi_seams(open_prs=None, merge_calls=None):
    """Override the verify-integrate gh module seams so VERIFY/INTEGRATE touch no
    network. Returns a restore callable."""
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


# ==========================================================================
# Behaviour 1 — the template signal_rule is always_ok.
# ==========================================================================

def test_review_template_signal_rule_is_always_ok():
    assert amc.AGENT_PORT_TEMPLATES["REVIEW"]["signal_rule"] == "always_ok", \
        amc.AGENT_PORT_TEMPLATES["REVIEW"]


def test_review_template_emits_unchanged_ok_empty():
    """The declared emits stay [OK, EMPTY] (verify-integrate REVIEW_MANIFEST,
    UNCHANGED) so a seeded route with a REVIEW--EMPTY-->PERSIST edge still
    validates; the EMPTY edge is valid-but-dead."""
    assert amc.AGENT_PORT_TEMPLATES["REVIEW"]["emits"] == ["OK", "EMPTY"], \
        amc.AGENT_PORT_TEMPLATES["REVIEW"]


# ==========================================================================
# Behaviour 2 — the REVIEW agent dispatch resume computes OK for EMPTY and
# non-empty review_findings (always_ok). This is the per-dispatch signal rule
# the runner applies on resume (run_tick passes entry["signal"]["rule"]).
# ==========================================================================

def test_review_agent_signal_ok_for_empty_findings():
    """The dogfood case: a clean review (zero findings) computes OK, not EMPTY,
    so the route continues to INTEGRATE rather than branching to PERSIST."""
    entry = amc._build_agent_entry("REVIEW", "auto-maintainer-reviewer")
    rule = entry["signal"]["rule"]
    assert rule == "always_ok", entry
    # An EMPTY review_findings slot value -> OK (not EMPTY).
    assert ad.compute_signal(rule, []) == "OK"


def test_review_agent_signal_ok_for_nonempty_findings():
    entry = amc._build_agent_entry("REVIEW", "auto-maintainer-reviewer")
    rule = entry["signal"]["rule"]
    assert ad.compute_signal(rule, [_FINDING_EXAMPLE]) == "OK"


# ==========================================================================
# Behaviour 3 — make_review's deterministic no-op emits OK (not EMPTY) and
# writes review_findings [].
# ==========================================================================

def test_make_review_noop_emits_ok_and_writes_empty_findings():
    rti = {"project_dir": "/tmp/x", "runtime_dir": "/tmp/x/runtime",
           "source": None, "now": None, "governance": {"mode": "auto-merge"}}
    manifest, run = rt.make_review(rti)
    assert manifest is vi.REVIEW_MANIFEST
    ctx = fc.TickContext()
    ctx.register_slot("verdicts", {"type": "array"}, version="1.0.0")
    ctx.register_slot("review_findings", {"type": "array"}, version="1.0.0")
    ctx.write("verdicts", [vi.derive_verdict(_OK_PR, _DEFAULT_BRANCH).to_dict()])
    result = run(ctx)
    # The advisory no-op now ALWAYS continues to INTEGRATE: signal OK, not EMPTY.
    assert result.signal == "OK", result
    assert result.writes == {"review_findings": []}, result.writes


# ==========================================================================
# Behaviour 4 — a migrated REVIEW entry carries signal_rule always_ok.
# ==========================================================================

_STALE_REVIEW = {
    "kind": "agent",
    "manifest": {"reads": ["verdicts"], "writes": ["review_verdicts"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "my-reviewer",
            "inputs": ["verdicts"],
            "writes": "review_verdicts",
            "cardinality": "once",
            "output_example": [{"approved": True, "severity": "none"}],
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}


def test_migrated_review_entry_carries_always_ok():
    amap = {"REVIEW": copy.deepcopy(_STALE_REVIEW)}
    healed = amc.migrate_known_port_entries(amap)["REVIEW"]
    assert healed["signal"]["rule"] == "always_ok", healed


# ==========================================================================
# Behaviour 5 — the DOGFOOD e2e: an AGENT REVIEW with ZERO findings REACHES
# INTEGRATE and merges, instead of bypassing to PERSIST on the EMPTY edge.
# ==========================================================================

# An AGENT REVIEW entry built from the live template (so it carries always_ok).
def _agent_review_entry():
    return amc._build_agent_entry("REVIEW", "auto-maintainer-reviewer")


# The close-the-loop route with an EMPTY-bypass edge present (valid-but-dead):
# REVIEW--OK-->INTEGRATE and REVIEW--EMPTY-->PERSIST. With always_ok the EMPTY
# edge is never taken, so a clean review ALWAYS reaches INTEGRATE.
_DOGFOOD_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "VERIFY", "REVIEW", "INTEGRATE",
               "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "VERIFY"},
        {"state": "PULL", "signal": "EMPTY", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "REVIEW"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "REVIEW"},
        # The advisory REVIEW: OK -> INTEGRATE (always taken); EMPTY -> PERSIST
        # (valid-but-dead, kept for back-compat validate_signals).
        {"state": "REVIEW", "signal": "OK", "next": "INTEGRATE"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "INTEGRATE", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def _setup_dogfood_project():
    project_dir = tempfile.mkdtemp(prefix="sched-review-dogfood-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_DOGFOOD_ROUTE, f)
    with open(os.path.join(cfg, "governance.json"), "w") as f:
        json.dump({"mode": "auto-merge"}, f)
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["REVIEW"] = _agent_review_entry()
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, cfg, state_path, journal_path


def _write_outputs(paused, contents):
    dispatches = paused["dispatches"]
    assert len(dispatches) == len(contents), (len(dispatches), len(contents))
    for d, content in zip(dispatches, contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def test_agent_review_zero_findings_reaches_integrate_and_merges():
    """The DOGFOOD bug fixed: an agent REVIEW that produces ZERO findings emits
    OK (always_ok), so the tick flows REVIEW->INTEGRATE and the mergeable PR is
    merged — instead of branching REVIEW--EMPTY-->PERSIST and never merging."""
    project_dir, runtime_dir, state_path, journal_path = _setup_dogfood_project()
    merge_calls = []
    restore = _patch_vi_seams(merge_calls=merge_calls)
    try:
        # Fresh step: runs GUARD..VERIFY (script) and PAUSES at the agent REVIEW.
        paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                             state_path=state_path, journal_path=journal_path,
                             source=_stub_source())
        assert isinstance(paused, dict), paused
        assert paused["status"] == "paused", paused
        assert paused["state"] == "REVIEW", paused
        d = paused["dispatches"][0]
        assert d["writes"] == "review_findings", d
        assert d["signal_rule"] == "always_ok", d

        # The clean reviewer writes ZERO findings (an empty JSON array).
        _write_outputs(paused, [json.dumps([])])

        # Resume: the EMPTY review_findings computes OK (always_ok), so the route
        # continues to INTEGRATE (NOT the dead EMPTY->PERSIST edge), merges the
        # PR, and reaches DONE.
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            sig = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                              state_path=state_path, journal_path=journal_path,
                              source=_stub_source(), resume=True,
                              return_run_result=True)
    finally:
        restore()

    # The tick visited INTEGRATE (NOT bypassed to PERSIST on the EMPTY edge).
    assert "INTEGRATE" in sig.path, sig.path
    assert sig.final_state == "DONE", sig.path
    # The merge sink WAS called for the mergeable PR — the PR actually merges.
    assert merge_calls == ["acme/widget#42"], merge_calls
    doc = ds.DurableState(state_path).load()
    ir = doc.get("integration_result", {})
    assert len(ir.get("merged", [])) == 1, ir
    # REVIEW ran but produced ZERO findings (advisory, clean review).
    assert doc.get("review_findings", []) == [], doc


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
