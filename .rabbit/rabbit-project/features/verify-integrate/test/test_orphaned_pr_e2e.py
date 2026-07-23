#!/usr/bin/env python3
"""End-to-end + unit tests for the orphaned-loop-PR convergence path.

An orphaned loop PR is one whose DRIVER ISSUE is CLOSED: the work is resolved or
abandoned, so the PR will never merge and must be CLOSED (not merged, not left to
linger open forever refiring the loop). VERIFY resolves orphan-status per PR via
an INJECTABLE resolver (production: gh_closing_issue_state — a READ; VERIFY stays
read-only w.r.t. GitHub) and forces such a verdict ok=False + orphaned=True.
INTEGRATE, as the FIRST per-verdict disposition, CLOSES an orphaned PR via an
INJECTABLE close sink (production: gh pr close --delete-branch --comment) —
trust-gated on permits('merge', mode) exactly like merge: only at auto-merge does
it actually close; at propose/dry-run the would-close intent is recorded under
skipped. A close-sink fault is recorded under errors (the tick is never wedged).

These tests drive VERIFY/INTEGRATE with FAKE resolvers/sinks (no network),
exactly as tick-orchestrator will — a real fsm-contracts TickContext, the
registered slots, and the StateResult committed through fc.apply_result.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
_FSM_SRC = os.path.join(_FEATURES_DIR, "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)
_SG_SRC = os.path.join(_FEATURES_DIR, "safety-governance", "src")
if _SG_SRC not in sys.path:
    sys.path.insert(0, _SG_SRC)
_LD_SRC = os.path.join(_FEATURES_DIR, "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402


_DEFAULT_BRANCH = "main"


# --------------------------------------------------------------------------
# Fixtures.
# --------------------------------------------------------------------------

def _pr(number=1, base=_DEFAULT_BRANCH, mergeable="MERGEABLE"):
    return {
        "number": number,
        "url": f"https://github.com/acme/widget/pull/{number}",
        "headRefName": f"auto-maintainer/fix-{number}",
        "baseRefName": base,
        "mergeable": mergeable,
        "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
    }


def _verify_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
                      version=vi.VERDICTS_SLOT["version"])
    ctx.register_slot(vi.CROSS_CHECK_SLOT["name"], vi.CROSS_CHECK_SLOT["schema"],
                      version=vi.CROSS_CHECK_SLOT["version"])
    return ctx


def _integrate_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
                      version=vi.VERDICTS_SLOT["version"])
    ctx.register_slot(vi.INTEGRATION_RESULT_SLOT["name"],
                      vi.INTEGRATION_RESULT_SLOT["schema"],
                      version=vi.INTEGRATION_RESULT_SLOT["version"])
    return ctx


def _verdict(number=1, ok=True, orphaned=False, base=_DEFAULT_BRANCH,
             mergeable=True):
    v = vi.Verdict(
        pr_ref=f"acme/widget#{number}",
        url=f"https://github.com/acme/widget/pull/{number}",
        ok=ok,
        ci_state="passing",
        mergeable=mergeable,
        base=base,
        reasons=[],
        orphaned=orphaned,
    )
    return v.to_dict()


def _run_integrate(integrate, ctx):
    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)
    return result, ctx.read("integration_result")


# ==========================================================================
# Behaviour (g): the Verdict schema carries `orphaned` and round-trips through
# to_dict/from_dict; the schema version was bumped for the additive field.
# ==========================================================================

def test_verdict_orphaned_round_trip():
    v = vi.Verdict(
        pr_ref="acme/widget#7",
        url="https://github.com/acme/widget/pull/7",
        ok=False,
        ci_state="passing",
        mergeable=True,
        base="main",
        reasons=["driver issue is closed"],
        orphaned=True,
    )
    d = v.to_dict()
    assert d["orphaned"] is True
    assert vi.Verdict.from_dict(d) == v


def test_verdict_orphaned_defaults_false():
    v = vi.Verdict(pr_ref="acme/widget#1", url="", ok=True,
                   ci_state="passing", mergeable=True, base="main")
    assert v.orphaned is False
    assert v.to_dict()["orphaned"] is False


def test_verdict_from_dict_back_compat_without_orphaned():
    """An older persisted verdict dict with no `orphaned` key defaults to False
    (back-compat, additive optional field)."""
    d = {
        "schema_version": "1.0.0",
        "pr_ref": "acme/widget#1",
        "url": "",
        "ok": True,
        "ci_state": "passing",
        "mergeable": True,
        "base": "main",
        "reasons": [],
    }
    v = vi.Verdict.from_dict(d)
    assert v.orphaned is False


def test_verdict_schema_version_bumped_for_orphaned():
    """The additive orphaned field bumped VERDICT_SCHEMA_VERSION past 1.0.0."""
    assert vi.VERDICT_SCHEMA_VERSION != "1.0.0"


# ==========================================================================
# Behaviour (g): IntegrationResult carries `closed_orphaned` and round-trips;
# the schema version was bumped for the additive field.
# ==========================================================================

def test_integration_result_closed_orphaned_round_trip():
    r = vi.IntegrationResult(
        closed_orphaned=[{"pr_ref": "acme/widget#1", "issue_ref": "acme/widget#5"}],
    )
    d = r.to_dict()
    assert d["closed_orphaned"] == [
        {"pr_ref": "acme/widget#1", "issue_ref": "acme/widget#5"}]
    assert vi.IntegrationResult.from_dict(d) == r


def test_integration_result_closed_orphaned_defaults_empty():
    d = vi.IntegrationResult().to_dict()
    assert d["closed_orphaned"] == []


def test_integration_result_schema_version_bumped_for_closed_orphaned():
    assert vi.INTEGRATION_RESULT_SCHEMA_VERSION != "1.0.0"


# ==========================================================================
# Behaviour (determinism seam): gh_closing_issue_state resolves the closing
# issue number then shells `gh issue view <n> --json state` via an INJECTED fake
# runner — NO network — returning the state string. No closing issue -> None.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_gh_closing_issue_state_returns_state():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if "pr" in cmd and "view" in cmd:
            return _FakeCompleted('[{"number": 42}]')
        # gh issue view <n> --json state
        return _FakeCompleted("CLOSED\n")

    state = vi.gh_closing_issue_state("acme/widget#7", repo="acme/widget",
                                      runner=fake_runner)
    assert state == "CLOSED"
    # the second command is the gh issue view for the resolved issue number.
    issue_cmd = cmds[-1]
    assert issue_cmd[:3] == ["gh", "issue", "view"]
    assert "42" in issue_cmd
    assert "--repo" in issue_cmd
    assert issue_cmd[issue_cmd.index("--repo") + 1] == "acme/widget"


def test_gh_closing_issue_state_open():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        if "pr" in cmd and "view" in cmd:
            return _FakeCompleted('[{"number": 3}]')
        return _FakeCompleted("OPEN\n")

    assert vi.gh_closing_issue_state("#7", runner=fake_runner) == "OPEN"


def test_gh_closing_issue_state_no_closing_issue_returns_none():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted("[]")

    assert vi.gh_closing_issue_state("#7", runner=fake_runner) is None


# ==========================================================================
# Behaviour (a): VERIFY marks a verdict orphaned=True + ok=False (with a reason)
# when the injected resolver returns 'CLOSED'. VERIFY stays read-only.
# ==========================================================================

def test_verify_marks_orphaned_when_resolver_returns_closed():
    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    def closed_resolver(pr_ref, repo=None):  # noqa: ARG001
        return "CLOSED"

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       orphan_resolver=closed_resolver)
    ctx = _verify_ctx()
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))

    v = ctx.read("verdicts")[0]
    assert v["orphaned"] is True
    assert v["ok"] is False
    joined = " ".join(v["reasons"]).lower()
    assert "orphaned" in joined
    assert "closed" in joined


# ==========================================================================
# Behaviour (b): VERIFY leaves orphaned=False when the resolver returns 'OPEN',
# None, or RAISES (conservative — never orphaned on uncertainty).
# ==========================================================================

def test_verify_open_issue_not_orphaned():
    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       orphan_resolver=lambda pr_ref, repo=None: "OPEN")
    ctx = _verify_ctx()
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))
    v = ctx.read("verdicts")[0]
    assert v["orphaned"] is False
    assert v["ok"] is True


def test_verify_no_closing_issue_not_orphaned():
    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       orphan_resolver=lambda pr_ref, repo=None: None)
    ctx = _verify_ctx()
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))
    v = ctx.read("verdicts")[0]
    assert v["orphaned"] is False
    assert v["ok"] is True


def test_verify_resolver_raises_is_conservative_not_orphaned():
    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    def boom(pr_ref, repo=None):  # noqa: ARG001
        raise RuntimeError("gh not authenticated")

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       orphan_resolver=boom)
    ctx = _verify_ctx()
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))
    v = ctx.read("verdicts")[0]
    assert v["orphaned"] is False
    assert v["ok"] is True


# ==========================================================================
# Behaviour: VERIFY defaults orphan_resolver to the production
# gh_closing_issue_state (no scheduling change — construction without the new
# param keeps working).
# ==========================================================================

def test_verify_orphan_resolver_defaults_to_production_fn():
    verify = vi.Verify(source=lambda repo=None, label=None: [],
                       default_branch=_DEFAULT_BRANCH)
    assert verify._orphan_resolver is vi.gh_closing_issue_state


# ==========================================================================
# Behaviour (determinism seam): gh_pr_close_sink shells `gh pr close <n>
# --delete-branch --comment <body>` via an INJECTED fake runner (no network),
# with --repo when given and check=True.
# ==========================================================================

def test_gh_pr_close_sink_assembles_command():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted("")

    vi.gh_pr_close_sink("acme/widget#9", repo="acme/widget", runner=fake_runner)
    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "pr", "close"]
    assert "9" in cmd
    assert "--delete-branch" in cmd
    assert "--comment" in cmd
    assert "--repo" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme/widget"
    assert captured["kwargs"].get("check") is True


def test_gh_pr_close_sink_omits_repo_when_unset():
    captured = {}

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        captured["cmd"] = cmd
        return _FakeCompleted("")

    vi.gh_pr_close_sink("#9", runner=fake_runner)
    assert "--repo" not in captured["cmd"]


# ==========================================================================
# Behaviour (c): INTEGRATE at auto-merge CLOSES an orphaned verdict via the
# close sink, records it under closed_orphaned, and does NOT merge it.
# ==========================================================================

def test_integrate_auto_merge_closes_orphaned_pr():
    closed = []

    def close_sink(pr_ref, repo=None):  # noqa: ARG001
        closed.append(pr_ref)

    merged = []

    def merge_sink(pr_ref, repo=None, auto=False):  # noqa: ARG001
        merged.append(pr_ref)
        return {"pr_ref": pr_ref, "url": ""}

    integrate = vi.Integrate(mode="auto-merge", merge_sink=merge_sink,
                             default_branch=_DEFAULT_BRANCH,
                             close_sink=close_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=1, ok=False, orphaned=True)])

    _, res = _run_integrate(integrate, ctx)
    assert closed == ["acme/widget#1"]
    assert merged == []
    assert res["merged"] == []
    assert len(res["closed_orphaned"]) == 1
    assert res["closed_orphaned"][0]["pr_ref"] == "acme/widget#1"
    assert "issue_ref" in res["closed_orphaned"][0]


# ==========================================================================
# Behaviour (d): INTEGRATE at propose/dry-run does NOT call the close sink; the
# would-close intent is recorded under skipped (a human closes it).
# ==========================================================================

def test_integrate_propose_records_would_close_under_skipped():
    closed = []

    def close_sink(pr_ref, repo=None):  # noqa: ARG001
        closed.append(pr_ref)

    integrate = vi.Integrate(mode="propose", default_branch=_DEFAULT_BRANCH,
                             close_sink=close_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=2, ok=False, orphaned=True)])

    _, res = _run_integrate(integrate, ctx)
    assert closed == []
    assert res["closed_orphaned"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["pr_ref"] == "acme/widget#2"
    assert "orphaned" in res["skipped"][0]["reason"].lower()


def test_integrate_dry_run_records_would_close_under_skipped():
    closed = []
    integrate = vi.Integrate(
        mode="dry-run", default_branch=_DEFAULT_BRANCH,
        close_sink=lambda pr_ref, repo=None: closed.append(pr_ref))
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=3, ok=False, orphaned=True)])
    _, res = _run_integrate(integrate, ctx)
    assert closed == []
    assert res["closed_orphaned"] == []
    assert len(res["skipped"]) == 1


# ==========================================================================
# Behaviour (e): a close-sink FAULT is recorded under errors and does NOT wedge
# the tick (the run still completes with signal OK).
# ==========================================================================

def test_integrate_close_sink_fault_records_error_not_wedged():
    def boom(pr_ref, repo=None):  # noqa: ARG001
        raise RuntimeError("gh pr close failed: protected")

    integrate = vi.Integrate(mode="auto-merge", default_branch=_DEFAULT_BRANCH,
                             close_sink=boom)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=4, ok=False, orphaned=True)])

    result, res = _run_integrate(integrate, ctx)
    assert result.signal == "OK"
    assert res["closed_orphaned"] == []
    assert len(res["errors"]) == 1
    assert res["errors"][0]["pr_ref"] == "acme/widget#4"
    assert "gh pr close failed" in res["errors"][0]["reason"]


# ==========================================================================
# Behaviour (f): a non-orphaned ok verdict still MERGES as before (no
# regression) — the orphan disposition only intercepts orphaned verdicts.
# ==========================================================================

def test_integrate_non_orphaned_ok_verdict_still_merges():
    merged = []
    closed = []

    def merge_sink(pr_ref, repo=None, auto=False):  # noqa: ARG001
        merged.append(pr_ref)
        return {"pr_ref": pr_ref, "url": ""}

    integrate = vi.Integrate(
        mode="auto-merge", merge_sink=merge_sink,
        default_branch=_DEFAULT_BRANCH,
        close_sink=lambda pr_ref, repo=None: closed.append(pr_ref))
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=5, ok=True, orphaned=False)])

    _, res = _run_integrate(integrate, ctx)
    assert merged == ["acme/widget#5"]
    assert closed == []
    assert res["closed_orphaned"] == []
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#5"]


# ==========================================================================
# Behaviour: INTEGRATE close_sink defaults to the production gh_pr_close_sink
# (no scheduling change — construction without the new param keeps working).
# ==========================================================================

def test_integrate_close_sink_defaults_to_production_fn():
    integrate = vi.Integrate(mode="auto-merge", default_branch=_DEFAULT_BRANCH)
    assert integrate._close_sink is vi.gh_pr_close_sink


# ==========================================================================
# Behaviour: a mixed batch at auto-merge — one orphaned (closed, not merged),
# one ok non-orphaned (merged) — partitions correctly and the orphaned PR is
# never merged.
# ==========================================================================

def test_integrate_mixed_orphaned_and_ok_batch():
    merged = []
    closed = []
    integrate = vi.Integrate(
        mode="auto-merge",
        merge_sink=lambda pr_ref, repo=None, auto=False: (
            merged.append(pr_ref) or {"pr_ref": pr_ref, "url": ""}),
        default_branch=_DEFAULT_BRANCH,
        close_sink=lambda pr_ref, repo=None: closed.append(pr_ref))
    ctx = _integrate_ctx()
    ctx.write("verdicts", [
        _verdict(number=1, ok=False, orphaned=True),
        _verdict(number=2, ok=True, orphaned=False),
    ])

    _, res = _run_integrate(integrate, ctx)
    assert closed == ["acme/widget#1"]
    assert merged == ["acme/widget#2"]
    assert [c["pr_ref"] for c in res["closed_orphaned"]] == ["acme/widget#1"]
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#2"]
