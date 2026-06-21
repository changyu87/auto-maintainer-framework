#!/usr/bin/env python3
"""SessionStart auto-resume hook for the auto-maintainer heartbeat.

The maintainer loop is warm-only (DESIGN §2.8): its recurring heartbeat is a
session-scheduled prompt, so it stops the moment a Claude session ends. To make
the heartbeat *durable* across sessions (DESIGN §3.3.2 heartbeat bootstrap) this
hook re-arms the in-session heartbeat on the next session whenever the durable
loop-intent says the loop should be running.

It runs on SessionStart, reads the durable loop-intent + disposition from the
project runtime dir, and asks ``heartbeat.should_auto_resume`` for the pure
decision. On a True decision it stamps the cross-session dedup (so repeated
SessionStart events in ONE session arm at most one heartbeat) and emits an
``additionalContext`` block instructing the session to re-arm the heartbeat by
invoking ``/auto-maintainer:start``. On a False decision (no intent, a latched
STOPPED/ABORTED, an owed RESTART, or this session already armed) it stays silent.

The decision logic itself lives in heartbeat.py (deterministic, unit-tested);
this hook is the thin SessionStart wiring. It NEVER schedules a heartbeat itself
(a hook cannot dispatch the Agent tool); it asks the session to do so.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the platform offers a durable
  plugin-level clock (a native cron API), removing the need to re-arm the
  in-session heartbeat from SessionStart.
"""

import json
import os
import sys

# heartbeat.py ships in the plugin's lib/ dir, a sibling of this hooks/ dir.
# Put lib/ on the path so the import resolves in the installed plugin.
_HOOK_DIR = os.path.dirname(os.path.abspath(__file__))
_PLUGIN_ROOT = os.path.dirname(_HOOK_DIR)
_LIB = os.path.join(_PLUGIN_ROOT, "lib")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)
# In the dev worktree heartbeat.py lives in the feature src/ dir instead.
_DEV_SRC = os.path.join(os.path.dirname(os.path.dirname(_HOOK_DIR)), "src")
if os.path.isdir(_DEV_SRC) and _DEV_SRC not in sys.path:
    sys.path.insert(0, _DEV_SRC)

import heartbeat as hb  # noqa: E402


_RESUME_CONTEXT = (
    "[auto-maintainer] Durable loop-intent is RUNNING but this session has no "
    "active heartbeat yet. Re-arm it now by invoking the /auto-maintainer:start "
    "skill, which clears any latch, runs a tick, and reschedules the recurring "
    "heartbeat at the configured interval. (The previous session's in-session "
    "heartbeat ended with that session; the durable loop-intent survives, so the "
    "loop auto-resumes here.)"
)


def _resolve_runtime_dir():
    """The project runtime dir, resolved exactly as run_tick does: the
    CLAUDE_PROJECT_DIR env var when set, else cwd; the markers live under
    ``.auto-maintainer/`` there."""
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.join(project_dir, ".auto-maintainer")


def _read_session_id(stdin_text):
    """Extract the session id from the SessionStart hook event JSON on stdin.

    Falls back to a stable per-runtime constant when the payload has no
    session_id, so the dedup still functions (arm-once) even without one."""
    try:
        event = json.loads(stdin_text) if stdin_text.strip() else {}
    except (ValueError, TypeError):
        event = {}
    return event.get("session_id") or "session"


def decide(runtime_dir, session_id):
    """The pure auto-resume decision + dedup stamp.

    Returns the additionalContext string when the heartbeat should be re-armed
    (and records the dedup so this session arms at most once), else None. Kept
    pure of stdin/stdout so it is unit-testable."""
    if not hb.should_auto_resume(runtime_dir, session_id):
        return None
    hb.mark_resumed(runtime_dir, session_id)
    return _RESUME_CONTEXT


def main():
    try:
        stdin_text = sys.stdin.read()
    except Exception:
        stdin_text = ""

    runtime_dir = _resolve_runtime_dir()
    session_id = _read_session_id(stdin_text)

    try:
        context = decide(runtime_dir, session_id)
    except Exception:
        # A hook must never break the session: on any error, stay silent.
        context = None

    if context:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": context,
            }
        }
        sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main())
