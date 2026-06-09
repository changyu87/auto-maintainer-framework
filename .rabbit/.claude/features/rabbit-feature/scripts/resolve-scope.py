#!/usr/bin/env python3
# resolve-scope.py — build a prompt that maps a request to affected rabbit features.
#
# Usage:
#   resolve-scope.py [--verbose|-v] "<request-description>"
#   resolve-scope.py --help
#
# Output: assembled prompt to stdout. Caller dispatches with default model.
# Agent response JSON: {"features": ["feat-a"], "rationale": "one sentence"}
#
# Exit codes:
#   0  success
#   1  runtime error (find-feature.py or format-feature-context.py failed)
#   2  invocation error (bad arguments, missing required env when not in git)
#
# Version: 1.1.1
# Owner: rabbit-workflow team (rabbit-feature)
# Deprecation criterion: when feature-scope resolution is automated by the dispatch infrastructure.

import argparse
import os
import subprocess
import sys


def _vprint(verbose, msg):
    if verbose:
        sys.stderr.write(f"[resolve-scope] {msg}\n")


def main():
    parser = argparse.ArgumentParser(
        prog="resolve-scope.py",
        description=(
            "Build a prompt that maps a natural-language request to the list of "
            "rabbit features whose files the request will modify. The prompt is "
            "written to stdout; the caller dispatches it to a default-model Agent."
        ),
        epilog=(
            "Agent response JSON schema: "
            '{"features": ["feat-a"], "rationale": "one sentence"}. '
            "Exit 0 on success, 1 on runtime error, 2 on invocation error."
        ),
    )
    parser.add_argument(
        "request",
        help="natural-language request description",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="emit debug progress to stderr",
    )
    # argparse exits 2 on bad usage by default — matches contract.
    args = parser.parse_args()

    request = args.request
    verbose = args.verbose

    script_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.environ.get("RABBIT_ROOT")
    if repo_root:
        _vprint(verbose, f"using RABBIT_ROOT={repo_root}")
    else:
        try:
            repo_root = subprocess.check_output(
                ["git", "-C", script_dir, "rev-parse", "--show-toplevel"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            _vprint(verbose, f"resolved repo root via git: {repo_root}")
        except (subprocess.CalledProcessError, FileNotFoundError):
            sys.stderr.write(
                "ERROR: cannot resolve repo root.\n"
                "  RABBIT_ROOT environment variable is not set, and the script is\n"
                "  not located inside a git working tree. Set RABBIT_ROOT to the\n"
                "  rabbit-workflow repository root (e.g., RABBIT_ROOT=/path/to/repo),\n"
                "  or run the script from inside a git checkout of the repo.\n"
            )
            sys.exit(1)

    find_feature = os.path.join(repo_root, ".claude/features/contract/scripts/find-feature.py")
    if not os.path.isfile(find_feature):
        sys.stderr.write(f"ERROR: find-feature.py not found: {find_feature}\n")
        sys.exit(1)

    format_script = os.path.join(script_dir, "format-feature-context.py")
    if not os.path.isfile(format_script):
        sys.stderr.write(f"ERROR: format-feature-context.py not found: {format_script}\n")
        sys.exit(1)

    _vprint(verbose, f"invoking find-feature.py list-json on {repo_root}")
    ff_proc = subprocess.run(
        ["python3", find_feature, repo_root, "list-json"],
        capture_output=True,
    )
    if ff_proc.returncode != 0:
        sys.stderr.write(
            f"ERROR: find-feature.py exited {ff_proc.returncode}\n"
            f"  stderr: {ff_proc.stderr.decode(errors='replace').strip()}\n"
        )
        sys.exit(1)
    list_json = ff_proc.stdout
    _vprint(verbose, f"find-feature.py emitted {len(list_json)} bytes")

    _vprint(verbose, "invoking format-feature-context.py")
    fmt_proc = subprocess.run(
        ["python3", format_script],
        input=list_json,
        capture_output=True,
    )
    if fmt_proc.returncode != 0:
        sys.stderr.write(
            f"ERROR: format-feature-context.py exited {fmt_proc.returncode}\n"
            f"  stderr: {fmt_proc.stderr.decode(errors='replace').strip()}\n"
        )
        sys.exit(1)
    feature_context = fmt_proc.stdout.decode()
    _vprint(verbose, f"feature context: {len(feature_context)} chars")

    prompt = f"""You are a feature-scope resolver for a rabbit-workflow repository.

Given a natural-language request, identify which features the request targets.
A request targets a feature if the implementation work will modify files within
that feature's directory.

REGISTERED FEATURES:
{feature_context}

REQUEST:
{request}

Respond with ONLY valid JSON on a single line — no markdown, no explanation:
{{"features": ["feature-name-1", "feature-name-2"], "rationale": "one sentence"}}

Rules:
- Pick features whose source files the implementation will modify.
- The REGISTERED FEATURES list above is the authoritative set; do not include features not in that list.
- Use each feature's summary above to decide whether the request's work falls inside that feature's surface.
- Omit features whose files will not be modified.
- Return an empty features list [] if no features are targeted."""

    print(prompt)
    _vprint(verbose, "done")


if __name__ == "__main__":
    main()
