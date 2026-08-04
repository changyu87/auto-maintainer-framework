#!/usr/bin/env python3
"""stop-dispatcher.py — Claude Code Stop hook dispatcher.

Enumerates every active feature's `feature.json runtime.Stop` declarations,
invokes each declared API via `contract.lib.runtime`, partitions returns
into print/inject/ok/error, and emits at most one JSON object to stdout.

Issue #545 (Inv 37): after the per-feature Stop payloads, INVOKES
rabbit-auto-evolve's `scripts/advise-restart.py status` (a contract INVOKE, not
a cross-feature edit) and, while the advisory marker is present, appends ONE
concise ADVISORY-restart line per tick-end (icon 🔄, distinct from the hard
#503 resume banner so it reads as OPTIONAL). The Stop dispatcher does NOT clear
the advisory marker — it persists across tick-ends until a SessionStart
consumes it. Absent / erroring advise-restart.py degrades gracefully (no line).

Inv 54: also re-checks the restart-sensitive surface snapshot taken at
SessionStart (hooks/restart_snapshot.py) and, when a loaded surface
(hooks/skills/agents/settings/CLAUDE.md) has changed on disk since session
start — via ANY update path, not just `/rabbit-update install` — appends ONE
`restart ADVISED` line (icon 🔄). The snapshot is not rewritten here, so the
advisory persists across tick-ends until a fresh SessionStart re-baselines it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _dispatcher_lib import (  # noqa: E402
    advisory_restart_payloads,
    dispatch_event,
    render_emission,
)
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
            "stop-dispatcher.py — Claude Code Stop hook.\n"
            "Enumerates every active feature's runtime.Stop declarations, "
            "invokes each via contract.lib.runtime, emits at most one JSON "
            "object to stdout.\n"
        )
        return 0
    try:
        sys.stdin.read()
    except Exception:
        pass
    root = str(repo_root())
    payloads = dispatch_event("Stop", root)
    payloads.extend(advisory_restart_payloads(root))
    payloads.extend(restart_advisory_payloads(root))
    emission = render_emission(payloads)
    if emission is not None:
        sys.stdout.write(json.dumps(emission) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
