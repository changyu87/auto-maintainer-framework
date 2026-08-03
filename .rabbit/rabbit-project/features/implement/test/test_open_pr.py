#!/usr/bin/env python3
"""Deterministic tests for src/open_pr.py — the implementer's script-backed
worktree setup + explicit PR base (spec: "Script-backed worktree setup +
explicit PR base"; spec-rules §4 Script-Backed Orchestration).

The script fixes the live wrong-base STACKED PR bug (#844 based on #831's
branch, #846 on #844's): during a back-to-back drain burst the prompt-tier git
prose let consecutive implementer runs branch off the PREVIOUS loop branch
instead of `main`. The script makes the order-critical git sequence
UNCONDITIONAL and deterministic:

  1. resolve the repo default branch (`gh repo view --json defaultBranchRef`),
  2. `git fetch origin <default>`,
  3. `git worktree add <wt> -b <branch> origin/<default>` — start-point is the
     FRESHLY-FETCHED remote ref, NEVER local HEAD,
  4. `gh pr create --base <default>` — an EXPLICIT base, never inferred/tracked.

These tests drive the script through an INJECTABLE runner (no network), asserting
the exact argv the script emits so the start-point and the PR base can never
drift to a sibling loop branch.

Owner: changyu87
"""

import importlib.util
import os
from types import SimpleNamespace

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OPEN_PR = os.path.join(_FEATURE_DIR, "src", "open_pr.py")


def _load():
    spec = importlib.util.spec_from_file_location("open_pr", _OPEN_PR)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeRunner:
    """Records every argv the script runs and returns canned stdout for the two
    reads (default-branch resolve + pr create). No subprocess, no network."""

    def __init__(self, default_branch="main", pr_url="https://x/pull/1"):
        self.calls = []
        self.default_branch = default_branch
        self.pr_url = pr_url

    def __call__(self, argv, capture_output=True, text=True, cwd=None):
        self.calls.append(list(argv))
        stdout = ""
        if argv[:3] == ["gh", "repo", "view"]:
            stdout = self.default_branch + "\n"
        elif argv[:3] == ["gh", "pr", "create"]:
            stdout = self.pr_url + "\n"
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    def _argv_for(self, prefix):
        return [c for c in self.calls if c[:len(prefix)] == prefix]


def test_module_present_and_importable():
    assert os.path.isfile(_OPEN_PR), "src/open_pr.py must exist"
    _load()


def test_resolve_default_branch_reads_gh_repo_view():
    m = _load()
    r = FakeRunner(default_branch="main")
    branch = m.resolve_default_branch(r)
    assert branch == "main"
    view = r._argv_for(["gh", "repo", "view"])
    assert len(view) == 1, "must resolve the default branch exactly once"
    assert "defaultBranchRef" in view[0], (
        "must resolve via gh repo view --json defaultBranchRef")


def test_setup_worktree_fetches_the_default_from_origin():
    m = _load()
    r = FakeRunner(default_branch="main")
    m.setup_worktree(r, branch="loop/x", worktree="/tmp/wt")
    fetches = r._argv_for(["git", "fetch"])
    assert fetches, "setup must git fetch the default branch"
    assert fetches[0] == ["git", "fetch", "origin", "main"], (
        f"must fetch origin main, got {fetches[0]}")


def test_setup_worktree_start_point_is_origin_default_never_head():
    """The load-bearing invariant (fixes #844/#846 stacking): the worktree
    start-point is the FRESHLY-FETCHED remote ref origin/<default>, NEVER local
    HEAD / whatever is currently checked out."""
    m = _load()
    r = FakeRunner(default_branch="main")
    m.setup_worktree(r, branch="loop/x", worktree="/tmp/wt")
    adds = r._argv_for(["git", "-c", "core.hooksPath=/dev/null", "worktree",
                        "add"])
    assert len(adds) == 1
    argv = adds[0]
    assert argv == ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add",
                    "/tmp/wt", "-b", "loop/x",
                    "origin/main"], f"unexpected worktree add argv: {argv}"
    # explicit start-point present, and never HEAD or a bare local ref
    assert "origin/main" in argv
    assert "HEAD" not in argv


