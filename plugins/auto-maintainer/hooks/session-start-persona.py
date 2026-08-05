#!/usr/bin/env python3
"""SessionStart persona/banner hook for the auto-maintainer plugin.

Injects the dispatcher persona/banner into the session context on
SessionStart (DESIGN 3.9.2 / 3.10.3). This is the v1 seed of the
"CLAUDE.md substitute": it proves the plugin loads and gives every
session a stable persona header.

Reads the SessionStart hook event JSON on stdin (unused for now) and
emits an additionalContext block on stdout per the Claude Code
SessionStart hook contract.

Version: 0.6.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when the maintainer loop ships its
  full persona/config injection and this seed folds into it.
"""

import json
import sys

_PERSONA = (
    "[auto-maintainer] Dispatcher persona active. "
    "This session is running the auto-maintainer plugin: an autonomous "
    "repository maintenance loop. A fresh install seeds an aggressive "
    "default config (auto-merge, full acting route incl. REVIEW); run "
    "/auto-maintainer:configure to adjust it, /auto-maintainer:start to run "
    "the loop, and /auto-maintainer:status for the current state. "
    "Its limits are operator-owned: configuration (budget, backoff, "
    "issue_filter, mode) sets the bounds and the deterministic runner enforces "
    "them, so the loop invents no ad-hoc token, tick, or issue caps of its own "
    "and does not silently narrow the configured scope; it keeps working while a "
    "tick reports refire and surfaces anything unusual to you — you bound it via "
    "/auto-maintainer:configure or halt it via /auto-maintainer:stop."
)


def main():
    # Drain stdin (the hook event payload); not needed for the banner.
    try:
        sys.stdin.read()
    except Exception:
        pass

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _PERSONA,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
