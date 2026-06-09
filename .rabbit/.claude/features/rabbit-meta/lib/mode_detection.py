"""mode_detection — detect whether rabbit is running vendored or standalone.

Inv 1 of the rabbit-meta feature. Pure stdlib; no side effects, no env reads,
no logging.

Version: 2.0.0
Owner: rabbit-workflow team
Deprecation criterion: inherits from rabbit-meta feature deprecation
    (when rabbit's per-project install model is superseded by a native
    Claude Code workflow contract mechanism).
"""

import os


def detect_mode(cwd: str) -> str:
    """Return "vendored" if cwd is a `.rabbit/` directory vendored into a host
    project, else "standalone".

    Vendored signature: basename(cwd) == ".rabbit" AND dirname(cwd) exists AND
    that parent contains at least one entry whose name is not ".rabbit".
    Any filesystem error (missing path, permission, etc.) returns "standalone"
    as a safe default; the function MUST NOT raise.
    """
    try:
        entries = os.listdir(os.path.dirname(cwd))
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return "standalone"
    if os.path.basename(cwd) == ".rabbit" and any(name != ".rabbit" for name in entries):
        return "vendored"
    return "standalone"


def is_vendored(mode: str) -> bool:
    """Return True iff `mode` denotes a vendored `.rabbit/` install.

    Coexistence predicate: dual-accepts BOTH the current "vendored" spelling
    and the older "plugin" spelling so an install whose persisted mode marker
    still carries "plugin" is honoured. Every other value (including
    "standalone") returns False.

    Deprecation criterion: drop the "plugin" acceptance once no install
    carries the older marker spelling.
    """
    return mode in ("vendored", "plugin")
