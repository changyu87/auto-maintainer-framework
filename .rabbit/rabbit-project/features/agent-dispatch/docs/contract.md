---
feature: agent-dispatch
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the agent-adapter schema or invocation-envelope reaches a breaking major version, or when subagent dispatch moves to a transport other than the in-session Agent tool. See spec.md / feature.json.
---

# agent-dispatch — Contract

```json
{
  "provides": {
    "files": [
      "Agent-adapter schema (versioned) + is_agent_entry / validate_agent_adapter",
      "build_envelopes(adapter, slot_values, tick_context) -> invocation envelopes",
      "render(envelope) -> structured-markdown prompt (inputs as derivative view, output as schema)",
      "validate_output(returned_text, slot_schema) -> (ok, parsed|error) for executor re-dispatch",
      "collect_outputs(adapter_entry, outputs) -> slot value; compute_signal(rule, slot_value) -> signal"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "dispatches a subagent / calls the Agent tool (the executor's job, DESIGN §2.8)",
    "calls a model, the network, the filesystem, or the wall clock (deterministic; tick_id/mode are passed in)",
    "loads the adapter-map or route (adapter-wiring) or walks the route (scheduling)",
    "performs per-feature deep schema validation (validate_output takes the schema as an argument)",
    "edits files in other features"
  ]
}
```
