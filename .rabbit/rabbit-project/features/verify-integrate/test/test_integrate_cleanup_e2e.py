#!/usr/bin/env python3
"""End-to-end + unit tests for the verify-integrate INTEGRATE + CLEANUP states (slice 2).

INTEGRATE is the single highest-stakes act-side state (DESIGN §3.7). After the
loop redesign (DESIGN §3.7.3) it is a THIN merge: it reads the `verdicts` slot
and, for each `ok` verdict, merges the PR via an INJECTABLE merge sink
(production: `gh pr merge <pr> --merge --delete-branch`; tests pass a stub — the
determinism seam, no network). A merge happens ONLY when permits('merge', mode)
is True (the trust-ladder gate, auto-merge only) AND merge_guardrails(pr_meta,
default_branch) passes. INTEGRATE NO LONGER reads review_verdicts or gates on a
model review approval — REVIEW is now an ADVISORY quality state (DESIGN §3.7.7),
not a merge gate (the deterministic IMPLEMENT run.py gate + VERIFY + guardrails +
trust ladder are the merge gates). Non-ok verdicts, not-permitted modes
(dry-run/propose NO-OP), and guardrail violations go to `skipped`; a merge sink
that raises records the PR under `errors`. INTEGRATE writes the
`integration_result` slot and emits OK.

CLEANUP (v1-thin) reads `integration_result` and emits OK: branch cleanup is
folded into INTEGRATE's --delete-branch and release/tag is deferred, so v1 is a
deterministic pass-through that exists for the §2.6 route contract.

The e2e tests drive both states exactly as tick-orchestrator will — building a
real fsm-contracts TickContext, registering the slots, running the state, and
committing its StateResult through `fc.apply_result` under the manifest + signal
vocabulary (bounded-scope). INTEGRATE consumes the REAL safety_governance
permits + merge_guardrails (the sibling lib is put on sys.path, mirroring how the
adapters resolve siblings).

Owner: changyu87
"""

import json
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

# safety-governance is a sibling feature consumed UNCHANGED (permits +
# merge_guardrails); put its src/ on the path so INTEGRATE resolves it by module
# name, mirroring how the other adapters resolve their siblings.
_SG_SRC = os.path.join(_FEATURES_DIR, "safety-governance", "src")
if _SG_SRC not in sys.path:
    sys.path.insert(0, _SG_SRC)
