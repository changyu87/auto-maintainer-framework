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

Inv 54f: ONE exception to "snapshot not rewritten" — when the SUBMITTED prompt
is `/reload-skills` (a Claude Code built-in that reloads SKILL.md definitions
mid-session without a restart, and so never fires the SessionStart re-baseline),
this dispatcher re-baselines ONLY the SKILL.md-tier keys of the snapshot before
the re-check, so the reload-tier advisory clears. Hard-restart-tier keys
(hooks/settings/CLAUDE.md/agents) are left untouched — a genuine hard-restart
change still fires its advisory.
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
    from restart_snapshot import (  # noqa: E402
        rebaseline_skill_tier,
        restart_advisory_payloads,
    )
except ImportError:  # helper absent (partial deploy) — degrade gracefully
    def restart_advisory_payloads(repo_root):  # noqa: D103
        return []

    def rebaseline_skill_tier(repo_root):  # noqa: D103
        return


# Inv 54f: the Claude Code built-in that reloads SKILL.md definitions
# mid-session without a restart.
_RELOAD_SKILLS_COMMAND = "/reload-skills"


def _submitted_prompt() -> str:
    """Return the submitted prompt text from the UserPromptSubmit stdin JSON
    payload (`{"prompt": ...}`), or "" when stdin is empty/unparseable.
    Best-effort: never raises."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return ""
    if not raw.strip():
        return ""
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    prompt = data.get("prompt")
    return prompt if isinstance(prompt, str) else ""


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
    prompt = _submitted_prompt()
    root = str(repo_root())
    # Inv 54f: a `/reload-skills` prompt re-baselines the SKILL.md tier of the
    # snapshot BEFORE the re-check, so the reload-tier advisory clears on this
    # and every later tick. The hard-restart tier is left untouched.
    if prompt.strip() == _RELOAD_SKILLS_COMMAND:
        rebaseline_skill_tier(root)
    payloads = dispatch_event("UserPromptSubmit", root)
    payloads.extend(restart_advisory_payloads(root))
    emission = render_emission(payloads)
    if emission is not None:
        sys.stdout.write(json.dumps(emission) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
