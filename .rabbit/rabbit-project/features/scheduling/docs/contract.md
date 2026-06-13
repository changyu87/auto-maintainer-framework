---
feature: scheduling
version: 0.2.2
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
      "governance threading: run_tick loads sg.load_governance(project_dir) once per tick and threads it into the factory runtime dict under a 'governance' key (so future acting adapters can consult permits/budget); the existing runtime keys project_dir/runtime_dir/source/now are preserved",
      "TICK_CHECKPOINT_KEY = 'tick_checkpoint': the durable-state key under which the agent yield/resume checkpoint is persisted while a tick is PAUSED at an agent-state ({next_state, slots, path, signals, pending:{state, writes, schema_ref, signal_rule, cardinality}}); the SOLE source of truth for the paused dispatch (crash-safety). Cleared on reaching the terminal",
      "persisted_tick_checkpoint(state_path) -> {} | checkpoint: reads the durable PAUSED checkpoint (default {} when no tick is paused)",
      "run_tick agent yield/resume contract (DESIGN §2.8): when the resolved route contains >=1 agent-state, run_tick PAUSES at each agent-state and returns a PAUSED dict {status:'paused', state:<name>, dispatches:[{subagent_type, prompt (rendered markdown), writes, schema_ref, signal_rule, cardinality, item?}...]} after durably checkpointing (it NEVER calls the Agent tool). run_tick(resume_dispatch=[<raw subagent output strings>]) validates + applies the outputs and continues to the next pause or terminal; a validation failure returns {status:'invalid_output', state:<name>, reason:<str>} with the checkpoint left intact (re-dispatchable). A fresh run_tick with no resume_dispatch that finds an existing checkpoint re-emits the SAME PAUSED dispatch (crash-safety, idempotent). A pure-script route is UNCHANGED (runs via tick_orchestrator.run, returns the disposition signal string)",
      "JSON tick CLI run_tick.main(argv) (also the __main__ entrypoint): a THIN deterministic wrapper around the EXISTING run_tick structured returns (no new tick logic) so the later executor skill can drive the yield/resume loop. Bare invocation (no --step/--resume) is UNCHANGED — calls run_tick() and prints the one-line HUMAN trace (existing bash callers keep working). --step runs to the next pause/terminal and prints a SINGLE JSON object to stdout: done -> {status:'done', signal:'<idle|halt|...>', trace:'<one-line trace>'}; paused -> {status:'paused', state:<name>, dispatches:[{subagent_type, prompt, writes, schema_ref, signal_rule, cardinality, item?}...]}; invalid_output -> {status:'invalid_output', state:..., reason:...}. --resume <file> reads a JSON array of raw subagent output strings (dispatch order), calls run_tick(resume_dispatch=<list>), and prints the same envelope shape. In --step/--resume mode stdout is PURE JSON (the skill parses stdout) — the human trace is captured into the JSON `trace` field, never leaked raw. Exit codes: done/paused -> 0; invalid_output (bad agent output OR malformed/missing --resume file) -> 1 (no crash). The path flags --runtime-dir/--state/--journal/--project-dir point the CLI at a temp runtime for tests; omitted -> production defaults (resolve_runtime_paths). The PULL source is not a CLI flag (defaults to DEFAULT_PULL_SOURCE / live gh); tests stub it by overriding DEFAULT_PULL_SOURCE"
    ],
    "scripts": [
      "src/run_tick.py: deterministic single-tick runner — resolves runtime, loads governance via sg.load_governance(project_dir) and threads it into the runtime dict, calls adapter_wiring.build_loop(DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime, start='GUARD', initial=[...]) to load (project-local override else default) -> resolve -> validate -> (route, states), runs tick_orchestrator.run(...), evaluates + persists the durable cross-tick budget window via sg.evaluate_budget(gov, prior_budget_state, now, tick_spend), prints a tick trace (incl. route source #59, plus mode + a compact budget=<spent>/<ceiling-or-none> win=<window_key> field and a budget_paused=<reason> indicator when blocked), persists the per-tick ephemeral read products work_items + work_orders + execution_plan + handoffs (each when the active route produced it, else empty) + the EXIT disposition signal (one invocation = one tick). run_tick(...) accepts injectable now (tz-aware budget clock; defaults to the host local-aware now) + tick_spend (default 0; no model spender yet, tests inject it) + resume_dispatch (default None; the executor feeds back the paused agent-state's raw subagent outputs). When the route contains agent-states run_tick runs the pausable driver (yield/resume seam) instead of tick_orchestrator.run, checkpointing under TICK_CHECKPOINT_KEY and returning the PAUSED/invalid_output dict; the budget readiness gate is evaluated at FRESH tick start only, not on resume",
      "src/start.py / src/stop.py / src/status.py: deterministic control scripts (script-tier) owning all state operations; status.py reports disposition + the four read-product counts work_items/work_orders/execution_plan/handoffs (always reported, including 0, matching the tick trace, #69) + the route source (default vs override:<path>, #59) via run_tick.route_source + the governance mode + the compact budget field (budget=<spent>/<ceiling-or-none> win=<window_key>) + a budget_paused=<reason> indicator when the durable budget is exhausted"
    ],
    "skills": [
      "ship/skills/start/SKILL.md (/auto-maintainer:start): runs tick #1 via start.py then schedules the recurring ~1-min in-session heartbeat re-running run_tick.py",
      "ship/skills/stop/SKILL.md (/auto-maintainer:stop): invokes stop.py (latch STOPPED) and cancels the heartbeat",
      "ship/skills/status/SKILL.md (/auto-maintainer:status): invokes status.py and reports the real disposition + last-pull work_items count",
      "ship/skills/tick/SKILL.md (/auto-maintainer:tick): the executor skill — drives run_tick.py --step/--resume and presses the Agent button at agent-states (dispatches the runner's named subagent(s) with the rendered prompt, feeds outputs back via ${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json) until the tick completes; all tick logic stays in run_tick.py (the skill only relays dispatch requests + results)"
    ],
    "agents": [
      "ship/agents/auto-maintainer-echo.md (auto-maintainer-echo): the domain-free PROOF triager subagent — dispatched by subagent_type at the TRIAGE agent-state, echoes each input work_item back as one accepted work_order and returns ONLY the work_orders JSON array (work-intake:WORK_ORDERS). The echo-TRIAGE adapter-map entry (kind=agent, reads work_items, writes work_orders, dispatch subagent_type=auto-maintainer-echo cardinality once, signal nonempty_else_empty) is valid drop-in config: adapter_wiring.build_loop ACCEPTS it (TRIAGE resolves to an AgentState) and run_tick runs it end-to-end via the yield/resume seam"
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
      "safety-governance: load_governance, evaluate_budget (and the threaded config; consumed UNCHANGED — load governance + the durable budget window's window_key/spent_tokens)",
      "agent-dispatch: build_envelopes, render, validate_output, collect_outputs, compute_signal (consumed UNCHANGED — the deterministic helpers around an in-session agent dispatch; run_tick emits the dispatch request and applies provided results, never dispatching itself)"
    ]
  },
  "invokes": {
    "scripts": ["src/run_tick.py (invoked once per tick by the /auto-maintainer:start heartbeat)"],
    "external": ["adapter-wiring: build_loop(default_route, default_map, runtime, start, initial) -> (route, states)"],
    "agents": []
  },
  "never": [
    "edits or forks fsm-contracts, tick-orchestrator, durable-state, lifecycle-dispositions, work-intake, prioritize, implement, adapter-wiring, safety-governance, or agent-dispatch (consumed unchanged)",
    "calls the Agent tool / any model / subprocess from run_tick (the yield/resume seam only EMITS dispatch requests and applies provided results; the executor that performs the Agent dispatch is a later slice)",
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
