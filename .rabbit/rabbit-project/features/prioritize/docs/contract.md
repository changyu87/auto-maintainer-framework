---
feature: prioritize
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when ordering ceases to be deterministic (e.g. a model-backed prioritizer adapter replaces the default), or when the ExecutionPlan schema reaches a breaking major version. See spec.md / feature.json.
---

# prioritize — Contract

```json
{
  "provides": {
    "files": [
      "ExecutionPlan slot schema (versioned, machine-first: ordered + status)",
      "PRIORITIZE state: run(TickContext) -> StateResult, reads work_orders, writes execution_plan, emits OK|EMPTY (deterministic identity ordering + pending status backfill + same-feature serialization: at most one accepted order per blast-radius feature per tick, the FIFO-first wins, surplus deferred; cross-feature orders stay parallel — #214)"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "adds an explicit `groups` surface to the plan (DESIGN §1.1 [v2]); same-feature serialization shrinks `ordered` to one-per-feature instead",
    "re-orders by severity/priority (no such key on WorkOrder yet; identity/FIFO order among the kept orders only)",
    "permanently drops a deferred same-feature order (it re-enters a later tick's plan once the head order's PR lands)",
    "writes order status to the tracker (in-slot status backfill only; the outward write is deferred to safety-governance)",
    "calls a model, the wall clock, randomness, the network, or the filesystem (PRIORITIZE is deterministic and effect-free)",
    "edits files in other features"
  ]
}
```
