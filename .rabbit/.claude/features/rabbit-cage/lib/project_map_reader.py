"""project_map_reader — plugin-mode project-map I/O + path matching.

Reads `<repo_root>/.rabbit/rabbit-project/project-map.json` and matches
absolute target paths against the per-feature `paths` globs declared in
the map. Used by `hooks/scope-guard.py` to drive the plugin-mode decision
tree (Inv 17). The schema of the map is owned by the `contract` feature
(see contract Inv 59 / `schemas/project-map.json.schema.json`); this
module trusts that schema and degrades safely (returns `None`) on any
parse error or missing file so scope-guard's default-safe branch wins.

Path-matching: globs in the map are repo-root-relative; we resolve them
against the absolute target via stdlib `fnmatch.fnmatch`, with a
`/`-suffixed prefix variant so `src/auth/**` matches both `src/auth/x.py`
and any deeper file. No `pathlib.PurePath.match` (its `**` semantics
differ from fnmatch).

Version: 1.0.0
Owner: rabbit-workflow team (rabbit-cage)
Deprecation criterion: when scope-guard.py is replaced by a native
    Claude Code per-project scope mechanism that consumes the
    project-map directly.
"""

import fnmatch
import json
import os
from typing import Optional


def load_map(repo_root: str) -> Optional[dict]:
    """Return the parsed project-map.json dict, or None on missing/invalid.

    Missing file is the common case in a fresh install — scope-guard
    treats `None` as "no declared features" and applies its default-safe
    branch (ALLOW any user-code edit).
    """
    path = os.path.join(repo_root, ".rabbit", "rabbit-project", "project-map.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    return doc


def match_path(target_path: str, map_dict: dict, repo_root: str) -> Optional[str]:
    """Return the feature name whose `paths` glob matches target_path, or None.

    `target_path` is an absolute path. The map's `paths` entries are
    interpreted as repo-root-relative globs supporting `*` and `**` via
    stdlib `fnmatch` (which expands `**` by treating `*` as cross-segment
    on the joined string — sufficient for the simple `src/auth/**` style
    used by project-map.json).
    """
    if not isinstance(map_dict, dict):
        return None
    features = map_dict.get("features")
    if not isinstance(features, dict):
        return None

    # Repo-root-relative form of the target. If target is outside repo_root,
    # nothing matches.
    abs_target = os.path.realpath(target_path)
    abs_root = os.path.realpath(repo_root)
    if not abs_target.startswith(abs_root + os.sep):
        return None
    rel = abs_target[len(abs_root) + 1:]

    for name, entry in features.items():
        if not isinstance(entry, dict):
            continue
        paths = entry.get("paths", [])
        if not isinstance(paths, list):
            continue
        for glob in paths:
            if not isinstance(glob, str):
                continue
            if fnmatch.fnmatch(rel, glob):
                return name
            # Treat trailing /** as "any descendant" — fnmatch handles this
            # already since '*' matches '/'. The match above is sufficient.
    return None
