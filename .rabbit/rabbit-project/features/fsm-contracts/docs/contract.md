---
feature: fsm-contracts
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the tick-FSM contract schema reaches a breaking major version (see feature.json / spec.md).
---

# fsm-contracts — Contract

```json
{
  "provides": {
    "files": [
      "TickContext slot schema",
      "StateResult envelope schema",
      "closed signal vocabulary schema",
      "per-state read/write/emit manifest schema",
      "route.json transition-table schema"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {"scripts": [], "agents": []},
  "never": [
    "contains transition-resolution or run-loop logic (owned by tick-orchestrator)",
    "defines anchor-invariant rules (owned by the lifecycle core)",
    "hard-codes maintainer-domain slot payloads (owned by consumer state features)"
  ]
}
```
