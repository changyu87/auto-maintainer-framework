#!/usr/bin/env python3
"""resolve-housekeep-scope.py — enumerate the CONSUMING PROJECT's features for
a housekeeping wave (issue #1179).

A user-invoked housekeeping wave operates on the consumer's OWN declared
features, never on rabbit-workflow's framework features. The scope a wave
anchors on therefore depends on the install mode:

  - standalone install: the project IS the repo; its features live under
    `<root>/.claude/features/<name>/feature.json`.
  - vendored/plugin install: rabbit is vendored under `<host>/.rabbit/` and the
    consuming project's features live under
    `<root>/rabbit-project/features/<name>/feature.json` (where `<root>` is the
    rabbit install root, i.e. the `.rabbit/` dir). rabbit's OWN framework
    features under `<root>/.claude/features/*` are EXCLUDED — they are the
    tool, not the user's project.

This mode-aware anchoring is the §1179 acceptance criterion: the wave targets
the consuming project, not rabbit's self-repo. It deliberately differs from
`contract/scripts/find-feature.py`, which (in vendored mode) returns BOTH
rabbit's own features AND the project's — the right set for dispatch
infrastructure, the WRONG set for a user-facing housekeeping wave.

Mode detection mirrors the established convention (find-feature.py Inv 20,
feature-touch.py): the vendored-mode marker value is dual-accepted as either
`vendored` (canonical) or `plugin` (legacy). Precedence:
  (i)  <root>/.runtime/mode               → root IS the rabbit install root
  (ii) <root>/.rabbit/.runtime/mode       → root is the host; rabbit_root=<root>/.rabbit
A marker candidate is accepted only when <rabbit_root>/.claude/ exists.
Structural fallback (no marker): vendored iff <rabbit_root>/rabbit-project/
exists; otherwise standalone.

Usage:
  resolve-housekeep-scope.py list   [--root <dir>]   # feature names, one/line
  resolve-housekeep-scope.py paths  [--root <dir>]   # feature dirs, one/line

`--root` defaults to the rabbit session cwd. Exit 0 on success, 2 on bad
invocation.

Version: 0.5.0
Owner: rabbit-workflow team
Deprecation criterion: when feature discovery is handled natively by the rabbit
    CLI as a first-class housekeeping subcommand.
"""
from __future__ import annotations

import glob
import json
import os
import sys

_VENDORED_MODE_VALUES = frozenset({"vendored", "plugin"})


def _detect_vendored_root(root: str):
    """Return the rabbit_root if vendored mode is detected, else None.

    Mirrors find-feature.py's fixed precedence + .claude/ validation, plus a
    structural fallback to a present `rabbit-project/` work tree when the
    gitignored runtime marker is absent (the per-session worktree case).
    """
    candidates = (
        (os.path.join(root, ".runtime", "mode"), root),
        (os.path.join(root, ".rabbit", ".runtime", "mode"),
         os.path.join(root, ".rabbit")),
    )
    for mode_file, candidate_root in candidates:
        try:
            with open(mode_file, encoding="utf-8") as f:
                if f.read().strip() not in _VENDORED_MODE_VALUES:
                    continue
        except OSError:
            continue
        if os.path.isdir(os.path.join(candidate_root, ".claude")):
            return candidate_root
    # Structural fallback: a present project work tree means vendored even when
    # the ephemeral runtime marker is missing.
    for candidate_root in (root, os.path.join(root, ".rabbit")):
        if os.path.isdir(os.path.join(candidate_root, "rabbit-project", "features")):
            return candidate_root
    return None


def _feature_jsons(root: str):
    """Yield feature.json paths for the CONSUMING PROJECT only.

    Vendored: `<rabbit_root>/rabbit-project/features/*/feature.json` ONLY —
    rabbit's own `<rabbit_root>/.claude/features/*` are EXCLUDED.
    Standalone: `<root>/.claude/features/*/feature.json`.
    """
    rabbit_root = _detect_vendored_root(root)
    if rabbit_root is not None:
        base = os.path.join(rabbit_root, "rabbit-project", "features")
    else:
        base = os.path.join(root, ".claude", "features")
    yield from sorted(glob.glob(os.path.join(base, "*", "feature.json")))


def cmd_list(root: str) -> int:
    for fj in _feature_jsons(root):
        try:
            with open(fj, encoding="utf-8") as f:
                name = json.load(f).get("name", "")
        except (OSError, ValueError):
            name = ""
        if name:
            print(name)
    return 0


def cmd_paths(root: str) -> int:
    for fj in _feature_jsons(root):
        print(os.path.dirname(fj))
    return 0


def _parse_root(argv):
    root = os.getcwd()
    rest = []
    i = 0
    while i < len(argv):
        if argv[i] == "--root":
            if i + 1 >= len(argv):
                sys.stderr.write("ERROR: --root requires a value\n")
                return None, None
            root = argv[i + 1]
            i += 2
            continue
        rest.append(argv[i])
        i += 1
    return root, rest


def main(argv) -> int:
    if not argv:
        sys.stderr.write(
            "usage:\n"
            "  resolve-housekeep-scope.py list  [--root <dir>]\n"
            "  resolve-housekeep-scope.py paths [--root <dir>]\n"
        )
        return 2
    sub = argv[0]
    if sub not in ("list", "paths"):
        sys.stderr.write(f"ERROR: unknown subcommand {sub!r}\n")
        return 2
    root, _rest = _parse_root(argv[1:])
    if root is None:
        return 2
    if not os.path.isdir(root):
        sys.stderr.write(f"ERROR: not a directory: {root}\n")
        return 2
    if sub == "list":
        return cmd_list(root)
    return cmd_paths(root)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
