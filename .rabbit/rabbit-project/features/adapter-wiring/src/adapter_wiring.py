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
  - build_loop(default_route, default_map, runtime, start, initial,
        migrate=None) -> (route, states)
  - scaffold_adapter(port, src_dir, ...) -> path     (§3.4.4 authoring tool)
  - wire_adapter(port, address, project_dir, ...) -> (route, map)
  - validate_adapter_conformance(address, runtime, ...) -> CheckResult

The §3.4.4 authoring tool (scaffold_adapter + wire_adapter +
validate_adapter_conformance) is the BYO-adapter convenience that sits ON this
mechanism: scaffold emits a skeleton conforming to the factory convention
(manifest + run(TickContext) -> StateResult), wire records the port -> adapter
map entry + the route.json state, and the conformance validator resolves the
new adapter and runs the SAME load-time checks (validate_signals /
validate_data_readiness + the factory/manifest shape) so BYO-adapter is a
CHECKED operation. It reuses the existing resolver/validator unchanged; it adds
no new contract.

An adapter-map value is EITHER a "module:factory" string (a script factory,
above) OR an agent-adapter object (a dict). adapter-wiring CONSUMES the
agent-dispatch helper lib UNCHANGED to classify + validate the agent object:
an agent entry resolves to (manifest, AgentState) where AgentState carries the
dispatch + signal the executor (a later slice) consumes. Agent entries are
RESOLVED + VALIDATED here but NOT executed (no Agent dispatch); adapter-wiring
does not import scheduling.

It CONSUMES fsm-contracts (validate_route, StateManifest, CheckResult),
tick-orchestrator (validate_signals, validate_data_readiness), and
agent-dispatch (is_agent_entry, validate_agent_adapter); it does not
re-implement them.

Version: 0.4.0
Owner: changyu87
Deprecation criterion: Superseded when the route/adapter wiring model changes
  incompatibly (e.g. the adapter factory convention or route.json schema
  reaches a breaking major version), or when a native rabbit/plugin config
  system subsumes it (see feature.json / docs/spec.md).
"""

import importlib
import json
import os
import sys
from dataclasses import dataclass

# Consume the sibling features via sys.path. Resolve them relative to this
# file's feature dir so the module imports both in the worktree and in the
# installed plugin lib/ (where the modules are flat siblings, already on the
# path once _SRC is inserted). adapter-wiring CONSUMES these unchanged.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURES = os.path.dirname(os.path.dirname(_SRC))
for _dep in ("fsm-contracts", "tick-orchestrator", "agent-dispatch"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import agent_dispatch as ad  # noqa: E402


class WiringError(Exception):
    """Raised when route/map config is malformed or an adapter cannot be
    resolved. The message NAMES the offending port/file so the failure is
    locatable to a source artifact (spec-rules §1: determinism)."""


@dataclass(frozen=True)
class AgentState:
    """The resolved form of an agent-adapter object entry (DESIGN §2.8/§3.4.6).

    `resolve_states` returns each state uniformly as `(manifest, second)`: for a
    script entry `second` is the run callable; for an agent entry `second` is an
    AgentState. It carries the agent-adapter's `manifest` (the fsm-contracts
    StateManifest, identical to the first element of the tuple), the `dispatch`
    list, the `signal` rule, and the raw `entry`. The executor (a later slice)
    consumes it to build + dispatch invocation envelopes via agent-dispatch.
    adapter-wiring resolves + validates an AgentState but never executes it.
    """
    manifest: object
    dispatch: list
    signal: dict
    entry: dict


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
    `default_map`. Same override logic as load_route.

    Each value is EITHER a `str` (script factory address, "module:factory") OR a
    `dict` (agent-adapter object). Any other type is a locatable WiringError
    naming the port. The agent dict is NOT deep-validated here — that is
    resolve_states' job via agent-dispatch."""
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
            "adapter map must be a JSON object of port -> 'module:factory' "
            "string or agent-adapter object")
    for port, entry in amap.items():
        if not isinstance(entry, (str, dict)):
            raise WiringError(
                f"port '{port}' has an invalid adapter entry of type "
                f"{type(entry).__name__}; expected a 'module:factory' string "
                f"or an agent-adapter object")
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


