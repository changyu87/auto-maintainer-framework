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

    Reads the disposition marker and the last tick's persisted read-product
    counts (work_items, work_orders, execution_plan, handoffs) from the runtime
    dir run_tick resolves, WITHOUT creating it. Returns a single human-readable
    line naming the disposition, the four counts, the route source, and the
    runtime dir. ALL four count fields are ALWAYS reported, including 0 (#69),
    matching the tick trace's unconditional fields — so a reader can distinguish
    "stage not routed" from "stage ran, produced nothing", and status never
    diverges from the trace. The route source (#59) reuses run_tick.route_source
    — the SAME helper the trace uses — so status and the trace never diverge on
    whether an override is active.
    """
    runtime_dir, state_path, _journal_path = rt.resolve_runtime_paths()
    disposition = ld.read_disposition(runtime_dir)
    work_items = rt.persisted_work_items_count(state_path)
    work_orders = rt.persisted_work_orders_count(state_path)
    execution_plan = rt.persisted_execution_plan_count(state_path)
    handoffs = rt.persisted_handoffs_count(state_path)
    route_src = rt.route_source_label()
    # Governance surface (#69 style — always shown): mode + the compact budget
    # field (+ budget_paused when exhausted), via the SAME helper the tick trace
    # uses, so status and the trace never diverge. Resolved from the SAME
    # project_dir / state_path run_tick reads.
    gov_fields = rt.governance_status(rt._resolve_project_dir(), state_path)
    # Outbound REPORT surface (§3.11): the last tick's reported=<filed>/<skipped>
    # from the small durable last-reported fact (NOT re-running the flush). Always
    # shown (#69 style), matching the tick trace's unconditional reported token.
    last_reported = rt.persisted_last_reported(state_path)
    reported_field = (f"reported={last_reported.get('filed', 0)}/"
                      f"{last_reported.get('skipped', 0)}")
    line = (f"[status] disposition={disposition} work_items={work_items} "
            f"work_orders={work_orders} "
            f"execution_plan={execution_plan} handoffs={handoffs} "
            f"route={route_src} runtime_dir={runtime_dir} {gov_fields} "
            f"{reported_field}")
    return line


if __name__ == "__main__":
    sys.stdout.write(status_line() + "\n")
