---
feature: scheduling
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoding is removed.
---

# scheduling — Contract

```json
{
  "provides": {
    "files": [
      "DEMO_WORK state: run(TickContext) -> StateResult, reads counter, journals the increment intent, writes counter+1, emits OK below THRESHOLD else EMPTY",
      "DEMO_WORK per-state manifest (reads/writes/emits)"
    ],
    "scripts": [
      "src/run_tick.py: deterministic single-tick runner — assembles the route GUARD->DRAIN->DEMO_WORK->PERSIST->EXIT, seeds a TickContext from durable state, runs tick_orchestrator.run(...), prints a tick trace, returns/persists the EXIT disposition signal (one invocation = one tick)"
    ],
    "skills": [
      "ship/skills/start/SKILL.md (/auto-maintainer:start): runs tick #1 then schedules the recurring ~1-min in-session heartbeat",
      "ship/skills/stop/SKILL.md (/auto-maintainer:stop): sets disposition STOPPED and cancels the heartbeat"
    ]
  },
  "reads": {
    "files": [
      "durable state JSON file (path injected via run_tick state_path / TickContext 'state_path' slot)",
      "journal JSONL file (path injected via run_tick journal_path / TickContext 'journal_path' slot)",
      "disposition + lock markers (under the injected runtime_dir)"
    ],
    "external": [
      "fsm-contracts: TickContext, StateResult, StateManifest, SignalVocabulary, apply_result",
      "tick-orchestrator: run, RunResult",
      "durable-state: DRAIN/PERSIST states, DRAIN/PERSIST manifests, DurableState, Journal, SCHEMA_VERSION",
      "lifecycle-dispositions: Guard, Exit, Disposition, read_disposition/write_disposition, lock helpers"
    ]
  },
  "invokes": {
    "scripts": ["src/run_tick.py (invoked once per tick by the /auto-maintainer:start heartbeat)"],
    "agents": []
  },
  "never": [
    "edits or forks fsm-contracts, tick-orchestrator, durable-state, or lifecycle-dispositions (consumed unchanged)",
    "re-implements the run loop / transition resolution (owned by tick-orchestrator)",
    "re-implements DRAIN/PERSIST/journal/durable persistence (owned by durable-state)",
    "re-implements GUARD/EXIT/disposition/mutex (owned by lifecycle-dispositions)",
    "makes the tick interval or route configurable (deferred to the configuration feature, auto-maintainer-framework#17)",
    "assembles or copies the plugin tree (owned by packaging-config, which collects ship/)"
  ]
}
```
