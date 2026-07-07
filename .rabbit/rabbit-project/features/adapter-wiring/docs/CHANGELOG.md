# Changelog — adapter-wiring

All notable changes to this feature are recorded here. Versions follow the
spec/contract `version:` frontmatter.

## 0.5.0

- Enforce the DESIGN §2.2 bounded-parallel safety invariant at wiring
  validation (#335): an ACTING (a dispatch declaring an `effect`) `per_item`
  dispatch MUST declare `isolation == "worktree"`. A violating agent entry is a
  locatable `WiringError` naming the port, raised during `resolve_states` (via
  the new `_validate_acting_isolation` guard in `_resolve_agent`) — so
  un-isolated parallel acting is rejected at LOAD, before any tick runs. This
  was previously only documented; it is now safe-by-construction and closes the
  gap that allowed unprotected parallel dispatch in the dogfood.
- Additive + backward compatible: a `once` dispatch, or a non-acting (no
  `effect`) dispatch, is unconstrained; conforming acting-per_item entries (which
  already default to `isolation: "worktree"`) resolve unchanged.

## 0.4.0

- Public surface change (additive): `build_loop` gains an optional `migrate=None`
  kwarg — a pure `dict -> dict` callable run on the loaded adapter-map AFTER
  `load_adapter_map` and BEFORE `resolve_states`/`validate_wiring`, so a caller
  (e.g. scheduling) can self-heal stale adapter-map entries on load. The migrated
  map feeds resolve + validate exactly like a loaded map, so a malformed
  transform surfaces as a `WiringError` (never a silent pass).
- adapter-wiring stays template-agnostic: it only invokes the supplied callable
  and carries no knowledge of what it rewrites (no template/port-template
  coupling — that belongs to scheduling).
- Backward compatible: `migrate=None` (the default) is byte-for-byte unchanged
  behaviour; the loaded map is resolved as-is.

## 0.3.0

- New §3.4.4 adapter authoring/scaffold tool (additive, #52): `scaffold_adapter`
  emits a skeleton module conforming to the factory convention (`make(runtime)
  -> (StateManifest, run)` with `run(TickContext) -> StateResult`);
  `wire_adapter` records the `port -> adapter` map entry + adds the route state
  in the project-local override files; `validate_adapter_conformance` resolves
  the new adapter via the existing resolver and runs the SAME load-time checks
  (factory/manifest shape + `validate_signals` / `validate_data_readiness`) so
  BYO-adapter is a CHECKED operation.
- The tool reuses the existing resolver/validators and the fsm-contracts factory
  convention UNCHANGED; it adds no new contract surface beyond the three
  functions and writes only project-local `.auto-maintainer/` override files.
- Backward compatible: the mechanism's five functions are untouched.

## 0.2.0

- Public surface change (additive): `load_adapter_map` now accepts adapter-map
  values that are EITHER a `"module:factory"` string (script, unchanged) OR an
  agent-adapter object (a dict); any other type is a locatable `WiringError`
  naming the port.
- `resolve_states` now recognizes agent-adapter object entries (DESIGN
  §2.8/§3.4.6): an agent entry is deep-validated via the new `agent-dispatch`
  dependency (`is_agent_entry` + `validate_agent_adapter`) and resolved to
  `(manifest, AgentState)`. Script entries are unchanged: `(manifest, run)`.
- New resolved type `AgentState` (dataclass) carrying `manifest`, `dispatch`,
  `signal`, and the raw `entry`. The executor (a later slice) consumes it; agent
  entries are resolved + validated here but NOT executed.
- New dependency: consumes `agent-dispatch` UNCHANGED. `validate_wiring` /
  `build_loop` are unchanged — both entry kinds populate the manifests dict, so
  agent manifests participate in signal + data-readiness validation.
- Backward compatible: every existing script-only route resolves exactly as
  before.

## 0.1.0

- Initial implementation: `load_route`, `load_adapter_map`, `resolve_states`,
  `validate_wiring`, `build_loop` over the `"module:factory"` adapter factory
  convention. Loads project-local `route.json` / `adapter-map.json` overrides,
  resolves each routed port to its adapter, and validates the wiring at LOAD.
