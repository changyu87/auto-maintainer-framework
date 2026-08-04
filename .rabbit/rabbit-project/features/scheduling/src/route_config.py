#!/usr/bin/env python3
"""route_config — the guided route wiring CLI (spec §3.4.3 / §3.10.2).

scheduling owns DEFAULT_ROUTE, so it ships this deterministic
load-modify-VALIDATE-save editor for the project-local override
``${project_dir}/.auto-maintainer/route.json``. A user can insert / remove a
state, or add / remove an edge, WITHOUT hand-writing the route JSON.

Every edit is applied to the route dict and then VALIDATED by building the loop
(``adapter_wiring.build_loop`` over the edited route + the active adapter-map:
resolve + signals + data-readiness + anchor invariants) BEFORE writing. A failing
edit is REJECTED — non-zero exit, file NOT written. An invalid route can never be
saved.

Subcommands:
  --show       print the active route (override else DEFAULT_ROUTE) + its source
               (#59), a readable derivative of the machine-first route.
  --describe   emit a machine-first catalog of the current states/edges + the
               editable operations (for the skill to drive).
  insert-state --state S --after A --before B
  remove-state --state S
  add-edge --state S --signal G --next N
  remove-edge --state S --signal G

adapter-wiring is CONSUMED UNCHANGED (its validators); this module never modifies
it. The CLI lives in scheduling because it needs scheduling's DEFAULT_ROUTE +
DEFAULT_ADAPTER_MAP defaults.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API), or when a native rabbit/plugin config
  system subsumes the wiring-config CLIs.
"""

import argparse
import json
import os
import sys

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402
import run_tick as rt  # noqa: E402


# The editable operations the CLI advertises (so the skill can drive them).
_OPERATIONS = ["insert-state", "remove-state", "add-edge", "remove-edge"]


def _override_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "route.json")


def load_route(project_dir):
    """The ACTIVE route: the project-local override when present, else
    scheduling's DEFAULT_ROUTE. Read-only — does not write."""
    path = _override_path(project_dir)
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return json.loads(json.dumps(rt.DEFAULT_ROUTE))


def _validate_route(route, project_dir):
    """Build the loop over the edited route + the active adapter-map (loaded by
    adapter-wiring), which resolves + validates signals + data-readiness + anchor
    invariants. Raises WiringError on any failure. Returns None on success."""
    adapter_map = aw.load_adapter_map(rt.DEFAULT_ADAPTER_MAP, project_dir)
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": os.path.join(project_dir, ".auto-maintainer"),
        "source": None,
        "now": None,
        "governance": {"mode": "dry-run"},
    }
    states = aw.resolve_states(route, adapter_map, runtime)
    manifests = {name: m for name, (m, _r) in states.items()}
    verdict = aw.validate_wiring(route, manifests, "GUARD", rt._INITIAL_SLOTS)
    if not verdict.passed:
        raise aw.WiringError(
            "invalid wiring: " + "; ".join(verdict.messages))


def _save_route(route, project_dir):
    path = _override_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(route, f, indent=2, sort_keys=True)
        f.write("\n")


def _insert_state(route, state, after, before):
    """Insert `state` into route["states"] (immediately after `after`), wiring
    `after -OK-> state` and `state -OK/EMPTY-> before`, and re-pointing `after`'s
    existing OK edge that targeted `before` to point at `state`. A minimal,
    deterministic edit; the validator is the safety net."""
    if state in route["states"]:
        raise ValueError(f"state '{state}' already present")
    states = list(route["states"])
    if after not in states:
        raise ValueError(f"--after state '{after}' not in route")
    if before not in states:
        raise ValueError(f"--before state '{before}' not in route")
    idx = states.index(after)
    states.insert(idx + 1, state)
    route["states"] = states
    edges = [dict(e) for e in route["edges"]]
    # Re-point any `after -*-> before` edge to target `state` instead.
    for e in edges:
        if e["state"] == after and e["next"] == before:
            e["next"] = state
    # New edges from `state` into `before` for the OK + EMPTY signals (covers the
    # read-and-idle producers' OK/EMPTY emit set). The validator drops any that
    # are signal-invalid for the resolved adapter.
    edges.append({"state": state, "signal": "OK", "next": before})
    edges.append({"state": state, "signal": "EMPTY", "next": before})
    route["edges"] = edges
    return route


def _remove_state(route, state):
    if state not in route["states"]:
        raise ValueError(f"state '{state}' not in route")
    route["states"] = [s for s in route["states"] if s != state]
    # Drop edges out of `state`; re-point edges INTO `state` is left to the user
    # via add-edge (a deterministic minimal edit). For a state inserted by
    # insert-state, re-point predecessors that targeted it back at its OK target.
    successor = None
    for e in route["edges"]:
        if e["state"] == state and e["signal"] == "OK":
            successor = e["next"]
            break
    edges = []
    for e in route["edges"]:
        if e["state"] == state:
            continue  # drop edges out of the removed state
        if e["next"] == state and successor is not None:
            e = dict(e)
            e["next"] = successor
        edges.append(e)
    route["edges"] = edges
    return route


