---
feature: observability
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the event-log schema or escalation contract reaches a breaking major version, or when surfacing moves to a different sink than a local JSONL log + tracker issue-comment. See spec.md / feature.json.
---

# observability — Contract

```json
{
  "provides": {
    "files": [
      "Event-log schema (versioned, machine-first JSONL) + EventLog(path): append/read/tail",
      "EVENT_SCHEMA_VERSION, EVENT_KINDS (closed vocabulary)",
      "Escalation channel: escalate(target_ref, message, sink=, now=) -> issue-comment on the triggering issue via an injectable gh sink, provenance-stamped"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {
    "files": ["${CLAUDE_PROJECT_DIR}/.auto-maintainer/events.jsonl (its own append-only log)"],
    "external": []
  },
  "invokes": {
    "scripts": [],
    "agents": [],
    "external": ["gh issue comment <ref> (via the injectable escalation sink; default only)"]
  },
  "never": [
    "creates a new tracked item / files a DiscoveredIssue (that is REPORT / outbound-report §3.11)",
    "calls the live gh CLI from tests (the escalation sink is injectable; tests stub it)",
    "reads the wall clock implicitly (ts comes from an injected now)",
    "walks the route, dispatches a subagent, or decides control flow",
    "edits files in other features"
  ]
}
```
