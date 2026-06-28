---
feature: prioritize
version: 0.4.0
owner: changyu87
deprecation_criterion: Superseded when ordering ceases to be deterministic (e.g. a model-backed prioritizer adapter replaces the default), or when the ExecutionPlan schema reaches a breaking major version. See spec.md / feature.json.
---

# prioritize — Contract

```json
{
  "provides": {
    "files": [
      "ExecutionPlan slot schema (versioned, machine-first: ordered + status)",
      "PRIORITIZE state: run(TickContext) -> StateResult, reads work_orders, writes execution_plan, emits OK|EMPTY (deterministic identity ordering + pending status backfill)",
      "same-feature serialization (#214, #257, #258): at most one accepted order per target feature per tick (FIFO-first wins, the rest deferred); cross-feature orders stay parallel. Target feature(s) read from the order's authoritative target_feature field (TRIAGE-stamped, #258); when that field is absent PRIORITIZE falls back to re-deriving from feature:/component: prefixed labels, a Component: body line, and a conventional title prefix (name: / type(scope):, for label-less issues) only (never generic labels, never a bare conventional-commit type, never the 'and' connector)"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "performs parallel grouping (DESIGN §1.1 [v2])",
    "re-orders by severity/priority (no such key on WorkOrder yet; identity/FIFO order only)",
    "writes order status to the tracker (in-slot status backfill only; the outward write is deferred to safety-governance)",
    "calls a model, the wall clock, randomness, the network, or the filesystem (PRIORITIZE is deterministic and effect-free)",
    "edits files in other features"
  ]
}
```
