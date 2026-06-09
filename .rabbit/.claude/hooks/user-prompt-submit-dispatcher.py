#!/usr/bin/env python3
"""user-prompt-submit-dispatcher.py — Claude Code UserPromptSubmit hook dispatcher.

Enumerates every active feature's `feature.json runtime.UserPromptSubmit`
declarations, invokes each declared API via `contract.lib.runtime`,
partitions returns into print/inject/ok/error, and emits at most one
JSON object to stdout.

Inv 54: also re-checks the restart-sensitive surface snapshot taken at
SessionStart (hooks/restart_snapshot.py) and, when a loaded surface
(hooks/skills/agents/settings/CLAUDE.md) has changed on disk since session
start — via ANY update path, not just `/rabbit-update install` — appends ONE
`restart ADVISED` line (icon 🔄). The snapshot is not rewritten here, so the
advisory persists across prompts until a fresh SessionStart re-baselines it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _dispatcher_lib import dispatch_event, render_emission  # noqa: E402
try:
    from restart_snapshot import restart_advisory_payloads  # noqa: E402
except ImportError:  # helper absent (partial deploy) — degrade gracefully
    def restart_advisory_payloads(repo_root):  # noqa: D103
        return []


def repo_root() -> Path:
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.check_output(
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        return here


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        sys.stdout.write(
            "user-prompt-submit-dispatcher.py — Claude Code UserPromptSubmit hook.\n"
            "Enumerates every active feature's runtime.UserPromptSubmit declarations, "
            "invokes each via contract.lib.runtime, emits at most one JSON "
            "object to stdout.\n"
        )
        return 0
    try:
        sys.stdin.read()
    except Exception:
        pass
    root = str(repo_root())
    payloads = dispatch_event("UserPromptSubmit", root)
    payloads.extend(restart_advisory_payloads(root))
    emission = render_emission(payloads)
    if emission is not None:
        sys.stdout.write(json.dumps(emission) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