# safety_governance imports lifecycle_dispositions; put it on the path too.
_LD_SRC = os.path.join(_FEATURES_DIR, "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402


_DEFAULT_BRANCH = "main"


# --------------------------------------------------------------------------
# Fixtures — a verdict-dict builder, a recording merge sink, a fresh ctx.
# --------------------------------------------------------------------------

def _verdict(number=1, ok=True, base=_DEFAULT_BRANCH, mergeable=True,
             ci_state="passing", reasons=None):
    """A Verdict dict in the `verdicts` slot shape (what VERIFY writes)."""
    v = vi.Verdict(
        pr_ref=f"acme/widget#{number}",
        url=f"https://github.com/acme/widget/pull/{number}",
        ok=ok,
        ci_state=ci_state,
        mergeable=mergeable,
        base=base,
        reasons=list(reasons or []),
    )
    return v.to_dict()


def _recording_sink():
    """A merge sink that records each pr_ref it was asked to merge and returns an
    IMMEDIATE-merge entry (`auto_enabled=False`), standing in for the no-queue
    immediate `gh pr merge --merge --delete-branch` path (merges now). Accepts the
    `base_branch` kwarg INTEGRATE now passes so the sink can probe a merge queue."""
    calls = []

    def sink(pr_ref, repo=None, base_branch=None):  # noqa: ARG001
        calls.append(pr_ref)
        return {"pr_ref": pr_ref,
                "url": f"https://github.com/acme/widget/pull/{pr_ref.split('#')[-1]}",
                "auto_enabled": False}

    sink.calls = calls
    return sink


def _queued_sink():
    """A merge sink standing in for the QUEUED path (merge queue present, or native
    auto-merge enabled while checks pend): the PR is not merged now but was queued,
    so it returns `auto_enabled=True` (a pending success, NOT an error)."""
    calls = []

    def sink(pr_ref, repo=None, base_branch=None):  # noqa: ARG001
        calls.append(pr_ref)
        return {"pr_ref": pr_ref,
                "url": f"https://github.com/acme/widget/pull/{pr_ref.split('#')[-1]}",
                "auto_enabled": True}

    sink.calls = calls
    return sink


def _fresh_ctx():
    """A TickContext with the slots INTEGRATE/CLEANUP touch. After the loop
    redesign INTEGRATE reads `verdicts` + `gate_results` (the review-approval
    coupling is gone), so review_verdicts is NOT registered here. The
    gate_results slot carries one GateResult per gated PR (the cumulative GATE
    output INTEGRATE now consults before merging)."""
    ctx = fc.TickContext()
    ctx.register_slot(
        vi.VERDICTS_SLOT["name"],
        vi.VERDICTS_SLOT["schema"],
        version=vi.VERDICTS_SLOT["version"],
    )
    ctx.register_slot(
        vi.GATE_RESULTS_SLOT["name"],
        vi.GATE_RESULTS_SLOT["schema"],
        version=vi.GATE_RESULTS_SLOT["version"],
    )
    ctx.register_slot(
        vi.INTEGRATION_RESULT_SLOT["name"],
        vi.INTEGRATION_RESULT_SLOT["schema"],
        version=vi.INTEGRATION_RESULT_SLOT["version"],
    )
    return ctx


def _write_verdicts(ctx, verdicts):
    """Write the verdicts slot AND seed a PASSING GateResult for every ok verdict
    (these INTEGRATE tests exercise the verdict/mode/guardrail gates, which are
    ORTHOGONAL to the cumulative regression gate — a passing gate lets an ok PR
    reach those gates exactly as before GATE existed). Non-ok verdicts get a
    passing gate too; INTEGRATE skips them on the verdict gate first regardless."""
    ctx.write("verdicts", verdicts)
    ctx.write("gate_results", [
        vi.GateResult(pr_ref=v["pr_ref"], issue_ref=None, passed=True).to_dict()
        for v in verdicts
    ])


# ==========================================================================
# Behaviour: the IntegrationResult schema is typed, machine-first, versioned
# and round-trips through to_dict/from_dict.
# ==========================================================================

def test_integration_result_round_trip():
    r = vi.IntegrationResult(
        merged=[{"pr_ref": "acme/widget#1",
                 "url": "https://github.com/acme/widget/pull/1"}],
        skipped=[{"pr_ref": "acme/widget#2", "reason": "not ok"}],
        errors=[{"pr_ref": "acme/widget#3", "reason": "boom"}],
    )
    d = r.to_dict()
    assert d["schema_version"] == vi.INTEGRATION_RESULT_SCHEMA_VERSION
    assert d["merged"] == [{"pr_ref": "acme/widget#1",
                            "url": "https://github.com/acme/widget/pull/1"}]
    assert d["skipped"] == [{"pr_ref": "acme/widget#2", "reason": "not ok"}]
    assert d["errors"] == [{"pr_ref": "acme/widget#3", "reason": "boom"}]

    back = vi.IntegrationResult.from_dict(d)
    assert back == r


def test_integration_result_empty_round_trip():
    r = vi.IntegrationResult()
    d = r.to_dict()
    assert d["merged"] == []
    assert d["skipped"] == []
    assert d["errors"] == []
    assert vi.IntegrationResult.from_dict(d) == r


# ==========================================================================
# Behaviour: the integration_result slot descriptor is typed, versioned.
# ==========================================================================

def test_integration_result_slot_descriptor_is_versioned():
    slot = vi.INTEGRATION_RESULT_SLOT
    assert slot["name"] == "integration_result"
    assert slot["schema"] == {"type": "object"}
    assert slot["version"] == vi.INTEGRATION_RESULT_SCHEMA_VERSION


# ==========================================================================
# Behaviour (loop redesign §3.7.3 + GATE §2.2 [v2] coexistence): INTEGRATE is
# THIN — its manifest declares the guaranteed `verdicts` read (NOT
# review_verdicts). It CONSULTS gate_results at runtime but reads it OPTIONALLY,
# so gate_results is NOT a declared manifest read until scheduling wires GATE.
# ==========================================================================

def test_integrate_manifest_declares_reads_writes_emits():
    m = vi.INTEGRATE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("verdicts",)
    assert "review_verdicts" not in m.reads
    assert "gate_results" not in m.reads
    assert m.writes == ("integration_result",)
    assert set(m.emits) == {"OK"}


def test_integrate_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    assert vocab.is_member("OK")
    assert not vocab.is_member("MERGED")


# ==========================================================================
# E2E Behaviour: at auto-merge, a clean ok verdict is MERGED via the stub sink;
# integration_result records it under `merged`; signal OK. NO review approval is
# present in the context — the thin INTEGRATE merges anyway (RED criterion).
# ==========================================================================

def test_integrate_e2e_auto_merge_merges_ok_verdict():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=1, ok=True)])
    # No review_verdicts written — the thin INTEGRATE does not need them.

    result = integrate.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == ["acme/widget#1"]
    assert len(res["merged"]) == 1
    assert res["merged"][0]["pr_ref"] == "acme/widget#1"
    assert res["skipped"] == []
    assert res["errors"] == []
    assert res["schema_version"] == vi.INTEGRATION_RESULT_SCHEMA_VERSION


