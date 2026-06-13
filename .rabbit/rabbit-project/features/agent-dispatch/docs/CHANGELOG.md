# Changelog — agent-dispatch

All notable changes to the agent-adapter schema, the invocation envelope, and
the deterministic helper surface are recorded here. Versions follow the
`version` field in `spec.md` / `contract.md` / `feature.json`.

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
