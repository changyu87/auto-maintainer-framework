---
feature: scheduling
version: 0.1.3
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoding is removed.
---

# scheduling — Contract

```json
{
  "provides": {
    "files": [
      "DEFAULT_ROUTE: the shipped read-and-idle spine GUARD->DRAIN->PULL->PERSIST->EXIT as a route.json dict (states/edges/terminal, incl. the DONE/HALTED terminals)",
      "DEFAULT_ADAPTER_MAP: every known port -> 'run_tick:make_<port>' (incl. TRIAGE, PRIORITIZE, IMPLEMENT, resolvable even though the default route omits them)",
      "built-in adapter factories make_guard/make_exit/make_drain/make_persist/make_pull/make_triage/make_prioritize/make_implement — each factory(runtime) -> (StateManifest, run_callable) wrapping the EXISTING sibling adapter unchanged (the adapter-wiring factory convention)",
      "persisted-read-product helpers persisted_work_items/_count, persisted_work_orders/_count, persisted_execution_plan/_count, persisted_handoffs/_count — read the per-tick ephemeral read products from durable state for status reporting",
      "route_source(project_dir=None) -> (label, path): the single source of truth for the loaded route's SOURCE — ('override', '<project_dir>/.auto-maintainer/route.json') when that file exists (the SAME path adapter-wiring loads), else ('default', None); route_source_label(...) renders it as the trace/status token 'default' or 'override:<abs path>' (#59)",
      "BUDGET_KEY = 'budget': the durable-state key under which the cross-tick budget window {window_key, spent_tokens} is persisted (a durable cross-tick fact like the counter, NOT a per-tick ephemeral read product)",
      "persisted_budget_state(state_path) -> {window_key, spent_tokens}: reads the durable budget window from durable state (default {} when never written), for status reporting",
      "governance threading: run_tick loads sg.load_governance(project_dir) once per tick and threads it into the factory runtime dict under a 'governance' key (so future acting adapters can consult permits/budget); the existing runtime keys project_dir/runtime_dir/source/now are preserved"
    ],
    "scripts": [
      "src/run_tick.py: deterministic single-tick runner — resolves runtime, loads governance via sg.load_governance(project_dir) and threads it into the runtime dict, calls adapter_wiring.build_loop(DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime, start='GUARD', initial=[...]) to load (project-local override else default) -> resolve -> validate -> (route, states), runs tick_orchestrator.run(...), evaluates + persists the durable cross-tick budget window via sg.evaluate_budget(gov, prior_budget_state, now, tick_spend), prints a tick trace (incl. route source #59, plus mode + a compact budget=<spent>/<ceiling-or-none> win=<window_key> field and a budget_paused=<reason> indicator when blocked), persists the per-tick ephemeral read products work_items + work_orders + execution_plan + handoffs (each when the active route produced it, else empty) + the EXIT disposition signal (one invocation = one tick). run_tick(...) accepts injectable now (tz-aware budget clock; defaults to the host local-aware now) + tick_spend (default 0; no model spender yet, tests inject it)",
      "src/start.py / src/stop.py / src/status.py: deterministic control scripts (script-tier) owning all state operations; status.py reports disposition + the four read-product counts work_items/work_orders/execution_plan/handoffs (always reported, including 0, matching the tick trace, #69) + the route source (default vs override:<path>, #59) via run_tick.route_source + the governance mode + the compact budget field (budget=<spent>/<ceiling-or-none> win=<window_key>) + a budget_paused=<reason> indicator when the durable budget is exhausted"
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
      "project-local ${project_dir}/.auto-maintainer/route.json and adapter-map.json (override config; loaded + validated by adapter-wiring, default used when absent)",
      "project-local ${project_dir}/.auto-maintainer/governance.json (governance config; loaded by safety-governance, documented defaults used when absent)",
      "durable budget window {window_key, spent_tokens} under the durable-state BUDGET_KEY (a durable cross-tick fact)"
    ],
    "external": [
      "fsm-contracts: TickContext, StateResult, StateManifest, SignalVocabulary, apply_result",
      "tick-orchestrator: run, RunResult",
      "durable-state: DRAIN/PERSIST states, DRAIN/PERSIST manifests, DurableState, Journal, SCHEMA_VERSION",
      "lifecycle-dispositions: Guard, Exit, Disposition, read_disposition/write_disposition, lock helpers",
      "work-intake: Pull, PULL_MANIFEST, Triage, TRIAGE_MANIFEST, WORK_ITEMS_SLOT, WORK_ORDERS_SLOT, gh_issue_source, parse_gh_issues",
      "prioritize: PRIORITIZE_MANIFEST, EXECUTION_PLAN_SLOT, run (PRIORITIZE reads work_orders, writes execution_plan)",
      "implement: IMPLEMENT_MANIFEST, HANDOFFS_SLOT, run (dry-run IMPLEMENT reads execution_plan, writes handoffs; INERT)",
      "adapter-wiring: build_loop, WiringError (load + resolve + validate route-as-data against DEFAULT_ROUTE/DEFAULT_ADAPTER_MAP)",
      "safety-governance: load_governance, evaluate_budget (and the threaded config; consumed UNCHANGED — load governance + the durable budget window's window_key/spent_tokens)"
    ]
  },
  "invokes": {
    "scripts": ["src/run_tick.py (invoked once per tick by the /auto-maintainer:start heartbeat)"],
    "external": ["adapter-wiring: build_loop(default_route, default_map, runtime, start, initial) -> (route, states)"],
    "agents": []
  },
  "never": [
    "edits or forks fsm-contracts, tick-orchestrator, durable-state, lifecycle-dispositions, work-intake, prioritize, implement, adapter-wiring, or safety-governance (consumed unchanged)",
    "enforces act-skip on a budget-blocked tick (deferred to the acting doer next milestone; this slice only loads + surfaces + persists governance state)",
    "re-implements the run loop / transition resolution (owned by tick-orchestrator)",
    "re-implements DRAIN/PERSIST/journal/durable persistence (owned by durable-state)",
    "re-implements GUARD/EXIT/disposition/mutex (owned by lifecycle-dispositions)",
    "re-implements route load/resolve/validate (owned by adapter-wiring; scheduling only supplies the default route + the built-in factories)",
    "makes the tick interval configurable (deferred to the configuration feature, auto-maintainer-framework#17)",
    "assembles or copies the plugin tree (owned by packaging-config, which collects ship/)"
  ]
}
```