# ==========================================================================
# E2E Behaviour (RED criterion): an ok+guardrail-passing PR is NOT skipped for
# lack of review approval. With NO review machinery in context AND a review_findings
# slot that is empty/absent, the ok PR still merges (REVIEW is advisory now).
# ==========================================================================

def test_integrate_e2e_merges_ok_pr_without_any_review_approval():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=99, ok=True)])

    res = ctx_run(integrate, ctx)
    assert sink.calls == ["acme/widget#99"]
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#99"]
    assert res["skipped"] == []


def ctx_run(integrate, ctx):
    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)
    return ctx.read("integration_result")


# ==========================================================================
# E2E Behaviour: a non-ok verdict is SKIPPED (never merged); the skip reason
# carries the verdict's reasons; the sink is NOT called.
# ==========================================================================

def test_integrate_e2e_non_ok_verdict_skipped_sink_not_called():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=2, ok=False, ci_state="failing",
                                    reasons=["CI not passing (ci_state=failing)"])])

    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == []
    assert res["merged"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["pr_ref"] == "acme/widget#2"
    assert "CI" in res["skipped"][0]["reason"]


# ==========================================================================
# E2E Behaviour: an ok verdict at mode='propose' is NOT permitted to merge — it
# is SKIPPED with a not-permitted reason and the sink is NEVER called (the
# dry-run/propose NO-OP: log intent, a human merges).
# ==========================================================================

def test_integrate_e2e_propose_mode_not_permitted_skipped_sink_not_called():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="propose", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=3, ok=True)])

    result = integrate.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == []
    assert res["merged"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["pr_ref"] == "acme/widget#3"
    assert "propose" in res["skipped"][0]["reason"]


# ==========================================================================
# E2E Behaviour: at dry-run an ok verdict is likewise NOT merged (NO-OP).
# ==========================================================================

def test_integrate_e2e_dry_run_mode_not_permitted_skipped():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="dry-run", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=4, ok=True)])

    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == []
    assert res["merged"] == []
    assert len(res["skipped"]) == 1


# ==========================================================================
# E2E Behaviour: an ok verdict that VIOLATES a merge guardrail (a non-default
# base that slipped past as ok) is SKIPPED with the violation; sink NOT called.
# The guardrail is a hard backstop BELOW the trust ladder (§3.8.1).
# ==========================================================================

def test_integrate_e2e_guardrail_violation_skipped_sink_not_called():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    # ok flag is True but the base is wrong — the guardrail must still block it.
    _write_verdicts(ctx, [_verdict(number=5, ok=True, base="release-1.x")])

    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == []
    assert res["merged"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["pr_ref"] == "acme/widget#5"
    assert "base" in res["skipped"][0]["reason"].lower()


# ==========================================================================
# E2E Behaviour: when the merge sink RAISES, the PR is recorded under `errors`
# (not merged, not skipped) and the run still completes with signal OK.
# ==========================================================================

def test_integrate_e2e_merge_sink_raises_records_error():
    def raising_sink(pr_ref, repo=None, base_branch=None):  # noqa: ARG001
        raise RuntimeError("gh merge failed: protected branch")

    integrate = vi.Integrate(mode="auto-merge", merge_sink=raising_sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=6, ok=True)])

    result = integrate.run(ctx)
    assert result.signal == "OK"
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert res["merged"] == []
    assert res["skipped"] == []
    assert len(res["errors"]) == 1
    assert res["errors"][0]["pr_ref"] == "acme/widget#6"
    assert "gh merge failed" in res["errors"][0]["reason"]


# ==========================================================================
# E2E Behaviour: a mixed batch at auto-merge — one ok (merged), one non-ok
# (skipped), one ok-but-guardrail-violation (skipped) — partitions correctly.
# ==========================================================================

