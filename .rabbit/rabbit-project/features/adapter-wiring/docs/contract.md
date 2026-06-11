---
feature: adapter-wiring
version: 0.1.0
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
      "load_adapter_map(default_map, project_dir) -> map",
      "resolve_states(route, adapter_map, runtime) -> states",
      "validate_wiring(route, manifests, start, initial) -> CheckResult",
      "build_loop(default_route, default_map, runtime, start, initial) -> (route, states)",
      "the adapter factory convention: 'module:factory', factory(runtime) -> (StateManifest, run_callable)"
    ],
    "skills": []
  },
  "reads": {
    "files": [
      "${project_dir}/.auto-maintainer/route.json (project-local override)",
      "${project_dir}/.auto-maintainer/adapter-map.json (project-local override)"
    ],
    "external": [
      "fsm-contracts (validate_route, StateManifest, CheckResult)",
      "tick-orchestrator (validate_signals, validate_data_readiness)"
    ]
  },
  "invokes": {
    "scripts": [
      "importlib.import_module on each configured adapter module, then its factory(runtime)"
    ],
    "agents": []
  },
  "never": [
    "defines the maintainer default route or the default adapter-map",
    "defines or ships the built-in adapter factories (scheduling owns those)",
    "imports specific adapters statically (scheduling/durable-state/lifecycle-dispositions/work-intake) — resolution is dynamic by 'module:factory' string",
    "performs network I/O or runs AI",
    "executes the route (tick-orchestrator owns the run loop)"
  ]
}
```
