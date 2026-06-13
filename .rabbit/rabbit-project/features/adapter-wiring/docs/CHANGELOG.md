# Changelog — adapter-wiring

All notable changes to this feature are recorded here. Versions follow the
spec/contract `version:` frontmatter.

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
