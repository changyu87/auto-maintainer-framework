#!/usr/bin/env python3
"""stop — deterministic STOPPED latch for the maintainer loop (script-tier).

This is the script-backed control for ``/auto-maintainer:stop`` (spec-rules §1).
It exists to fix auto-maintainer-framework#30, where the stop SKILL asked the
model to hand-roll Python against a non-existent ``runtime_dir`` import and the
command broke with an ImportError. State writes are NEVER prompt-tier: this
script owns the STOPPED write so the skill only invokes it (then cancels the
heartbeat via CronDelete, the one inherently agent-mediated step).

It resolves the runtime dir the SAME way ``run_tick`` does — by reusing
``run_tick.resolve_runtime_paths`` (no duplicated path logic) — and writes the
durable disposition marker via the lifecycle-dispositions API
(``write_disposition``). The marker it writes is exactly the one the next tick's
GUARD reads, so a stop latches: the loop stays STOPPED until a human restarts it.

scheduling CONSUMES run_tick + lifecycle-dispositions UNCHANGED; it never edits
or forks them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the control surface is replaced.
"""

import os
import sys

# Resolve sibling modules via sys.path exactly as run_tick does. In the worktree
# the consumed features live under ../<dep>/src; in the installed plugin lib/
# they are flat siblings of this file (already importable once this dir is on
# the path). Importing run_tick first reuses its path setup and its
# resolve_runtime_paths, so stop never duplicates the runtime-path logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import run_tick as rt  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import heartbeat as hb  # noqa: E402


def stop():
    """Latch the loop STOPPED and return the runtime dir the marker landed in.

    Resolves the runtime dir via run_tick's resolver, writes disposition
    STOPPED through the lifecycle-dispositions API, clears the durable
    loop-intent (so a future session's SessionStart hook does NOT auto-resume
    the heartbeat), and prints a one-line confirmation. Idempotent: calling it
    when already STOPPED simply re-writes the same marker and re-clears intent.
    """
    runtime_dir, _state_path, _journal_path = rt.resolve_runtime_paths()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    # Clear the durable loop-intent: a stop must NOT auto-resume next session.
    hb.clear_loop_intent(runtime_dir)
    sys.stdout.write(
        f"[stop] disposition={ld.Disposition.STOPPED} runtime_dir={runtime_dir}\n")
    return runtime_dir


if __name__ == "__main__":
    stop()
