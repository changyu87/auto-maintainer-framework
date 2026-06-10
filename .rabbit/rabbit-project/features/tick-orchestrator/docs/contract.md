---
feature: tick-orchestrator
version: 0.1.0
owner: changyu87
deprecation_criterion: Retired/superseded when the declarative-route execution model is replaced (see feature.json / spec.md).
---

# tick-orchestrator — Contract

```json
{
  "provides": {
    "files": [],
    "scripts": [
      "resolve_next(route, state, signal) -> next_state",
      "run loop (route execution to terminal)",
      "structural route validators (signal-validity, data-readiness)"
    ],
    "skills": []
  },
  "reads": {
    "files": ["a project route.json", "fsm-contracts schemas"],
    "external": []
  },
  "invokes": {
    "scripts": ["each state's run(TickContext) -> StateResult (uniform contract)"],
    "agents": []
  },
  "never": [
    "names a specific concrete state in router code",
    "embeds maintainer-domain logic",
    "performs journaling, checkpointing, disposition selection, or single-writer mutex (deferred features own those)",
    "enforces anchor invariants (spine-specific; deferred to the lifecycle core)"
  ]
}
```
