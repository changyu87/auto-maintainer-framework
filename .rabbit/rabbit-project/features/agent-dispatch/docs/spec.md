---
feature: agent-dispatch
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the agent-adapter schema or invocation-envelope reaches a breaking major version, or when subagent dispatch moves to a transport other than the in-session Agent tool.
---

# agent-dispatch

## Purpose

The **deterministic helper library for the agent-adapter mechanism** (DESIGN
§2.8, §3.4.6). It owns the agent-adapter schema and every *deterministic* step
around an in-session subagent dispatch — parse/validate the schema, build the
invocation envelope, render it to a prompt, validate the returned output against
the target slot schema, collect outputs, and compute the route signal.

It does **NOT** dispatch anything: issuing the `Agent` call is the session's job
(the executor, DESIGN §2.8 — a later slice). This feature is pure, deterministic,
script-tier; it is the plumbing both `adapter-wiring` (schema validation) and
`scheduling` (the run_tick yield/resume seam + executor) consume.

## The agent-adapter schema (this feature owns it)

An adapter-map entry is either a **factory-address string** (a script-adapter,
unchanged) or an **agent-adapter object** (DESIGN §3.4.6):

```json
{
  "kind": "agent",
  "manifest": { "reads": ["..."], "writes": ["..."], "emits": ["OK", "..."] },
  "dispatch": [
    { "subagent_type": "<registered subagent>",
      "task": "<per-dispatch instructions>",
      "inputs": ["<slot>", "..."],
      "cardinality": "once" | { "per_item": "<dotted path into a read slot>" },
      "writes": "<target slot>" }
  ],
  "signal": { "rule": "<closed-vocab rule>" }
}
```

- `dispatch` is a list whose entries run **in parallel** (the executor's concern);
  a single entry = a single subagent.
- `cardinality`: `once` (one dispatch over the whole input) or
  `{ per_item: <path> }` (fan-out — one dispatch per element of the named
  collection, e.g. `execution_plan.ordered`).
- parallel/`per_item` outputs land **either** as distinct slots (each entry's
  `writes`) **or**, for a `per_item` entry, **collected into one list slot**.
- `signal.rule` is from a **closed vocabulary** (v1): `nonempty_else_empty`
  (OK if the written slot is non-empty else EMPTY), `blocked_if_any` (BLOCKED if
  any written element carries a blocked status, else OK), `always_ok`.

## Public surface (deterministic functions)

- `AGENT_ADAPTER_SCHEMA_VERSION` — the schema version string.
- `is_agent_entry(entry) -> bool` — a string adapter-map entry is a script
  factory address; a dict with `kind == "agent"` is an agent-adapter.
- `validate_agent_adapter(entry)` — well-formedness: manifest present with
  reads/writes/emits; ≥1 dispatch entry; each entry has subagent_type + inputs +
  cardinality + writes; cardinality and signal rule are in the closed vocab.
  Raises a clear error on violation (deterministic, locatable).
- `build_envelopes(adapter, slot_values, tick_context) -> [envelope]` — produce
  the invocation envelope(s) the executor will dispatch. `once` → one envelope per
  dispatch entry; `{per_item: path}` → one envelope per element of that collection,
  each carrying its `item`. Envelope shape:
  `{ state, task, inputs, item?, output_contract: {slot, schema_ref}, context: {tick_id, mode} }`.
- `render(envelope) -> str` — deterministic **structured-markdown** prompt
  (DESIGN §3.4.6): `inputs` rendered as a readable **derivative view** (generic
  slot→markdown; free-text fields fenced/block-quoted to preserve boundaries) —
  **no raw JSON for inputs**; the **`## Return`** section states the target slot
  schema, because the output is the machine-first artifact the next state
  consumes.
- `validate_output(returned_text, slot_schema) -> (ok, parsed | error)` — parse
  the subagent's returned text (tolerating code fences), check it structurally
  conforms to the slot schema (at least the declared top-level type), return the
  parsed value or a locatable error so the executor can **re-dispatch on
  mismatch**.
- `collect_outputs(adapter_entry, outputs) -> slot_value` — assemble dispatch
  outputs into the target slot value (`once` → the value itself; `per_item` →
  the list of element outputs).
- `compute_signal(rule, written_slot_value) -> signal` — apply a closed-vocab
  signal rule deterministically. The model never selects control flow.

## Invariants

- Deterministic and effect-free: no `Agent` dispatch, no model call, no network,
  no filesystem, no wall clock. Pure functions of their arguments (the `tick_id`
  / `mode` in `context` are passed in, not read).
- Machine-first split (DESIGN §1, §3.4.6): inputs render to a **derivative view**;
  the output contract names a **schema** the return must match.
- Closed vocabularies for `cardinality` and `signal.rule`; unknown values raise.
- Bounded scope: owns the schema + these helpers only; it neither loads the
  adapter-map (adapter-wiring) nor walks the route nor dispatches (scheduling /
  executor).

## Deferred (NOT in this slice)

- The **executor** that actually issues the `Agent` dispatch and drives the
  yield/resume seam (DESIGN §2.8) — `scheduling` + the tick skill, later slices.
- `adapter-wiring` recognizing agent entries in the adapter-map — next slice
  (it will consume `is_agent_entry` / `validate_agent_adapter` here).
- Per-feature deep output validation beyond structural/type conformance (the
  producing feature's concern; `validate_output` takes the schema as an argument).
- Multi-step in-state pipelines / ensembles (use route-chaining instead, §3.4.6).
