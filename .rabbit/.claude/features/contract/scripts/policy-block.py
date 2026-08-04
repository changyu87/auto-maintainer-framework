#!/usr/bin/env python3
"""policy-block.py — emit the canonical rabbit-workflow policy block to stdout.

This block is the MANDATORY prepend for every Agent dispatch (rabbit's own
subagents AND Claude's built-in ones). The dispatcher captures stdout and
prepends to the prompt field of the Agent tool call. Per hard-rules R6.

Usage:
  policy-block.py                                # philosophy.md + spec-rules.md + coding-rules.md
  policy-block.py --include <path>               # plus the named file
  policy-block.py --include a --include b ...    # multiple includes compose

Files are looked up at:
  <repo>/.claude/features/policy/philosophy.md
  <repo>/.claude/features/policy/spec-rules.md
  <repo>/.claude/features/policy/coding-rules.md
where <repo> is computed from this script's location.

Exit:
  0 success
  1 a --include path is missing
  2 invocation error

Version: 1.1.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when policy injection is handled natively by the dispatch infrastructure.
"""

import os
import subprocess
import sys

# Make the contract feature's lib/ importable so we can reuse the canonical
# framing from contract.lib.policy_block instead of inlining it here.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.policy_block import render_policy_block  # noqa: E402


def get_repo_root():
    env_root = os.environ.get("RABBIT_ROOT")
    if env_root:
        return env_root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "-C", script_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        return None


def parse_args(argv):
    includes = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print_usage()
            sys.exit(0)
        elif arg == "--include":
            if i + 1 >= len(argv):
                print("ERROR: --include requires a path arg", file=sys.stderr)
                sys.exit(2)
            includes.append(argv[i + 1])
            i += 2
        else:
            print(f"ERROR: unknown arg '{arg}'", file=sys.stderr)
            sys.exit(2)
    return includes


def print_usage():
    print(__doc__, file=sys.stderr)


def main():
    includes = parse_args(sys.argv[1:])

    repo_root = get_repo_root()
    if not repo_root:
        print("ERROR: cannot determine repo root", file=sys.stderr)
        sys.exit(1)

    policy_dir = os.path.join(repo_root, ".claude", "features", "policy")
    phil = os.path.join(policy_dir, "philosophy.md")
    spec_rules = os.path.join(policy_dir, "spec-rules.md")
    coding_rules = os.path.join(policy_dir, "coding-rules.md")

    # Validate --include paths upfront.
    for p in includes:
        if not os.path.isfile(p):
            print(f"ERROR: --include path does not exist: {p}", file=sys.stderr)
            sys.exit(1)

    # Sanity: canonical files must exist.
    for f in (phil, spec_rules, coding_rules):
        if not os.path.isfile(f):
            print(f"ERROR: missing canonical policy file: {f}", file=sys.stderr)
            sys.exit(1)

    paths = [phil, spec_rules, coding_rules, *includes]
    print(render_policy_block(paths))
    sys.exit(0)


if __name__ == "__main__":
    main()
