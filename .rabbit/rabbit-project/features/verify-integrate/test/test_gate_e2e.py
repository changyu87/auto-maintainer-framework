#!/usr/bin/env python3
"""End-to-end + unit tests for the verify-integrate GATE state (DESIGN §2.2 [v2]).

GATE is the deterministic, script-tier self-contained regression gate inserted
between REVIEW and INTEGRATE. It reads the `verdicts` slot (the open loop PRs) +
the configured `regression_command` (safety_governance.load_config /
regression_command accessor) and writes one `GateResult` per gated PR into the
`gate_results` slot, emitting OK.

- `regression_command` null => GATE is a no-op PASS (every passed=True,
  reason=null); no worktree is ever created.
- Otherwise CUMULATIVE: a DISPOSABLE integration worktree at current `main` is
  created; each `ok` verdict PR is merged (deterministic PR-number order) with
  `--no-ff` into the growing tree and the regression is run after each clean
  merge, so PR k is validated on top of `main` + the already-passed 1..k-1. A
  textual conflict or a nonzero regression rolls the PR out, EXCLUDES it, and
  records the failure; the worktree is ALWAYS removed.

EVERYTHING external — git (worktree add/fetch/merge/reset/remove), the
regression subprocess, and gh issue-ref resolution — is behind an INJECTABLE
callable, so these tests drive the cumulative logic with a FAKE runner scripting
outcomes: NO real git, NO real PRs, NO network. GATE never merges to `main` and
never calls the INTEGRATE merge sink.

The INTEGRATE gate-handling tests assert INTEGRATE reads `gate_results`, merges
only ok+gate-passed verdicts, and for a gate-failed PR posts a machine-readable
marker+JSON comment on the PR's linked issue via an injectable comment sink,
recording it under IntegrationResult.gate_failed (never merged).

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
# Fixtures — verdict builder, a scriptable fake git+regression runner, a fake
# issue-ref resolver, a fresh ctx with the GATE slots.
# --------------------------------------------------------------------------

def _verdict(number=1, ok=True, base=_DEFAULT_BRANCH, mergeable=True,
             ci_state="passing", reasons=None):
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


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGit:
    """A fake git+regression runner scripting outcomes per PR.

    Records every command it saw (so tests assert order + roll-back + always-
    removed-worktree), and returns a scripted result for `git merge` and for the
    regression command keyed by which PR is currently being integrated. It never
    touches a real filesystem or the network.

    - `merge_conflicts`: set of PR numbers whose `git merge` returns nonzero
      (textual conflict).
    - `regression_fails`: set of PR numbers whose regression returns nonzero.
    - `regression_output`: str returned as regression stdout (for bounded-tail).
    """

    def __init__(self, regression_command,
                 merge_conflicts=(), regression_fails=(),
                 regression_output="ok"):
        self._regression_command = regression_command
        self._merge_conflicts = set(merge_conflicts)
        self._regression_fails = set(regression_fails)
        self._regression_output = regression_output
        self.commands = []
        # the PR number currently being merged/tested (set by GATE via the
        # per-PR merge, tracked from the fetch/merge ref).
        self._current_pr = None

    def __call__(self, cmd, **kwargs):
        # The regression command is passed as a shell STRING; git commands are
        # passed as a LIST. Record and branch on that shape.
        is_git = isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
        self.commands.append(list(cmd) if is_git else [cmd])
        if not is_git:
            # regression run: keyed by _current_pr
            if self._current_pr in self._regression_fails:
                return _FakeCompleted(returncode=1,
                                      stdout=self._regression_output)
            return _FakeCompleted(returncode=0, stdout=self._regression_output)
        # git subcommand — skip a leading `-C <dir>` so the real subcommand
        # (merge/fetch/reset/rev-parse/worktree) is found.
        sub = self._git_sub(cmd)
        if sub == "merge":
            # detect which PR ref is being merged so the regression run keys off
            # the same PR. GATE fetches/merges FETCH_HEAD or a ref carrying the
            # PR number; we scan the command for the tracked PR number.
            pr_num = self._pr_num_from_cmd(cmd)
            if pr_num is not None:
                self._current_pr = pr_num
            if self._current_pr in self._merge_conflicts:
                return _FakeCompleted(returncode=1, stdout="CONFLICT")
            return _FakeCompleted(returncode=0)
        if sub == "fetch":
            pr_num = self._pr_num_from_cmd(cmd)
            if pr_num is not None:
                self._current_pr = pr_num
            return _FakeCompleted(returncode=0)
        # worktree add/remove, reset, rev-parse, merge --abort, etc.
        if sub == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="deadbeef\n")
        return _FakeCompleted(returncode=0)

    @staticmethod
    def _git_sub(cmd):
        """The git subcommand, skipping a leading `-C <dir>` (and `git`)."""
        i = 1
        while i < len(cmd) and cmd[i] == "-C":
            i += 2
        return cmd[i] if i < len(cmd) else ""

    @staticmethod
    def _pr_num_from_cmd(cmd):
        for tok in cmd:
            # a PR ref like pull/3/head or a bare number token; scan for a digit
            # token that identifies the PR.
            for part in str(tok).replace("/", " ").split():
                if part.isdigit():
                    return int(part)
        return None


def _fake_issue_resolver(mapping):
    """A fake gh issue-ref resolver: pr_ref -> issue_ref (or None)."""
    def resolve(pr_ref, repo=None):  # noqa: ARG001
        return mapping.get(pr_ref)
    resolve.calls = []
    orig = resolve

    def wrapped(pr_ref, repo=None):
        wrapped.calls.append(pr_ref)
        return orig(pr_ref, repo=repo)
    wrapped.calls = []
    return wrapped


def _is_git_sub(cmd, sub):
    """Whether a recorded command is `git [-C <dir>] <sub> ...` (the runner uses
    `git -C <worktree>` for merge/reset/rev-parse/fetch and bare `git worktree`)."""
    if not cmd or cmd[0] != "git":
        return False
    i = 1
    while i < len(cmd) and cmd[i] == "-C":
        i += 2
    return i < len(cmd) and cmd[i] == sub


def _fresh_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
                      version=vi.VERDICTS_SLOT["version"])
    ctx.register_slot(vi.GATE_RESULTS_SLOT["name"], vi.GATE_RESULTS_SLOT["schema"],
                      version=vi.GATE_RESULTS_SLOT["version"])
    return ctx


def _run_gate(gate, ctx):
    result = gate.run(ctx)
    vocab = fc.SignalVocabulary(vi.GATE_SIGNALS)
    assert fc.validate_state_result(result).passed is True
    fc.apply_result(ctx, vi.GATE_MANIFEST, result, vocab)
    return ctx.read("gate_results")


# ==========================================================================
# Behaviour: the GateResult schema is typed, machine-first, versioned and
# round-trips through to_dict/from_dict.
# ==========================================================================

def test_gate_result_round_trip():
    r = vi.GateResult(pr_ref="acme/widget#1", issue_ref="acme/widget#42",
                      passed=False, reason="regression",
                      failure_summary="E   assert 1 == 2")
    d = r.to_dict()
    assert d["schema_version"] == vi.GATE_RESULT_SCHEMA_VERSION
    assert d["pr_ref"] == "acme/widget#1"
    assert d["issue_ref"] == "acme/widget#42"
    assert d["passed"] is False
    assert d["reason"] == "regression"
    assert d["failure_summary"] == "E   assert 1 == 2"
    assert vi.GateResult.from_dict(d) == r


def test_gate_result_passed_defaults():
    r = vi.GateResult(pr_ref="acme/widget#1", issue_ref=None, passed=True)
    d = r.to_dict()
    assert d["passed"] is True
    assert d["reason"] is None
    assert d["failure_summary"] == ""
    assert vi.GateResult.from_dict(d) == r


def test_gate_results_slot_descriptor_is_versioned():
    slot = vi.GATE_RESULTS_SLOT
    assert slot["name"] == "gate_results"
    assert slot["schema"] == {"type": "array"}
    assert slot["version"] == vi.GATE_RESULT_SCHEMA_VERSION


def test_gate_manifest_declares_reads_writes_emits():
    m = vi.GATE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("verdicts",)
    assert m.writes == ("gate_results",)
    assert set(m.emits) == {"OK"}


def test_gate_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(vi.GATE_SIGNALS)
    assert vocab.is_member("OK")
    assert not vocab.is_member("PASS")


# ==========================================================================
# (a) E2E: regression_command None => GATE is a no-op PASS (every passed=True,
# reason=null) and NO worktree / git command is ever run.
# ==========================================================================

def test_gate_e2e_no_regression_command_is_noop_pass():
    fake = _FakeGit(regression_command=None)
    gate = vi.Gate(regression_command=None, runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])

    results = _run_gate(gate, ctx)
    assert len(results) == 2
    assert all(r["passed"] is True for r in results)
    assert all(r["reason"] is None for r in results)
    # No git command ran at all — no worktree created when the gate is unconfigured.
    assert fake.commands == []


def test_gate_e2e_no_regression_command_only_ok_verdicts_gated():
    """Even the no-op path considers only ok verdicts (a non-ok PR won't merge)."""
    fake = _FakeGit(regression_command=None)
    gate = vi.Gate(regression_command=None, runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1, ok=True),
                           _verdict(number=2, ok=False,
                                    reasons=["not mergeable"])])
    results = _run_gate(gate, ctx)
    refs = [r["pr_ref"] for r in results]
    assert refs == ["acme/widget#1"]