def _resolve_agent(port, entry):
    """Validate an agent-adapter object entry via agent-dispatch (UNCHANGED) and
    resolve it to (manifest, AgentState). agent-dispatch's ValueError is
    re-raised as a locatable WiringError naming `port` (spec-rules §1)."""
    try:
        ad.validate_agent_adapter(entry)
    except ValueError as exc:
        raise WiringError(
            f"port '{port}' has a malformed agent-adapter entry: {exc}")
    m = entry["manifest"]
    manifest = fc.StateManifest(
        reads=m["reads"], writes=m["writes"], emits=m["emits"])
    agent_state = AgentState(
        manifest=manifest, dispatch=entry["dispatch"],
        signal=entry["signal"], entry=entry)
    return manifest, agent_state


def resolve_states(route, adapter_map, runtime):
    """For each state in the route, resolve its adapter-map entry into a uniform
    `(manifest, second)` pair and assemble `{state: (manifest, second)}`.

    A STRING entry is a "module:factory" script address: import the module, get
    the factory, call factory(runtime) -> (manifest, run); `second` is the run
    callable (UNCHANGED). An AGENT entry (a dict, classified by
    agent_dispatch.is_agent_entry) is validated via
    agent_dispatch.validate_agent_adapter and resolved to (manifest,
    AgentState); `second` is the AgentState.

    An unknown port (no map entry), an unimportable module, a missing factory, a
    malformed address, or a malformed agent entry is a locatable WiringError
    naming the port."""
    states = {}
    for name in route["states"]:
        if name not in adapter_map:
            raise WiringError(
                f"port '{name}' has no adapter-map entry")
        entry = adapter_map[name]
        if ad.is_agent_entry(entry):
            states[name] = _resolve_agent(name, entry)
        else:
            states[name] = _resolve_factory(name, entry, runtime)
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


def build_loop(default_route, default_map, runtime, start, initial,
               migrate=None):
    """Load + resolve + validate, returning (route, states) ready for
    tick_orchestrator.run(route, states, ctx, vocab, start). The project_dir is
    read from `runtime['project_dir']`. A bad route/wiring raises WiringError
    before anything runs.

    `migrate` is an optional pure dict -> dict callable run on the loaded
    adapter-map AFTER load_adapter_map and BEFORE resolve_states, so a caller
    (e.g. scheduling) can self-heal stale adapter-map entries on load. The
    migrated map feeds resolve + validate exactly like a loaded map, so a bad
    transform surfaces as a WiringError (never a silent pass). adapter-wiring
    stays template-agnostic: it only invokes the supplied callable and knows
    nothing about what it rewrites. migrate=None (the default) is byte-for-byte
    unchanged behaviour."""
    project_dir = runtime["project_dir"]
    route = load_route(default_route, project_dir)
    adapter_map = load_adapter_map(default_map, project_dir)
    if migrate is not None:
        adapter_map = migrate(adapter_map)
    states = resolve_states(route, adapter_map, runtime)

    manifests = {name: m for name, (m, _r) in states.items()}
    verdict = validate_wiring(route, manifests, start, initial)
    if not verdict.passed:
        raise WiringError(
            "invalid wiring: " + "; ".join(verdict.messages))

    return route, states


# --------------------------------------------------------------------------
# §3.4.4 Adapter authoring / scaffold tool — the BYO-adapter convenience that
# sits ON the mechanism above. It does THREE things, each delegating to the
# already-implemented resolver/validator so a BYO adapter is a CHECKED
# operation, not a hand-edit:
#   1. scaffold_adapter — emit a skeleton module conforming to the factory
#      convention (manifest + run(TickContext) -> StateResult).
#   2. wire_adapter — record the port -> adapter map entry + add the port as a
#      state in route.json (project-local override files).
#   3. validate_adapter_conformance — resolve the new adapter via the existing
#      resolver and run the same load-time checks (factory/manifest shape +
#      validate_signals / validate_data_readiness over a one-state route).
# --------------------------------------------------------------------------

