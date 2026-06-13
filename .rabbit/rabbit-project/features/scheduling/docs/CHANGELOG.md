# scheduling — Changelog

## contract 0.2.2 — 2026-06-13

- Shipped two plugin assets into `ship/` (collected verbatim by the build's
  `_copy_tree(ship_dir, plugin_root)`, NO build change):
  - `ship/skills/tick/SKILL.md` (`/auto-maintainer:tick`) — the **executor
    skill** that drives `run_tick.py --step`/`--resume` and presses the `Agent`
    button at agent-states: it steps the runner, and at each PAUSE dispatches the
    runner's named subagent(s) with the rendered prompt and feeds the outputs back
    via `${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json` until the
    tick completes. All tick logic stays in `run_tick.py`; the skill only relays.
  - `ship/agents/auto-maintainer-echo.md` (`auto-maintainer-echo`) — the
    domain-free **proof triager** subagent: echoes each input `work_item` into one
    accepted `work_order` and returns ONLY the `work_orders` JSON array
    (`work-intake:WORK_ORDERS`).
- Added an e2e test (`test/test_ship_tick_skill_e2e.py`) proving the shipped
  wiring is real: both ship files exist + parse (`name: tick`,
  `name: auto-maintainer-echo`, lifecycle metadata present), the echo-TRIAGE
  agent-adapter entry VALIDATES via `adapter_wiring.build_loop` (TRIAGE resolves
  to an `AgentState` dispatching `auto-maintainer-echo`), and a TRIAGE-agent route
  runs end-to-end through `run_tick`'s yield/resume seam with a canned echo output
  (advances past TRIAGE). No `run_tick.py` / `status.py` logic changed; siblings
  consumed unchanged. Additive: new shipped assets + a `provides.agents` block;
  no existing return contract or typed schema field altered.

## contract 0.2.1 — 2026-06-13

- Added a JSON **tick CLI** to `run_tick.py` (`run_tick.main(argv)`, also the
  `__main__` entrypoint) so the later executor skill can drive the yield/resume
  loop deterministically. It is a THIN deterministic wrapper around the EXISTING
  `run_tick(...)` structured returns (the yield/resume seam) — NO new tick logic.
- Bare invocation (`python run_tick.py`, no flags) is UNCHANGED: it calls
  `run_tick()` and prints the one-line HUMAN trace, so existing pure-script bash
  callers keep working (backward-compatible).
- `--step` runs to the next pause/terminal and prints a SINGLE JSON object to
  stdout: `done -> {"status":"done","signal":"<idle|halt|...>","trace":"<one-line
  trace>"}`; `paused -> {"status":"paused","state":"<name>","dispatches":[...]}`;
  `invalid_output -> {"status":"invalid_output","state":...,"reason":...}`.
- `--resume <file>` reads a JSON array of raw subagent output strings (dispatch
  order), calls `run_tick(resume_dispatch=<list>)`, and prints the same envelope
  shape (paused again, done, or invalid_output).
- In `--step`/`--resume` mode stdout is PURE JSON (the skill parses stdout): the
  human trace `run_tick` writes to stdout is captured into the JSON `trace` field,
  never leaked raw. Exit codes: `done`/`paused` -> 0; `invalid_output` (a bad
  agent output OR a malformed/missing `--resume` file) -> 1 (no crash/traceback).
- The path flags `--runtime-dir`/`--state`/`--journal`/`--project-dir` point the
  CLI at a temp runtime for tests; when omitted the CLI uses the production
  defaults (`resolve_runtime_paths`) exactly like bare mode. The PULL source is
  not a CLI flag (defaults to `DEFAULT_PULL_SOURCE` / the live `gh` CLI); tests
  stub it by overriding `DEFAULT_PULL_SOURCE`. New public CLI surface only; no
  typed schema field changed and no existing return contract altered.

## contract 0.2.0 — 2026-06-13

- Gave `run_tick` a **yield/resume seam** (DESIGN §2.8 executor protocol) so a
  route containing **agent-states** pauses at each agent-state (emitting a
  rendered dispatch request) and resumes when given the dispatch result.
  Consumes `agent-dispatch` + `adapter-wiring` UNCHANGED. Pure-script routes are
  byte-for-byte unchanged (still run via `tick_orchestrator.run`, return the
  disposition signal string; all prior scheduling tests stay green).
- Backward-compatible split: after `adapter_wiring.build_loop`, `run_tick`
  inspects the resolved `states`; a route with no agent-states runs the legacy
  path, a route with >=1 `adapter_wiring.AgentState` runs the new pausable
  driver.
- New durable key `TICK_CHECKPOINT_KEY = "tick_checkpoint"` storing the PAUSED
  tick (`{next_state, slots (full live TickContext slot snapshot), path,
  signals, pending:{state, writes, schema_ref, signal_rule, cardinality}}`) —
  the SOLE source of truth for the paused dispatch (crash-safety). Cleared on
  reaching the terminal. Added `persisted_tick_checkpoint(state_path)`.
