---
feature: adapter-wiring
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when the route/adapter wiring model changes incompatibly (e.g. the adapter factory convention or route.json schema reaches a breaking major version), or when a native rabbit/plugin config system subsumes it.
---

# adapter-wiring

## Purpose

The §3.4.3 mechanism that makes the framework **actually** ports-and-adapters at
runtime: load a declarative **`route.json`** + a **`port → adapter` map** (project
config), resolve each routed port to its adapter, **validate the wiring at load**,
and hand the orchestrator a ready `(route, states)` pair. This is what lets a
project **insert / reorder / swap adapter states by editing data, not code** — the
foundation for bring-your-own adapters.

Today `scheduling/run_tick` hardcodes the route + states; this feature replaces
that with data-driven loading, fulfilling DESIGN §0's ports-and-adapters promise.

> Design references: §2.4 (ports-and-adapters via script contracts), §3.4.1
> (route.json schema — owned by fsm-contracts), §3.4.3 (override + routing
> mechanism), §3.10.2 (project-local config: port→adapter wiring via
> `CLAUDE_PROJECT_DIR`). Tool-tier: **script** (deterministic loader/resolver/
> validator) — spec-rules §1.

## Paths governed

Greenfield. Code under `.../features/adapter-wiring/src/`. It is a pure mechanism:
it does NOT define the maintainer's default route or the built-in adapters
(scheduling supplies those); it loads/resolves/validates whatever paths it is
given.

## Adapter-map entry kinds

An adapter-map value is EITHER a **script factory address** (a string) OR an
**agent-adapter object** (a dict). Both resolve to a uniform `(manifest, second)`
pair the orchestrator consumes; only the `second` element differs.

### Script entry (`"module:factory"`, the BYO contract)

An adapter is addressed in the map as **`"module:factory"`**, where `factory` is a
callable:

```
factory(runtime) -> (StateManifest, run_callable)
```

