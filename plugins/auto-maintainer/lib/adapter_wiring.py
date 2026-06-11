#!/usr/bin/env python3
"""adapter-wiring — the route-as-data + adapter wiring mechanism (DESIGN §3.4.3).

This is what makes the framework actually ports-and-adapters at RUNTIME: load a
declarative route.json + a port -> adapter map (project config), resolve each
routed port to its adapter, validate the wiring at LOAD, and hand the
orchestrator a ready (route, states) pair. A project can insert / reorder / swap
adapter states by editing DATA, not code.

It is a PURE mechanism: it does NOT define the maintainer's default route or the
built-in adapters (scheduling supplies those). It loads / resolves / validates
whatever paths it is given, resolving adapters dynamically by their map
"module:factory" address strings. It never imports scheduling /
durable-state / lifecycle-dispositions / work-intake directly.

The adapter factory convention (the bring-your-own contract): an adapter is
addressed as "module:factory", where

    factory(runtime) -> (StateManifest, run_callable)

and run_callable has the fsm-contracts signature run(TickContext) ->
StateResult. `runtime` carries the resolved runtime dir + any injected config a
factory needs. Core anchors (GUARD/DRAIN/PERSIST/EXIT) are addressed the same
way; the validator enforces their anchor invariants.

Public surface:
  - load_route(default_route, project_dir) -> route
  - load_adapter_map(default_map, project_dir) -> map
  - resolve_states(route, adapter_map, runtime) -> states
  - validate_wiring(route, manifests, start, initial) -> CheckResult
  - build_loop(default_route, default_map, runtime, start, initial)
        -> (route, states)

It CONSUMES fsm-contracts (validate_route, StateManifest, CheckResult) and
tick-orchestrator (validate_signals, validate_data_readiness); it does not
re-implement them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the route/adapter wiring model changes
  incompatibly (e.g. the adapter factory convention or route.json schema
  reaches a breaking major version), or when a native rabbit/plugin config
  system subsumes it (see feature.json / docs/spec.md).
"""

import importlib
import json
import os

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
import os  # noqa: E402
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_contracts as fc
import tick_orchestrator as to


class WiringError(Exception):
    """Raised when route/map config is malformed or an adapter cannot be
    resolved. The message NAMES the offending port/file so the failure is
    locatable to a source artifact (spec-rules §1: determinism)."""


# Project-local config lives under ${project_dir}/.auto-maintainer/ — the same
# runtime dir the loop already uses (spec "Open questions").
_CONFIG_DIRNAME = ".auto-maintainer"
_ROUTE_FILENAME = "route.json"
_MAP_FILENAME = "adapter-map.json"

# Anchor symbols. These are FIXED core states the validator enforces invariants
# over; they are not project-overridable per DESIGN §1.1.
_ENTRY_ANCHOR = "GUARD"
_PERSIST_ANCHOR = "PERSIST"
_EXIT_ANCHOR = "EXIT"


def _config_path(project_dir, filename):
    return os.path.join(project_dir, _CONFIG_DIRNAME, filename)


def load_route(default_route, project_dir):
    """Return the project-local ${project_dir}/.auto-maintainer/route.json if it
    exists, else `default_route` (supplied by the caller). The returned object
    is validated against the fsm-contracts route.json shape; a malformed route
    is a locatable WiringError naming route.json."""
    path = _config_path(project_dir, _ROUTE_FILENAME)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                route = json.load(f)
        except (OSError, ValueError) as exc:
            raise WiringError(
                f"failed to read project-local route.json at {path}: {exc}")
    else:
        route = default_route

    verdict = fc.validate_route(route)
    if not verdict.passed:
        where = path if os.path.isfile(path) else "default route"
        raise WiringError(
            f"malformed route.json ({where}): " + "; ".join(verdict.messages))
    return route


def load_adapter_map(default_map, project_dir):
    """Return the project-local
    ${project_dir}/.auto-maintainer/adapter-map.json if it exists, else
    `default_map`. Same override logic as load_route."""
    path = _config_path(project_dir, _MAP_FILENAME)
    if os.path.isfile(path):
        try:
            with open(path, "r") as f:
                amap = json.load(f)
        except (OSError, ValueError) as exc:
            raise WiringError(
                f"failed to read project-local adapter-map.json at {path}: "
                f"{exc}")
    else:
        amap = default_map

    if not isinstance(amap, dict):
        raise WiringError(
            "adapter map must be a JSON object of port -> 'module:factory'")
    return amap


