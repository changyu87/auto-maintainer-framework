#!/usr/bin/env python3
"""validate-meta-contract.py — thin CLI shim around
contract.lib.checks.validate_meta_contract.

Usage: validate-meta-contract.py <feature-dir>
Exit:  0 pass; 1 validation error(s); 2 invocation error.

Version: 1.0.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when meta-contract validation is provided natively by the rabbit CLI.
"""

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
from lib.checks import validate_meta_contract  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1]:
        print("usage: validate-meta-contract.py <feature-dir>", file=sys.stderr)
        return 2
    feature_dir = sys.argv[1]
    if not os.path.isdir(feature_dir):
        print(f"ERROR: not a directory: {feature_dir}", file=sys.stderr)
        return 2
    result = validate_meta_contract(feature_dir)
    stream = sys.stdout if result.passed else sys.stderr
    for line in result.messages:
        print(line, file=stream)
    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
