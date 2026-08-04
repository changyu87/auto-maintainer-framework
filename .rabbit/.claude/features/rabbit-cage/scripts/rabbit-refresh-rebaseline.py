#!/usr/bin/env python3
"""rabbit-refresh-rebaseline.py — clear the reload-tier restart advisory (Inv 54f).

The `/rabbit-refresh` command body invokes this script so that running
`/rabbit-refresh` re-baselines the `SKILL.md` tier of the mid-session restart
snapshot, clearing the `reload ADVISED` advisory WITHOUT a full restart.

Why a script (not dispatcher prompt-matching): the prior Inv 54f wiring matched
the submitted prompt text `/reload-skills` in the UserPromptSubmit dispatcher.
That path is DEAD — `/reload-skills` is a Claude Code CLIENT-LOCAL built-in that
reloads `SKILL.md` definitions in-process and NEVER fires the UserPromptSubmit
hook, so the dispatcher never observed it and `rebaseline_skill_tier` was never
called. The reload-tier advisory therefore persisted until a full restart — the
exact thing it advised against. `/rabbit-refresh` IS a real submitted command
whose body runs deterministic `!` bash, so re-baselining here (script-tier per
spec-rules section 1) is the reliable clearing surface.

It re-baselines ONLY the `SKILL.md`-tier keys of the snapshot (delegating to the
shared helper `hooks/restart_snapshot.py`'s `rebaseline_skill_tier`), leaving the
hard-restart tier (hooks / settings / `CLAUDE.md` / agents) and skill-`scripts/`
keys untouched — a genuine hard-restart change still legitimately fires its
advisory.

Best-effort and graceful: any error degrades to a silent no-op (never breaks the
`/rabbit-refresh` command). Resolves the same repo root the dispatchers use so it
re-baselines the same `<repo_root>/.rabbit-restart-snapshot` file.

Version: 1.0.0
Owner: rabbit-workflow team (rabbit-cage)
Deprecation criterion: when Claude Code reloads skills/hooks/agents in-session
    without a restart, making the stale-load advisory unnecessary.
"""

import os
import subprocess
import sys
from pathlib import Path

# The shared snapshot helper is deployed alongside the dispatchers at
# `.claude/hooks/restart_snapshot.py`; in the source tree it lives under this
# feature's `hooks/`. Add the sibling hooks dir to the path and import it.
_HOOKS_DIR = Path(__file__).resolve().parent.parent / "hooks"
if str(_HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(_HOOKS_DIR))


def _repo_root() -> Path:
    """Resolve the repo root the dispatchers write the snapshot under: the
    `RABBIT_ROOT` env override, else the git toplevel of the current working
    directory, else the cwd. Mirrors user-prompt-submit-dispatcher.repo_root so
    the re-baseline targets the same `.rabbit-restart-snapshot`."""
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return Path(env)
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        return Path.cwd()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        sys.stdout.write(
            "rabbit-refresh-rebaseline.py — re-baseline the SKILL.md tier of "
            "the restart snapshot so /rabbit-refresh clears the reload "
            "advisory (Inv 54f). Best-effort; always exits 0.\n"
        )
        return 0
    try:
        from restart_snapshot import rebaseline_skill_tier
        rebaseline_skill_tier(str(_repo_root()))
    except Exception:
        # Best-effort: the helper may be absent (partial deploy) or the
        # snapshot may not exist yet. Never break /rabbit-refresh.
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
