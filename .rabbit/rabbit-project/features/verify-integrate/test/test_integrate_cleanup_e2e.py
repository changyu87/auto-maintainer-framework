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

import os
import subprocess
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
    """A merge sink that records each pr_ref it was asked to merge and returns a
    merged-entry dict, standing in for `gh pr merge --merge --delete-branch`."""
    calls = []

    def sink(pr_ref, repo=None):  # noqa: ARG001
        calls.append(pr_ref)
        return {"pr_ref": pr_ref,
                "url": f"https://github.com/acme/widget/pull/{pr_ref.split('#')[-1]}"}

    sink.calls = calls
    return sink


def _fresh_ctx():
    """A TickContext with the slots INTEGRATE/CLEANUP touch. After the loop
    redesign INTEGRATE reads ONLY `verdicts` (the review-approval coupling is
    gone), so review_verdicts is NOT registered here."""
    ctx = fc.TickContext()
    ctx.register_slot(
        vi.VERDICTS_SLOT["name"],
        vi.VERDICTS_SLOT["schema"],
        version=vi.VERDICTS_SLOT["version"],
    )
    ctx.register_slot(
        vi.INTEGRATION_RESULT_SLOT["name"],
        vi.INTEGRATION_RESULT_SLOT["schema"],
        version=vi.INTEGRATION_RESULT_SLOT["version"],
    )
    return ctx


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
# Behaviour (loop redesign §3.7.3): INTEGRATE is THIN — its manifest reads ONLY
# `verdicts` (NOT review_verdicts), writes integration_result, emits OK.
# ==========================================================================

def test_integrate_manifest_declares_reads_writes_emits():
    m = vi.INTEGRATE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("verdicts",)
    assert "review_verdicts" not in m.reads
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
    ctx.write("verdicts", [_verdict(number=1, ok=True)])
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
    ctx.write("verdicts", [_verdict(number=99, ok=True)])

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
    ctx.write("verdicts", [_verdict(number=2, ok=False, ci_state="failing",
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
    ctx.write("verdicts", [_verdict(number=3, ok=True)])

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
    ctx.write("verdicts", [_verdict(number=4, ok=True)])

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
    ctx.write("verdicts", [_verdict(number=5, ok=True, base="release-1.x")])

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
    def raising_sink(pr_ref, repo=None):  # noqa: ARG001
        raise RuntimeError("gh merge failed: protected branch")

    integrate = vi.Integrate(mode="auto-merge", merge_sink=raising_sink,
                             default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=6, ok=True)])

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
    ctx.write("verdicts", [
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
    ctx.write("verdicts", [_verdict(number=8, ok=True)])
    integrate.run(ctx)
    assert sink.calls == ["acme/widget#8"]


# ==========================================================================
# Behaviour (determinism seam): gh_pr_merge_sink assembles the exact gh merge
# command (--merge --delete-branch, --repo when given) via an INJECTED fake
# subprocess runner — NO network. Returns a {pr_ref, url} merged entry.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.returncode = 0


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
# Behaviour (preserved invariant): the merge call keeps check=True, so a failed
# `gh pr merge` raises CalledProcessError — the merge fault is loud and locatable
# at the merge boundary, never silently swallowed by the URL-derivation change.
# ==========================================================================

def test_gh_pr_merge_sink_preserves_check_true_on_failure():
    captured = {}

    def failing_runner(cmd, **kwargs):
        captured["kwargs"] = kwargs
        raise subprocess.CalledProcessError(returncode=1, cmd=cmd)

    raised = False
    try:
        vi.gh_pr_merge_sink("acme/widget#9", repo="acme/widget",
                            runner=failing_runner)
    except subprocess.CalledProcessError:
        raised = True
    assert raised, "a failed gh merge must propagate CalledProcessError"
    assert captured["kwargs"].get("check") is True


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
