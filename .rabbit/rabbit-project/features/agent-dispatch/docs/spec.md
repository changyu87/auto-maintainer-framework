---
feature: agent-dispatch
version: 0.2.0
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
      "writes": "<target slot>",
      "output_schema": <optional schema dict or concrete example shape> }
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
- `output_schema` is **optional** on each dispatch entry. It is a JSON value —
  either a JSON-schema-style dict (e.g. `{"type": "object"}`) or a concrete
  example shape (e.g. `[{"id": "..."}]`). When present it is carried verbatim
  into the envelope's `output_contract.schema` and embedded in the rendered
  prompt so a protocol-naive subagent sees the exact shape to produce. When
  absent, the envelope falls back to a coarse `{"type": ...}` schema.

## Public surface (deterministic functions)

- `AGENT_ADAPTER_SCHEMA_VERSION` — the schema version string.
- `is_agent_entry(entry) -> bool` — a string adapter-map entry is a script
  factory address; a dict with `kind == "agent"` is an agent-adapter.
- `validate_agent_adapter(entry)` — well-formedness: manifest present with
  reads/writes/emits; ≥1 dispatch entry; each entry has subagent_type + inputs +
  cardinality + writes; cardinality and signal rule are in the closed vocab; an
  optional `output_schema` (any JSON value) is accepted, its absence is valid.
  Raises a clear error on violation (deterministic, locatable).
- `build_envelopes(adapter, slot_values, tick_context, state, output_dir) ->
  [envelope]` — produce the invocation envelope(s) the executor will dispatch.
  `once` → one envelope per dispatch entry; `{per_item: path}` → one envelope per
  element of that collection, each carrying its `item`. Each envelope gets a
  deterministic, unique `output_path` =
  `os.path.join(output_dir, f"{state}-{dispatch_index}-{item_index}.json")`
  (`item_index` is 0 for `once`). Envelope shape:
  `{ state, task, inputs, item?, output_contract: {slot, schema, output_path}, context: {tick_id, mode} }`,
  where `schema` is the entry's `output_schema` when present, else a coarse
  `{"type": ...}` fallback. The `output_path` is a computed string only; the file
  is written by the subagent and read by the executor — never by this library.
- `render(envelope) -> str` — deterministic **structured-markdown** prompt
  (DESIGN §3.4.6): `inputs` rendered as a readable **derivative view** (generic
  slot→markdown; free-text fields fenced/block-quoted to preserve boundaries) —
  **no raw JSON for inputs**; the **`## Handoff`** section is the SELF-CONTAINED
  contract — it embeds the actual output schema (pretty-printed JSON of the
  shape), instructs the subagent to **write a single JSON value matching the
  schema to `output_contract.output_path` using its file-writing tool**, then to
  **reply with ONLY a one-line acknowledgement and NOT include the JSON in the
  reply**. The rendered prompt is thus the complete handoff contract; subagents
  stay protocol-free.
- `validate_output(file_content, schema) -> (ok, parsed | error)` — parse the
  JSON content the subagent wrote to its output file (tolerating code fences),
  check it structurally conforms to `schema` (at least the declared top-level
  type — when `schema` is a rich example, a list means array and a dict means
  object), return the parsed value or a locatable error so the executor can
  **re-dispatch on mismatch**.
- `collect_outputs(adapter_entry, outputs) -> slot_value` — assemble dispatch
  outputs into the target slot value (`once` → the value itself; `per_item` →
  the list of element outputs).
- `compute_signal(rule, written_slot_value) -> signal` — apply a closed-vocab
  signal rule deterministically. The model never selects control flow.

## Invariants

- Deterministic and effect-free: no `Agent` dispatch, no model call, no network,
  no filesystem I/O, no wall clock. Pure functions of their arguments (the
  `tick_id` / `mode` in `context` are passed in, not read). `output_path` is a
  computed string (via `os.path.join`); the file itself is written by the
  subagent and read by the executor, never by this library.
- Machine-first split (DESIGN §1, §3.4.6): inputs render to a **derivative view**;
  the self-contained `## Handoff` section embeds the **schema** the output must
  match and names the **output_path** the subagent writes — the output file is
  the canonical machine-first artifact the next state consumes.
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
