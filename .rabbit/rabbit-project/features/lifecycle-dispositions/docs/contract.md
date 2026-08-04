---
feature: lifecycle-dispositions
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the lifecycle disposition model changes incompatibly (e.g. v2 parallelism introduces per-stream dispositions) or the marker encoding is replaced.
---

# lifecycle-dispositions — Contract

```json
{
  "provides": {
    "files": ["disposition marker + lock marker under the injected runtime dir"],
    "scripts": [
      "Disposition (closed set RUNNING|IDLE|STOPPED|ABORTED|RESTART_NEEDED)",
      "read_disposition(runtime_dir) / write_disposition(runtime_dir, value)",
      "acquire_lock(runtime_dir) / release_lock(runtime_dir) / lock_is_held(runtime_dir) (single-writer mutex with stale-marker detection)",
      "Guard(runtime_dir).run(TickContext) -> StateResult (entry anchor)",
      "Exit(runtime_dir).run(TickContext) -> StateResult (terminal anchor)"
    ],
    "skills": []
  },
  "reads": {
    "files": [
      "the disposition + lock markers under the runtime dir (sole source of truth)",
      "fsm-contracts (TickContext, StateResult, StateManifest)"
    ],
    "external": ["/proc/<pid>/stat for stale-lock (owner liveness) detection"]
  },
  "invokes": {
    "scripts": ["fsm-contracts StateResult / StateManifest constructors"],
    "agents": []
  },
  "never": [
    "imports or depends on durable-state (orthogonal; owns the state document + journal + DRAIN/PERSIST)",
    "owns the heartbeat, route assembly, or demo work (scheduling owns those)",
    "resolves transitions or runs the loop (tick-orchestrator owns routing)",
    "embeds maintainer-domain logic"
  ]
}
```
