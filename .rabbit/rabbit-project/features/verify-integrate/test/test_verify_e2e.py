#!/usr/bin/env python3
"""End-to-end + unit tests for the verify-integrate VERIFY adapter state (slice 1).

VERIFY is the read-only act-side gate (DESIGN §3.7). It lists the loop's open PRs
via an INJECTABLE source (production: `gh pr list --label auto-maintainer
--state open --json ...`; tests pass a stub — the determinism seam, mirror of
`work_intake.gh_issue_source`), derives one `Verdict` per PR, writes the
`verdicts` slot, and emits OK when any open PRs were found else EMPTY.

`ok` is the conservative AND of the BLOCKING conditions: mergeable AND
base == default branch (DESIGN §3.7.1/§3.7.2 — VERIFY is THIN; CI is RECORDED on
the Verdict but no longer gates ok, because the correctness gate lives in
IMPLEMENT). VERIFY also runs the CONDITIONAL cross-feature complement (§3.7.6):
when the injected `cross_cutting_risk` slot has risk=True it runs each named
feature's run.py via the injectable complement-runner and, if any fails, flips
every verdict ok=False. VERIFY is READ-ONLY w.r.t. GitHub — it never merges,
closes, or writes to GitHub.

The PR set is sourced LIVE from gh (the cross-tick model): the VERIFY manifest
declares reads=[] because the open-PR set is NOT a blackboard slot.

The e2e tests drive VERIFY exactly as tick-orchestrator will — building a real
fsm-contracts TickContext, registering the `verdicts` slot, running the state,
and committing its StateResult through `fc.apply_result` under the manifest +
signal vocabulary (bounded-scope).

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402


_DEFAULT_BRANCH = "main"


# --------------------------------------------------------------------------
# Fixtures — a builder for gh-shaped PR dicts and a fresh ctx.
# --------------------------------------------------------------------------

def _rollup(*states):
    """Build a statusCheckRollup list in gh's shape (objects with `status`/
    `conclusion`). A passing check is COMPLETED/SUCCESS; failing is
    COMPLETED/FAILURE; pending is IN_PROGRESS with no conclusion."""
    out = []
    for s in states:
        if s == "passing":
            out.append({"status": "COMPLETED", "conclusion": "SUCCESS"})
        elif s == "failing":
            out.append({"status": "COMPLETED", "conclusion": "FAILURE"})
        elif s == "pending":
            out.append({"status": "IN_PROGRESS", "conclusion": None})
        else:
            raise ValueError(s)
    return out


def _pr(number=1, base=_DEFAULT_BRANCH, mergeable="MERGEABLE",
        checks=("passing",)):
    """A gh-shaped open PR dict (the `gh pr list --json
    number,url,headRefName,baseRefName,mergeable,statusCheckRollup` shape)."""
    return {
        "number": number,
        "url": f"https://github.com/acme/widget/pull/{number}",
        "headRefName": f"auto-maintainer/fix-{number}",
        "baseRefName": base,
        "mergeable": mergeable,
        "statusCheckRollup": list(_rollup(*checks)),
    }


def _fresh_ctx(cross_cutting_risk=None):
    ctx = fc.TickContext()
    ctx.register_slot(
        vi.VERDICTS_SLOT["name"],
        vi.VERDICTS_SLOT["schema"],
        version=vi.VERDICTS_SLOT["version"],
    )
    ctx.register_slot(
        vi.CROSS_CHECK_SLOT["name"],
        vi.CROSS_CHECK_SLOT["schema"],
        version=vi.CROSS_CHECK_SLOT["version"],
    )
    if cross_cutting_risk is not None:
        # Register + write the work-intake cross_cutting_risk slot directly (the
        # determinism seam — runtime seeding is FT-E; VERIFY's tests inject it).
        ctx.register_slot("cross_cutting_risk", {"type": "object"},
                          version="1.0.0")
        ctx.write("cross_cutting_risk", cross_cutting_risk)
    return ctx


# ==========================================================================
# Behaviour: the Verdict schema is typed, machine-first, versioned and
# round-trips through to_dict/from_dict.
# ==========================================================================

def test_verdict_round_trip():
    v = vi.Verdict(
        pr_ref="acme/widget#7",
        url="https://github.com/acme/widget/pull/7",
        ok=True,
        ci_state="passing",
        mergeable=True,
        base="main",
        reasons=[],
    )
    d = v.to_dict()
    assert d["schema_version"] == vi.VERDICT_SCHEMA_VERSION
    assert d["pr_ref"] == "acme/widget#7"
    assert d["ok"] is True
    assert d["ci_state"] == "passing"
    assert d["mergeable"] is True
    assert d["base"] == "main"
    assert d["reasons"] == []

    back = vi.Verdict.from_dict(d)
    assert back == v


# ==========================================================================
# Behaviour: derive_verdict — passing CI + mergeable + default base -> ok True,
# ci_state passing, no reasons.
# ==========================================================================

def test_derive_verdict_ok_when_passing_mergeable_default_base():
    v = vi.derive_verdict(_pr(number=7), _DEFAULT_BRANCH)
    assert v.ci_state == "passing"
    assert v.mergeable is True
    assert v.base == "main"
    assert v.ok is True
    assert v.reasons == []
    assert v.pr_ref == "acme/widget#7"
    assert v.url == "https://github.com/acme/widget/pull/7"


# ==========================================================================
# Behaviour (THIN VERIFY, §3.7.1/§3.7.2): failing CI is RECORDED but no longer
# gates ok. A mergeable PR on the default branch is ok=True even with failing CI,
# and CI contributes NO blocking reason.
# ==========================================================================

def test_derive_verdict_failing_ci_recorded_but_not_gating():
    v = vi.derive_verdict(
        _pr(checks=("passing", "failing")), _DEFAULT_BRANCH)
    assert v.ci_state == "failing"
    assert v.ok is True
    assert not any("ci" in r.lower() for r in v.reasons)


# ==========================================================================
# Behaviour: pending CI -> ci_state pending RECORDED, ok still True (thin).
# ==========================================================================

def test_derive_verdict_pending_ci_recorded_but_not_gating():
    v = vi.derive_verdict(
        _pr(checks=("passing", "pending")), _DEFAULT_BRANCH)
    assert v.ci_state == "pending"
    assert v.ok is True
    assert not any("ci" in r.lower() for r in v.reasons)


# ==========================================================================
# Behaviour: empty / missing rollup -> ci_state unknown RECORDED, ok still True.
# ==========================================================================

def test_derive_verdict_unknown_ci_recorded_but_not_gating():
    pr = _pr()
    pr["statusCheckRollup"] = []
    v = vi.derive_verdict(pr, _DEFAULT_BRANCH)
    assert v.ci_state == "unknown"
    assert v.ok is True
    assert not any("ci" in r.lower() for r in v.reasons)


# ==========================================================================
# Behaviour: non-default base -> ok False, reason names the base.
# ==========================================================================

def test_derive_verdict_non_default_base_not_ok():
    v = vi.derive_verdict(_pr(base="release-1.x"), _DEFAULT_BRANCH)
    assert v.base == "release-1.x"
    assert v.ok is False
    assert any("base" in r.lower() for r in v.reasons)


# ==========================================================================
# Behaviour: CONFLICTING mergeable -> mergeable False, ok False, reason set.
# ==========================================================================

def test_derive_verdict_conflicting_mergeable_not_ok():
    v = vi.derive_verdict(_pr(mergeable="CONFLICTING"), _DEFAULT_BRANCH)
    assert v.mergeable is False
    assert v.ok is False
    assert any("merge" in r.lower() for r in v.reasons)


# ==========================================================================
# Behaviour: UNKNOWN mergeable -> mergeable False (conservative), ok False.
# ==========================================================================

def test_derive_verdict_unknown_mergeable_not_ok():
    v = vi.derive_verdict(_pr(mergeable="UNKNOWN"), _DEFAULT_BRANCH)
    assert v.mergeable is False
    assert v.ok is False


# ==========================================================================
# Behaviour: the verdicts slot descriptor is typed, machine-first, versioned.
# ==========================================================================

def test_verdicts_slot_descriptor_is_versioned():
    slot = vi.VERDICTS_SLOT
    assert slot["name"] == "verdicts"
    assert slot["schema"] == {"type": "array"}
    assert slot["version"] == vi.VERDICT_SCHEMA_VERSION


# ==========================================================================
# Behaviour: per-state manifest is {reads: [cross_cutting_risk],
# writes: [verdicts, cross_check], emits: [OK, EMPTY]} and conforms to the
# fsm-contracts manifest shape. The open-PR set is still sourced LIVE from gh
# (NOT a slot); the only slot read is the work-intake cross_cutting_risk verdict.
# ==========================================================================

def test_verify_manifest_declares_reads_writes_emits():
    m = vi.VERIFY_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("cross_cutting_risk",)
    assert set(m.writes) == {"verdicts", "cross_check"}
    assert set(m.emits) == {"OK", "EMPTY"}


def test_verify_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(vi.VERIFY_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("EMPTY")
    assert not vocab.is_member("MAYBE")


# ==========================================================================
# E2E Behaviour: a stub source over fixture PRs -> verdicts slot written with
# one Verdict per PR; signal OK.
# ==========================================================================

def test_verify_e2e_stub_source_writes_verdicts_and_ok():
    prs = [
        _pr(number=1),
        # failing CI no longer flips ok: a mergeable PR on the default branch is
        # ok=True even with failing CI (thin VERIFY).
        _pr(number=2, checks=("failing",)),
        # a non-default base IS a blocking condition -> ok=False.
        _pr(number=3, base="release-1.x"),
        # a conflicting tree IS a blocking condition -> ok=False.
        _pr(number=4, mergeable="CONFLICTING"),
    ]

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return list(prs)

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    result = verify.run(ctx)

    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(vi.VERIFY_SIGNALS)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result, vocab)

    verdicts = ctx.read("verdicts")
    assert isinstance(verdicts, list)
    assert len(verdicts) == 4
    by_ref = {v["pr_ref"]: v for v in verdicts}
    assert by_ref["acme/widget#1"]["ok"] is True
    assert by_ref["acme/widget#2"]["ok"] is True
    assert by_ref["acme/widget#2"]["ci_state"] == "failing"
    assert by_ref["acme/widget#3"]["ok"] is False
    assert by_ref["acme/widget#4"]["ok"] is False
    # Every verdict carries the schema version (machine-first, versioned).
    assert all(v["schema_version"] == vi.VERDICT_SCHEMA_VERSION
               for v in verdicts)
    # With no cross_cutting_risk slot, the cross_check records ran=False (thin).
    cc = ctx.read("cross_check")
    assert cc["ran"] is False
    assert cc["results"] == []


# ==========================================================================
# E2E Behaviour: an empty source -> verdicts=[], signal EMPTY.
# ==========================================================================

def test_verify_e2e_empty_source_emits_empty():
    def empty_source(repo=None, label=None):  # noqa: ARG001
        return []

    verify = vi.Verify(source=empty_source, default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    result = verify.run(ctx)

    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(vi.VERIFY_SIGNALS)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result, vocab)

    assert ctx.read("verdicts") == []


# ==========================================================================
# E2E Behaviour: VERIFY resolves the default branch via its injectable helper
# when none is passed — a PR onto that resolved branch is ok.
# ==========================================================================

def test_verify_resolves_default_branch_via_injectable_helper():
    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=5, base="trunk")]

    def fake_default_branch(repo=None):  # noqa: ARG001
        return "trunk"

    verify = vi.Verify(source=stub_source,
                       default_branch_source=fake_default_branch)
    ctx = _fresh_ctx()
    result = verify.run(ctx)

    vocab = fc.SignalVocabulary(vi.VERIFY_SIGNALS)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result, vocab)

    verdicts = ctx.read("verdicts")
    assert verdicts[0]["base"] == "trunk"
    assert verdicts[0]["ok"] is True


# ==========================================================================
# Behaviour (determinism seam): gh_open_pr_source assembles the exact gh
# command (label, state, json fields) via an INJECTED fake subprocess runner —
# NO network. Mirrors work_intake's gh source command-assembly test.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def test_gh_open_pr_source_assembles_command():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted("[]")

    prs = vi.gh_open_pr_source(repo="acme/widget", runner=fake_runner)
    assert prs == []

    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "pr", "list"]
    assert "--label" in cmd
    assert cmd[cmd.index("--label") + 1] == "auto-maintainer"
    assert "--state" in cmd
    assert cmd[cmd.index("--state") + 1] == "open"
    assert "--json" in cmd
    json_fields = cmd[cmd.index("--json") + 1]
    for field in ("number", "url", "headRefName", "baseRefName",
                  "mergeable", "statusCheckRollup"):
        assert field in json_fields
    # --repo carries the destination chosen by the caller.
    assert "--repo" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme/widget"


def test_gh_open_pr_source_omits_repo_flag_when_unset():
    captured = {}

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        captured["cmd"] = cmd
        return _FakeCompleted("[]")

    vi.gh_open_pr_source(runner=fake_runner)
    assert "--repo" not in captured["cmd"]


def test_gh_open_pr_source_parses_json_payload():
    payload = (
        '[{"number": 9, "url": "https://github.com/acme/widget/pull/9",'
        ' "headRefName": "auto-maintainer/x", "baseRefName": "main",'
        ' "mergeable": "MERGEABLE", "statusCheckRollup": []}]'
    )

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted(payload)

    prs = vi.gh_open_pr_source(runner=fake_runner)
    assert len(prs) == 1
    assert prs[0]["number"] == 9
    assert prs[0]["baseRefName"] == "main"


# ==========================================================================
# Behaviour (read-only invariant): VERIFY's run performs NO GitHub writes — the
# only edge it touches is the injected read-only source. A source that records
# its calls proves the run reads exactly once and never invokes a merge/close.
# ==========================================================================

def test_verify_is_read_only_only_calls_the_source():
    calls = {"source": 0}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        calls["source"] += 1
        return [_pr(number=1)]

    # No merge/close sink is injectable into VERIFY at all (slice 1); the state
    # exposes only a read source + a default-branch resolver. Running it touches
    # the source exactly once and produces verdicts without any write edge.
    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH)
    ctx = _fresh_ctx()
    result = verify.run(ctx)

    assert calls["source"] == 1
    assert set(result.writes.keys()) == {"verdicts", "cross_check"}
    assert not hasattr(verify, "merge")
    assert not hasattr(verify, "close")


# ==========================================================================
# CROSS-FEATURE COMPLEMENT RUN (DESIGN §3.7.6)
# ==========================================================================

# --------------------------------------------------------------------------
# Behaviour: the CrossCheck schema is typed, machine-first, versioned, and
# round-trips through to_dict/from_dict.
# --------------------------------------------------------------------------

def test_cross_check_round_trip():
    cc = vi.CrossCheck(
        ran=True,
        reason="both touch the shared slot registry",
        results=[{"feature": "scheduling", "passed": True,
                  "returncode": 0, "summary": "12 passed, 0 failed"}],
    )
    d = cc.to_dict()
    assert d["schema_version"] == vi.CROSS_CHECK_SCHEMA_VERSION
    assert d["ran"] is True
    assert d["reason"] == "both touch the shared slot registry"
    assert d["results"][0]["feature"] == "scheduling"
    assert vi.CrossCheck.from_dict(d) == cc


def test_cross_check_slot_descriptor_is_versioned():
    slot = vi.CROSS_CHECK_SLOT
    assert slot["name"] == "cross_check"
    assert slot["schema"] == {"type": "object"}
    assert slot["version"] == vi.CROSS_CHECK_SCHEMA_VERSION


# --------------------------------------------------------------------------
# Behaviour: the deterministic feature-run.py path resolver builds
# <features_root>/<feature>/test/run.py under an INJECTED root (no network, no
# real sibling suite).
# --------------------------------------------------------------------------

def test_feature_run_py_path_resolves_under_injected_root():
    path = vi.feature_run_py_path("scheduling", features_root="/tmp/feats")
    assert path == os.path.join("/tmp/feats", "scheduling", "test", "run.py")


# --------------------------------------------------------------------------
# Behaviour (determinism seam): default_complement_runner shells the resolved
# run.py via an INJECTED fake subprocess runner — no network — and maps the
# returncode to the {feature, passed, returncode, summary} verdict shape. A
# missing run.py is recorded as passed=False (never a silent pass).
# --------------------------------------------------------------------------

def test_default_complement_runner_passing_via_fake_subprocess(tmp_root=None):
    import tempfile
    root = tempfile.mkdtemp()
    feat_test = os.path.join(root, "sibling", "test")
    os.makedirs(feat_test)
    with open(os.path.join(feat_test, "run.py"), "w") as f:
        f.write("# stub run.py\n")

    captured = {}

    class _Proc:
        returncode = 0
        stdout = "PASS x\n\n5 passed, 0 failed\n"
        stderr = ""

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _Proc()

    res = vi.default_complement_runner(
        "sibling", features_root=root, runner=fake_runner)
    assert res["feature"] == "sibling"
    assert res["passed"] is True
    assert res["returncode"] == 0
    assert res["summary"] == "5 passed, 0 failed"
    # the runner shelled the resolved run.py, not the network.
    assert captured["cmd"][1].endswith(
        os.path.join("sibling", "test", "run.py"))


def test_default_complement_runner_missing_run_py_is_failure():
    import tempfile
    root = tempfile.mkdtemp()  # no sibling dir created

    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        raise AssertionError("runner must not be called for a missing run.py")

    res = vi.default_complement_runner(
        "ghost", features_root=root, runner=fake_runner)
    assert res["passed"] is False
    assert res["returncode"] == 1
    assert "ghost" in res["summary"]


# --------------------------------------------------------------------------
# E2E Behaviour: cross_cutting_risk.risk=False -> NO complement-runner call;
# cross_check records ran=False; verdicts reflect only mergeable+base.
# --------------------------------------------------------------------------

def test_verify_risk_false_does_not_run_complement():
    calls = {"runner": 0}

    def spy_runner(feature, features_root=None):  # noqa: ARG001
        calls["runner"] += 1
        return {"feature": feature, "passed": True, "returncode": 0,
                "summary": ""}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       complement_runner=spy_runner)
    ctx = _fresh_ctx(cross_cutting_risk={
        "schema_version": "1.0.0", "risk": False, "features": [], "reason": ""})
    result = verify.run(ctx)

    vocab = fc.SignalVocabulary(vi.VERIFY_SIGNALS)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result, vocab)

    assert calls["runner"] == 0
    cc = ctx.read("cross_check")
    assert cc["ran"] is False
    assert cc["results"] == []
    assert ctx.read("verdicts")[0]["ok"] is True


def test_verify_absent_risk_slot_stays_thin():
    """No cross_cutting_risk slot at all (FT-E seeding not yet wired) -> VERIFY
    tolerates the absence, runs no complement, records ran=False."""
    calls = {"runner": 0}

    def spy_runner(feature, features_root=None):  # noqa: ARG001
        calls["runner"] += 1
        return {"feature": feature, "passed": True, "returncode": 0,
                "summary": ""}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       complement_runner=spy_runner)
    ctx = _fresh_ctx()  # no cross_cutting_risk slot registered/written
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))

    assert calls["runner"] == 0
    assert ctx.read("cross_check")["ran"] is False


# --------------------------------------------------------------------------
# E2E Behaviour: cross_cutting_risk.risk=True -> the complement-runner is called
# for EACH named feature; cross_check records the per-feature results.
# --------------------------------------------------------------------------

def test_verify_risk_true_runs_complement_per_feature():
    seen = []

    def spy_runner(feature, features_root=None):  # noqa: ARG001
        seen.append(feature)
        return {"feature": feature, "passed": True, "returncode": 0,
                "summary": "ok"}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       complement_runner=spy_runner)
    ctx = _fresh_ctx(cross_cutting_risk={
        "schema_version": "1.0.0", "risk": True,
        "features": ["scheduling", "fsm-contracts"],
        "reason": "both touch the slot registry"})
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))

    assert seen == ["scheduling", "fsm-contracts"]
    cc = ctx.read("cross_check")
    assert cc["ran"] is True
    assert cc["reason"] == "both touch the slot registry"
    assert [r["feature"] for r in cc["results"]] == \
        ["scheduling", "fsm-contracts"]


# --------------------------------------------------------------------------
# E2E Behaviour (the GATE): a FAILING complement flips EVERY verdict ok=False
# with a cross-feature-break reason naming the failing feature + the triager's
# overlap reason; a verdict that was otherwise ok is now not ok.
# --------------------------------------------------------------------------

def test_verify_failing_complement_flips_all_verdicts():
    def failing_runner(feature, features_root=None):  # noqa: ARG001
        passed = feature != "scheduling"  # scheduling's suite fails
        return {"feature": feature, "passed": passed,
                "returncode": 0 if passed else 1, "summary": "x"}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1), _pr(number=2)]  # both mergeable+default -> ok

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       complement_runner=failing_runner)
    ctx = _fresh_ctx(cross_cutting_risk={
        "schema_version": "1.0.0", "risk": True,
        "features": ["scheduling", "fsm-contracts"],
        "reason": "shared slot registry"})
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))

    verdicts = ctx.read("verdicts")
    assert all(v["ok"] is False for v in verdicts)
    for v in verdicts:
        joined = " ".join(v["reasons"]).lower()
        assert "cross-feature break" in joined
        assert "scheduling" in joined
        assert "shared slot registry" in joined
    cc = ctx.read("cross_check")
    assert cc["ran"] is True
    failing = [r for r in cc["results"] if not r["passed"]]
    assert [r["feature"] for r in failing] == ["scheduling"]


# --------------------------------------------------------------------------
# E2E Behaviour: ALL complements pass -> verdicts keep their mergeable+base ok
# (the complement does not touch a verdict that was already ok).
# --------------------------------------------------------------------------

def test_verify_passing_complement_keeps_ok():
    def passing_runner(feature, features_root=None):  # noqa: ARG001
        return {"feature": feature, "passed": True, "returncode": 0,
                "summary": "all green"}

    def stub_source(repo=None, label=None):  # noqa: ARG001
        return [_pr(number=1)]

    verify = vi.Verify(source=stub_source, default_branch=_DEFAULT_BRANCH,
                       complement_runner=passing_runner)
    ctx = _fresh_ctx(cross_cutting_risk={
        "schema_version": "1.0.0", "risk": True,
        "features": ["scheduling"], "reason": "shared registry"})
    result = verify.run(ctx)
    fc.apply_result(ctx, vi.VERIFY_MANIFEST, result,
                    fc.SignalVocabulary(vi.VERIFY_SIGNALS))

    v = ctx.read("verdicts")[0]
    assert v["ok"] is True
    assert not any("cross-feature break" in r.lower() for r in v["reasons"])
