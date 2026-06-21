---
feature: work-intake
version: 0.4.0
owner: changyu87
deprecation_criterion: Superseded when the tracker-read model changes incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a breaking major version). See spec.md / feature.json.
---

# work-intake — Contract

```json
{
  "provides": {
    "files": [
      "WorkItem slot schema (versioned, machine-first)",
      "PULL state: run(TickContext) -> StateResult, writes work_items, emits OK|EMPTY",
      "WorkOrder slot schema (versioned, machine-first; decision-carrying)",
      "TRIAGE state: run(TickContext) -> StateResult, reads work_items, writes work_orders, emits OK|EMPTY (deterministic validity gate)"
    ],
    "scripts": [],
    "skills": [],
    "agents": [
      "ship/agents/auto-maintainer-triager (the read-only TRIAGE judge subagent; protocol-free, prompt-contracted; collected into the plugin's agents/ by the build's ship/ pass)"
    ]
  },
  "reads": {"files": [], "external": []},
  "invokes": {
    "scripts": [],
    "agents": [],
    "external": ["gh issue list --state open --json number,title,body,url,state,labels,author,createdAt,updatedAt [--repo <repo>]", "gh issue view <number> --json comments [--repo <repo>]"]
  },
  "never": [
    "performs dedup-vs-closed / 1-level decompose / dependency ordering / WHAT-generation seam (slice 3+)",
    "implements or executes maintainer work",
    "edits files in other features",
    "calls the live gh CLI from tests (the issue source is injectable; tests use a stub)",
    "calls the wall clock implicitly in TRIAGE (the staleness reference time is injectable; tests pin it)"
  ]
}
```