# ==========================================================================
# (b) E2E: cumulative pass — 3 ok PRs all pass; each regression is invoked on
# the GROWING tree in deterministic PR-number order (merge k then regress k).
# ==========================================================================

def test_gate_e2e_cumulative_all_pass_in_order():
    fake = _FakeGit(regression_command="pytest")
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    # deliberately out of order to prove deterministic ordering by PR number.
    ctx.write("verdicts", [_verdict(number=3), _verdict(number=1),
                           _verdict(number=2)])

    results = _run_gate(gate, ctx)
    assert [r["pr_ref"] for r in results] == [
        "acme/widget#1", "acme/widget#2", "acme/widget#3"]
    assert all(r["passed"] is True for r in results)
    assert all(r["reason"] is None for r in results)

    # A disposable worktree was created and removed.
    assert any(c[:2] == ["git", "worktree"] and "add" in c
               for c in fake.commands)
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in fake.commands)
    # The regression ran once per PR (3 non-git invocations).
    regression_runs = [c for c in fake.commands if c and c[0] != "git"]
    assert len(regression_runs) == 3


# ==========================================================================
# (c) E2E: the middle PR's regression FAILS -> it is rolled back (git reset
# --hard) and EXCLUDED; the 3rd PR is thus gated on the 1st (not the 2nd).
# ==========================================================================

