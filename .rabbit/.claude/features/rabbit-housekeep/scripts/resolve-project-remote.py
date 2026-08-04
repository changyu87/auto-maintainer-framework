#!/usr/bin/env python3
"""resolve-project-remote.py — resolve the consuming project's GitHub remote
to a 'owner/repo' slug for use as RABBIT_ISSUE_REPO (issue #1206).

When a housekeep wave files a sub-issue via file-item.py, the sub-issue must
land in the CONSUMING PROJECT's GitHub issue tracker, not in the rabbit
framework's own repo. file-item.py routes to RABBIT_ISSUE_REPO when that env
var is set; this script resolves the slug from the consuming project's git
remote so the wave can set RABBIT_ISSUE_REPO before invoking file-item.py.

Usage:
  resolve-project-remote.py [--root <dir>]
    Run `git -C <root> remote get-url origin`, parse the URL, print slug.
    Exits 0 and prints 'owner/repo' on success.
    Exits 1 on failure (no git repo, no origin remote, unrecognised URL).

  resolve-project-remote.py --url <remote-url>
    Parse a raw URL string directly (for testing without a live git repo).
    Exits 0 and prints 'owner/repo' on success, 1 on unrecognised format.

Supported URL formats:
  - SSH:   git@github.com:owner/repo.git  -> owner/repo
  - HTTPS: https://github.com/owner/repo.git -> owner/repo
  - HTTPS: https://github.com/owner/repo    -> owner/repo

Version: 0.1.0
Owner: rabbit-workflow team
Deprecation criterion: when rabbit-housekeep is retired or sub-issue routing
    is handled natively by the rabbit CLI.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys


# Match SSH: git@github.com:owner/repo[.git]
_SSH_RE = re.compile(r"git@[^:]+:([^/]+/[^/]+?)(?:\.git)?$")
# Match HTTPS: https://github.com/owner/repo[.git]
_HTTPS_RE = re.compile(r"https?://[^/]+/([^/]+/[^/]+?)(?:\.git)?$")


def parse_url(url: str) -> str | None:
    """Return 'owner/repo' from a GitHub remote URL, or None if unrecognised."""
    url = url.strip()
    for pattern in (_SSH_RE, _HTTPS_RE):
        m = pattern.match(url)
        if m:
            return m.group(1)
    return None


def remote_from_git(root: str) -> str | None:
    """Return the origin remote URL from `git -C <root> remote get-url origin`."""
    try:
        out = subprocess.check_output(
            ["git", "-C", root, "remote", "get-url", "origin"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return out.strip()
    except subprocess.CalledProcessError:
        return None


def main(argv: list[str]) -> int:
    if "--url" in argv:
        idx = argv.index("--url")
        if idx + 1 >= len(argv):
            sys.stderr.write("ERROR: --url requires a value\n")
            return 1
        url = argv[idx + 1]
        slug = parse_url(url)
        if slug is None:
            sys.stderr.write(f"ERROR: unrecognised remote URL format: {url!r}\n")
            return 1
        print(slug)
        return 0

    root = os.getcwd()
    if "--root" in argv:
        idx = argv.index("--root")
        if idx + 1 >= len(argv):
            sys.stderr.write("ERROR: --root requires a value\n")
            return 1
        root = argv[idx + 1]

    url = remote_from_git(root)
    if url is None:
        sys.stderr.write(
            f"ERROR: could not get origin remote from {root!r}; "
            "is this a git repo with an 'origin' remote?\n"
        )
        return 1

    slug = parse_url(url)
    if slug is None:
        sys.stderr.write(
            f"ERROR: unrecognised remote URL format: {url!r}\n"
        )
        return 1

    print(slug)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
