#!/usr/bin/env python3
"""audit-owner.py — enforce team ownership of repo-level features (issue #416 Part C).

Validates that a feature's feature.json `owner` field equals the canonical
"rabbit-workflow team" value. Repo-level features distributed as part of
rabbit-workflow MUST be team-owned, never owned by an individual; an
individual owner reintroduces the bus-factor / false-bottleneck drift that
#416 corrected. This is defense-in-depth: it catches future drift back to
individual owners.

Retired features (feature.json status=retired) short-circuit to pass, matching
contract.lib.checks.validate_feature's Inv 36b retired short-circuit.

Usage: audit-owner.py <feature-dir>
Exit:  0 owner == "rabbit-workflow team" (or retired); 1 owner mismatch;
       2 invocation error.

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: when contract.lib.checks.validate_feature is exposed
via a first-class CLI in the contract feature and enforces the team-owner
rule centrally.
"""
from __future__ import annotations

import json
import os
import sys

REQUIRED_OWNER = "rabbit-workflow team"


def main() -> int:
    if len(sys.argv) < 2 or not sys.argv[1]:
        print("usage: audit-owner.py <feature-dir>", file=sys.stderr)
        return 2
    feature_dir = sys.argv[1]
    if not os.path.isdir(feature_dir):
        print(f"ERROR: not a directory: {feature_dir}", file=sys.stderr)
        return 2

    feature_name = os.path.basename(os.path.realpath(feature_dir))
    feature_json = os.path.join(feature_dir, "feature.json")
    if not os.path.isfile(feature_json):
        print(f"ERROR: {feature_name}: missing feature.json", file=sys.stderr)
        return 2

    try:
        with open(feature_json) as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: {feature_name}: cannot read feature.json: {e}", file=sys.stderr)
        return 2

    # Retired features short-circuit to pass (mirrors validate_feature Inv 36b).
    if isinstance(data, dict) and data.get("status") == "retired":
        print(f"RETIRED: {feature_name} (status=retired; owner check skipped)")
        return 0

    owner = data.get("owner") if isinstance(data, dict) else None
    if owner == REQUIRED_OWNER:
        print(f"OK: {feature_name} owner is {REQUIRED_OWNER!r}")
        return 0

    print(
        f"FAIL: {feature_name}: owner must be {REQUIRED_OWNER!r}, "
        f"got {owner!r}. Repo-level features distributed as part of "
        f"rabbit-workflow MUST be team-owned, not individual-owned "
        f"(see policy/spec-rules.md §3, issue #416).",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
