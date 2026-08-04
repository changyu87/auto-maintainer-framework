---
feature: durable-state
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the durable-state schema reaches a breaking major version (e.g. v2 adds compaction/rotation, DESIGN §3.2.5) or when the persistence layer is replaced.
---

# durable-state — Contract

```json
{
  "provides": {
    "files": [
      "DurableState: load/save a versioned JSON document, atomic temp+rename",
      "Journal: append-only record-before-act log with stable dedup_key",
      "DRAIN state: run(TickContext) -> StateResult, finishes owed work idempotently",
      "PERSIST state: run(TickContext) -> StateResult, writes durable state to disk",
      "DRAIN/PERSIST per-state manifests (reads/writes/emits)",
      "idempotency / dedup-key convention for outward effects"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {
    "files": [
      "durable state JSON file (path injected via TickContext 'state_path' slot)",
      "journal JSONL file (path injected via TickContext 'journal_path' slot)"
    ],
    "external": [
      "fsm-contracts: TickContext, StateResult, StateManifest"
    ]
  },
  "invokes": {"scripts": [], "agents": []},
  "never": [
    "edits or forks fsm-contracts (consumed only, owned by fsm-contracts)",
    "resolves transitions or runs the tick loop (owned by tick-orchestrator)",
    "owns the disposition machine / GUARD / EXIT / mutex (owned by lifecycle-dispositions)",
    "owns the route, heartbeat, or demo work (owned by scheduling)",
    "hard-codes the on-disk state/journal location (injected by the loop core)"
  ]
}
```
