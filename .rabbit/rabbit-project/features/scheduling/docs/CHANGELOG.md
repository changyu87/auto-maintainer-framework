# scheduling — Changelog

## contract 0.6.0 — 2026-06-13

- **Agent-tick resume now reads subagent-WRITTEN OUTPUT FILES (DESIGN §3.4.6
  file-based context isolation), not an orchestrator-marshalled blob.** `run_tick`
  resolves `output_dir = ${runtime_dir}/dispatch-out/` (created `mkdir -p`) and
  passes it to `ad.build_envelopes(..., output_dir=output_dir)`. Each dispatch
  carries an `output_path` (under `dispatch-out/`); the rendered `## Handoff`
  names it and mandates the subagent WRITE its JSON there. The PAUSED dispatch
  records now carry `output_path` (replacing `schema_ref`).
- **At pause, any pre-existing file at each `output_path` is DELETED** — a stale
  prior-tick file can never be misread on resume; a missing fresh write surfaces
  as `invalid_output`, never a stale read.
- **`run_tick(resume=True)` reads the output files** at the checkpoint's
  `output_path`s: a MISSING file → `{status:'invalid_output', reason:'missing
  output file: <path>'}` (re-dispatchable; checkpoint intact; no crash); else the
  content is validated via `ad.validate_output(content, schema)`; on all valid it
  collects + persists the slot, computes the signal, and continues. The old
  `resume_dispatch` list input and the `dispatch-result.json` marshalling are
  REMOVED (superseded by file-reading).
- **`run_tick.py --resume` takes NO file argument** now (it reads the checkpoint's
  output files); `--step` is unchanged. The checkpoint persists `output_dir` + the
  per-dispatch `output_path`/`schema`, so a crash-safety re-emit produces the
  byte-identical `output_path`.
- Crash-safety preserved: a fresh `--step` with an existing checkpoint re-emits
  the same PAUSE (byte-identical `output_path`); two consecutive agent ticks
  through DRAIN still clean (#109 stays fixed — the durable checkpoint remains the
  sole paused-dispatch source, no journal intent).
- scheduling consumes `agent-dispatch` and all sibling features UNCHANGED; edits
  live ONLY in scheduling (`run_tick.py` + tests + docs).

## contract 0.5.1 — 2026-06-13

- **Fix #109 — second consecutive AGENT-route tick crashed in DRAIN with
  `KeyError: 'target_counter'`.** The agent yield/resume driver
  (`_drive_agent_tick`) recorded an `agent-dispatch:<tick>:<state>` intent in the
  durable-state tick journal on each pause. That journal is the
  counter-reconciliation ledger: `durable_state.drain_run` reads `target_counter`
  from every unconfirmed intent. The agent-dispatch intent has no `target_counter`
  and is never confirmed, so it survived into the NEXT tick's DRAIN and crashed it.
  The record was REDUNDANT — the durable checkpoint (`TICK_CHECKPOINT_KEY`) is
  already the SOLE crash-safety source of truth for a paused dispatch. Removed the
  redundant `journal.record({...})` (and the now-orphaned local
  `journal = ds.Journal(journal_path)`) from the agent driver; the durable
  checkpoint is untouched, so paused-dispatch crash-safety (a fresh `--step`
  re-emits the same PAUSE) is unchanged. Pure-script routes are unaffected.
- scheduling consumes `durable-state` and all sibling features UNCHANGED;
  `drain_run` is NOT modified (defense-in-depth tolerance of non-counter intents
  is a separate follow-up).
- Closes #109.

## contract 0.5.0 — 2026-06-10

- **`/auto-maintainer:start` skill reworked to v0.2.0** — executor-driven first
  tick + prompt-cron heartbeat. The skill now (1) clears the FRESH-start latch via
  `start.py --clear-only` (clear `STOPPED` → `IDLE`, or REFUSE on `ABORTED` and
  stop), (2) runs tick #1 **through the `/auto-maintainer:tick` executor** — NOT
  `start.py`'s in-process `run_tick` — so an AGENT route's agent-state dispatches
  are fulfilled (DESIGN §2.8 in-session executor model), and (3) schedules a
  recurring ~1-min heartbeat as a **prompt** job firing `/auto-maintainer:tick`
  each interval (NOT a bare `run_tick.py` command, which cannot dispatch
  agent-states). The latch is cleared once at start, not re-cleared per heartbeat.
