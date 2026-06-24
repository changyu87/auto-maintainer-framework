# Changelog — agent-dispatch

All notable changes to the agent-adapter schema, the invocation envelope, and
the deterministic helper surface are recorded here. Versions follow the
`version` field in `spec.md` / `contract.md` / `feature.json`.

## 0.3.2

Fix empty-schema misclassification in `validate_output`. The live bug:
`_expected_type({})` returned `"object"`, so an empty `schema` dict `{}` — the
`_SLOT_SCHEMAS.get(writes, {})` "no schema, accept anything" sentinel — was
read as `{"type": "object"}`. `validate_output` then spuriously rejected a
valid top-level list with `expected top-level type object, got list`.

- `_expected_type`: an **empty** dict `{}` now returns `None`, imposing no
  top-level type check (the content is accepted as-is). A **non-empty** dict
  without a `"type"` key still derives `"object"`; `{"type": ...}` still
  returns that type; a list still derives `"array"` — all unchanged.
- No other behavior changes: `validate_agent_adapter`, `build_envelopes`,
  `render` (scheduling, fence-stripping, JSON parsing), `collect_outputs`,
  and `compute_signal` are untouched.
- Library remains deterministic and effect-free.

## 0.3.1

Fix free-text fence collision in `render` (#126). The live bug: `render`
wrapped free-text/multiline input fields (e.g. a GitHub issue `body`) in a
fixed 3-backtick fence; when the content itself contained a ```` ``` ````
code fence, the inner fence terminated the wrapper early and the body bled
into the surrounding markdown, corrupting the prompt and pulling the
following `## Handoff` header out of place.

- `render` (`_render_scalar`): wrap free-text/multiline content in a
  CommonMark-correct **dynamic-length fence** — scan the content for the
  longest run of consecutive backticks and use `(longest_run + 1)` backticks,
  minimum 3, for both the opening and closing fence. Content with ```` ``` ````
  is wrapped in a 4-backtick fence; content with a 4-run is wrapped in 5; the
  common no-backtick case keeps a plain 3-backtick fence. The fence length is
  a pure function of the content, so render stays deterministic.
- No other behavior changes: envelope shape, `output_example` handling,
  `validate_*`, `build_envelopes`, and the `## Handoff` block are unchanged.
- Library remains deterministic and effect-free.

## 0.3.0

Make the embedded handoff contract a concrete EXAMPLE to MIMIC, and reject a
JSON-Schema descriptor at the wiring boundary (#119). The live bug: an entry's
`output_schema` was authored as a descriptor `{"type":"array","items":{...}}`;
render embedded it and the protocol-naive subagent wrote the descriptor verbatim
instead of a bare array, failing validation. Concrete examples are mimicked
reliably; schema notation is not.

- Rename the user-facing dispatch-entry field to `output_example` (a concrete
  example value the subagent copies and adapts). `output_schema` becomes a
  DEPRECATED back-compat alias: `output_example` wins when both are present,
  `output_schema` is read when `output_example` is absent. Existing adapter-maps
  using `output_schema` (with a concrete example) keep working. The envelope's
  internal `output_contract` key name `schema` is UNCHANGED so `run_tick` /
  `scheduling` are untouched.
- `render`: reframe the `## Handoff` output block as a concrete example to mimic
  ("produce a JSON value shaped EXACTLY like this example — copy its structure,
  replace the placeholder values"). The embedded value is never called a
  "schema." Write-to-file + one-line-ack instructions unchanged.
- `validate_agent_adapter`: add a descriptor guard — reject an `output_example`
  (or the deprecated `output_schema` alias) that is a dict whose `"type"` is a
  JSON-Schema type name AND which also carries `"items"` or `"properties"`. A
  concrete example (a list, or a dict without that combination) passes.
- `validate_output`: unchanged (derives expected top-level type from the example;
  list -> array, dict -> object).
- Library remains deterministic and effect-free.

## 0.2.0

Make the rendered prompt the COMPLETE, self-contained handoff contract
(DESIGN §3.4.6): the envelope carries the embedded output schema plus a
deterministic `output_path`, and the prompt mandates write-to-file + a one-line
ack. Subagents stay protocol-free.

- `validate_agent_adapter`: accept an OPTIONAL `output_schema` field on each
  `dispatch` entry (a JSON value — a schema dict or a concrete example shape).
  Absence remains valid.
- `build_envelopes`: add a required `output_dir` parameter. Each envelope now
  carries a deterministic, unique `output_path` =
  `os.path.join(output_dir, f"{state}-{dispatch_index}-{item_index}.json")`
  (`item_index` 0 for `once`). The `output_contract` is now
  `{slot, schema, output_path}` — `schema` is the entry's `output_schema` when
  present, else a coarse `{"type": ...}` fallback. Replaces the prior
  `schema_ref`-only shape.
- `render`: replace the `## Return` section with a self-contained `## Handoff`
  section embedding the pretty-printed schema, a write-to-file instruction
  naming `output_path`, and a one-line-ack / do-not-include-the-JSON
  instruction. `## Task` and the `## Inputs` derivative view are unchanged.
- `validate_output`: now validates the JSON content of the subagent's output
  file; when passed a rich example schema it derives the expected top-level
  type (list -> array, dict -> object). Code-fence tolerance unchanged.
- Library remains deterministic and effect-free: `output_path` is a computed
  string; this library never writes the file or dispatches.

## 0.1.0

Initial slice: the deterministic helper library for the agent-adapter
mechanism. Owns the agent-adapter schema and `is_agent_entry`,
`validate_agent_adapter`, `build_envelopes`, `render`, `validate_output`,
`collect_outputs`, `compute_signal`.
