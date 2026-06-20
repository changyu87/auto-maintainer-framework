#!/usr/bin/env python3
"""adapter_map_config — the guided adapter-map wiring CLI (spec §3.4.3 / §3.10.2).

scheduling owns DEFAULT_ADAPTER_MAP and every port's runtime details, so it ships
this deterministic load-modify-VALIDATE-save editor for the project-local override
``${project_dir}/.auto-maintainer/adapter-map.json``. A user can set a port to a
script factory address (a "module:factory" string) OR to an AGENT entry without
hand-writing the agent-adapter JSON.

For an agent entry on a KNOWN agent-capable port (TRIAGE, IMPLEMENT, …) the user
supplies ONLY the ``subagent_type``; the CLI fills the rest — the ``writes`` slot,
``cardinality``, ``effect`` (only for ACTING ports), and a CONCRETE
``output_example`` — from ``AGENT_PORT_TEMPLATES[port]``. For an unknown/custom
port the CLI additionally requires ``--writes`` + (if acting) ``--effect`` + an
``--output-example``, since they cannot be inferred.

Every edit is VALIDATED by resolving the resulting map
(``adapter_wiring.resolve_states`` via ``build_loop``, which deep-validates agent
entries through ``agent_dispatch.validate_agent_adapter``) BEFORE writing. An
invalid entry is REJECTED — non-zero exit, file NOT written.

``AGENT_PORT_TEMPLATES`` is scheduling-owned (built from the ports' own slot
owners: work-intake ``WORK_ORDERS_SLOT``, implement ``HANDOFFS_SLOT``), which is
exactly why this CLI lives in scheduling and not in dependency-free
adapter-wiring. adapter-wiring + agent-dispatch are CONSUMED UNCHANGED (their
validators); this module never modifies them.

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

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adapter_wiring as aw  # noqa: E402
import agent_dispatch as ad  # noqa: E402
import work_intake as wi  # noqa: E402
import implement as im  # noqa: E402
import run_tick as rt  # noqa: E402


# --------------------------------------------------------------------------
# AGENT_PORT_TEMPLATES (scheduling-owned).
#
# Each known agent-capable port -> {writes, cardinality, effect?, output_example},
# built from the ports' OWN slot owners so a bare subagent_type is enough to
# produce a valid agent entry. `effect` is present ONLY for ACTING ports. Each
# `output_example` is a CONCRETE example value in the slot's top-level type — a
# real sample output to mimic — NEVER a JSON-Schema descriptor (the agent-adapter
# rule / #119: protocol-naive subagents copy a concrete example reliably but are
# confused by a descriptor).
# --------------------------------------------------------------------------

# A concrete accepted work_order (the shape TRIAGE produces): work-intake's
# WorkOrder.to_dict for an accepted item. A real example value, not a schema.
_WORK_ORDER_EXAMPLE = {
    "schema_version": wi.WORK_ORDER_SCHEMA_VERSION,
    "id": "owner/repo#1-wo",
    "work_item_id": "owner/repo#1",
    "title": "Fix the crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/owner/repo/issues/1",
    "labels": ["bug"],
    "decision": "accepted",
    "reason": "",
    "created_at": "2026-06-01T00:00:00Z",
}

# A concrete planned handoff (the shape IMPLEMENT produces per work_order). A real
# example value — mirrors implement._planned_handoff's closed handoff schema.
_HANDOFF_EXAMPLE = {
    "schema_version": im.HANDOFF_SCHEMA_VERSION,
    "work_order_id": "owner/repo#1-wo",
    "status": "planned",
    "artifact": {"kind": "none", "ref": None},
    "discovered_work": [],
    "blocked_reason": None,
}

AGENT_PORT_TEMPLATES = {
    # TRIAGE: maps work_items -> work_orders, NON-acting (no outward effect), one
    # dispatch over the whole work_items list.
    "TRIAGE": {
        "writes": wi.WORK_ORDERS_SLOT["name"],
        "reads": wi.TRIAGE_MANIFEST.reads,
        "emits": list(wi.TRIAGE_MANIFEST.emits),
        "cardinality": "once",
        "signal_rule": "nonempty_else_empty",
        "output_example": [_WORK_ORDER_EXAMPLE],
    },
    # IMPLEMENT: maps execution_plan -> handoffs, ACTING (effect=implement), one
    # dispatch PER ordered work_order in the execution_plan.
    "IMPLEMENT": {
        "writes": im.HANDOFFS_SLOT["name"],
        "reads": im.IMPLEMENT_MANIFEST.reads,
        "emits": list(im.IMPLEMENT_MANIFEST.emits),
        "cardinality": {"per_item": "execution_plan.ordered"},
        "signal_rule": "blocked_if_any",
        "effect": "implement",
        "isolation": "worktree",
        "output_example": _HANDOFF_EXAMPLE,
    },
}


def _override_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "adapter-map.json")


def load_map(project_dir):
    """The ACTIVE adapter-map: the project-local override when present, else
    scheduling's DEFAULT_ADAPTER_MAP. Read-only — does not write."""
    path = _override_path(project_dir)
    if os.path.isfile(path):
        with open(path) as f:
            return json.load(f)
    return dict(rt.DEFAULT_ADAPTER_MAP)