def test_integrate_e2e_mixed_batch_partitions():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [
        _verdict(number=1, ok=True),
        _verdict(number=2, ok=False, ci_state="pending",
                 reasons=["CI not passing (ci_state=pending)"]),
        _verdict(number=3, ok=True, base="release-1.x"),
    ])

    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)

    res = ctx.read("integration_result")
    assert sink.calls == ["acme/widget#1"]
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#1"]
    assert {s["pr_ref"] for s in res["skipped"]} == {"acme/widget#2",
                                                     "acme/widget#3"}
    assert res["errors"] == []


# ==========================================================================
# E2E Behaviour: INTEGRATE consumes the REAL safety_governance permits +
# merge_guardrails (no stub) — a clean ok verdict at auto-merge merges.
# ==========================================================================

def test_integrate_e2e_uses_real_safety_governance():
    import safety_governance as sg
    # Sanity: the real gate permits merge only at auto-merge.
    assert sg.permits("merge", "auto-merge") is True
    assert sg.permits("merge", "propose") is False

    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=8, ok=True)])
    integrate.run(ctx)
    assert sink.calls == ["acme/widget#8"]


# ==========================================================================
# Behaviour (auto-merge sink): INTEGRATION_RESULT_SCHEMA_VERSION bumps additively
# to 1.2.0 for the new auto_merge_enabled field, and the field round-trips.
# ==========================================================================

def test_integration_result_schema_version_is_1_2_0():
    assert vi.INTEGRATION_RESULT_SCHEMA_VERSION == "1.2.0"


def test_integration_result_round_trip_carries_auto_merge_enabled():
    entry = {"pr_ref": "acme/widget#1",
             "url": "https://github.com/acme/widget/pull/1"}
    r = vi.IntegrationResult(auto_merge_enabled=[entry])
    d = r.to_dict()
    assert d["schema_version"] == "1.2.0"
    assert d["auto_merge_enabled"] == [entry]
    assert vi.IntegrationResult.from_dict(d) == r


def test_integration_result_empty_has_auto_merge_enabled_list():
    d = vi.IntegrationResult().to_dict()
    assert d["auto_merge_enabled"] == []


# ==========================================================================
# E2E Behaviour: at auto-merge, when the merge sink QUEUED GitHub native
# auto-merge (checks pending) it returns auto_enabled=True — the PR is recorded
# under the NEW auto_merge_enabled list (a pending success, NOT an error / NOT
# merged). The sink was still called.
# ==========================================================================

def test_integrate_e2e_auto_merge_queued_records_auto_merge_enabled():
    sink = _queued_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=1, ok=True)])

    res = ctx_run(integrate, ctx)
    assert sink.calls == ["acme/widget#1"]
    assert res["merged"] == []
    assert res["errors"] == []
    assert [e["pr_ref"] for e in res["auto_merge_enabled"]] == ["acme/widget#1"]
    assert res["auto_merge_enabled"][0]["url"]


# ==========================================================================
# E2E Behaviour: at auto-merge, when the sink MERGED immediately (already green)
# it returns auto_enabled=False — the PR is recorded under `merged` (as before),
# and auto_merge_enabled stays empty.
# ==========================================================================

def test_integrate_e2e_auto_merge_immediate_green_records_merged():
    sink = _recording_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=1, ok=True)])

    res = ctx_run(integrate, ctx)
    assert [e["pr_ref"] for e in res["merged"]] == ["acme/widget#1"]
    assert res["auto_merge_enabled"] == []
    assert res["errors"] == []


# ==========================================================================
# E2E Behaviour: a merge sink that raises on BOTH paths (genuine merge fault) is
# recorded under `errors` — never auto_merge_enabled, never merged.
# ==========================================================================

def test_integrate_e2e_auto_merge_both_paths_fail_records_error():
    def both_fail_sink(pr_ref, repo=None, base_branch=None):  # noqa: ARG001
        raise RuntimeError("gh merge failed: --auto and immediate both failed")

    integrate = vi.Integrate(mode="auto-merge", merge_sink=both_fail_sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=7, ok=True)])

    res = ctx_run(integrate, ctx)
    assert res["merged"] == []
    assert res["auto_merge_enabled"] == []
    assert len(res["errors"]) == 1
    assert res["errors"][0]["pr_ref"] == "acme/widget#7"