def test_gate_e2e_middle_regression_fail_rolled_back_and_excluded():
    fake = _FakeGit(regression_command="pytest", regression_fails={2})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2),
                           _verdict(number=3)])

    results = _run_gate(gate, ctx)
    by_ref = {r["pr_ref"]: r for r in results}
    assert by_ref["acme/widget#1"]["passed"] is True
    assert by_ref["acme/widget#2"]["passed"] is False
    assert by_ref["acme/widget#2"]["reason"] == "regression"
    assert by_ref["acme/widget#3"]["passed"] is True

    # A rollback (git reset --hard) happened for the failed PR.
    assert any(_is_git_sub(c, "reset") and "--hard" in c
               for c in fake.commands)
    # 3 regression runs (one per PR; #2 fails but #3 still runs on top of #1).
    regression_runs = [c for c in fake.commands if c and c[0] != "git"]
    assert len(regression_runs) == 3


# ==========================================================================
# (d) E2E: a PR whose merge CONFLICTS is excluded with reason 'conflict' (the
# regression is NOT run for it; the merge is aborted).
# ==========================================================================

def test_gate_e2e_merge_conflict_excluded_reason_conflict():
    fake = _FakeGit(regression_command="pytest", merge_conflicts={2})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2),
                           _verdict(number=3)])

    results = _run_gate(gate, ctx)
    by_ref = {r["pr_ref"]: r for r in results}
    assert by_ref["acme/widget#2"]["passed"] is False
    assert by_ref["acme/widget#2"]["reason"] == "conflict"
    assert by_ref["acme/widget#1"]["passed"] is True
    assert by_ref["acme/widget#3"]["passed"] is True

    # a merge --abort happened for the conflicting PR.
    assert any(_is_git_sub(c, "merge") and "--abort" in c
               for c in fake.commands)
    # the conflicting PR's regression is NOT run: only #1 and #3 regress.
    regression_runs = [c for c in fake.commands if c and c[0] != "git"]
    assert len(regression_runs) == 2