def _validate_map(amap, project_dir):
    """Resolve the candidate map through adapter_wiring.build_loop (over the active
    route + the candidate map), which deep-validates agent entries via
    agent-dispatch and enforces the anchor invariants. Raises WiringError on any
    invalid entry. Returns None on success."""
    route = aw.load_route(rt.DEFAULT_ROUTE, project_dir)
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": os.path.join(project_dir, ".auto-maintainer"),
        "source": None,
        "now": None,
        "governance": {"mode": "dry-run"},
    }
    states = aw.resolve_states(route, amap, runtime)
    manifests = {name: m for name, (m, _r) in states.items()}
    verdict = aw.validate_wiring(route, manifests, "GUARD", rt._INITIAL_SLOTS)
    if not verdict.passed:
        raise aw.WiringError(
            "invalid wiring: " + "; ".join(verdict.messages))


def _save_map(amap, project_dir):
    path = _override_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(amap, f, indent=2, sort_keys=True)
        f.write("\n")


def _build_agent_entry(port, subagent_type, writes=None, effect=None,
                       output_example=None):
    """Assemble an agent-adapter entry for `port`.

    For a KNOWN port (in AGENT_PORT_TEMPLATES) the user supplies ONLY
    `subagent_type`; writes/cardinality/effect/output_example come from the
    template. For an unknown/custom port the caller must pass `writes` (+ `effect`
    if acting + `output_example`). Raises ValueError when a custom port is missing
    a required field."""
    template = AGENT_PORT_TEMPLATES.get(port)
    if template is not None:
        writes_slot = template["writes"]
        cardinality = template["cardinality"]
        reads = list(template["reads"])
        emits = list(template["emits"])
        signal_rule = template["signal_rule"]
        eff = template.get("effect")
        example = template["output_example"]
    else:
        if not writes:
            raise ValueError(
                f"port '{port}' is not a known agent-capable port; "
                f"--writes is required for a custom port")
        if output_example is None:
            raise ValueError(
                f"port '{port}' is a custom port; --output-example is required "
                f"(a concrete sample output value to mimic)")
        writes_slot = writes
        # A custom port reads its writes slot's producer? Unknown — require the
        # user-supplied writes as the single read+write, cardinality once.
        cardinality = "once"
        reads = [writes]
        emits = ["OK", "EMPTY"]
        signal_rule = "nonempty_else_empty"
        eff = effect
        example = output_example

    dispatch = {
        "subagent_type": subagent_type,
        "inputs": reads,
        "writes": writes_slot,
        "cardinality": cardinality,
        "output_example": example,
    }
    if eff:
        dispatch["effect"] = eff
        # An acting dispatch runs isolated by default (worktree), matching the
        # acting-state contract; the template may override.
        dispatch["isolation"] = (template or {}).get("isolation", "worktree")
    entry = {
        "kind": "agent",
        "manifest": {"reads": reads, "writes": [writes_slot], "emits": emits},
        "dispatch": [dispatch],
        "signal": {"rule": signal_rule},
    }
    return entry