# ==========================================================================
# E2E Behaviour (merge-queue-aware, REAL sink through Integrate): INTEGRATE at
# auto-merge drives the PRODUCTION gh_pr_merge_sink with an injected fake gh
# runner (no network). This is the full path — verdict -> guardrails -> sink ->
# GraphQL merge-queue probe on the PR base -> the queue-correct `gh pr merge`.
# ==========================================================================

def _real_sink_with_runner(runner):
    """Bind the production gh_pr_merge_sink to a fake gh runner so Integrate drives
    the real merge-queue-aware sink logic end-to-end (no network)."""
    def sink(pr_ref, repo=None, base_branch=None):
        return vi.gh_pr_merge_sink(pr_ref, repo=repo, base_branch=base_branch,
                                   runner=runner)
    return sink


def _gh_router(has_queue, immediate_ok=True, not_yet_mergeable=False,
               merge_stderr="", queue_returncode=0, queue_stderr=""):
    """Build a fake gh runner + a cmds log. Routes the GraphQL merge-queue probe
    and the `gh pr merge` calls per the scenario flags."""
    cmds = []

    def runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if cmd[:3] == ["gh", "api", "graphql"]:
            mq = {"id": "MQ_1"} if has_queue else None
            return _FakeCompleted(
                json.dumps({"data": {"repository": {"mergeQueue": mq}}}))
        # gh pr merge ...
        if has_queue:
            return _FakeCompleted("", returncode=queue_returncode,
                                  stderr=queue_stderr)
        if "--auto" in cmd:
            return _FakeCompleted("")  # the not-yet-mergeable fallback succeeds
        if immediate_ok:
            return _FakeCompleted("")
        return _FakeCompleted(
            "", returncode=1,
            stderr=(merge_stderr or
                    ("Pull request is not mergeable: checks still pending"
                     if not_yet_mergeable else "HTTP 404: Not Found")))

    runner.cmds = cmds
    return runner


def test_integrate_e2e_real_sink_queue_present_records_auto_merge_enabled():
    runner = _gh_router(has_queue=True)
    integrate = vi.Integrate(mode="auto-merge",
                             merge_sink=_real_sink_with_runner(runner),
                             repo="acme/widget", default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=1, ok=True)])

    res = ctx_run(integrate, ctx)
    assert res["merged"] == []
    assert res["errors"] == []
    assert [e["pr_ref"] for e in res["auto_merge_enabled"]] == ["acme/widget#1"]
    merge_cmd = [c for c in runner.cmds if c[1:3] == ["pr", "merge"]][0]
    assert "--auto" in merge_cmd
    assert "--merge" not in merge_cmd
    assert "--delete-branch" not in merge_cmd


def test_integrate_e2e_real_sink_no_queue_immediate_green_merged():
    runner = _gh_router(has_queue=False, immediate_ok=True)
    integrate = vi.Integrate(mode="auto-merge",
                             merge_sink=_real_sink_with_runner(runner),
                             repo="acme/widget", default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=2, ok=True)])

    res = ctx_run(integrate, ctx)
    assert [e["pr_ref"] for e in res["merged"]] == ["acme/widget#2"]
    assert res["auto_merge_enabled"] == []
    merge_cmd = [c for c in runner.cmds if c[1:3] == ["pr", "merge"]][0]
    assert "--merge" in merge_cmd
    assert "--delete-branch" in merge_cmd


def test_integrate_e2e_real_sink_no_queue_not_yet_mergeable_auto_enabled():
    runner = _gh_router(has_queue=False, immediate_ok=False,
                        not_yet_mergeable=True)
    integrate = vi.Integrate(mode="auto-merge",
                             merge_sink=_real_sink_with_runner(runner),
                             repo="acme/widget", default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=3, ok=True)])

    res = ctx_run(integrate, ctx)
    assert res["merged"] == []
    assert [e["pr_ref"] for e in res["auto_merge_enabled"]] == ["acme/widget#3"]


