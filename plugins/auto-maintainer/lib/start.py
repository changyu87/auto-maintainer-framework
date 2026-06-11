#!/usr/bin/env python3
"""start — deterministic fresh-start + tick #1 for the maintainer (script-tier).

This is the script-backed control for ``/auto-maintainer:start`` (spec-rules §1).
It exists to fix auto-maintainer-framework#44, where the start SKILL did not
clear a latched ``STOPPED`` disposition (from a prior ``/stop``): GUARD then
halted every tick and the loop would not run, so the skill resorted to
hand-rolling Python to clear the latch (a #30-class prompt-tier regression).
Disposition handling is NEVER prompt-tier: this script owns the clear-or-refuse
decision so the skill only invokes it.

It resolves the runtime dir the SAME way ``run_tick`` does — by reusing
``run_tick.resolve_runtime_paths`` (no duplicated path logic) — reads the current
disposition (lifecycle-dispositions API), and prepares a fresh start:

  - ``STOPPED`` — a prior human stop latched the loop. Start IS the §1.2 human
    resume, so clear the latch to a runnable state (IDLE) and announce it, then
    run tick #1.
  - ``ABORTED`` — a fault latched the loop. REFUSE: print a clear "investigate;
    not auto-cleared" message and exit non-zero WITHOUT ticking. A fault is
    never silently cleared.
  - otherwise (RUNNING / IDLE / absent) — proceed straight to tick #1.

Tick #1 itself is NOT re-implemented here: start calls ``run_tick.run_tick`` so
the route GUARD->DRAIN->PULL->PERSIST->EXIT lives in exactly one place. The
recurring heartbeat keeps using ``run_tick`` directly (no reset per tick); only
the FRESH start goes through this script.

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
# they are flat siblings of this file. Importing run_tick first reuses its path
# setup and its resolve_runtime_paths, so start never duplicates that logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_tick as rt  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402


class StartRefused(Exception):
    """Raised when start refuses to run because a fault (ABORTED) is latched.

    A fault is never silently cleared: the human must investigate and resolve it
    before the loop may resume. The CLI entrypoint maps this to a non-zero exit.
    """


def start(runtime_dir=None, state_path=None, journal_path=None, source=None):
    """Prepare a fresh start, then run tick #1; return the EXIT disposition signal.

    Resolves the runtime dir the same way run_tick does when paths are not
    injected (the installed case). Reads the disposition and acts:

      - STOPPED -> clear the latch to IDLE (start IS the human resume), announce
        it, then tick.
      - ABORTED -> raise StartRefused (the CLI exits non-zero); NO tick runs and
        the fault latch stays in place.
      - otherwise -> tick directly.

    `source` is the injectable PULL issue source forwarded to run_tick (tests
    inject a stub; production defaults to work-intake's live gh source).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = rt.resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal

    disposition = ld.read_disposition(runtime_dir)

    if disposition == ld.Disposition.ABORTED:
        raise StartRefused(
            "[start] disposition=ABORTED — a fault is latched; investigate and "
            "resolve it before resuming. NOT auto-cleared; no tick run.")

    if disposition == ld.Disposition.STOPPED:
        # Start is the human resume: clear the STOPPED latch to a runnable state.
        ld.write_disposition(runtime_dir, ld.Disposition.IDLE)
        sys.stdout.write(
            "[start] cleared latched STOPPED disposition -> IDLE (human resume)\n")

    return rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                       journal_path=journal_path, source=source)


if __name__ == "__main__":
    # Production entrypoint: the /auto-maintainer:start skill invokes this once
    # for tick #1 from the installed plugin with no path wiring and no injected
    # source. It clears a latched STOPPED (or refuses on ABORTED) and then runs
    # tick #1 via run_tick (which prints the tick trace). A latched fault exits
    # non-zero so the skill surfaces it instead of silently clearing it.
    try:
        start()
    except StartRefused as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)