- **`/auto-maintainer:tick` skill hardened to v0.1.1 (#100)** — the resume step
  now MANDATES the `Write` tool writing a JSON array of the verbatim subagent
  outputs (dispatch order) to the **absolute**
  `${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json` path — never an
  improvised `python -c` (truncates/mis-escapes large/quoted/newline payloads) and
  never a relative path (resolves against the wrong directory). The runner
  validates the payload against the slot schema, so faithful serialization matters.
- Both skills ship via the build's `ship/` collection with NO build change.
  scheduling consumes `start.py`/`run_tick.py` and all sibling features UNCHANGED.
- Closes #100.

## contract 0.4.0 — 2026-06-10

- `start.py` gains a **`--clear-only`** mode that performs ONLY the disposition
  decision — clear a latched `STOPPED` → `IDLE` (announce it), REFUSE on
  `ABORTED` (exit non-zero), or no-op on `RUNNING`/`IDLE`/absent — and does
  **NOT** run tick #1. Exits 0 on the cleared/no-op cases, non-zero on the
  `ABORTED` refusal.
- This separates the FRESH-start latch-clear from tick #1, which the
  **in-session executor model** (DESIGN §2.8) needs: tick #1 of an AGENT route
  must go through the executor skill (which presses the `Agent` button), not
  start.py's in-process `run_tick` (which would just pause).
- The clear-or-refuse decision is factored into ONE helper shared by both
  `--clear-only` and the default clear+tick mode — not duplicated or forked.
  DEFAULT behaviour (no flag) is unchanged (clear/refuse + run tick #1), so
  existing callers and tests are backward-compatible.
- scheduling consumes `run_tick` + `lifecycle-dispositions` UNCHANGED.

## contract 0.3.0 — 2026-06-10

- `run_tick` now emits a **structured event log** (observability §3.9.1) to
  `${runtime_dir}/events.jsonl` each tick, consuming the `observability` lib
  UNCHANGED (`observability.EventLog` + the closed `EVENT_KINDS` vocabulary). The
  EventLog opens at the same `runtime_dir` the tick already resolves (injectable
  for tests). Each tick appends, in order:
  - `tick_start` (detail: route `source` + trust `mode`) at a FRESH tick start;
  - `state_run` + `signal` per visited non-terminal state — the pure-script path
    derives them from the returned `RunResult.path`/`RunResult.signals`; the
    agent-driver path emits them inline as each SCRIPT state runs;
  - `pause` + `dispatch` (detail: `subagent_type` + `writes`) when pausing at an
    agent-state;
  - `resume` on a `--resume` invocation (naming the resumed agent-state);
  - `disposition` (the resulting disposition + EXIT signal);
  - `tick_end` (detail: the four read-product counts
    `work_items`/`work_orders`/`execution_plan`/`handoffs` + the final signal).
- The event `ts` reuses the tick's already-resolved tz-aware budget `now` (the
  injected `now`; never an implicit wall clock), so the log is DETERMINISTIC; `seq`
  is monotonic across a multi-invocation agent tick (observability assigns it via
  the file's line count, so `step → resume → done` all append to one
  `events.jsonl`).
- Event emission is purely **ADDITIVE**: it changes NO existing behaviour — the
  one-line trace, signals, disposition, slot persistence, #64 read-product
  ephemerality, the durable budget window, and every existing scheduling test stay
  green. `run_tick` emits no kind outside `EVENT_KINDS`. Added the consumed-
  unchanged `observability` dependency to the contract's `reads.external` +
  `never` (edits) lists.
- Added an e2e test suite (`test/test_events_e2e.py`) proving the ordered event
  sequence for a default tick, the step→resume single-log monotonic seq for an
  agent route, deterministic `ts`, and the closed-vocabulary guard.

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