def test_integrate_e2e_real_sink_captures_gh_stderr_into_errors():
    runner = _gh_router(has_queue=True, queue_returncode=1,
                        queue_stderr="HTTP 403: Resource not accessible")
    integrate = vi.Integrate(mode="auto-merge",
                             merge_sink=_real_sink_with_runner(runner),
                             repo="acme/widget", default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    _write_verdicts(ctx, [_verdict(number=4, ok=True)])

    res = ctx_run(integrate, ctx)
    assert res["merged"] == []
    assert res["auto_merge_enabled"] == []
    assert len(res["errors"]) == 1
    assert res["errors"][0]["pr_ref"] == "acme/widget#4"
    assert "403" in res["errors"][0]["reason"]
    assert "exit status 1" not in res["errors"][0]["reason"]


def test_integrate_e2e_real_sink_probe_uses_pr_base_branch():
    runner = _gh_router(has_queue=False, immediate_ok=True)
    integrate = vi.Integrate(mode="auto-merge",
                             merge_sink=_real_sink_with_runner(runner),
                             repo="acme/widget", default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    # base is the default branch (so guardrails pass) — the probe must carry it.
    _write_verdicts(ctx, [_verdict(number=5, ok=True, base=_DEFAULT_BRANCH)])

    ctx_run(integrate, ctx)
    graphql_cmd = [c for c in runner.cmds if c[:3] == ["gh", "api", "graphql"]][0]
    assert any(_DEFAULT_BRANCH in str(part) for part in graphql_cmd)


def test_integrate_e2e_real_sink_propose_and_dry_run_never_call_gh():
    for mode in ("propose", "dry-run"):
        runner = _gh_router(has_queue=True)
        integrate = vi.Integrate(mode=mode,
                                 merge_sink=_real_sink_with_runner(runner),
                                 repo="acme/widget",
                                 default_branch=_DEFAULT_BRANCH)
        ctx = _fresh_ctx()
        _write_verdicts(ctx, [_verdict(number=6, ok=True)])

        res = ctx_run(integrate, ctx)
        assert runner.cmds == [], f"{mode} must never shell gh"
        assert res["merged"] == []
        assert res["auto_merge_enabled"] == []
        assert len(res["skipped"]) == 1


# ==========================================================================
# Behaviour (merge-queue-aware sink): helpers for a fake gh runner that answers
# the GraphQL merge-queue probe (`gh api graphql`) and the `gh pr merge` calls.
# A merge-queue branch REJECTS a method flag, so the sink must add the PR to the
# queue with `gh pr merge <n> --auto` (no method, no --delete-branch).
# ==========================================================================

def _graphql_response(has_queue):
    """A `gh api graphql` stdout for repository.mergeQueue(branch): non-null when
    has_queue, null otherwise."""
    mq = {"id": "MQ_1"} if has_queue else None
    return json.dumps({"data": {"repository": {"mergeQueue": mq}}})


def _is_graphql(cmd):
    return cmd[:3] == ["gh", "api", "graphql"]


def _is_pr_merge(cmd):
    return cmd[1:3] == ["pr", "merge"]


def test_gh_pr_merge_sink_queue_present_uses_auto_no_method():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=True))
        return _FakeCompleted("")  # gh pr merge --auto succeeds (queued)

    entry = vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                                base_branch="main", runner=fake_runner)
    merge_cmd = [c for c in cmds if _is_pr_merge(c)][0]
    assert merge_cmd[:3] == ["gh", "pr", "merge"]
    assert "--auto" in merge_cmd
    # a queue branch REJECTS a method flag and owns branch deletion.
    assert "--merge" not in merge_cmd
    assert "--squash" not in merge_cmd
    assert "--rebase" not in merge_cmd
    assert "--delete-branch" not in merge_cmd
    assert entry["auto_enabled"] is True
    assert entry["pr_ref"] == "acme/widget#9"


def test_gh_pr_merge_sink_probe_uses_pr_base_branch():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=False))
        return _FakeCompleted("")

    vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                        base_branch="release-9.x", runner=fake_runner)
    graphql_cmd = [c for c in cmds if _is_graphql(c)][0]
    # the merge-queue probe carried the PR's base branch.
    assert any("release-9.x" in str(part) for part in graphql_cmd)


def test_gh_pr_merge_sink_no_queue_immediate_green_merges():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=False))
        return _FakeCompleted("")  # immediate --merge succeeds

    entry = vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                                base_branch="main", runner=fake_runner)
    merge_cmd = [c for c in cmds if _is_pr_merge(c)][0]
    assert "--merge" in merge_cmd
    assert "--delete-branch" in merge_cmd
    assert "--auto" not in merge_cmd
    assert entry["auto_enabled"] is False


