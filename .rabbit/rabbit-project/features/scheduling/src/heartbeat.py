#!/usr/bin/env python3
"""heartbeat — durable loop-intent + cross-session auto-resume decision.

The maintainer loop is **warm-only** (DESIGN §2.8): the recurring heartbeat is a
session-scheduled prompt, so it stops ticking the moment the Claude session ends
and does NOT come back by itself in the next session. This module makes the
heartbeat *durable* in the only way the platform allows for a warm-only loop:
not by keeping a clock alive across sessions (a plugin cannot), but by persisting
a durable **loop-intent** marker and re-arming the in-session heartbeat from a
SessionStart hook on the next session (DESIGN §3.3.2 heartbeat bootstrap, §3.3.4
RESTART_NEEDED -> SessionStart auto-resume).

Three durable facts live in the runtime dir, alongside the disposition + lock
markers owned by lifecycle-dispositions (this module NEVER edits those — it reads
the disposition to gate auto-resume):

  - ``loop-intent`` — set to ``running`` by ``/auto-maintainer:start`` and cleared
    by ``/auto-maintainer:stop``. It records "the human wants the loop ticking",
    independent of whether a session is currently open. This is the durable bit
    that survives a session ending.
  - ``last-resume-session`` — the session id the heartbeat was last auto-armed
    for. It is the **at-most-one-refire dedup** across sessions: a single session
    re-arms the heartbeat at most once, even if SessionStart fires several times
    (startup / resume / clear / compact all match the hook), so the loop never
    accumulates duplicate heartbeats.

The auto-resume decision (``should_auto_resume``) is pure and deterministic given
the runtime dir + session id, so it is unit-testable without any session, clock,
or scheduler: it returns True only when (1) intent is ``running``, (2) the loop is
not latched STOPPED/ABORTED and not owed a RESTART (a latched loop must not be
silently re-armed — a human resume / a restart is required), and (3) this session
has not already armed the heartbeat. On a True decision the caller records the
session via ``mark_resumed`` so the dedup holds for the rest of the session.

The runtime dir is injectable on every entry point (tests pass a temp dir,
production passes the project runtime dir), matching the rest of scheduling.

scheduling CONSUMES lifecycle-dispositions UNCHANGED; this module only reads its
disposition marker via the public API.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the platform offers a durable
  plugin-level clock (a native cron API), removing the need to re-arm an
  in-session heartbeat from a SessionStart hook.
"""

import os

# Resolve sibling modules via sys.path exactly as run_tick does: in the worktree
# the consumed features live under ../<dep>/src; in the installed plugin lib/
# they are flat siblings of this file.
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "lifecycle-dispositions"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import lifecycle_dispositions as ld  # noqa: E402


# The durable loop-intent value: the human wants the loop ticking. Absent file
# (or any other content) means "no intent" — do not auto-resume.
INTENT_RUNNING = "running"

_INTENT_MARKER = "loop-intent"
_RESUME_DEDUP_MARKER = "last-resume-session"


def _intent_path(runtime_dir):
    return os.path.join(runtime_dir, _INTENT_MARKER)


def _resume_dedup_path(runtime_dir):
    return os.path.join(runtime_dir, _RESUME_DEDUP_MARKER)


def _atomic_write(path, value):
    """Write a marker atomically (temp + os.replace) so a crash never leaves a
    half-written marker that the next session would misread."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(value)
    os.replace(tmp, path)


# --------------------------------------------------------------------------
# Durable loop-intent — set by /start, cleared by /stop.
# --------------------------------------------------------------------------

def record_loop_intent(runtime_dir):
    """Durably record that the loop should be running (write the intent marker).

    Called by ``/auto-maintainer:start`` after the latch-clear succeeds, so the
    intent survives the session ending. Idempotent: re-recording writes the same
    marker. Also clears any stale cross-session dedup so the FIRST SessionStart
    of a new session re-arms even if a marker lingered from a prior run."""
    _atomic_write(_intent_path(runtime_dir), INTENT_RUNNING)
    # A fresh start may begin a new arming epoch; drop a stale dedup marker so
    # the next SessionStart is free to arm. (mark_resumed re-stamps per session.)
    clear_resume_dedup(runtime_dir)


def clear_loop_intent(runtime_dir):
    """Durably clear the loop-intent (remove the marker). Idempotent.

    Called by ``/auto-maintainer:stop``: with no intent, a later SessionStart
    will NOT auto-resume the heartbeat. The STOPPED disposition latch is the
    other, independent safeguard (read by ``should_auto_resume``)."""
    try:
        os.unlink(_intent_path(runtime_dir))
    except FileNotFoundError:
        pass
    # A stop ends the current arming epoch; clear the dedup so a future /start
    # in this same session can arm again.
    clear_resume_dedup(runtime_dir)


def read_loop_intent(runtime_dir):
    """Return the durable loop-intent string, or None when no intent is set."""
    path = _intent_path(runtime_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r") as f:
        value = f.read().strip()
    return value or None


def loop_intent_is_running(runtime_dir):
    """True when the durable intent says the loop should be running."""
    return read_loop_intent(runtime_dir) == INTENT_RUNNING


# --------------------------------------------------------------------------
# Cross-session auto-resume dedup — at-most-one-refire per session.
# --------------------------------------------------------------------------

def read_resume_dedup(runtime_dir):
    """Return the session id the heartbeat was last armed for, or None."""
    path = _resume_dedup_path(runtime_dir)
    if not os.path.isfile(path):
        return None
    with open(path, "r") as f:
        value = f.read().strip()
    return value or None


def mark_resumed(runtime_dir, session_id):
    """Record that the heartbeat was armed for ``session_id`` (dedup stamp).

    After this, ``should_auto_resume(runtime_dir, session_id)`` is False for the
    same session, so repeated SessionStart events in one session arm at most one
    heartbeat (the at-most-one-refire-across-sessions guarantee)."""
    _atomic_write(_resume_dedup_path(runtime_dir), str(session_id))


def clear_resume_dedup(runtime_dir):
    """Remove the dedup stamp (idempotent)."""
    try:
        os.unlink(_resume_dedup_path(runtime_dir))
    except FileNotFoundError:
        pass


def should_auto_resume(runtime_dir, session_id):
    """The pure SessionStart auto-resume decision.

    Returns True only when ALL hold:
      1. durable loop-intent is ``running`` (the human wants ticking), AND
      2. the loop is not latched/owed: disposition is not STOPPED, ABORTED, or
         RESTART_NEEDED. A latched loop must be resumed by a human (STOPPED) or a
         restart flow (RESTART_NEEDED) / investigated (ABORTED) — never silently
         re-armed by a SessionStart, AND
      3. this session has not already armed the heartbeat (dedup): the recorded
         last-resume session id differs from ``session_id``.

    The decision is a pure function of on-disk state + the session id, so it is
    deterministic and unit-testable with no session, clock, or scheduler."""
    if not loop_intent_is_running(runtime_dir):
        return False
    disposition = ld.read_disposition(runtime_dir)
    if disposition in (ld.Disposition.STOPPED, ld.Disposition.ABORTED,
                       ld.Disposition.RESTART_NEEDED):
        return False
    if read_resume_dedup(runtime_dir) == str(session_id):
        return False
    return True
