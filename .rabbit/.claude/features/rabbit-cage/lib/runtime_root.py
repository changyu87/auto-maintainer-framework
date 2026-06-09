"""runtime_root — canonical single-`.rabbit` runtime-root resolver (Inv 52).

The one function every rabbit runtime-artifact writer (mode marker,
`last-update-check` throttle file, assembled prompts, `impl-suggestion-*.json`,
`tdd-report-*.json`) must agree on: given a resolved `repo_root`, return the
SINGLE `.rabbit/` runtime root.

In a vendored install the dispatcher resolves `repo_root` to `RABBIT_ROOT`,
which IS the vendored `.rabbit` install dir. Anchoring another cwd-relative
`.rabbit/` literal on top of that doubles the segment to
`<host>/.rabbit/.rabbit/...` (issue #1046) — splitting runtime artifacts across
two trees so readers and writers disagree. This resolver collapses that: it
returns `repo_root` unchanged when it is already a `.rabbit` dir, and appends
`.rabbit` only in the standalone case where `repo_root` is the git toplevel.

Pure stdlib, no side effects, no env reads. Idempotent: feeding its own
vendored output back as `repo_root` returns the same path, never re-doubling.

Version: 1.0.0
Owner: rabbit-workflow team (rabbit-cage)
Deprecation criterion: when Claude Code exposes a native per-project runtime
    root that subsumes the vendored `.rabbit/` install layout.
"""

import os


def rabbit_runtime_root(repo_root: str) -> str:
    """Return the canonical single-`.rabbit` runtime root for `repo_root`.

    Vendored (`basename(repo_root) == ".rabbit"`): `repo_root` IS the runtime
    root — return it unchanged (appending `.rabbit` would double the segment).
    Standalone (any other basename): the runtime root is `<repo_root>/.rabbit`.
    """
    repo_root = os.path.normpath(repo_root)
    if os.path.basename(repo_root) == ".rabbit":
        return repo_root
    return os.path.join(repo_root, ".rabbit")