- New `run_tick(..., resume_dispatch=None)` parameter. The PAUSED return
  contract: `{"status":"paused", "state":<name>, "dispatches":[{subagent_type,
  prompt (rendered markdown via agent_dispatch.render), writes, schema_ref,
  signal_rule, cardinality, item?}...]}`. A `once` dispatch yields one record
  (no `item`); a `{per_item: <path>}` dispatch yields one record per resolved
  element, each carrying its `item`. On resume each output is validated via
  `agent_dispatch.validate_output` (the `writes`-slot schema for `once`, a
  generic element parse for `per_item`); a validation failure returns
  `{"status":"invalid_output", "state":<name>, "reason":<str>}` with the
  checkpoint left intact (re-dispatchable) — never a crash. On success the
  collected slot value is applied, the signal computed via
  `agent_dispatch.compute_signal`, and the driver continues to the next pause or
  the terminal.
- Crash-safety: a fresh `run_tick` with no `resume_dispatch` that finds an
  existing checkpoint re-emits the SAME PAUSED dispatch (idempotent — rendered
  from the durable checkpoint so the bytes match the first emission).
- `run_tick` NEVER calls the Agent tool / a model / a subprocess; it only emits
  dispatch requests and applies provided results (deterministic given injected
  `resume_dispatch`). The budget readiness gate is evaluated at FRESH tick start
  only, not on resume. Read products stay #64 per-tick ephemeral; the budget
  stays a durable cross-tick fact.

## contract 0.1.3 — 2026-06-10

- Wired `safety-governance` into the tick loop (slice 1: load + surface +
  persist; consumed UNCHANGED). `run_tick` now loads governance via
  `sg.load_governance(project_dir)` (project-local
  `.auto-maintainer/governance.json`, else the documented defaults) and threads
  the config into the factory `runtime` dict under a new `governance` key, so
  future acting adapters can consult `permits`/budget. The existing runtime keys
  (`project_dir`/`runtime_dir`/`source`/`now`) are preserved.
- Added a durable, cross-tick **budget window** under the new durable-state key
  `BUDGET_KEY = "budget"` (`{window_key, spent_tokens}`). Each tick resolves a
  tz-aware `now` (injected when tz-aware, else `datetime.now().astimezone()`),
  calls `sg.evaluate_budget(gov, prior_budget_state, now, tick_spend)`, and
  PERSISTS the returned `budget_state` (the lib performs window rollover /
  auto-resume). The budget window is a durable cross-tick fact like the counter,
  NOT a per-tick ephemeral read product (#64) — it is not reset under the #64
  logic; only a window rollover resets `spent_tokens`. Added the
  `persisted_budget_state(state_path)` helper.
- Surfaced governance state in BOTH the tick trace and `status.py` (#69 style,
  always shown): `mode=<mode>` and a compact
  `budget=<spent>/<ceiling-or-"none"> win=<window_key>` field, plus a
  `budget_paused=<reason>` indicator when `evaluate_budget` returns
  `allowed=False`. Placed after the existing fields; all current fields/order
  preserved.
- `run_tick(...)` gained injectable `now` (the tz-aware budget clock; defaults to
  the host local-aware now) and `tick_spend` (default 0 — no model spender yet;
  tests inject it). Act-skip enforcement on a budget-blocked tick is DEFERRED to
  the acting doer (next milestone); this slice only loads + surfaces + persists.
  New public surface (status/trace fields, durable `budget` key); informational
  stdout only — no typed schema field changed.

## contract 0.1.2 — 2026-06-10

- Wired the two new deterministic adapters into the route-as-data loop:
  `PRIORITIZE` (prioritize) and `IMPLEMENT` (implement, dry-run) are now in
  `DEFAULT_ADAPTER_MAP` (mapped to `run_tick:make_prioritize` /
  `run_tick:make_implement`), so an override route
  `TRIAGE → PRIORITIZE → IMPLEMENT` wires with NO code change. The default route
  is unchanged (read-and-idle spine; the two new ports are wireable but omitted).
- Surfaced two new per-tick ephemeral read products, `execution_plan` and
  `handoffs`, alongside `work_items`/`work_orders`. They are persisted per #64
  discipline (overwritten each tick, empty when the active route did not route the
  producing stage — no stale carry-forward) and shown unconditionally in BOTH the
  tick trace and `status.py` (#69), in the order
  `work_items work_orders execution_plan handoffs`.
- Added persisted-read-product helpers `persisted_execution_plan/_count` and
  `persisted_handoffs/_count` (mirroring the work_orders helpers). Added `BLOCKED`
  (IMPLEMENT's signal) to the closed signal vocabulary. Consumes prioritize +
  implement UNCHANGED; edits only in scheduling. Informational stdout only; no
  typed schema field changed.

## contract 0.1.1 — 2026-06-10

- Fixed auto-maintainer-framework#69: `status.py` now ALWAYS reports
  `work_orders=N`, including `work_orders=0`, matching the tick trace's
  unconditional `work_orders=N` field. The previous conditional (append
  `work_orders` only when the count was truthy) made a default (no-TRIAGE)
  tick's status drop the field, so a reader could not distinguish "no TRIAGE
  routed" from "TRIAGE ran, found nothing", and status diverged from the tick
  trace. Field order is unchanged (disposition, work_items, work_orders, route,
  runtime_dir). Informational stdout only; no typed schema field changed.