def test_gh_pr_merge_sink_no_queue_not_yet_mergeable_enables_auto():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=False))
        if _is_pr_merge(cmd) and "--auto" not in cmd:
            # immediate merge refused: PR not yet mergeable (checks pending).
            return _FakeCompleted(
                "", returncode=1,
                stderr="Pull request is not mergeable: required status checks "
                       "are still pending")
        return _FakeCompleted("")  # the --auto --merge fallback succeeds

    entry = vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                                base_branch="main", runner=fake_runner)
    fallback = [c for c in cmds if _is_pr_merge(c) and "--auto" in c][0]
    assert "--auto" in fallback
    assert "--merge" in fallback
    assert entry["auto_enabled"] is True


def test_gh_pr_merge_sink_captures_stderr_into_error():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=True))
        # the queued `gh pr merge --auto` fails with a real gh message.
        return _FakeCompleted(
            "", returncode=1,
            stderr="HTTP 403: Resource not accessible by integration")

    raised = None
    try:
        vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                            base_branch="main", runner=fake_runner)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "a non-zero gh must raise so INTEGRATE records it"
    assert "403" in str(raised)
    assert "exit status 1" not in str(raised)


def test_gh_pr_merge_sink_probe_tolerant_of_graphql_error_treats_as_no_queue():
    cmds = []

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        cmds.append(cmd)
        if _is_graphql(cmd):
            # a probe failure MUST be tolerated (treated as no-queue), not fatal.
            return _FakeCompleted("", returncode=1, stderr="graphql boom")
        return _FakeCompleted("")

    entry = vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                                base_branch="main", runner=fake_runner)
    merge_cmd = [c for c in cmds if _is_pr_merge(c)][0]
    # tolerant -> no-queue path -> immediate --merge --delete-branch.
    assert "--merge" in merge_cmd
    assert entry["auto_enabled"] is False


# ==========================================================================
# Behaviour (determinism seam): gh_pr_merge_sink assembles the exact gh merge
# command (--merge --delete-branch, --repo when given) via an INJECTED fake
# subprocess runner — NO network. Returns a {pr_ref, url} merged entry.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_gh_pr_merge_sink_assembles_command():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted("")

    entry = vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                                runner=fake_runner)
    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "pr", "merge"]
    assert "--merge" in cmd
    assert "--delete-branch" in cmd
    assert "--repo" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme/widget"
    # the PR number is passed (gh accepts the number or url).
    assert "9" in cmd
    assert entry["pr_ref"] == "acme/widget#9"


def test_gh_pr_merge_sink_omits_repo_flag_when_unset():
    captured = {}

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        captured["cmd"] = cmd
        return _FakeCompleted("")

    vi.gh_pr_merge_sink("acme/widget#9", runner=fake_runner)
    assert "--repo" not in captured["cmd"]


# ==========================================================================
# Behaviour (observability fix): _pr_url derives the merged PR's web URL so a
# merged IntegrationResult entry carries a real link instead of url:''. Pure,
# never raises for URL derivation.
#   - `owner/repo#number` ref -> https://github.com/owner/repo/pull/number
#   - bare `#number` ref + repo ('owner/repo') -> derived from repo
#   - no owner/repo derivable AND no repo -> '' (never raises)
# ==========================================================================

def test_pr_url_derives_from_owner_repo_ref():
    assert (vi._pr_url("changyu87/auto-maintainer-framework#287", None)
            == "https://github.com/changyu87/auto-maintainer-framework/pull/287")


def test_pr_url_falls_back_to_repo_for_bare_number_ref():
    assert (vi._pr_url("#287", "changyu87/auto-maintainer-framework")
            == "https://github.com/changyu87/auto-maintainer-framework/pull/287")


def test_pr_url_returns_empty_when_no_owner_repo_and_no_repo():
    assert vi._pr_url("#287", None) == ""


# ==========================================================================
# Behaviour: gh_pr_merge_sink now returns the DERIVED url (not '') so a merged
# entry carries a real link — a successful merge no longer looks like nothing
# happened (observability fix). Injected succeeding runner, no network.
# ==========================================================================

def test_gh_pr_merge_sink_returns_derived_url():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted("")

    entry = vi.gh_pr_merge_sink(
        "changyu87/auto-maintainer-framework#287",
        repo="changyu87/auto-maintainer-framework", runner=fake_runner)
    assert entry["pr_ref"] == "changyu87/auto-maintainer-framework#287"
    assert (entry["url"]
            == "https://github.com/changyu87/auto-maintainer-framework/pull/287")


