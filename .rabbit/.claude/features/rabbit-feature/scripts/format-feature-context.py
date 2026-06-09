#!/usr/bin/env python3
# format-feature-context.py — read find-feature.py list-json output from stdin,
# emit a human-readable feature context block to stdout.
#
# Usage:
#   python3 find-feature.py <repo-root> list-json | python3 format-feature-context.py
#
# Input:  JSON array of {name, path, summary, tdd_state} objects on stdin.
#         Only 'name' is required per entry; optional keys are tolerated
#         with default fallbacks. Missing required 'name' or malformed
#         JSON exits non-zero.
# Output: formatted text block, one feature per entry, to stdout.
#
# Version: 1.1.1
# Owner: rabbit-workflow team (rabbit-feature)
# Deprecation criterion: when feature-scope resolution is automated by the dispatch infrastructure.

import json
import sys

try:
    features = json.load(sys.stdin)
except Exception as e:
    sys.stderr.write(f"ERROR: malformed JSON on stdin: {e}\n")
    sys.exit(1)

if not isinstance(features, list):
    sys.stderr.write("ERROR: expected JSON array on stdin\n")
    sys.exit(1)

lines = []
for i, f in enumerate(features):
    if not isinstance(f, dict):
        sys.stderr.write(f"ERROR: entry {i} is not an object\n")
        sys.exit(1)
    name = f.get("name")
    if not name:
        sys.stderr.write(f"ERROR: entry {i} missing required 'name' key\n")
        sys.exit(1)
    lines.append(f"Feature: {name}")
    path = f.get("path", "")
    if path:
        lines.append(f"  Path: {path}")
    summary = f.get("summary", "no summary")
    lines.append(f"  Summary: {summary}")
    tdd_state = f.get("tdd_state", "")
    if tdd_state:
        lines.append(f"  TDD state: {tdd_state}")
    lines.append("")
print("\n".join(lines))
