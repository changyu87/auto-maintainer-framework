---
feature: agent-dispatch
version: 0.3.1
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
      "output_example": <optional concrete example value the subagent mimics> }
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
- `output_example` is **optional** on each dispatch entry. It is a **concrete
  example value** — a sample valid output the subagent copies and adapts (e.g.
  a bare list `[{"id": "..."}]` for an array slot, or an object `{"id": "..."}`
  for an object slot), NOT a JSON-Schema descriptor. When present it is carried
  verbatim into the envelope's `output_contract` (internal key `schema`,
  unchanged so `run_tick` / `scheduling` are untouched) and embedded in the
  rendered prompt, framed as an example to mimic, so a protocol-naive subagent
  copies the exact shape. When absent, the envelope falls back to a coarse
  `{"type": ...}` value.
- `output_schema` is a **DEPRECATED back-compat alias** for `output_example`
  (#119). When both are present `output_example` wins; when only `output_schema`
  is present it is read as the example. Existing adapter-maps using
  `output_schema` (with a concrete example value) keep working. The
  deprecation criterion: removed once all adapter-maps migrate to
  `output_example`.
- **Descriptor guard** (#119): `validate_agent_adapter` REJECTS an
  `output_example` (or the deprecated `output_schema` alias) that looks like a
  JSON-Schema descriptor — a dict whose `"type"` is one of `object` / `array` /
  `string` / `number` / `integer` / `boolean` / `null` AND which also has an
  `"items"` or `"properties"` key. The earlier bug: a descriptor
  `{"type": "array", "items": {...}}` was embedded, and the protocol-naive
  subagent wrote the descriptor verbatim instead of a bare array. Concrete
  examples are mimicked reliably; descriptor notation is not.

## Public surface (deterministic functions)

- `AGENT_ADAPTER_SCHEMA_VERSION` — the schema version string.
- `is_agent_entry(entry) -> bool` — a string adapter-map entry is a script
  factory address; a dict with `kind == "agent"` is an agent-adapter.
- `validate_agent_adapter(entry)` — well-formedness: manifest present with
  reads/writes/emits; ≥1 dispatch entry; each entry has subagent_type + inputs +
  cardinality + writes; cardinality and signal rule are in the closed vocab; an
  optional `output_example` (or the deprecated `output_schema` alias) holding a
  concrete example value is accepted, its absence is valid. It REJECTS an
  example authored as a JSON-Schema descriptor (the #119 descriptor guard).
  Raises a clear error on violation (deterministic, locatable).
- `build_envelopes(adapter, slot_values, tick_context, state, output_dir) ->
  [envelope]` — produce the invocation envelope(s) the executor will dispatch.
  `once` → one envelope per dispatch entry; `{per_item: path}` → one envelope per
  element of that collection, each carrying its `item`. Each envelope gets a
  deterministic, unique `output_path` =
  `os.path.join(output_dir, f"{state}-{dispatch_index}-{item_index}.json")`
  (`item_index` is 0 for `once`). Envelope shape:
  `{ state, task, inputs, item?, output_contract: {slot, schema, output_path}, context: {tick_id, mode} }`,
  where the internal `schema` key (name unchanged for `run_tick` / `scheduling`)
  carries the entry's `output_example` (or the deprecated `output_schema` alias)
  when present, else a coarse `{"type": ...}` fallback. The `output_path` is a
  computed string only; the file is written by the subagent and read by the
  executor — never by this library.
- `render(envelope) -> str` — deterministic **structured-markdown** prompt
  (DESIGN §3.4.6): `inputs` rendered as a readable **derivative view** (generic
  slot→markdown; free-text fields fenced/block-quoted to preserve boundaries) —
  **no raw JSON for inputs**. Free-text/multiline fences use a
  **dynamic-length fence** (#126): scan the content for the longest run of
  consecutive backticks and wrap with `(longest_run + 1)` backticks, minimum 3,
  so content that itself contains a code fence (e.g. a GitHub issue `body` with
  ```` ``` ````) cannot terminate the wrapper early; the opening and closing
  fences use the same (longer) length. The common no-backtick case is a normal
  3-backtick fence. The **`## Handoff`** section is the SELF-CONTAINED
  contract — it embeds the concrete output **example** (pretty-printed JSON),
  framed as a value to **mimic** ("produce a JSON value shaped EXACTLY like this
  example — copy its structure, replace the placeholder values"), never called a
  "schema" (#119); it instructs the subagent to **write that JSON to
  `output_contract.output_path` using its file-writing tool**, then to **reply
  with ONLY a one-line acknowledgement and NOT include the JSON in the reply**.
  The rendered prompt is thus the complete handoff contract; subagents stay
  protocol-free.
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
  the self-contained `## Handoff` section embeds the concrete output **example**
  the subagent mimics and names the **output_path** the subagent writes — the
  output file is the canonical machine-first artifact the next state consumes.
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
