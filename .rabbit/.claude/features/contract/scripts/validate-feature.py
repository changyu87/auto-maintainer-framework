#!/usr/bin/env python3
"""validate-feature.py — thin CLI shim around
contract.lib.checks.validate_feature (spec Inv 11, Inv 25, Inv 28b, Inv 29,
Inv 63).

Usage:
  validate-feature.py <feature-dir>          validate one feature dir
  validate-feature.py all                     sweep every feature dir
  validate-feature.py <dir-a> <dir-b> ...     sweep the named feature dirs
Exit:  0 pass; 1 validation error(s); 2 invocation error.

The single-feature mode is unchanged and backward compatible. The aggregate
sweep mode (`all` / multiple dirs) absorbs the cross-feature audit sweep
formerly provided by the rabbit-feature-audit skill (Inv 63): it validates
each feature dir, prints a stable `<name>: PASS|FAIL` line plus that feature's
CheckResult messages, then a `SUMMARY:` line, and exits non-zero if any feature
fails.

Version: 2.1.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when feature validation is provided natively by the rabbit CLI.
"""

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
from lib.checks import get_repo_root, validate_feature  # noqa: E402

# The non-feature policy directory carries no feature.json and is not a feature.
_NON_FEATURE_DIRS = {"policy"}


def _validate_one(feature_dir: str) -> int:
    """Single-feature mode (unchanged, backward compatible)."""
    if not os.path.isdir(feature_dir):
        print(f"ERROR: not a directory: {feature_dir}", file=sys.stderr)
        return 2
    result = validate_feature(feature_dir)
    stream = sys.stdout if result.passed else sys.stderr
    for line in result.messages:
        print(line, file=stream)
    return 0 if result.passed else 1


def _resolve_all_feature_dirs() -> list:
    """Resolve every immediate subdirectory of .claude/features/.

    Skips non-directories, dotfiles, and the non-feature policy directory.
    Returns a sorted list of absolute paths. Returns None when the features
    root cannot be resolved.
    """
    repo_root = get_repo_root()
    if not repo_root:
        return None
    features_root = os.path.join(repo_root, ".claude", "features")
    if not os.path.isdir(features_root):
        return None
    dirs = []
    for name in sorted(os.listdir(features_root)):
        if name.startswith("."):
            continue
        if name in _NON_FEATURE_DIRS:
            continue
        path = os.path.join(features_root, name)
        if os.path.isdir(path):
            dirs.append(path)
    return dirs


def _sweep(feature_dirs: list) -> int:
    """Aggregate sweep mode: validate each dir, print per-feature lines plus a
    SUMMARY line, and return 0 iff every feature passes (1 otherwise)."""
    total = 0
    passed = 0
    failed = 0
    for feature_dir in feature_dirs:
        name = os.path.basename(os.path.normpath(feature_dir))
        total += 1
        result = validate_feature(feature_dir)
        if result.passed:
            passed += 1
            print(f"{name}: PASS")
        else:
            failed += 1
            print(f"{name}: FAIL")
        for line in result.messages:
            print(f"    {line}")
    print(f"SUMMARY: {total} features, {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


def main() -> int:
    args = sys.argv[1:]
    if not args or not args[0]:
        print("usage: validate-feature.py <feature-dir> | all | <dir> [<dir> ...]", file=sys.stderr)
        return 2

    if args[0] == "all" and len(args) == 1:
        feature_dirs = _resolve_all_feature_dirs()
        if feature_dirs is None:
            print("ERROR: could not resolve .claude/features/ root", file=sys.stderr)
            return 2
        return _sweep(feature_dirs)

    if len(args) == 1:
        return _validate_one(args[0])

    # Multiple positional feature-dir paths -> sweep exactly those dirs.
    for feature_dir in args:
        if not os.path.isdir(feature_dir):
            print(f"ERROR: not a directory: {feature_dir}", file=sys.stderr)
            return 2
    return _sweep(args)


if __name__ == "__main__":
    sys.exit(main())
