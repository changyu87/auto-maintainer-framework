#!/usr/bin/env python3
"""status — deterministic loop-status reporter for the maintainer (script-tier).

This is the script-backed control for ``/auto-maintainer:status`` (spec-rules
§1). It exists to fix auto-maintainer-framework#29, where the status command
shipped a hardcoded slice-1 stub ("no loop configured yet") and never read real
state. Reporting state is NEVER prompt-tier: this script reads the REAL durable
state so the skill only invokes it and relays the output.

It resolves the runtime dir the SAME way ``run_tick`` does — by reusing
``run_tick.resolve_runtime_paths`` (no duplicated path logic) — then reads the
disposition marker (lifecycle-dispositions API) and the last pull's persisted
``work_items`` count (via run_tick's durable-state helper). Reading is
non-mutating: asking for status never creates the runtime dir. When the loop was
never started (no marker, no state file) the defaults surface a sane "not
started" view: disposition IDLE, work_items 0.

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
# setup and its resolve_runtime_paths, so status never duplicates that logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_tick as rt  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402


def status_line():
    """Build the one-line loop status from REAL on-disk state.

    Reads the disposition marker and the last pull's persisted work_items count
    from the runtime dir run_tick resolves, WITHOUT creating it. Returns a single
    human-readable line naming the disposition, work_items count, and runtime dir.
    """
    runtime_dir, state_path, _journal_path = rt.resolve_runtime_paths()
    disposition = ld.read_disposition(runtime_dir)
    work_items = rt.persisted_work_items_count(state_path)
    return (f"[status] disposition={disposition} work_items={work_items} "
            f"runtime_dir={runtime_dir}")


if __name__ == "__main__":
    sys.stdout.write(status_line() + "\n")