and `run_callable` has the fsm-contracts signature `run(TickContext) ->
StateResult`. `runtime` carries the resolved runtime dir + any injected config a
factory needs (e.g. PULL's issue source). **This factory signature is the entire
contract a third-party adapter implements** — write a `factory`, point the map at
it. Core anchors (`GUARD/DRAIN/PERSIST/EXIT`) are also addressed this way but are
**fixed** (the validator enforces their anchor invariants; they are not
project-overridable per §1.1). A script entry resolves to `(manifest,
run_callable)`.

### Agent entry (the agent-adapter object, DESIGN §2.8 / §3.4.6)

An adapter-map value may instead be an **agent-adapter object** — a dict whose
schema is owned by the `agent-dispatch` feature:

```json
{
  "kind": "agent",
  "manifest": {"reads": [...], "writes": [...], "emits": [...]},
  "dispatch": [{"subagent_type": "...", "inputs": [...],
                "cardinality": "once" | {"per_item": "<path>"},
                "writes": "<slot>", "task": "..."}],
  "signal": {"rule": "nonempty_else_empty" | "blocked_if_any" | "always_ok"}
}
```

adapter-wiring CONSUMES `agent-dispatch` unchanged: it classifies an entry with
`agent_dispatch.is_agent_entry(entry)` and deep-validates it with
`agent_dispatch.validate_agent_adapter(entry)`. A malformed agent entry (bad or
missing manifest, empty dispatch, bad cardinality, bad signal rule) is a
**locatable `WiringError` naming the port** (the underlying `ValueError` from
agent-dispatch is re-raised as a `WiringError`). An agent entry resolves to
`(manifest, AgentState)`.

Agent entries are **resolved and validated at LOAD here, but NOT executed** — no
`Agent` dispatch happens in adapter-wiring; execution is a later slice. adapter-
wiring does NOT import scheduling.

### The resolved `AgentState`

`AgentState` is the resolved form of an agent entry — a small record carrying
`manifest`, `dispatch`, `signal`, and the raw `entry`. The executor (a later
slice) consumes it to build + dispatch invocation envelopes. Because
`resolve_states` returns `(manifest, second)` uniformly — `second` is a
`run_callable` for a script state or an `AgentState` for an agent state —
`validate_wiring` / `build_loop` are unchanged: they operate only on the
`manifest` (the first element), which both kinds populate.

## Public surface

1. **`load_route(default_route, project_dir) -> route`** — return the project-local
   `${project_dir}/.auto-maintainer/route.json` if present, else `default_route`
   (supplied by the caller). Validate the loaded object against fsm-contracts'
   `route.json` schema (`validate_route`); a malformed route is a locatable error.
2. **`load_adapter_map(default_map, project_dir) -> map`** — same override logic for
   the `port → adapter` map. Each value is EITHER a `str` (script factory address)
   OR a `dict` (agent-adapter object); any other type is a locatable `WiringError`
   naming the port. The agent dict is NOT deep-validated here — that is
   `resolve_states`' job via agent-dispatch.
3. **`resolve_states(route, adapter_map, runtime) -> states`** — for each state in
   the route: a **string** entry imports `module`, gets `factory`, calls
   `factory(runtime)` → `(manifest, run)`; an **agent** entry
   (`agent_dispatch.is_agent_entry` is True) is validated via
   `agent_dispatch.validate_agent_adapter` and resolved to
   `(manifest, AgentState)`, where the manifest mirrors the entry's
   `manifest.{reads,writes,emits}`. Build the `states` map the orchestrator
   consumes: each value is uniformly `(manifest, second)`. An unknown port (no map
   entry), unimportable module, missing factory, or malformed agent entry is a
   **locatable error** naming the port (determinism, spec-rules §1).
4. **`validate_wiring(route, manifests, start, initial) -> CheckResult`** — run, at
   LOAD time, `tick-orchestrator`'s `validate_signals` + `validate_data_readiness`
   over the resolved manifests, plus the anchor invariants (entry is GUARD;
   PERSIST precedes EXIT; EXIT terminal route present). A bad route/wiring fails
   **before any tick runs**.
5. **`build_loop(default_route, default_map, runtime, start, initial) -> (route, states)`**
   — convenience: load + resolve + validate, returning exactly what
   `tick_orchestrator.run(route, states, ctx, vocab, start)` needs.

## Determinism & testability

Pure file reads + `importlib` + validation; no network, no AI. Everything is
injectable (default route/map dicts, project dir, runtime) so tests drive it with
fixtures and stub factory modules — no plugin/filesystem assumptions. Failures are
locatable to the port/module that is wrong.

## Current behaviour

All five public-surface functions are implemented. Agent entries are resolved
and validated at load but NOT executed (execution is a later slice).

## Known gaps / deferred

- The convenience **adapter scaffold/authoring tool** (§3.4.4, #52) — v2; this
  feature is the mechanism the tool will sit on.
- `userConfig` prompts (token/mode/budget, §3.10.1) — config feature.
- Hot-reload of route/map mid-run; multiple named routes — later.
- The **default route + default adapter-map + built-in adapter factories** are
  NOT here — they belong to `scheduling` (the loop's default spine); this feature
  loads/validates whatever it is given.

## Interfaces (composition)

- Consumes `fsm-contracts` (route.json schema, `validate_route`, `StateManifest`),
  `tick-orchestrator` (`validate_signals`, `validate_data_readiness`), and
  `agent-dispatch` (`is_agent_entry`, `validate_agent_adapter`) — all UNCHANGED.
- Consumed by `scheduling`: `run_tick` calls `build_loop(...)` (passing
  scheduling's default route/map + the resolved runtime) instead of hardcoding,
  then feeds the result to `tick_orchestrator.run`.
- Does NOT import scheduling/durable-state/lifecycle-dispositions/work-intake
  directly — it resolves adapters dynamically by the map's `module:factory`
  strings, so it stays a generic mechanism with no built-in-adapter coupling.
