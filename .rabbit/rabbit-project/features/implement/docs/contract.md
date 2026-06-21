---
feature: implement
version: 0.3.0
owner: changyu87
deprecation_criterion: Superseded when the model-backed implement-then-PR doer (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the Handoff schema reaches a breaking major version. See spec.md / feature.json.
---

# implement — Contract

```json
{
  "provides": {
    "files": [
      "Handoff slot schema (versioned, machine-first: work_order_id, status, artifact, discovered_work, concerns, blocked_reason)",
      "IMPLEMENT state (dry-run reference adapter): run(TickContext) -> StateResult, reads execution_plan, writes handoffs, emits OK|BLOCKED (deterministic, inert)"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "calls a model (the dry-run rung is deterministic; the model-backed doer is a separate deferred adapter)",
    "creates a branch, commit, PR, or any VCS artifact",
    "reads the workspace slot or provisions an isolated worktree (deferred to the model-backed doer)",
    "writes to the tracker or filesystem (inert; git status stays clean after a tick)",
    "imposes a per-task budget cap (the token-ceiling budget lives in safety-governance)",
    "calls the wall clock, randomness, or the network",
    "edits files in other features"
  ]
}
```