# The scaffold skeleton. A protocol-naive author fills in the three TODOs
# (reads / writes / emits + the run body) and points the adapter-map at
# "<module>:make". It conforms to the factory convention verbatim:
# make(runtime) -> (StateManifest, run) where run(ctx) -> StateResult.
_SCAFFOLD_TEMPLATE = '''\
#!/usr/bin/env python3
"""BYO adapter for the {port!r} port (scaffolded by adapter-wiring §3.4.4).

Fill in the manifest (reads / writes / emits) and the run body, then point the
adapter-map at "{module}:{factory}". The factory convention:

    {factory}(runtime) -> (StateManifest, run)   #  run(ctx) -> StateResult

`runtime` carries the resolved runtime dir (runtime["project_dir"]) plus any
injected config this adapter needs. Validate this adapter with
adapter_wiring.validate_adapter_conformance before wiring it into a live loop.
"""

import fsm_contracts as fc


def {factory}(runtime):
    # TODO: declare the slots this state reads / writes and the signals it
    # emits. Every emitted signal must appear in the route's edges; every read
    # slot must be written by a predecessor (the conformance validator checks
    # this). Anchor invariants apply if {port!r} is a core anchor.
    manifest = fc.StateManifest(reads=[], writes=[], emits=["{default_signal}"])

    def run(ctx):
        # TODO: read ctx slots, do the work, return a StateResult whose `signal`
        # is in manifest.emits and whose `writes` keys are in manifest.writes.
        return fc.StateResult(signal="{default_signal}", writes={{}}, journal=[])

    return manifest, run
'''

_DEFAULT_FACTORY = "make"
_DEFAULT_SIGNAL = "OK"


def _module_name_for_port(port):
    """A safe python module name derived from the port symbol (lowercased,
    non-identifier chars -> '_'). 'PULL' -> 'pull'; 'MY-PORT' -> 'my_port'."""
    safe = "".join(c if (c.isalnum() or c == "_") else "_" for c in port)
    safe = safe.lower().strip("_")
    if not safe or not safe[0].isalpha() and safe[0] != "_":
        safe = "adapter_" + safe
    return safe


def scaffold_adapter(port, src_dir, factory=_DEFAULT_FACTORY,
                     default_signal=_DEFAULT_SIGNAL, overwrite=False):
    """Emit a skeleton adapter module for `port` into `src_dir`, conforming to
    the factory convention (`factory(runtime) -> (StateManifest, run)` with
    `run(ctx) -> StateResult`). Returns the written file path and its
    `"module:factory"` address as a (path, address) pair.

    The module name is derived deterministically from the port symbol. Refusing
    to clobber an existing file (unless `overwrite=True`) is a locatable
    WiringError so a scaffold never silently destroys an author's work."""
    module = _module_name_for_port(port)
    path = os.path.join(src_dir, module + ".py")
    if os.path.isfile(path) and not overwrite:
        raise WiringError(
            f"scaffold for port '{port}' would overwrite existing file {path}; "
            f"pass overwrite=True to replace it")
    if not os.path.isdir(src_dir):
        os.makedirs(src_dir)
    body = _SCAFFOLD_TEMPLATE.format(
        port=port, module=module, factory=factory,
        default_signal=default_signal)
    with open(path, "w") as f:
        f.write(body)
    return path, f"{module}:{factory}"


