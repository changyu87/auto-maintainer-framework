#!/usr/bin/env python3
"""End-to-end tests for the GATE setup-robustness behaviours (spec "Setup
robustness — never gate against a stale or wrong tree", DESIGN §2.2 [v2]).

The cumulative GATE integration worktree lives at a FIXED path, so a crashed
prior tick can leave it behind. GATE must:

  (a) Before `git worktree add`, best-effort clear any stale leftover
      (`git worktree remove --force` then `git worktree prune`) so a leftover
      never wedges every subsequent tick.
  (b) Check the `git worktree add` RETURN CODE: on failure it writes an EMPTY
      `gate_results` list (a setup failure is not any PR's fault) so INTEGRATE
      merges nothing, posts NO gate-fail marker, and the tick converges to idle
      — no false-fail into the park threshold. No per-PR gating is attempted.
  (c) Inside the per-PR loop, check the `git fetch origin pull/<n>/head` RETURN
      CODE before merging: on a fetch failure return
      `GateResult{passed:False, reason:"fetch-failed"}` for that PR WITHOUT
      merging, so a failed fetch can never silently merge the PREVIOUS PR's
      stale `FETCH_HEAD` into the cumulative tree. No rollback (no merge ran).

Everything external is behind the injectable `runner`, so these tests drive the
logic with a FAKE runner scripting per-command returncodes by matching argv —
NO real git, NO real PRs, NO network.

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

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402


_DEFAULT_BRANCH = "main"
_WORKTREE = "/tmp/am-gate-integration-test"


def _verdict(number=1, ok=True):
    return vi.Verdict(
        pr_ref=f"acme/widget#{number}",
        url=f"https://github.com/acme/widget/pull/{number}",
        ok=ok,
        ci_state="passing",
        mergeable=True,
        base=_DEFAULT_BRANCH,
        reasons=[],
    ).to_dict()


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _ScriptedGit:
    """A fake git+regression runner that scripts per-command returncodes by
    matching the argv shape. Records every command so tests can assert order and
    which commands ran / did not run.

    - `worktree_add_fails`: `git worktree add ...` returns nonzero.
    - `fetch_fails`: set of PR numbers whose `git fetch ... pull/<n>/head`
      returns nonzero (fetch failure).
    """

    def __init__(self, worktree_add_fails=False, fetch_fails=(),
                 fetch_stderr="fatal: couldn't find remote ref"):
        self._worktree_add_fails = worktree_add_fails
        self._fetch_fails = set(fetch_fails)
        self._fetch_stderr = fetch_stderr
        self.commands = []
        self._current_pr = None

    def __call__(self, cmd, **kwargs):
        is_git = isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
        self.commands.append(list(cmd) if is_git else [cmd])
        if not is_git:
            # regression run
            return _FakeCompleted(returncode=0, stdout="ok")
        sub = self._git_sub(cmd)
        if sub == "worktree" and "add" in cmd:
            if self._worktree_add_fails:
                return _FakeCompleted(returncode=1,
                                      stderr="fatal: worktree add failed")
            return _FakeCompleted(returncode=0)
        if sub == "fetch":
            num = self._pr_num_from_cmd(cmd)
            if num is not None:
                self._current_pr = num
            if num in self._fetch_fails:
                return _FakeCompleted(returncode=1, stderr=self._fetch_stderr)
            return _FakeCompleted(returncode=0)
        if sub == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="deadbeef\n")
        return _FakeCompleted(returncode=0)

    @staticmethod
    def _git_sub(cmd):
        i = 1
        while i < len(cmd) and cmd[i] == "-C":
            i += 2
        return cmd[i] if i < len(cmd) else ""

    @staticmethod
    def _pr_num_from_cmd(cmd):
        for tok in cmd:
            for part in str(tok).replace("/", " ").split():
                if part.isdigit():
                    return int(part)
        return None


def _fake_issue_resolver(mapping=None):
    mapping = mapping or {}

    def resolve(pr_ref, repo=None):  # noqa: ARG001
        return mapping.get(pr_ref)
    return resolve


def _is_git_sub(cmd, sub):
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
    ctx.register_slot(vi.GATE_RESULTS_SLOT["name"],
                      vi.GATE_RESULTS_SLOT["schema"],
                      version=vi.GATE_RESULTS_SLOT["version"])
    return ctx


def _run_gate(gate, ctx):
    result = gate.run(ctx)
    vocab = fc.SignalVocabulary(vi.GATE_SIGNALS)
    assert fc.validate_state_result(result).passed is True
    fc.apply_result(ctx, vi.GATE_MANIFEST, result, vocab)
    return ctx.read("gate_results")


def _idx_of(commands, predicate):
    """Index of the first recorded command matching `predicate`, or -1."""
    for i, c in enumerate(commands):
        if predicate(c):
            return i
    return -1


# ==========================================================================
# (a) Stale worktree cleanup: before `git worktree add`, GATE best-effort
# clears any leftover with `git worktree remove --force` then `git worktree
# prune`. Both happen BEFORE the add and are ignored (returncode not checked).
# ==========================================================================

def test_gate_cleans_stale_worktree_before_add():
    fake = _ScriptedGit()
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    _run_gate(gate, ctx)

    remove_idx = _idx_of(
        fake.commands,
        lambda c: _is_git_sub(c, "worktree") and "remove" in c and "--force" in c)
    prune_idx = _idx_of(
        fake.commands,
        lambda c: _is_git_sub(c, "worktree") and "prune" in c)
    add_idx = _idx_of(
        fake.commands,
        lambda c: _is_git_sub(c, "worktree") and "add" in c)

    assert remove_idx != -1, "GATE must best-effort `git worktree remove --force`"
    assert prune_idx != -1, "GATE must best-effort `git worktree prune`"
    assert add_idx != -1, "GATE must still `git worktree add`"
    # cleanup precedes the add.
    assert remove_idx < add_idx
    assert prune_idx < add_idx


def test_gate_stale_cleanup_targets_the_fixed_worktree_path():
    fake = _ScriptedGit()
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    _run_gate(gate, ctx)
    remove_cmds = [c for c in fake.commands
                   if _is_git_sub(c, "worktree") and "remove" in c]
    assert remove_cmds, "no `git worktree remove` recorded"
    assert _WORKTREE in remove_cmds[0], (
        f"remove must target the fixed worktree path: {remove_cmds[0]}")


# ==========================================================================
# (b) `git worktree add` fails: GATE writes an EMPTY gate_results list, attempts
# NO per-PR gating (no fetch / merge), and does NOT enter the try/finally
# (no worktree remove after a failed add). The tick converges to idle.
# ==========================================================================

def test_gate_worktree_add_failure_writes_empty_gate_results():
    fake = _ScriptedGit(worktree_add_fails=True)
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])
    results = _run_gate(gate, ctx)

    # EMPTY list — a setup failure is not any PR's fault (no gate-fail marker).
    assert results == []


def test_gate_worktree_add_failure_attempts_no_per_pr_gating():
    fake = _ScriptedGit(worktree_add_fails=True)
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])
    _run_gate(gate, ctx)

    # No PR was fetched or merged after the failed add.
    assert not any(_is_git_sub(c, "fetch") for c in fake.commands)
    assert not any(_is_git_sub(c, "merge") for c in fake.commands)
    # No regression run either.
    assert not any(c and c[0] != "git" for c in fake.commands)


def test_gate_worktree_add_failure_does_not_remove_after_failed_add():
    """A failed add did not create a worktree, so GATE must NOT enter the
    try/finally that removes it (nothing to remove). The only `worktree remove`
    allowed is the pre-add stale cleanup, which precedes the add."""
    fake = _ScriptedGit(worktree_add_fails=True)
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    _run_gate(gate, ctx)

    add_idx = _idx_of(
        fake.commands,
        lambda c: _is_git_sub(c, "worktree") and "add" in c)
    assert add_idx != -1
    # No `worktree remove` AFTER the failed add (only the pre-add stale cleanup).
    removes_after_add = [
        i for i, c in enumerate(fake.commands)
        if _is_git_sub(c, "worktree") and "remove" in c and i > add_idx]
    assert removes_after_add == [], (
        "GATE must not remove a worktree that a failed add never created")


# ==========================================================================
# (c) `git fetch` fails for a PR: GATE returns a failed GateResult with reason
# 'fetch-failed' for that PR WITHOUT merging it (never merges the prior PR's
# stale FETCH_HEAD), and does NOT roll back (no merge happened).
# ==========================================================================

def test_gate_fetch_failure_marks_pr_fetch_failed_without_merge():
    fake = _ScriptedGit(fetch_fails={1})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(
                       {"acme/widget#1": "acme/widget#41"}),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    results = _run_gate(gate, ctx)

    assert len(results) == 1
    r = results[0]
    assert r["pr_ref"] == "acme/widget#1"
    assert r["passed"] is False
    assert r["reason"] == "fetch-failed"
    assert r["issue_ref"] == "acme/widget#41"
    # failure_summary carries the fetch error tail.
    assert r["failure_summary"]

    # NO merge was attempted for the fetch-failed PR.
    assert not any(_is_git_sub(c, "merge") and "--abort" not in c
                   for c in fake.commands)
    # NO rollback (no merge happened, so no reset --hard).
    assert not any(_is_git_sub(c, "reset") and "--hard" in c
                   for c in fake.commands)


def test_gate_fetch_failure_of_one_pr_does_not_block_others():
    """PR #1's fetch fails (excluded, fetch-failed); PR #2 fetches fine and is
    gated normally — a failed fetch never merges the prior PR's stale FETCH_HEAD
    and never poisons the rest of the batch."""
    fake = _ScriptedGit(fetch_fails={1})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1), _verdict(number=2)])
    results = _run_gate(gate, ctx)
    by_ref = {r["pr_ref"]: r for r in results}

    assert by_ref["acme/widget#1"]["passed"] is False
    assert by_ref["acme/widget#1"]["reason"] == "fetch-failed"
    assert by_ref["acme/widget#2"]["passed"] is True
    assert by_ref["acme/widget#2"]["reason"] is None

    # PR #2 WAS merged (a real merge, not aborted); PR #1 was NOT merged.
    merge_cmds = [c for c in fake.commands
                  if _is_git_sub(c, "merge") and "--abort" not in c]
    assert len(merge_cmds) == 1, (
        "exactly one PR (the fetch-OK one) should have been merged")


def test_gate_fetch_failure_worktree_still_removed():
    """A fetch failure inside the per-PR loop still leaves the (successfully
    added) worktree removed by the finally-cleanup."""
    fake = _ScriptedGit(fetch_fails={1})
    gate = vi.Gate(regression_command="pytest", runner=fake,
                   repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                   issue_resolver=_fake_issue_resolver(),
                   worktree_dir=_WORKTREE)
    ctx = _fresh_ctx()
    ctx.write("verdicts", [_verdict(number=1)])
    _run_gate(gate, ctx)
    add_idx = _idx_of(
        fake.commands,
        lambda c: _is_git_sub(c, "worktree") and "add" in c)
    removes_after_add = [
        i for i, c in enumerate(fake.commands)
        if _is_git_sub(c, "worktree") and "remove" in c and i > add_idx]
    assert removes_after_add, "worktree must still be removed after a fetch fail"