def test_setup_worktree_add_runs_hooks_free():
    """The worktree add (and any checkout it does) runs HOOKS-FREE via
    `-c core.hooksPath=/dev/null` immediately after `git`, so the TARGET repo's
    post-checkout hook (e.g. ssbdci-grimlock's render_nested_components) never
    fires in the implementer's disposable worktree — mirroring verify-integrate's
    reconcile/GATE hooks-off fix."""
    m = _load()
    r = FakeRunner(default_branch="main")
    m.setup_worktree(r, branch="loop/x", worktree="/tmp/wt")
    adds = [c for c in r.calls if "worktree" in c and "add" in c]
    assert len(adds) == 1, "exactly one worktree add"
    argv = adds[0]
    assert argv[:3] == ["git", "-c", "core.hooksPath=/dev/null"], (
        f"worktree add must run hooks-free (`git -c core.hooksPath=/dev/null` "
        f"first), got {argv}")


def test_second_consecutive_setup_still_branches_from_origin_default():
    """Back-to-back drain regression: a second consecutive setup must STILL
    branch from origin/<default>, never from a sibling loop branch a prior burst
    run left checked out — no stacking."""
    m = _load()
    r = FakeRunner(default_branch="main")
    m.setup_worktree(r, branch="loop/first", worktree="/tmp/wt1")
    m.setup_worktree(r, branch="loop/second", worktree="/tmp/wt2")
    adds = r._argv_for(["git", "-c", "core.hooksPath=/dev/null", "worktree",
                        "add"])
    assert len(adds) == 2
    for argv in adds:
        assert argv[-1] == "origin/main", (
            f"every worktree add must start from origin/main, got {argv}")
        assert "HEAD" not in argv


def test_create_pr_passes_explicit_base_default():
    """gh pr create is called with an EXPLICIT --base <default>, never an
    inferred/tracked base that could drift to a sibling loop branch."""
    m = _load()
    r = FakeRunner(default_branch="main")
    url = m.create_pr(r, branch="loop/x", title="t", body_file="/tmp/body",
                      label="auto-maintainer")
    assert url == "https://x/pull/1"
    creates = r._argv_for(["gh", "pr", "create"])
    assert len(creates) == 1
    argv = creates[0]
    assert "--base" in argv, "gh pr create must pass an explicit --base"
    assert argv[argv.index("--base") + 1] == "main", (
        f"--base must be the resolved default branch, got {argv}")
    assert "--head" in argv and argv[argv.index("--head") + 1] == "loop/x"
    assert "--label" in argv and argv[argv.index("--label") + 1] == "auto-maintainer"


def test_create_pr_forwards_repo_when_given():
    m = _load()
    r = FakeRunner(default_branch="trunk")
    m.create_pr(r, branch="b", title="t", body_file="/tmp/body",
                repo="o/rr")
    argv = r._argv_for(["gh", "pr", "create"])[0]
    assert "--repo" in argv and argv[argv.index("--repo") + 1] == "o/rr"
    assert argv[argv.index("--base") + 1] == "trunk"


def test_setup_worktree_returns_the_default_branch():
    m = _load()
    r = FakeRunner(default_branch="develop")
    assert m.setup_worktree(r, branch="b", worktree="/tmp/wt") == "develop"
    adds = r._argv_for(["git", "-c", "core.hooksPath=/dev/null", "worktree",
                        "add"])
    assert adds[0][-1] == "origin/develop"


def test_cli_setup_uses_injected_runner():
    """The CLI `setup` subcommand drives the same deterministic sequence and is
    testable via an injected runner (no subprocess)."""
    m = _load()
    r = FakeRunner(default_branch="main")
    rc = m.main(["setup", "--branch", "loop/x", "--worktree", "/tmp/wt"],
                runner=r)
    assert rc == 0
    adds = r._argv_for(["git", "-c", "core.hooksPath=/dev/null", "worktree",
                        "add"])
    assert adds and adds[0][-1] == "origin/main"


def test_cli_create_uses_injected_runner():
    m = _load()
    r = FakeRunner(default_branch="main")
    rc = m.main(["create", "--branch", "loop/x", "--title", "t",
                 "--body-file", "/tmp/body", "--label", "auto-maintainer"],
                runner=r)
    assert rc == 0
    argv = r._argv_for(["gh", "pr", "create"])[0]
    assert argv[argv.index("--base") + 1] == "main"


def test_runner_failure_raises():
    """A nonzero shell result is a hard, locatable error (deterministic
    failure), not a silent pass."""
    m = _load()

    def failing(argv, capture_output=True, text=True, cwd=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    try:
        m.resolve_default_branch(failing)
    except Exception:
        return
    raise AssertionError("resolve_default_branch must raise on a nonzero result")