def _resolve_factory(port, address, runtime):
    """Import `module`, get `factory`, call factory(runtime). Any failure is a
    WiringError naming `port` so the offending port is locatable."""
    if not isinstance(address, str) or address.count(":") != 1:
        raise WiringError(
            f"port '{port}' has malformed adapter address {address!r}; "
            f"expected 'module:factory'")
    module_name, factory_name = address.split(":")
    if not module_name or not factory_name:
        raise WiringError(
            f"port '{port}' has malformed adapter address {address!r}; "
            f"expected 'module:factory'")

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise WiringError(
            f"port '{port}': cannot import module '{module_name}': {exc}")

    try:
        factory = getattr(module, factory_name)
    except AttributeError:
        raise WiringError(
            f"port '{port}': module '{module_name}' has no factory "
            f"'{factory_name}'")

    manifest, run = factory(runtime)
    return manifest, run


def resolve_states(route, adapter_map, runtime):
    """For each state in the route, look up its 'module:factory' in
    `adapter_map`, import the module, get the factory, call factory(runtime) ->
    (manifest, run), and assemble {state: (manifest, run)}.

    An unknown port (no map entry), an unimportable module, a missing factory,
    or a malformed address is a locatable WiringError naming the port."""
    states = {}
    for name in route["states"]:
        if name not in adapter_map:
            raise WiringError(
                f"port '{name}' has no adapter-map entry")
        states[name] = _resolve_factory(name, adapter_map[name], runtime)
    return states


def _reachable_without(route, start, blocked):
    """Set of states reachable from `start` over the edge graph, never entering
    `blocked`. `blocked` is a state name that prunes the traversal."""
    edges = route["edges"]
    seen = set()
    if start == blocked:
        return seen
    stack = [start]
    seen.add(start)
    while stack:
        cur = stack.pop()
        for e in edges:
            if e["state"] == cur and e["next"] != blocked \
                    and e["next"] not in seen:
                seen.add(e["next"])
                stack.append(e["next"])
    return seen


def validate_wiring(route, manifests, start, initial):
    """Validate the resolved wiring at LOAD time, before any tick runs. Runs
    tick-orchestrator's validate_signals + validate_data_readiness over the
    resolved manifests, plus the anchor invariants:

      - entry is GUARD (start == 'GUARD');
      - a terminal state exists;
      - if both PERSIST and EXIT are present, PERSIST precedes EXIT on every
        path (EXIT is unreachable from start when PERSIST is pruned).

    Returns a fsm-contracts CheckResult. The run is gated on this passing."""
    messages = []

    sig = to.validate_signals(route, manifests)
    if not sig.passed:
        messages.extend(sig.messages)

    ready = to.validate_data_readiness(route, manifests, start, initial)
    if not ready.passed:
        messages.extend(ready.messages)

    if start != _ENTRY_ANCHOR:
        messages.append(
            f"entry state '{start}' is not the GUARD anchor (entry must be "
            f"'{_ENTRY_ANCHOR}')")

    if not route["terminal"]:
        messages.append("no terminal state declared (EXIT anchor required)")

    states = set(route["states"])
    if _PERSIST_ANCHOR in states and _EXIT_ANCHOR in states:
        reachable = _reachable_without(route, start, _PERSIST_ANCHOR)
        if _EXIT_ANCHOR in reachable:
            messages.append(
                f"'{_EXIT_ANCHOR}' is reachable without passing "
                f"'{_PERSIST_ANCHOR}' (PERSIST must precede EXIT)")

    if messages:
        return fc.CheckResult(False, messages)
    return fc.CheckResult(True, ["OK: wiring is signal-valid, data-ready, and "
                                 "anchor-conforming"])


def build_loop(default_route, default_map, runtime, start, initial):
    """Load + resolve + validate, returning (route, states) ready for
    tick_orchestrator.run(route, states, ctx, vocab, start). The project_dir is
    read from `runtime['project_dir']`. A bad route/wiring raises WiringError
    before anything runs."""
    project_dir = runtime["project_dir"]
    route = load_route(default_route, project_dir)
    adapter_map = load_adapter_map(default_map, project_dir)
    states = resolve_states(route, adapter_map, runtime)

    manifests = {name: m for name, (m, _r) in states.items()}
    verdict = validate_wiring(route, manifests, start, initial)
    if not verdict.passed:
        raise WiringError(
            "invalid wiring: " + "; ".join(verdict.messages))

    return route, states