def test_gh_pr_merge_sink_url_falls_back_to_repo_for_bare_ref():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted("")

    entry = vi.gh_pr_merge_sink(
        "#287", repo="changyu87/auto-maintainer-framework", runner=fake_runner)
    assert (entry["url"]
            == "https://github.com/changyu87/auto-maintainer-framework/pull/287")


def test_gh_pr_merge_sink_url_empty_when_no_owner_repo_and_no_repo():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted("")

    entry = vi.gh_pr_merge_sink("#287", runner=fake_runner)
    assert entry["url"] == ""


# ==========================================================================
# Behaviour (preserved invariant, new mechanism): a failed `gh pr merge` is loud
# and locatable at the merge boundary. The sink no longer relies on check=True; it
# runs with captured stderr and RAISES carrying the gh stderr text so the fault is
# recorded diagnosably (never silently swallowed, never a bare "exit status 1").
# ==========================================================================

def test_gh_pr_merge_sink_no_queue_non_recoverable_failure_raises_with_stderr():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        if _is_graphql(cmd):
            return _FakeCompleted(_graphql_response(has_queue=False))
        # immediate merge fails with a non-mergeability reason (e.g. access).
        return _FakeCompleted(
            "", returncode=1, stderr="HTTP 404: Not Found (protected branch)")

    raised = None
    try:
        vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                            base_branch="main", runner=fake_runner)
    except Exception as exc:  # noqa: BLE001
        raised = exc
    assert raised is not None, "a failed gh merge must raise at the boundary"
    assert "404" in str(raised)
    assert "exit status 1" not in str(raised)


# ==========================================================================
# CLEANUP behaviour: the manifest is {reads: [integration_result], writes: [],
# emits: [OK]} and conforms to the fsm-contracts shape.
# ==========================================================================

def test_cleanup_manifest_declares_reads_writes_emits():
    m = vi.CLEANUP_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("integration_result",)
    assert m.writes == ()
    assert set(m.emits) == {"OK"}


# ==========================================================================
# E2E Behaviour: CLEANUP reads integration_result and emits OK (v1-thin
# pass-through; branch cleanup folded into INTEGRATE's --delete-branch).
# ==========================================================================

def test_cleanup_e2e_reads_integration_result_emits_ok():
    ctx = _fresh_ctx()
    ctx.write("integration_result", vi.IntegrationResult(
        merged=[{"pr_ref": "acme/widget#1",
                 "url": "https://github.com/acme/widget/pull/1"}],
    ).to_dict())

    cleanup = vi.Cleanup()
    result = cleanup.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"
    assert result.writes == {}

    vocab = fc.SignalVocabulary(vi.CLEANUP_SIGNALS)
    fc.apply_result(ctx, vi.CLEANUP_MANIFEST, result, vocab)


def test_cleanup_e2e_empty_integration_result_emits_ok():
    ctx = _fresh_ctx()
    ctx.write("integration_result", vi.IntegrationResult().to_dict())
    cleanup = vi.Cleanup()
    result = cleanup.run(ctx)
    assert result.signal == "OK"


# ==========================================================================
# REVIEW schema retention (#209): the ReviewVerdict schema is KEPT (scheduling +
# packaging-config still consume it), even though INTEGRATE no longer gates on
# it. Its round-trip + slot descriptor stay covered.
# ==========================================================================

def test_review_verdict_round_trip():
    rv = vi.ReviewVerdict(
        pr_ref="acme/widget#9", approved=False, severity="high",
        findings=[{"kind": "spec", "severity": "high", "file": "a.py",
                   "line": 12, "note": "solved the wrong problem"}],
        evidence={"files_examined": ["a.py"],
                  "rationale": "diff solved a different problem"})
    d = rv.to_dict()
    assert d["schema_version"] == vi.REVIEW_VERDICT_SCHEMA_VERSION
    assert vi.ReviewVerdict.from_dict(d) == rv


def test_review_verdict_defaults_no_findings():
    rv = vi.ReviewVerdict(pr_ref="acme/widget#1", approved=True)
    d = rv.to_dict()
    assert d["severity"] == "none"
    assert d["findings"] == []
    assert d["evidence"] == {"files_examined": [], "rationale": ""}
    assert vi.ReviewVerdict.from_dict(d) == rv


def test_review_verdicts_slot_descriptor_is_versioned():
    slot = vi.REVIEW_VERDICTS_SLOT
    assert slot["name"] == "review_verdicts"
    assert slot["schema"] == {"type": "array"}
    assert slot["version"] == vi.REVIEW_VERDICT_SCHEMA_VERSION
