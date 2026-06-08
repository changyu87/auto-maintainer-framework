---
feature: phase-ports
version: 1.0.0
owner: rabbit-workflow team
template_version: 2.0.0
status: active
---

# phase-ports

## Purpose

Define and enforce typed adapter-port contracts for the seven swappable phases of the autonomous TICK pipeline (PULL, TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, INTEGRATE, CLEANUP), and provide the mechanism by which a project wires custom scripts into those ports.

## Paths governed

- `rabbit-project/features/phase-ports/scripts/**`
- `rabbit-project/features/phase-ports/schemas/**`

## Public surface

No code exists yet. The intended public surface, drawn from the design context, is:

**Port type contracts (one per adapter phase)**

| Port name   | Input signature              | Output type        |
|-------------|------------------------------|--------------------|
| `PULL`      | `()`                         | `WorkItem[]`       |
| `TRIAGE`    | `WorkItem[]`                 | `WorkOrder[]`      |
| `PRIORITIZE`| `WorkOrder[]`                | `ExecutionPlan`    |
| `IMPLEMENT` | `(WorkOrder, Workspace)`     | `Handoff`          |
| `VERIFY`    | `Handoff`                    | `Verdict`          |
| `INTEGRATE` | `Verdict[]`                  | `IntegrationResult`|
| `CLEANUP`   | `IntegrationResult`          | `()`               |

**Project-config override mechanism**

A per-project configuration surface (file format and schema TBD) that maps each port name to an executable script path. When a port has no override, the framework falls back to a default built-in adapter.

**Shared domain types**

`WorkItem`, `WorkOrder`, `ExecutionPlan`, `Handoff`, `Verdict`, `IntegrationResult` — structural types that flow across port boundaries and form the lingua franca of the TICK pipeline.

## Current behaviour

(TBD — feature not yet implemented)

The following bullets describe the intended behaviour once implemented:

- The TICK pipeline runs in the fixed sequence: GUARD -> DRAIN -> PULL -> TRIAGE -> PRIORITIZE -> IMPLEMENT -> VERIFY -> INTEGRATE -> CLEANUP -> PERSIST -> EXIT. Only the seven adapter phases are subject to port overrides; the four core phases (GUARD, DRAIN, PERSIST, EXIT) are fixed and cannot be replaced.
- At pipeline startup, phase-ports reads the project configuration to resolve each of the seven port names to a concrete implementation. Unresolved ports use the built-in default adapter for that phase.
- Each port is invoked with exactly the input type declared in its contract and must return exactly the declared output type. Type mismatches at a port boundary are a hard error that halts the pipeline.
- A custom adapter wired to a port is an executable script. The framework invokes it as a subprocess, passing input via stdin (serialized to the agreed schema) and reading output from stdout.
- Input and output serialization uses a declared schema version; the port contract carries that version so mismatches can be detected at wire-up time rather than at invocation time.
- The `IMPLEMENT` port receives both a `WorkOrder` (what to do) and a `Workspace` (where to do it), giving adapters access to isolated working state without coupling them to a global mutable environment.
- The `INTEGRATE` port accepts `Verdict[]` (a list), allowing a batch of verified handoffs to be integrated together; adapters may choose to process them sequentially or in parallel.
- The `CLEANUP` port's output is `()` (unit); it is the only port with no meaningful return value, so its contract enforces that downstream phases do not depend on its output.
- Port contracts are versioned; a project config that references an incompatible contract version must fail at validation time with a clear error identifying the mismatched port.

## Known gaps

- No schema files exist. The structural types (`WorkItem`, `WorkOrder`, `ExecutionPlan`, `Handoff`, `Verdict`, `IntegrationResult`) are named but not yet defined. Serialization format (JSON, MessagePack, other) is not yet chosen.
- The project-config override format (file name, file format, key structure) is unspecified.
- Default built-in adapters for each port are referenced but not scoped — it is unclear whether they live inside this feature or in a separate `phase-defaults` feature.
- No validation layer exists to enforce port contract conformance at wire-up time or at invocation time.
- The `Workspace` type passed to `IMPLEMENT` is unnamed and unspecified beyond the type token; its fields (scratch directory, resource limits, credential access) are TBD.
- Error propagation across port boundaries (what happens when an adapter returns a malformed output, times out, or exits non-zero) is not yet defined.
- Versioning strategy for individual port contracts (independent per-port versions vs. a single bundle version) has not been decided.

## Resolved decisions (v1)

These resolve the original open questions with conservative v1 defaults. They constrain the first implementation; later versions may revisit any of them under the normal contract-versioning process.

1. **Serialization format.** JSON. Each port exchanges JSON on stdin/stdout. The shared domain types are defined as JSON Schema documents under `schemas/`, each carrying a top-level `schema_version`.
2. **Default adapter ownership.** Default built-in adapters do NOT live in phase-ports. phase-ports owns only the port contracts, the shared domain-type schemas, and the override-resolution logic. Each adapter phase's default implementation is owned by its own sibling feature (`triage-adapter`, `implement-adapter`, `verify-integrate-adapter`).
3. **Config location and format.** Port overrides live in a single JSON file `ports.json` at the project config root, with the shape `{"schema_version": "1.0.0", "ports": {"PULL": "<script-path>", ...}}`. A port absent from the map resolves to its default adapter (owned by the sibling feature, not phase-ports).
4. **Workspace definition (v1).** `Workspace` exposes exactly two fields in v1: `scratch_dir` (absolute path to an isolated working directory) and `branch` (the git branch name for the dispatch). Credential and quota fields are deferred.
5. **Error semantics.** Conservative: a port adapter failure (non-zero exit or schema-invalid output) is a hard error that aborts the current TICK run. Per-item skip-and-continue is deferred to v2.
6. **Contract versioning granularity.** Single bundle version for all seven ports in v1, carried as `schema_version` on the contract bundle. Per-port independent versioning is deferred.
7. **Type identity.** phase-ports owns the shared domain types (`WorkItem`, `WorkOrder`, `ExecutionPlan`, `Handoff`, `Verdict`, `IntegrationResult`). No separate `domain-types` feature.

## v1 implementation surface

- `schemas/domain-types.json` — JSON Schema defining the six shared domain types, with a top-level `schema_version`.
- `schemas/port-contracts.json` — the seven port contracts (name, input type, output type) referencing the domain types, with a bundle `schema_version`.
- `scripts/resolve_ports.py` — reads `ports.json`, validates its `schema_version` against the contract bundle, and resolves each of the seven port names to either an override script path or the sentinel `default`. A `schema_version` mismatch or an unknown port name is a hard error naming the offending port.