def _cmd_show(project_dir):
    amap = load_map(project_dir)
    path = _override_path(project_dir)
    source = f"override:{path}" if os.path.isfile(path) else "default"
    sys.stdout.write(f"adapter-map source={source}\n")
    sys.stdout.write(json.dumps(amap, indent=2, sort_keys=True) + "\n")
    return 0


def _commit(amap, project_dir, summary):
    """Validate the candidate map then write it; reject (non-zero, no write) on
    any validation failure."""
    try:
        _validate_map(amap, project_dir)
    except (aw.WiringError, ValueError) as exc:
        sys.stdout.write(f"REJECTED: {exc}\n")
        return 1
    _save_map(amap, project_dir)
    sys.stdout.write(f"OK: {summary} -> {_override_path(project_dir)}\n")
    return 0


def _cmd_set_agent(project_dir, port, subagent_type, writes, effect,
                   output_example):
    amap = load_map(project_dir)
    try:
        entry = _build_agent_entry(
            port, subagent_type, writes=writes, effect=effect,
            output_example=output_example)
    except ValueError as exc:
        sys.stdout.write(f"REJECTED: {exc}\n")
        return 1
    # Deep-validate the agent entry itself BEFORE writing — even for a CUSTOM
    # port that the active route does not include (resolve_states only validates
    # states present in the route, so a not-yet-routed custom port would slip
    # through). agent-dispatch's validator is the dependency-free deep check
    # (incl. the #119 schema-descriptor guard on output_example).
    try:
        ad.validate_agent_adapter(entry)
    except ValueError as exc:
        sys.stdout.write(f"REJECTED: {exc}\n")
        return 1
    amap[port] = entry
    return _commit(amap, project_dir,
                   f"set agent {port} -> {subagent_type}")


def _cmd_set_script(project_dir, port, address):
    amap = load_map(project_dir)
    amap[port] = address
    return _commit(amap, project_dir, f"set script {port} -> {address}")


def main(argv=None):
    """The adapter-map CLI entrypoint. Returns the process exit code.

    Subcommands: --show; set-agent --port P --subagent-type T [--writes W]
    [--effect E] [--output-example JSON]; set-script --port P --address A. Every
    mutating subcommand validates BEFORE writing; a failing edit exits non-zero
    and writes nothing.
    """
    parser = argparse.ArgumentParser(
        description="Guided adapter-map.json editor (load-modify-VALIDATE-save).")
    parser.add_argument("--project-dir", dest="project_dir")
    parser.add_argument("--show", action="store_true",
                        help="print the active adapter-map + source")
    sub = parser.add_subparsers(dest="cmd")

    p_agent = sub.add_parser("set-agent", help="set a port to an agent entry")
    p_agent.add_argument("--project-dir", dest="project_dir")
    p_agent.add_argument("--port", required=True)
    p_agent.add_argument("--subagent-type", dest="subagent_type", required=True)
    p_agent.add_argument("--writes", default=None,
                         help="writes slot (required for a custom port)")
    p_agent.add_argument("--effect", default=None,
                         help="effect for a custom acting port")
    p_agent.add_argument("--output-example", dest="output_example",
                         default=None,
                         help="concrete output example JSON for a custom port")

    p_script = sub.add_parser("set-script",
                              help="set a port to a 'module:factory' address")
    p_script.add_argument("--project-dir", dest="project_dir")
    p_script.add_argument("--port", required=True)
    p_script.add_argument("--address", required=True)

    args = parser.parse_args(argv)
    project_dir = args.project_dir or os.getcwd()

    if args.show or args.cmd is None:
        return _cmd_show(project_dir)

    if args.cmd == "set-agent":
        output_example = None
        if args.output_example is not None:
            try:
                output_example = json.loads(args.output_example)
            except ValueError as exc:
                sys.stdout.write(f"REJECTED: bad --output-example JSON: {exc}\n")
                return 1
        return _cmd_set_agent(
            project_dir, args.port, args.subagent_type, args.writes,
            args.effect, output_example)

    if args.cmd == "set-script":
        return _cmd_set_script(project_dir, args.port, args.address)

    return _cmd_show(project_dir)


if __name__ == "__main__":
    sys.exit(main())
