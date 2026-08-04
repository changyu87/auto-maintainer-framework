#!/usr/bin/env python3
"""open_pr.py — the implementer's script-backed worktree setup + explicit PR base.

The worktree-creation and PR-open steps were previously PROMPT-TIER prose in the
shipped implementer subagent (`git worktree add … origin/<default>` with a
discretionary "fetch first if needed", `gh pr create --base <default>` with
`<default>` a model-filled placeholder). During a back-to-back drain burst this
let consecutive implementer runs branch off the PREVIOUS loop branch instead of
`main` and open WRONG-BASE STACKED PRs (e.g. #844 based on #831's head, #846 on
#844's), which INTEGRATE then refuses and nothing recovers.

Per spec-rules §4 (Script-Backed Orchestration), that order-critical git
sequence lives here as a DETERMINISTIC script the subagent INVOKES rather than
hand-runs. It UNCONDITIONALLY:

  1. resolves the repo default branch (`gh repo view --json defaultBranchRef`),
  2. `git fetch origin <default>`,
  3. `git -c core.hooksPath=/dev/null worktree add <wt> -b <branch>
     origin/<default>` — the start-point is the FRESHLY-FETCHED remote ref,
     NEVER local HEAD / whatever is currently checked out, and the add runs
     HOOKS-FREE so the target repo's post-checkout hook never fires in the
     disposable worktree, and
  4. opens the PR with an EXPLICIT `gh pr create --base <default>` (never an
     inferred/tracked base).

So the PR base can never drift to a sibling loop branch, regardless of what a
prior burst run left checked out. The script owns the computed values (default
branch, branch name, worktree path) with an INJECTABLE runner for deterministic
tests; the subagent calls it in place of the raw git/gh prose.

Self-contained: stdlib-only (argparse/subprocess/sys), NO sibling-lib import, so
it ships byte-for-byte to the installed plugin's lib/ (mirroring test_gate.py).

Usage:
  open_pr.py setup  --branch <name> --worktree <path> [--repo <owner/repo>]
  open_pr.py create --branch <head> --title <t> --body-file <f>
                    [--label <l>] [--repo <owner/repo>]

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the model-backed implement-then-PR doer
  (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the
  Handoff contract reaches a breaking major version. See docs/spec.md.
"""

import argparse
import subprocess
import sys


class OpenPRError(RuntimeError):
    """A git/gh step exited nonzero — a locatable, deterministic failure."""


def _run(runner, argv):
    """Run one command via the injectable runner; raise on a nonzero result.

    The runner has the subprocess.run signature (returns an object with
    .returncode/.stdout/.stderr). Injectable so tests assert exact argv without
    any network."""
    result = runner(argv, capture_output=True, text=True)
    if result.returncode != 0:
        raise OpenPRError(
            f"command failed ({result.returncode}): {' '.join(argv)}\n"
            f"{result.stderr}")
    return (result.stdout or "").strip()


def resolve_default_branch(runner, repo=None):
    """Resolve the repo's default branch via `gh repo view --json
    defaultBranchRef`. Unconditional — the base/start-point is always the freshly
    resolved default, never inferred from local state."""
    argv = ["gh", "repo", "view"]
    if repo:
        argv.append(repo)
    argv += ["--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"]
    return _run(runner, argv)


def setup_worktree(runner, branch, worktree, repo=None):
    """Fetch the default branch fresh and create the worktree on a new branch off
    origin/<default>. Returns the resolved default branch.

    The start-point is ALWAYS `origin/<default>` (the freshly-fetched remote
    ref), never local HEAD — so a second consecutive run in a drain burst cannot
    stack on a sibling loop branch."""
    default = resolve_default_branch(runner, repo=repo)
    _run(runner, ["git", "fetch", "origin", default])
    # Hooks-free worktree add: `-c core.hooksPath=/dev/null` so the TARGET repo's
    # post-checkout hook (e.g. ssbdci-grimlock's render_nested_components) never
    # fires in the implementer's disposable worktree — the throwaway tree is only
    # for mechanical edit/commit/push and never needs the repo's checkout-render
    # hooks. Mirrors verify-integrate's reconcile/GATE hooks-off fix.
    _run(runner, ["git", "-c", "core.hooksPath=/dev/null", "worktree", "add",
                  worktree, "-b", branch, f"origin/{default}"])
    return default


def create_pr(runner, branch, title, body_file=None, body=None, label=None,
              repo=None):
    """Open the PR with an EXPLICIT `--base <default>` (never inferred/tracked)
    and `--head <branch>`. Returns the created PR url (gh's stdout)."""
    default = resolve_default_branch(runner, repo=repo)
    argv = ["gh", "pr", "create", "--base", default, "--head", branch,
            "--title", title]
    if body_file is not None:
        argv += ["--body-file", body_file]
    elif body is not None:
        argv += ["--body", body]
    if label:
        argv += ["--label", label]
    if repo:
        argv += ["--repo", repo]
    return _run(runner, argv)


def main(argv=None, runner=None):
    if runner is None:
        runner = subprocess.run
    parser = argparse.ArgumentParser(description="script-backed worktree setup "
                                                 "+ explicit PR base")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="fetch default + create worktree off "
                                           "origin/<default>")
    p_setup.add_argument("--branch", required=True)
    p_setup.add_argument("--worktree", required=True)
    p_setup.add_argument("--repo", default=None)

    p_create = sub.add_parser("create", help="gh pr create with explicit "
                                             "--base <default>")
    p_create.add_argument("--branch", required=True, help="the head branch")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--body-file", dest="body_file", default=None)
    p_create.add_argument("--body", default=None)
    p_create.add_argument("--label", default=None)
    p_create.add_argument("--repo", default=None)

    args = parser.parse_args(argv)

    if args.cmd == "setup":
        default = setup_worktree(runner, branch=args.branch,
                                 worktree=args.worktree, repo=args.repo)
        sys.stdout.write(default + "\n")
    else:
        url = create_pr(runner, branch=args.branch, title=args.title,
                        body_file=args.body_file, body=args.body,
                        label=args.label, repo=args.repo)
        sys.stdout.write(url + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