def _add_edge(route, state, signal, nxt):
    edges = [dict(e) for e in route["edges"]]
    for e in edges:
        if e["state"] == state and e["signal"] == signal:
            e["next"] = nxt
            route["edges"] = edges
            return route
    edges.append({"state": state, "signal": signal, "next": nxt})
    route["edges"] = edges
    return route


def _remove_edge(route, state, signal):
    route["edges"] = [
        e for e in route["edges"]
        if not (e["state"] == state and e["signal"] == signal)]
    return route


def _cmd_show(project_dir):
    route = load_route(project_dir)
    path = _override_path(project_dir)
    source = f"override:{path}" if os.path.isfile(path) else "default"
    sys.stdout.write(f"route source={source}\n")
    sys.stdout.write(json.dumps(route, indent=2, sort_keys=True) + "\n")
    return 0


def _cmd_describe(project_dir):
    route = load_route(project_dir)
    path = _override_path(project_dir)
    catalog = {
        "source": "override" if os.path.isfile(path) else "default",
        "states": route["states"],
        "edges": route["edges"],
        "terminal": route.get("terminal", []),
        "operations": _OPERATIONS,
    }
    sys.stdout.write(json.dumps(catalog, indent=2) + "\n")
    return 0


def _commit(route, project_dir, summary):
    """Validate the edited route then write it; reject (non-zero, no write) on any
    validation failure."""
    try:
        _validate_route(route, project_dir)
    except (aw.WiringError, ValueError) as exc:
        sys.stdout.write(f"REJECTED: {exc}\n")
        return 1
    _save_route(route, project_dir)
    sys.stdout.write(f"OK: {summary} -> {_override_path(project_dir)}\n")
    return 0


def _apply_edit(project_dir, edit_fn, summary):
    route = load_route(project_dir)
    try:
        route = edit_fn(route)
    except ValueError as exc:
        sys.stdout.write(f"REJECTED: {exc}\n")
        return 1
    return _commit(route, project_dir, summary)


def main(argv=None):
    """The route CLI entrypoint. Returns the process exit code.

    --show / --describe are read-only; insert-state / remove-state / add-edge /
    remove-edge each apply a deterministic edit, VALIDATE via
    adapter_wiring.build_loop, and write ONLY on success — a failing edit exits
    non-zero and writes nothing.
    """
    parser = argparse.ArgumentParser(
        description="Guided route.json editor (load-modify-VALIDATE-save).")
    parser.add_argument("--project-dir", dest="project_dir")
    parser.add_argument("--show", action="store_true",
                        help="print the active route + source")
    parser.add_argument("--describe", action="store_true",
                        help="emit the machine-first states/edges catalog")
    sub = parser.add_subparsers(dest="cmd")

    p_ins = sub.add_parser("insert-state", help="insert a state into the route")
    p_ins.add_argument("--project-dir", dest="project_dir")
    p_ins.add_argument("--state", required=True)
    p_ins.add_argument("--after", required=True)
    p_ins.add_argument("--before", required=True)

    p_rm = sub.add_parser("remove-state", help="remove a state from the route")
    p_rm.add_argument("--project-dir", dest="project_dir")
    p_rm.add_argument("--state", required=True)

    p_ae = sub.add_parser("add-edge", help="add/replace an edge")
    p_ae.add_argument("--project-dir", dest="project_dir")
    p_ae.add_argument("--state", required=True)
    p_ae.add_argument("--signal", required=True)
    p_ae.add_argument("--next", dest="nxt", required=True)

    p_re = sub.add_parser("remove-edge", help="remove an edge")
    p_re.add_argument("--project-dir", dest="project_dir")
    p_re.add_argument("--state", required=True)
    p_re.add_argument("--signal", required=True)

    args = parser.parse_args(argv)
    project_dir = args.project_dir or os.getcwd()

    if args.cmd is None:
        if args.describe:
            return _cmd_describe(project_dir)
        return _cmd_show(project_dir)

    if args.cmd == "insert-state":
        return _apply_edit(
            project_dir,
            lambda r: _insert_state(r, args.state, args.after, args.before),
            f"insert-state {args.state}")
    if args.cmd == "remove-state":
        return _apply_edit(
            project_dir, lambda r: _remove_state(r, args.state),
            f"remove-state {args.state}")
    if args.cmd == "add-edge":
        return _apply_edit(
            project_dir,
            lambda r: _add_edge(r, args.state, args.signal, args.nxt),
            f"add-edge {args.state}/{args.signal}")
    if args.cmd == "remove-edge":
        return _apply_edit(
            project_dir,
            lambda r: _remove_edge(r, args.state, args.signal),
            f"remove-edge {args.state}/{args.signal}")

    return _cmd_show(project_dir)


if __name__ == "__main__":
    sys.exit(main())
