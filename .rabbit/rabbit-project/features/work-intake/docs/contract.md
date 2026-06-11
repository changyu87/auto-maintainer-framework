---
feature: work-intake
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the tracker-read model changes incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a breaking major version). See spec.md / feature.json.
---

# work-intake — Contract

```json
{
  "provides": {
    "files": [
      "WorkItem slot schema (versioned, machine-first)",
      "PULL state: run(TickContext) -> StateResult, writes work_items, emits OK|EMPTY"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {"files": [], "external": []},
  "invokes": {
    "scripts": [],
    "agents": [],
    "external": ["gh issue list --state open --json number,title,body,url,state,labels,author,createdAt,updatedAt [--repo <repo>]"]
  },
  "never": [
    "performs TRIAGE / normalize / validate / dedup / decompose / order or writes work_orders (slice 2)",
    "implements or executes maintainer work",
    "edits files in other features",
    "calls the live gh CLI from tests (the issue source is injectable; tests use a stub)"
  ]
}
```