def wire_adapter(port, address, project_dir, default_route=None,
                 default_map=None):
    """Record `port -> address` in the project-local adapter-map.json and add
    `port` as a state in the project-local route.json, creating either file
    from the supplied default (or an empty skeleton) if it is absent. Returns
    the written (route, adapter_map) pair.

    This is the DATA half of BYO-adapter: it edits only the project-local
    override files under ${project_dir}/.auto-maintainer/. It does NOT validate
    the wiring (call validate_adapter_conformance, or build_loop, for that) and
    it does NOT add any edges — routing topology stays an explicit author
    choice. A pre-existing port keeps its place; only the map entry is set."""
    cfg = os.path.join(project_dir, _CONFIG_DIRNAME)
    if not os.path.isdir(cfg):
        os.makedirs(cfg)

    route_path = _config_path(project_dir, _ROUTE_FILENAME)
    if os.path.isfile(route_path):
        with open(route_path, "r") as f:
            route = json.load(f)
    elif default_route is not None:
        route = json.loads(json.dumps(default_route))
    else:
        route = {"schema_version": "1.0.0", "states": [], "edges": [],
                 "terminal": []}
    if port not in route["states"]:
        route["states"].append(port)

    map_path = _config_path(project_dir, _MAP_FILENAME)
    if os.path.isfile(map_path):
        with open(map_path, "r") as f:
            amap = json.load(f)
    elif default_map is not None:
        amap = json.loads(json.dumps(default_map))
    else:
        amap = {}
    amap[port] = address

    with open(route_path, "w") as f:
        json.dump(route, f, indent=2)
    with open(map_path, "w") as f:
        json.dump(amap, f, indent=2)
    return route, amap


def validate_adapter_conformance(address, runtime, port=None,
                                 emits_route=None, initial=None):
    """Resolve the adapter at `address` ("module:factory") and CHECK it against
    the contract, reusing the same machinery a live load uses. Returns a
    fsm-contracts CheckResult. This is what makes BYO-adapter a CHECKED
    operation (§3.4.4).

    `port` is the state name the adapter occupies (defaults to the address's
    module name); when `emits_route` is supplied, `port` MUST be the route
    state this adapter resolves so the validators key the manifest correctly.

    Checks, in order:
      - the address resolves: import module, get factory, call factory(runtime)
        -> (manifest, run) (a resolution failure is reported, not raised);
      - the factory returns a fsm-contracts StateManifest and a callable run;
      - the manifest emits a non-empty, declared signal set;
      - signal-validity + data-readiness over a single-state route for this
        port (reusing tick-orchestrator's validate_signals /
        validate_data_readiness). When `emits_route` is supplied the adapter is
        validated against that real route instead of the synthetic one, so an
        author can check the adapter in the topology it will actually run in.
    """
    try:
        manifest, run = _resolve_factory("<scaffold>", address, runtime)
    except WiringError as exc:
        return fc.CheckResult(False, [str(exc)])

    messages = []
    if not isinstance(manifest, fc.StateManifest):
        messages.append(
            f"factory at '{address}' did not return a fsm-contracts "
            f"StateManifest (got {type(manifest).__name__})")
    if not callable(run):
        messages.append(
            f"factory at '{address}' did not return a callable run "
            f"(got {type(run).__name__})")
    if messages:
        return fc.CheckResult(False, messages)

    if not manifest.emits:
        messages.append("adapter manifest declares no emitted signals")

    if port is None:
        port = address.split(":")[0]
    if emits_route is not None:
        route = emits_route
        start = port if port in route["states"] else route["states"][0]
        # Other states in the supplied route are not under test here; give them
        # an empty manifest so the reused validators run over the full topology
        # without falsely flagging a missing manifest for a sibling state.
        manifests = {s: fc.StateManifest(reads=[], writes=[], emits=[])
                     for s in route["states"]}
        manifests[port] = manifest
    else:
        # A synthetic single-state route: the port is GUARD-anchored start and
        # its own terminal, so the reused validators run with no topology
        # assumptions beyond this one adapter.
        route = {
            "schema_version": "1.0.0",
            "states": [port],
            "edges": [{"state": port, "signal": manifest.emits[0],
                       "next": port}],
            "terminal": [port],
        }
        start = port
        manifests = {port: manifest}

    sig = to.validate_signals(route, manifests)
    if not sig.passed:
        messages.extend(sig.messages)
    ready = to.validate_data_readiness(
        route, manifests, start, list(initial or []))
    if not ready.passed:
        messages.extend(ready.messages)

    if messages:
        return fc.CheckResult(False, messages)
    return fc.CheckResult(True, [
        f"OK: adapter '{address}' conforms (factory shape + manifest + "
        f"signal/data validation)"])
