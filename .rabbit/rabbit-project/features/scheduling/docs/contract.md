---
feature: scheduling
version: 0.1.1
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoding is removed.
---

# scheduling — Contract

```json
{
  "provides": {
    "files": [
      "DEFAULT_ROUTE: the shipped read-and-idle spine GUARD->DRAIN->PULL->PERSIST->EXIT as a route.json dict (states/edges/terminal, incl. the DONE/HALTED terminals)",
      "DEFAULT_ADAPTER_MAP: every known port -> 'run_tick:make_<port>' (incl. TRIAGE, resolvable even though the default route omits it)",
      "built-in adapter factories make_guard/make_exit/make_drain/make_persist/make_pull/make_triage — each factory(runtime) -> (StateManifest, run_callable) wrapping the EXISTING sibling adapter unchanged (the adapter-wiring factory convention)",
      "route_source(project_dir=None) -> (label, path): the single source of truth for the loaded route's SOURCE — ('override', '<project_dir>/.auto-maintainer/route.json') when that file exists (the SAME path adapter-wiring loads), else ('default', None); route_source_label(...) renders it as the trace/status token 'default' or 'override:<abs path>' (#59)"
    ],
    "scripts": [
      "src/run_tick.py: deterministic single-tick runner — resolves runtime, calls adapter_wiring.build_loop(DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime, start='GUARD', initial=[...]) to load (project-local override else default) -> resolve -> validate -> (route, states), runs tick_orchestrator.run(...), prints a tick trace (incl. route source — default vs override:<path>, #59), persists work_items (+ work_orders when the active route produced them) + the EXIT disposition signal (one invocation = one tick)",
      "src/start.py / src/stop.py / src/status.py: deterministic control scripts (script-tier) owning all state operations; status.py reports disposition + work_items count + work_orders count (always reported, including 0, matching the tick trace, #69) + the route source (default vs override:<path>, #59) via run_tick.route_source"
    ],
    "skills": [
      "ship/skills/start/SKILL.md (/auto-maintainer:start): runs tick #1 via start.py then schedules the recurring ~1-min in-session heartbeat re-running run_tick.py",
      "ship/skills/stop/SKILL.md (/auto-maintainer:stop): invokes stop.py (latch STOPPED) and cancels the heartbeat",
      "ship/skills/status/SKILL.md (/auto-maintainer:status): invokes status.py and reports the real disposition + last-pull work_items count"
    ]
  },
  "reads": {
    "files": [
      "durable state JSON file (path injected via run_tick state_path / TickContext 'state_path' slot)",
      "journal JSONL file (path injected via run_tick journal_path / TickContext 'journal_path' slot)",
      "disposition + lock markers (under the injected runtime_dir)",
      "project-local ${project_dir}/.auto-maintainer/route.json and adapter-map.json (override config; loaded + validated by adapter-wiring, default used when absent)"
    ],
    "external": [
      "fsm-contracts: TickContext, StateResult, StateManifest, SignalVocabulary, apply_result",
      "tick-orchestrator: run, RunResult",
      "durable-state: DRAIN/PERSIST states, DRAIN/PERSIST manifests, DurableState, Journal, SCHEMA_VERSION",
      "lifecycle-dispositions: Guard, Exit, Disposition, read_disposition/write_disposition, lock helpers",
      "work-intake: Pull, PULL_MANIFEST, Triage, TRIAGE_MANIFEST, WORK_ITEMS_SLOT, WORK_ORDERS_SLOT, gh_issue_source, parse_gh_issues",
      "adapter-wiring: build_loop, WiringError (load + resolve + validate route-as-data against DEFAULT_ROUTE/DEFAULT_ADAPTER_MAP)"
    ]
  },
  "invokes": {
    "scripts": ["src/run_tick.py (invoked once per tick by the /auto-maintainer:start heartbeat)"],
    "external": ["adapter-wiring: build_loop(default_route, default_map, runtime, start, initial) -> (route, states)"],
    "agents": []
  },
  "never": [
    "edits or forks fsm-contracts, tick-orchestrator, durable-state, lifecycle-dispositions, work-intake, or adapter-wiring (consumed unchanged)",
    "re-implements the run loop / transition resolution (owned by tick-orchestrator)",
    "re-implements DRAIN/PERSIST/journal/durable persistence (owned by durable-state)",
    "re-implements GUARD/EXIT/disposition/mutex (owned by lifecycle-dispositions)",
    "re-implements route load/resolve/validate (owned by adapter-wiring; scheduling only supplies the default route + the built-in factories)",
    "makes the tick interval configurable (deferred to the configuration feature, auto-maintainer-framework#17)",
    "assembles or copies the plugin tree (owned by packaging-config, which collects ship/)"
  ]
}
```
