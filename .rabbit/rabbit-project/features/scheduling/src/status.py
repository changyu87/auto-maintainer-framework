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

It also exposes a machine-first ``status_data()`` -> dict of EVERY surfaced
field (``plugin_version``, ``disposition``, ``awaiting``, ``mode``, the budget
window, the four read-product counts, ``reported``, the active ``route``, and
``runtime_dir``) and a DERIVED human view ``render_status(data)`` (philosophy
§1: the pretty view is produced FROM the machine artifact, never authored
alongside it). The CLI prints the human view by default, ``--json`` prints
``status_data()`` as JSON, and ``--line`` prints the retained byte-identical
legacy ``status_line()`` for back-compat + machine parsing.

scheduling CONSUMES run_tick + lifecycle-dispositions UNCHANGED; it never edits
or forks them.

Version: 0.3.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the control surface is replaced.
"""

import argparse
import json
import os
import subprocess
import sys

# Resolve sibling modules via sys.path exactly as run_tick does. In the worktree
# the consumed features live under ../<dep>/src; in the installed plugin lib/
# they are flat siblings of this file. Importing run_tick first reuses its path
# setup and its resolve_runtime_paths, so status never duplicates that logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

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

    The disposition stays RUNNING while a tick is mid-flight, so it cannot by
    itself tell "actively working" from "paused at an agent-state, awaiting a
    subagent's output" (#254). The ``awaiting`` field exposes that distinction
    as a machine-visible signal: ``awaiting=<state>`` names the paused
    agent-state when a durable tick checkpoint is present, else ``awaiting=none``.
    It reads the SAME run_tick.persisted_tick_checkpoint that is the sole source
    of truth for the paused dispatch, so status never diverges from the executor.
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
    # Awaiting-agent surface (#254): name the paused agent-state when a durable
    # tick checkpoint is present (the tick paused mid-flight to dispatch a
    # subagent and is awaiting its output), else `none`. This is the
    # machine-visible signal a RUNNING disposition cannot give on its own.
    checkpoint = rt.persisted_tick_checkpoint(state_path)
    awaiting = checkpoint.get("next_state", "none") if checkpoint else "none"
    line = (f"[status] disposition={disposition} work_items={work_items} "
            f"work_orders={work_orders} "
            f"execution_plan={execution_plan} handoffs={handoffs} "
            f"route={route_src} runtime_dir={runtime_dir} {gov_fields} "
            f"{reported_field} awaiting={awaiting}")
    return line


def _plugin_version(lib_dir=None):
    """The shipped plugin version, read from
    ``<lib_dir>/../.claude-plugin/plugin.json`` (the installed-plugin deployment
    context: this file ships as ``<plugin_root>/lib/status.py``, so the manifest
    is ``<plugin_root>/.claude-plugin/plugin.json``). Returns its ``version``
    string, or ``None`` when the file is absent/unparsable (e.g. the source
    tree). ``lib_dir`` defaults to this file's own directory; tests inject a
    temp dir so they never pollute the feature tree.
    """
    if lib_dir is None:
        lib_dir = os.path.dirname(os.path.abspath(__file__))
    path = os.path.join(lib_dir, "..", ".claude-plugin", "plugin.json")
    try:
        with open(path, "r") as f:
            return json.load(f).get("version")
    except (OSError, ValueError, AttributeError, TypeError):
        return None


# The FIXED distribution repo the release probe queries (the upstream where the
# plugin is published). Owned here so status never re-derives it.
_DIST_REPO = "changyu87/auto-maintainer-framework"


def _run_gh(args, timeout=10):
    """Run ``gh <args>`` and return trimmed stdout; raise RuntimeError on any
    non-zero exit. A thin deterministic wrapper (no AI) — the probe below catches
    every failure so status never crashes."""
    proc = subprocess.run(
        ["gh"] + args, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "gh failed")
    return proc.stdout.strip()


def _normalize_version(v):
    """Strip a leading ``v`` from a tag/version string (``v0.49.0`` -> ``0.49.0``)."""
    if v is None:
        return None
    v = v.strip()
    return v[1:] if v.startswith("v") else v


def DEFAULT_RELEASE_PROBE():
    """Resolve the latest PUBLISHED plugin version from the fixed distribution
    repo via ``gh`` (script-tier — spec-rules §1; no AI). Prefers the latest
    RELEASE (``gh api repos/<repo>/releases/latest``); falls back to the latest
    semver TAG when there is no published release. Returns the version string
    (leading ``v`` stripped) or raises — ``status_data`` catches any failure into
    ``release_check_error`` so the probe NEVER crashes status.
    """
    try:
        tag = _run_gh(["api", f"repos/{_DIST_REPO}/releases/latest",
                       "--jq", ".tag_name"])
        if tag:
            return _normalize_version(tag)
    except Exception:
        pass  # No published release (or a transient error) — fall back to tags.
    names = _run_gh(["api", f"repos/{_DIST_REPO}/tags", "--jq", ".[].name"])
    versions = [
        _normalize_version(n) for n in names.splitlines() if n.strip()]
    parsed = [(_parse_semver(v), v) for v in versions]
    parsed = [(p, v) for (p, v) in parsed if p is not None]
    if not parsed:
        raise RuntimeError("no semver tags found")
    return max(parsed, key=lambda pv: pv[0])[1]


def _parse_semver(v):
    """Parse ``v`` into a ``(major, minor, patch)`` int tuple, or ``None`` when it
    is missing/unparseable (tolerant — an unknown version never raises). A
    pre-release/build suffix (``1.2.3-rc1`` / ``1.2.3+build``) is dropped to its
    numeric core."""
    if v is None:
        return None
    core = str(v).strip().lstrip("v").split("-")[0].split("+")[0]
    parts = core.split(".")
    if len(parts) < 3:
        return None
    try:
        return tuple(int(p) for p in parts[:3])
    except ValueError:
        return None


def _update_available(latest, installed):
    """STRICT semver-greater: True iff ``latest`` parses strictly greater than
    ``installed``. False whenever EITHER side is unknown (None) or unparseable —
    the conservative guard so a bad/absent probe never claims an update."""
    lp = _parse_semver(latest)
    ip = _parse_semver(installed)
    if lp is None or ip is None:
        return False
    return lp > ip


def status_data(release_probe=None):
    """The machine-first status: a dict of EVERY surfaced field (philosophy §1).

    Reads the SAME real on-disk state ``status_line`` reads (the disposition
    marker, the four persisted read-product counts, the route source, the
    governance mode + durable budget window, the last-reported fact, and the
    awaiting-agent checkpoint) PLUS the shipped ``plugin_version`` and the ACTIVE
    ``route`` (states + happy-path chain) resolved via the SHARED
    ``run_tick.resolved_route`` / ``route_happy_chain`` helpers — the SAME
    resolution the tick runs, so status never diverges from the loop. Reading is
    NON-mutating: it never creates the runtime dir. The human view
    (``render_status``) and the ``--json`` CLI are both DERIVED from this dict.
    """
    runtime_dir, state_path, _journal_path = rt.resolve_runtime_paths()
    project_dir = rt._resolve_project_dir()
    disposition = ld.read_disposition(runtime_dir)

    # Governance surface — structured mode + budget window, read the SAME way
    # governance_status renders its string token (evaluate at the PERSISTED
    # window, not the wall clock, so a paused window is not masked). Reuses
    # run_tick's helpers so status never diverges from the tick trace (#69).
    gov = rt.sg.load_governance(project_dir)
    budget_state = rt.persisted_budget_state(state_path)
    budget = rt.sg.evaluate_budget(
        gov, budget_state, rt._clock_for_window(budget_state.get("window_key")))
    ceiling = gov.get("budget", {}).get("per_day_tokens")

    last_reported = rt.persisted_last_reported(state_path)
    checkpoint = rt.persisted_tick_checkpoint(state_path)

    active_route = rt.resolved_route(project_dir)

    # Release-check: the INJECTABLE, TOLERANT probe resolves the latest published
    # version (tests stub it with no network; the production default shells `gh`).
    # ANY failure yields latest_version=None + update_available=False + a non-null
    # release_check_error, and NEVER crashes status. update_available is a STRICT
    # semver-greater comparison — False whenever either version is unknown.
    plugin_version = _plugin_version()
    if release_probe is None:
        release_probe = DEFAULT_RELEASE_PROBE
    latest_version = None
    release_check_error = None
    try:
        latest_version = release_probe()
    except Exception as exc:  # tolerant: any failure -> a reason string, no crash
        release_check_error = str(exc) or type(exc).__name__
    update_available = _update_available(latest_version, plugin_version)

    return {
        "plugin_version": plugin_version,
        "latest_version": latest_version,
        "update_available": update_available,
        "release_check_error": release_check_error,
        "disposition": disposition,
        "awaiting": checkpoint.get("next_state", "none") if checkpoint
        else "none",
        "mode": gov.get("mode", ""),
        # The configured /start heartbeat (tick) cadence, read from the SAME
        # loaded config (load_governance == load_config) start.py's
        # heartbeat_interval_minutes() reads. The fallback sources the shipped
        # default from safety_governance.DEFAULT_GOVERNANCE (the single source of
        # truth, currently 10) so it never drifts from the shipped config.
        # Read-only.
        "heartbeat_interval_minutes": gov.get("heartbeat", {}).get(
            "interval_minutes",
            rt.sg.DEFAULT_GOVERNANCE["heartbeat"]["interval_minutes"]),
        "budget": {
            "spent": budget_state.get("spent_tokens", 0),
            "ceiling": ceiling,
            "window": budget_state.get("window_key", ""),
            "paused": None if budget.get("allowed", True)
            else budget.get("reason", ""),
        },
        "work_items": rt.persisted_work_items_count(state_path),
        "work_orders": rt.persisted_work_orders_count(state_path),
        "execution_plan": rt.persisted_execution_plan_count(state_path),
        "handoffs": rt.persisted_handoffs_count(state_path),
        "reported": {
            "filed": last_reported.get("filed", 0),
            "skipped": last_reported.get("skipped", 0),
        },
        "route": {
            "source": rt.route_source_label(project_dir),
            "states": active_route.get("states", []),
            "chain": rt.route_happy_chain(active_route),
        },
        "runtime_dir": runtime_dir,
    }


def _chain_lines(chain, width=64):
    """Render the happy-path chain as ``A → B → C …``, wrapped to `width`."""
    if not chain:
        return ["(none)"]
    lines = []
    cur = ""
    for i, state in enumerate(chain):
        piece = state if i == 0 else f" → {state}"
        if cur and len(cur) + len(piece) > width:
            lines.append(cur)
            cur = state
        else:
            cur += piece
    if cur:
        lines.append(cur)
    return lines


def render_status(data):
    """The DERIVED human view of ``status_data()`` (philosophy §1 — produced FROM
    the machine artifact, never authored alongside it).

    A header EMPHASIZING the plugin version (a ``(dev)`` fallback when absent),
    an aligned label/value block, and a Route section listing the active route's
    state count + happy-path chain (``A → B → C …``), shown EVEN WHEN the route
    is the default. No emojis (coding-rules §5): rule (``─``) and arrow (``→``)
    characters only.
    """
    version = data.get("plugin_version") or "(dev)"
    header = f"auto-maintainer   v{version}"
    rule = "─" * max(len(header), 40)

    # The release-check line (ASCII only — coding-rules §5): update-available when
    # a strictly-newer version was found, up-to-date when not, or a muted note when
    # the check errored. Derived from status_data's release-check fields.
    if data.get("update_available"):
        release_line = (
            f"update available: v{data.get('latest_version')} "
            f"(installed v{version}) - run /plugin marketplace update")
    elif data.get("release_check_error"):
        release_line = (
            f"release check unavailable ({data['release_check_error']})")
    else:
        release_line = f"up to date (v{version})"

    budget = data["budget"]
    ceiling = "none" if budget["ceiling"] is None else str(budget["ceiling"])
    budget_str = (f"{budget['spent']}/{ceiling}  win={budget['window'] or '-'}")
    if budget["paused"]:
        budget_str += f"  paused={budget['paused']}"

    rows = [
        ("disposition", data["disposition"]),
        ("awaiting", data["awaiting"]),
        ("mode", data["mode"]),
        ("heartbeat", f"{data['heartbeat_interval_minutes']} min"),
        ("budget", budget_str),
        ("reported", f"{data['reported']['filed']} filed / "
         f"{data['reported']['skipped']} skipped"),
        ("read products",
         f"work_items={data['work_items']} "
         f"work_orders={data['work_orders']} "
         f"execution_plan={data['execution_plan']} "
         f"handoffs={data['handoffs']}"),
        ("runtime", data["runtime_dir"]),
    ]
    label_w = max(len(label) for label, _ in rows)

    lines = [header, rule, f"  {release_line}", ""]
    for label, value in rows:
        lines.append(f"  {label.ljust(label_w)}  {value}")

    route = data["route"]
    lines.append("")
    lines.append(f"Route ({route['source']}, {len(route['states'])} states)")
    lines.append(rule)
    lines.extend(f"  {ln}" for ln in _chain_lines(route["chain"]))
    return "\n".join(lines)


def main(argv=None):
    """CLI: default prints the human view; ``--json`` the machine dict; ``--line``
    the retained byte-identical legacy one-line status."""
    parser = argparse.ArgumentParser(
        description="Report the maintainer loop status.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true",
                       help="print the machine-first status_data() as JSON")
    group.add_argument("--line", action="store_true",
                       help="print the legacy byte-identical one-line status")
    args = parser.parse_args(argv)
    if args.line:
        sys.stdout.write(status_line() + "\n")
    elif args.json:
        sys.stdout.write(json.dumps(status_data(), indent=2) + "\n")
    else:
        sys.stdout.write(render_status(status_data()) + "\n")


if __name__ == "__main__":
    main()