# ==========================================================================
# (f) E2E: failure_summary is a BOUNDED tail of the regression output (not the
# entire multi-thousand-line log).
# ==========================================================================

def test_gate_e2e_failure_summary_is_bounded():
    big = "\n".join(f"line {i}" for i in range(5000))
    fake = _FakeGit(regression_command="pytest", regression_fails={1},
                    regression_output=big)
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])

    results = _run_gate(gate, ctx)
    summary = results[0]["failure_summary"]
    assert summary  # non-empty on failure
    assert len(summary) <= 4096
    # it is the TAIL — the last line survives, an early line does not.
    assert "line 4999" in summary
    assert "line 0\n" not in summary


def test_gate_e2e_pass_has_empty_failure_summary():
    fake = _FakeGit(regression_command="pytest", regression_output="all good")
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    results = _run_gate(gate, ctx)
    assert results[0]["failure_summary"] == ""


# ==========================================================================
# (g) E2E: the disposable worktree is ALWAYS removed, even when a regression
# fails (finally-cleanup).
# ==========================================================================

def test_gate_e2e_worktree_removed_even_on_regression_failure():
    fake = _FakeGit(regression_command="pytest", regression_fails={1})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    _run_gate(gate, ctx)
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in fake.commands)


def test_gate_e2e_worktree_removed_when_runner_raises():
    """If the runner raises mid-integration, the worktree is STILL removed."""
    class _Boom:
        def __init__(self):
            self.commands = []

        def __call__(self, cmd, **kwargs):
            is_list = isinstance(cmd, (list, tuple))
            self.commands.append(list(cmd) if is_list else [cmd])
            if is_list and _is_git_sub(cmd, "merge") and "--abort" not in cmd:
                raise RuntimeError("git merge exploded")
            return _FakeCompleted(returncode=0, stdout="deadbeef\n")

    boom = _Boom()
    gate = vi.Gate(regression_command="pytest", runner=boom,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    raised = False
    try:
        gate.run(ctx)
    except RuntimeError:
        raised = True
    assert raised
    assert any(c[:2] == ["git", "worktree"] and "remove" in c
               for c in boom.commands)


# ==========================================================================
# INVARIANT: GATE never merges to main and never touches the INTEGRATE merge
# sink. It writes only the disposable worktree (never checks out / merges main).
# ==========================================================================

def test_gate_never_merges_to_main_or_calls_merge_sink():
    def poison_sink(pr_ref, repo=None):  # noqa: ARG001
        raise AssertionError("GATE must never call the merge sink")

    fake = _FakeGit(regression_command="pytest")
    # Gate takes no merge_sink param; assert it is not part of its surface AND
    # no git command merges INTO the default branch / pushes.
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver({}))
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])
    _run_gate(gate, ctx)

    # never a push, never a checkout of main followed by a merge onto it.
    assert not any(c[:2] == ["git", "push"] for c in fake.commands)
    # merges target the disposable worktree, identified by --no-ff; no bare
    # `git merge main` on the real checkout.
    merge_cmds = [c for c in fake.commands
                  if _is_git_sub(c, "merge") and "--abort" not in c]
    for c in merge_cmds:
        assert "--no-ff" in c, f"GATE must merge with --no-ff: {c}"


# ==========================================================================
# INTEGRATE gate-handling (e): merges only ok+gate-passed; for a gate-FAILED
# PR posts a machine-readable marker+JSON comment on the PR's linked issue via
# an injectable comment sink and records it under IntegrationResult.gate_failed.
# ==========================================================================

def _recording_sink():
    calls = []

    def sink(pr_ref, repo=None, auto=False):  # noqa: ARG001
        calls.append(pr_ref)
        return {"pr_ref": pr_ref,
                "url": f"https://github.com/acme/widget/pull/{pr_ref.split('#')[-1]}"}
    sink.calls = calls
    return sink


def _recording_comment_sink():
    calls = []

    def sink(issue_ref, body, repo=None):  # noqa: ARG001
        calls.append({"issue_ref": issue_ref, "body": body})
    sink.calls = calls
    return sink


