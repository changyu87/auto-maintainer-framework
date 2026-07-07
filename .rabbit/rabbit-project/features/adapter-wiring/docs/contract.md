---
feature: adapter-wiring
version: 0.5.0
owner: changyu87
deprecation_criterion: Superseded when the route/adapter wiring model changes incompatibly (e.g. the adapter factory convention or route.json schema reaches a breaking major version), or when a native rabbit/plugin config system subsumes it (see feature.json / docs/spec.md).
---

# adapter-wiring — Contract

The route-as-data + adapter wiring mechanism. It loads a declarative
`route.json` + a `port -> "module:factory"` adapter map (project config),
resolves each routed port to its adapter via `importlib`, validates the wiring
at LOAD time, and returns a ready `(route, states)` pair for
`tick_orchestrator.run`. It is a pure mechanism: it does NOT define the default
route or the built-in adapters, and it resolves adapters dynamically by string
so it has no static coupling to any concrete adapter feature.

An adapter-map entry is EITHER a `"module:factory"` script address OR an
agent-adapter object; the latter is classified + validated via the
`agent-dispatch` helper lib (consumed UNCHANGED) and resolved to an
`AgentState`. Agent entries are resolved + validated here but NOT executed. Agent
resolution additionally enforces the DESIGN §2.2 bounded-parallel invariant: an
acting (`effect`) `per_item` dispatch MUST declare `isolation == "worktree"`,
else a locatable `WiringError` naming the port.

## The adapter factory convention (the bring-your-own contract)

An adapter is addressed as `"module:factory"`, where:

```
factory(runtime) -> (StateManifest, run_callable)
```

and `run_callable` has the fsm-contracts signature `run(TickContext) ->
StateResult`. `runtime` carries the resolved runtime dir (`project_dir`) plus
any injected config a factory needs. This factory signature is the entire
contract a third-party adapter implements.

```json
{
  "provides": {
    "files": [],
    "scripts": [
      "load_route(default_route, project_dir) -> route",
      "load_adapter_map(default_map, project_dir) -> map  # values: 'module:factory' string OR agent-adapter object",
      "resolve_states(route, adapter_map, runtime) -> states  # {state: (manifest, run_callable | AgentState)}; agent resolution enforces DESIGN §2.2: an acting per_item dispatch must declare isolation=='worktree'",
      "validate_wiring(route, manifests, start, initial) -> CheckResult",
      "build_loop(default_route, default_map, runtime, start, initial, migrate=None) -> (route, states)  # optional migrate: pure dict->dict run on the loaded adapter-map after load, before resolve/validate",
      "the adapter factory convention: 'module:factory', factory(runtime) -> (StateManifest, run_callable)",
      "AgentState(manifest, dispatch, signal, entry): the resolved form of an agent-adapter object entry",
      "scaffold_adapter(port, src_dir, factory, default_signal, overwrite) -> (path, address)  # §3.4.4 authoring tool: emit a skeleton conforming to the factory convention",
      "wire_adapter(port, address, project_dir, default_route, default_map) -> (route, map)  # record port->adapter map entry + route state",
      "validate_adapter_conformance(address, runtime, port, emits_route, initial) -> CheckResult  # CHECK a BYO adapter via the reused resolver + validate_signals/validate_data_readiness"
    ],
    "skills": []
  },
  "reads": {
    "files": [
      "${project_dir}/.auto-maintainer/route.json (project-local override; wire_adapter also writes it)",
      "${project_dir}/.auto-maintainer/adapter-map.json (project-local override; wire_adapter also writes it)"
    ],
    "external": [
      "fsm-contracts (validate_route, StateManifest, CheckResult)",
      "tick-orchestrator (validate_signals, validate_data_readiness)",
      "agent-dispatch (is_agent_entry, validate_agent_adapter)"
    ]
  },
  "invokes": {
    "scripts": [
      "importlib.import_module on each configured script adapter module, then its factory(runtime)",
      "agent_dispatch.is_agent_entry + agent_dispatch.validate_agent_adapter on each agent-adapter object entry"
    ],
    "agents": []
  },
  "never": [
    "defines the maintainer default route or the default adapter-map",
    "defines or ships the built-in adapter factories (scheduling owns those)",
    "imports specific adapters statically (scheduling/durable-state/lifecycle-dispositions/work-intake) — resolution is dynamic by 'module:factory' string",
    "imports scheduling, or executes/dispatches an agent-adapter (execution is a later slice; adapter-wiring only resolves + validates)",
    "performs network I/O or runs AI",
    "executes the route (tick-orchestrator owns the run loop)"
  ]
}
```
