#!/usr/bin/env python3
"""rabbit-issue: show / close / reopen a GitHub issue.

Subcommands:
  show   <N>                              — print issue JSON
  close  <N> --reason completed   --commit-sha <sha> [--comment <c>]
  close  <N> --reason completed   --findings-comment-url <url> [--comment <c>]
  close  <N> --reason not-planned --reason-text <text> [--comment <c>]
  reopen <N> [--comment <c>]

`close` and `reopen` enforce the actionability safety guard (the issue
must carry a valid `feature:` label) before issuing the gh command.

The two close reasons are gated so a closure asserts something real
(issue #423):
  - `completed` requires EXACTLY ONE deliverable proof, mutually
    exclusive:
      - `--commit-sha <sha>` that resolves to a real commit in the local
        git repo — "completed" can only be claimed when work actually
        landed; or
      - `--findings-comment-url <url>`, a plausible GitHub issue-comment
        URL — the SMALL research-outcome path (issue #841): the findings
        are appended as a comment and that comment is the deliverable, so
        there is no landed commit. The URL is persisted as the close
        comment (audit link).
  - `not-planned` requires `--reason-text <text>` of at least 50 chars
    that is free of banned boilerplate phrases — a deliberate, specific
    justification, not a reflexive deferral.

Version: 1.3.0
Owner: rabbit-workflow team
Deprecation criterion: when rabbit-issue is retired
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _gh import (  # noqa: E402
    gh_issue_view,
    repo_slug,
    require_actionable,
    require_auth,
)

VALID_REASONS = ("completed", "not-planned")
SHOW_FIELDS = "number,title,state,stateReason,labels,body,createdAt,closedAt"

# Minimum length for a not-planned justification (issue #423 Part D).
REASON_MIN_LEN = 50

# A plausible GitHub issue-comment URL (issue #841). Shape:
#   https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<id>
COMMENT_URL_RE = re.compile(
    r"^https://github\.com/[^/\s]+/[^/\s]+/issues/\d+#issuecomment-\d+$"
)

# Boilerplate that signals a reflexive deferral rather than a real
# justification. Matched case-insensitively as substrings of --reason-text.
BANNED_PHRASES = (
    "too risky",
    "out of scope",
    "out-of-scope",
    "declined autonomous dispatch",
    "not now",
    "later",
    "don't want",
    "do not want",
)


def cmd_show(args: argparse.Namespace) -> None:
    issue = gh_issue_view(args.number, SHOW_FIELDS)
    print(json.dumps(issue, indent=2))


def _validate_commit_sha(sha: str) -> None:
    """Exit non-zero unless `sha` resolves to a commit in the local repo."""
    r = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "{}^{{commit}}".format(sha)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.exit(
            "rabbit-issue: --commit-sha {!r} does not resolve to a commit in "
            "the local git repo; `completed` requires a real landed commit"
            .format(sha)
        )


def _validate_findings_comment_url(url: str) -> None:
    """Exit non-zero unless `url` is a plausible GitHub issue-comment URL."""
    if not COMMENT_URL_RE.match(url):
        sys.exit(
            "rabbit-issue: --findings-comment-url {!r} is not a GitHub "
            "issue-comment URL; expected the form "
            "https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<id>"
            .format(url)
        )


def _validate_reason_text(text: str) -> None:
    """Exit non-zero unless `text` is a specific, non-boilerplate reason."""
    if len(text) < REASON_MIN_LEN:
        sys.exit(
            "rabbit-issue: --reason-text must be at least {} characters "
            "(got {}); state a specific reason for not-planned closure"
            .format(REASON_MIN_LEN, len(text))
        )
    lowered = text.lower()
    for phrase in BANNED_PHRASES:
        if phrase in lowered:
            sys.exit(
                "rabbit-issue: --reason-text contains banned boilerplate "
                "{!r}; give a specific, concrete justification".format(phrase)
            )


def cmd_close(args: argparse.Namespace) -> None:
    # Safety boundary first: refuse to act on non-actionable issues.
    require_actionable(args.number)

    # Then gate the close reason before the gh call (issue #423).
    if args.reason == "completed":
        # `completed` requires EXACTLY ONE deliverable proof: a landed
        # commit, or a research-findings comment URL (issue #841). They
        # are mutually exclusive.
        if args.commit_sha and args.findings_comment_url:
            sys.exit(
                "rabbit-issue: `--reason completed` takes EITHER --commit-sha "
                "OR --findings-comment-url, not both; pick the one proof that "
                "matches the deliverable"
            )
        if args.commit_sha:
            _validate_commit_sha(args.commit_sha)
        elif args.findings_comment_url:
            _validate_findings_comment_url(args.findings_comment_url)
        else:
            sys.exit(
                "rabbit-issue: `--reason completed` requires --commit-sha "
                "<sha> (a real landed commit) or --findings-comment-url "
                "<url> (a research-findings comment)"
            )
    else:  # not-planned
        if args.findings_comment_url:
            sys.exit(
                "rabbit-issue: --findings-comment-url applies only to "
                "`--reason completed`; a not-planned close requires "
                "--reason-text"
            )
        if not args.reason_text:
            sys.exit(
                "rabbit-issue: `--reason not-planned` requires --reason-text "
                "<text> (>= {} chars, no boilerplate)".format(REASON_MIN_LEN)
            )
        _validate_reason_text(args.reason_text)

    # Persist the validated audit trail as the close comment. A not-planned
    # close persists --reason-text (issue #476); a completed close via the
    # findings-comment path persists the comment URL (issue #841). The text
    # was being validated then dropped, leaving the closed issue with no
    # audit trail. When --comment is also supplied the validated text leads,
    # separated from the comment by a blank line.
    comment = args.comment
    if args.reason == "not-planned":
        lead = args.reason_text
    elif args.findings_comment_url:
        lead = args.findings_comment_url
    else:
        lead = ""
    if lead:
        comment = (
            "{}\n\n{}".format(lead, args.comment) if args.comment else lead
        )

    # argparse accepts the hyphen form (Python/shell-friendly), but
    # `gh issue close --reason` only accepts "completed" or "not planned"
    # (with a space). Translate at the gh boundary (issue #419). Only
    # "not-planned" needs translation; "completed" is unchanged.
    reason = args.reason.replace("-", " ")
    cmd = ["gh", "issue", "close", str(args.number),
           "-R", repo_slug(), "--reason", reason]
    if comment:
        cmd += ["--comment", comment]
    subprocess.run(cmd, check=True)


def cmd_reopen(args: argparse.Namespace) -> None:
    require_actionable(args.number)
    cmd = ["gh", "issue", "reopen", str(args.number), "-R", repo_slug()]
    if args.comment:
        cmd += ["--comment", args.comment]
    subprocess.run(cmd, check=True)


def main() -> None:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("show")
    s.add_argument("number", type=int)

    c = sub.add_parser("close")
    c.add_argument("number", type=int)
    c.add_argument("--reason", required=True, choices=VALID_REASONS)
    c.add_argument("--comment", default="")
    # Gating args (issue #423); required-ness is enforced per-reason in
    # cmd_close so the error message can name the reason it applies to.
    c.add_argument("--commit-sha", default="",
                   help="with --reason completed: a real commit in the local "
                        "git repo (mutually exclusive with "
                        "--findings-comment-url)")
    c.add_argument("--findings-comment-url", default="",
                   help="with --reason completed: a GitHub issue-comment URL "
                        "for a research SMALL-outcome close (mutually "
                        "exclusive with --commit-sha)")
    c.add_argument("--reason-text", default="",
                   help="required with --reason not-planned; >= {} chars, "
                        "no boilerplate".format(REASON_MIN_LEN))

    r = sub.add_parser("reopen")
    r.add_argument("number", type=int)
    r.add_argument("--comment", default="")

    args = p.parse_args()
    require_auth()
    {"show": cmd_show, "close": cmd_close, "reopen": cmd_reopen}[args.cmd](args)


if __name__ == "__main__":
    main()