def _integrate_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
                      version=vi.VERDICTS_SLOT["version"])
    ctx.register_slot(vi.GATE_RESULTS_SLOT["name"], vi.GATE_RESULTS_SLOT["schema"],
                      version=vi.GATE_RESULTS_SLOT["version"])
    ctx.register_slot(vi.INTEGRATION_RESULT_SLOT["name"],
                      vi.INTEGRATION_RESULT_SLOT["schema"],
                      version=vi.INTEGRATION_RESULT_SLOT["version"])
    return ctx


def _apply_integrate(integrate, ctx):
    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)
    return ctx.read("integration_result")


def _gate_result(number, passed=True, issue_ref=None, reason=None,
                 failure_summary=""):
    return vi.GateResult(
        pr_ref=f"acme/widget#{number}",
        issue_ref=issue_ref,
        passed=passed,
        reason=reason,
        failure_summary=failure_summary,
    ).to_dict()


def test_integrate_manifest_reads_verdicts_gate_results_optional():
    """Coexistence window (spec-rules §3): INTEGRATE CONSULTS gate_results at
    runtime but reads it OPTIONALLY, so the manifest declares only the guaranteed
    `verdicts` read — a hard gate_results read would fail data-readiness on the
    un-wired route (scheduling wires GATE + seeds gate_results in a later cycle).
    The runtime gating behaviour is covered by the merges-only-gate-passed tests
    below."""
    m = vi.INTEGRATE_MANIFEST
    assert "verdicts" in m.reads
    assert "gate_results" not in m.reads


def test_integrate_e2e_merges_only_gate_passed():
    sink = _recording_sink()
    comment_sink = _recording_comment_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH,
                             comment_sink=comment_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=1, ok=True),
                           _verdict(number=2, ok=True)])
    ctx.write("gate_results", [
        _gate_result(1, passed=True),
        _gate_result(2, passed=False, issue_ref="acme/widget#42",
                     reason="regression", failure_summary="E assert x"),
    ])

    result = integrate.run(ctx)
    vocab = fc.SignalVocabulary(vi.INTEGRATE_SIGNALS)
    fc.apply_result(ctx, vi.INTEGRATE_MANIFEST, result, vocab)
    res = ctx.read("integration_result")

    # only #1 merged; #2 is gate-failed, not merged.
    assert sink.calls == ["acme/widget#1"]
    assert [m["pr_ref"] for m in res["merged"]] == ["acme/widget#1"]
    assert [g["pr_ref"] for g in res["gate_failed"]] == ["acme/widget#2"]
    assert res["gate_failed"][0]["issue_ref"] == "acme/widget#42"
    assert res["gate_failed"][0]["reason"] == "regression"


def test_integrate_e2e_gate_failed_posts_marker_json_comment_on_issue():
    sink = _recording_sink()
    comment_sink = _recording_comment_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH,
                             comment_sink=comment_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=7, ok=True)])
    ctx.write("gate_results", [
        _gate_result(7, passed=False, issue_ref="acme/widget#77",
                     reason="conflict", failure_summary="CONFLICT in a.py"),
    ])

    integrate.run(ctx)
    assert len(comment_sink.calls) == 1
    call = comment_sink.calls[0]
    assert call["issue_ref"] == "acme/widget#77"
    body = call["body"]
    # fixed machine-readable marker.
    assert "<!-- auto-maintainer:gate-fail -->" in body
    # a JSON payload carrying pr_ref/reason/failure_summary.
    import json
    start = body.index("{")
    payload = json.loads(body[start:body.rindex("}") + 1])
    assert payload["pr_ref"] == "acme/widget#7"
    assert payload["reason"] == "conflict"
    assert payload["failure_summary"] == "CONFLICT in a.py"
    # the failed PR was NOT merged.
    assert sink.calls == []


def test_integrate_e2e_gate_failed_no_issue_ref_records_but_no_comment():
    """A gate-failed PR whose issue_ref could not be resolved is still recorded
    under gate_failed but no comment is attempted (nothing to comment on)."""
    sink = _recording_sink()
    comment_sink = _recording_comment_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH,
                             comment_sink=comment_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=8, ok=True)])
    ctx.write("gate_results", [
        _gate_result(8, passed=False, issue_ref=None, reason="regression",
                     failure_summary="boom")])
    res = _apply_integrate(integrate, ctx)
    assert [g["pr_ref"] for g in res["gate_failed"]] == ["acme/widget#8"]
    assert comment_sink.calls == []
    assert sink.calls == []


def test_integrate_e2e_ok_verdict_missing_gate_result_is_skipped():
    """Defensive: an ok verdict with NO matching GateResult is not merged — it
    is skipped with a 'no gate result' reason (never merged un-gated)."""
    sink = _recording_sink()
    comment_sink = _recording_comment_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH,
                             comment_sink=comment_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=9, ok=True)])
    ctx.write("gate_results", [])
    res = _apply_integrate(integrate, ctx)
    assert sink.calls == []
    assert res["merged"] == []
    assert len(res["skipped"]) == 1
    assert res["skipped"][0]["pr_ref"] == "acme/widget#9"
    assert "gate" in res["skipped"][0]["reason"].lower()


def test_integrate_e2e_non_ok_verdict_never_consults_gate():
    """A non-ok verdict is skipped for its verdict reason regardless of a
    (stale) gate result — the verdict gate comes first."""
    sink = _recording_sink()
    comment_sink = _recording_comment_sink()
    integrate = vi.Integrate(mode="auto-merge", merge_sink=sink,
                             default_branch=_DEFAULT_BRANCH,
                             comment_sink=comment_sink)
    ctx = _integrate_ctx()
    ctx.write("verdicts", [_verdict(number=10, ok=False,
                                    reasons=["not mergeable"])])
    ctx.write("gate_results", [_gate_result(10, passed=True)])
    res = _apply_integrate(integrate, ctx)
    assert sink.calls == []
    assert res["merged"] == []
    assert [s["pr_ref"] for s in res["skipped"]] == ["acme/widget#10"]
    assert res["gate_failed"] == []


def test_integration_result_gate_failed_round_trip():
    r = vi.IntegrationResult(
        merged=[{"pr_ref": "acme/widget#1", "url": "u"}],
        gate_failed=[{"pr_ref": "acme/widget#2", "issue_ref": "acme/widget#42",
                      "reason": "regression"}])
    d = r.to_dict()
    assert d["gate_failed"] == [{"pr_ref": "acme/widget#2",
                                 "issue_ref": "acme/widget#42",
                                 "reason": "regression"}]
    assert vi.IntegrationResult.from_dict(d) == r


def test_integration_result_gate_failed_defaults_empty():
    r = vi.IntegrationResult()
    assert r.to_dict()["gate_failed"] == []


# ==========================================================================
# Export surface: make_gate + GATE_MANIFEST are exported for scheduling to wire
# GATE into the route (factory(runtime) -> (StateManifest, run_callable)).
# ==========================================================================

def test_make_gate_exported_and_returns_manifest_and_callable():
    assert hasattr(vi, "make_gate")
    assert hasattr(vi, "GATE_MANIFEST")


# ==========================================================================
# Determinism seams: the production gh issue-ref resolver + comment sink
# assemble the exact gh commands via injected fake runners (no network).
# ==========================================================================

def test_gh_closing_issue_ref_assembles_command():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0, stdout='[{"number": 42}]')

    ref = vi.gh_closing_issue_ref("acme/widget#3", repo="acme/widget",
                                  runner=fake_runner)
    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "pr", "view"]
    assert ref == "acme/widget#42"


def test_gh_closing_issue_ref_none_when_no_closing_issue():
    def fake_runner(cmd, **kwargs):  # noqa: ARG001
        return _FakeCompleted(returncode=0, stdout='[]')
    ref = vi.gh_closing_issue_ref("acme/widget#3", repo="acme/widget",
                                  runner=fake_runner)
    assert ref is None


def test_gh_issue_comment_sink_assembles_command():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted(returncode=0)

    vi.gh_issue_comment_sink("acme/widget#42", "hello body",
                             repo="acme/widget", runner=fake_runner)
    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "issue", "comment"]
    assert "--body" in cmd
    assert cmd[cmd.index("--body") + 1] == "hello body"
    assert "42" in cmd
