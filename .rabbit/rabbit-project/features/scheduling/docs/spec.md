---
feature: scheduling
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoding is removed.
---

# scheduling

## Purpose

The **in-session durable heartbeat** that drives the tick loop, the user control
skills **`/auto-maintainer:start`** and **`/auto-maintainer:stop`**, and (slice 1)
the **demo route + tick-runner** that composes the real lifecycle-core anchors
into a self-restarting ~1-minute loop you can watch in an installed session.

> Design references: DESIGN.md §3.3.1 (scheduler detection: system cron else
> in-session durable heartbeat — a plugin can't install its own clock), §3.3.2
> (heartbeat bootstrap, user-authorized), §3.3.3 (immediate-refire + at-most-one
> dedup), §3.3.4 (restart-and-resume), §3.10.4 (run UX).

## The real loop (slice 3: route-as-data)

The route is now **data**, loaded + validated + resolved by `adapter-wiring`
(§3.4.3) — no longer hardcoded. The shipped **default route** is the read-and-idle
spine:

```
GUARD → DRAIN → PULL → PERSIST → EXIT
```

- Built-in adapters are wired via the factory convention
  (`factory(runtime) -> (manifest, run)`): scheduling provides factories for
  `GUARD`/`EXIT` (lifecycle-dispositions), `DRAIN`/`PERSIST` (durable-state),
  `PULL`/`TRIAGE` (work-intake), `PRIORITIZE` (prioritize), and `IMPLEMENT`
  (implement). The **default adapter-map** maps every known port (incl. `TRIAGE`,
  `PRIORITIZE`, and `IMPLEMENT`) to its factory, even though the default route
  uses a subset.
- **Override by config, not code:** a project-local
  `${CLAUDE_PROJECT_DIR}/.auto-maintainer/route.json` (and optional
  `adapters.json`) overrides the defaults. Inserting `TRIAGE` between PULL and
  PERSIST — or the full act-side chain
  `TRIAGE → PRIORITIZE → IMPLEMENT` (PRIORITIZE reads `work_orders` and writes
  `execution_plan`; the dry-run IMPLEMENT reads `execution_plan` and writes
  `handoffs`) — is a pure route.json edit. `adapter-wiring` resolves each port
  from the map and `validate_wiring` checks it at load. This is the
  ports-and-adapters promise made real at runtime: the
  `TRIAGE → PRIORITIZE → IMPLEMENT` route wires with NO code change because all
  three ports are pre-mapped in `DEFAULT_ADAPTER_MAP`. The dry-run IMPLEMENT is
  INERT (no VCS / filesystem effect), so the act-path tick still idles
  (read-and-idle): it produces `handoffs` but leaves no remaining work, so there
  is no busy disposition to select.
- **Read-and-idle:** with only read stages (no `IMPLEMENT` yet) `EXIT` goes
  **IDLE** after the route runs; it becomes refire/work-driven once an act stage
  lands. The slice-1 `DEMO_WORK` stub stays retired.
- **Per-tick read products (#64):** the four read products
  `work_items`/`work_orders`/`execution_plan`/`handoffs` are EPHEMERAL — each tick
  they reflect ONLY what THIS tick's route produced (PULL writes `work_items`;
  TRIAGE, if routed, writes `work_orders`; PRIORITIZE, if routed, writes
  `execution_plan`; IMPLEMENT, if routed, writes `handoffs`). They are NOT carried
  forward across ticks: a route without TRIAGE reports `work_orders=0`, and a
  route without PRIORITIZE/IMPLEMENT reports `execution_plan=0 handoffs=0`, never a
  stale count from an earlier act-path tick. All four are surfaced in the tick
  trace and in `status.py` (always shown, including 0, #69), in the order
  `work_items work_orders execution_plan handoffs`. (Durable state keeps cross-tick
  facts; the read-product snapshot is overwritten every tick.)

## Governance wiring (slice 1: load + surface + persist)

scheduling consumes `safety-governance` UNCHANGED to make the maintainer loop
governance-aware. This slice **loads + surfaces + persists** governance state;
enforcement of act-skip is **deferred** to the acting doer (next milestone).

- **Load once per tick.** `run_tick` calls `sg.load_governance(project_dir)`
  (project-local `${project_dir}/.auto-maintainer/governance.json`, else the
  documented defaults) and threads the loaded config into the factory `runtime`
  dict under a `governance` key — so future acting adapters can consult
  `permits`/budget — without disturbing the existing runtime keys
  (`project_dir`/`runtime_dir`/`source`/`now`).
- **Durable, cross-tick budget window (#69-style surface, durable like the
  counter).** A new durable key `budget` stores `{window_key, spent_tokens}`.
  Each tick resolves a tz-aware `now` (the injected `now` when it is tz-aware,
  else the host local-aware now `datetime.now().astimezone()`), loads the prior
  budget state from durable state (default `{}`), calls
  `sg.evaluate_budget(gov, budget_state, now, tick_spend=<injected, default 0>)`,
  and PERSISTS the returned `budget_state`. The lib performs the window rollover
  / auto-resume: a `now` on a later local day advances `window_key` and resets
  `spent_tokens`. `tick_spend` is `0` in production (no model spender yet); tests
  inject it. The budget window is a durable CROSS-TICK fact, **not** a per-tick
  ephemeral read product (#64): a tick within the same window carries the
  accumulated spend forward — only a window rollover (inside `evaluate_budget`)
  resets it.
- **Surface in the trace AND status.py** (always shown, #69 style): `mode=<mode>`
  and a compact `budget=<spent>/<ceiling-or-"none"> win=<window_key>` field
  (`none` = a null/unlimited per_day ceiling), placed after the existing fields
  (all current fields/order preserved). When `evaluate_budget` returns
  `allowed=False`, a `budget_paused=<reason>` indicator is appended (e.g.
  `budget_paused=per_day_exhausted`).
- **No act-skip this slice.** A budget-blocked tick is NOT skipped or idled
  differently — there is no model spender yet, and the acting doer will consult
  `permits`/budget itself next milestone. This slice only SURFACES the paused
  reason; the tick still completes (read-and-idle).

## Paths governed

Greenfield. Code under `.../features/scheduling/src/`. Shippable plugin
components (the two skills + the tick-runner entrypoint) live under the feature's
**`ship/`** dir (the convention `packaging-config`'s assembly collects).

## Public surface

1. **Tick-runner script** (`src/run_tick.py`, deterministic, script-tier) —
   defines the built-in adapter **factories** + the embedded `DEFAULT_ROUTE` and
   `DEFAULT_ADAPTER_MAP`, then calls `adapter_wiring.build_loop(DEFAULT_ROUTE,
   DEFAULT_ADAPTER_MAP, runtime, …)` to load (project-local override else default)
   → resolve → validate → `(route, states)`, runs `tick_orchestrator.run(...)`
   over a `TickContext` seeded from durable state, prints a tick trace (tick
   number, state path, `work_items`/`work_orders` counts, resulting disposition,
   and the **route source** — `default` vs the project-local override path, so a
   misplaced/absent `route.json` is visible, not silently ignored, #59),
   and returns/persists the outcome. One invocation = one tick. `PULL`'s issue
   source is the live `gh` CLI in production but **injectable** so tests pass a
   stub (no network).
2. **PULL integration (read-and-idle)** — the route uses work-intake's `PULL`
   state (writes `work_items`). After PULL + PERSIST, `EXIT` selects **IDLE** (no
   act stage yet), so the heartbeat re-pulls next interval rather than the loop
   busy-firing. The slice-1 `DEMO_WORK` stub is removed.
3. **Control scripts** (deterministic, script-tier — spec-rules §1; the fix for
   #29/#30/#44 where prompt-tier skills drifted/broke):
   - `src/start.py` — prepares a fresh start, then runs tick #1: if the
     disposition is `STOPPED` it clears the latch to a runnable state (start IS
     the §1.2 human resume); if `ABORTED` it REFUSES and tells the user to
     investigate (never silently clears a fault); otherwise proceeds. Reuses
     `run_tick`'s path resolution + the lifecycle API.
   - `src/status.py` — reads the disposition marker + the persisted `work_items`
     count (via `run_tick`'s `resolve_runtime_paths`), the **route source**
     (`default` vs the project-local `route.json` override path, #59), and the real loop
     status.
   - `src/stop.py` — writes disposition `STOPPED` (via the lifecycle-dispositions
     API) using the same runtime-path resolution. Owns the state write.
   These own ALL state operations so the skills never hand-roll Python.
4. **Shipped control skills** (`ship/skills/{start,stop,status}`):
   - `/auto-maintainer:start` — invokes `start.py` for tick #1 (which first clears
     a latched `STOPPED`, or refuses on `ABORTED`), then schedules a recurring
     ~1-min heartbeat (CronCreate) that re-runs `run_tick.py` (not `start.py` — no
     reset per tick); honors EXIT's signal — `refire` → one immediate extra tick
     with **at-most-one-refire dedup** (§3.3.3); `idle` → wait; `halt` → stop.
     **Interval hardcoded to 1 min** (#17). Heartbeat is **session-only** for
     slice 1 (durable + restart-resume deferred, #31).
   - `/auto-maintainer:stop` — invokes `stop.py` (latch STOPPED) then cancels the
     heartbeat (CronDelete).
   - `/auto-maintainer:status` — invokes `status.py` and reports the real
     disposition + last-pull `work_items` count (replaces packaging-config's
     slice-1 stub).
   Only the heartbeat scheduling (CronCreate/CronDelete) is agent-mediated (no
   plugin-level cron API); every state operation is a script.
5. **Scheduler detection (§3.3.1)** — slice 1 uses the in-session durable
   heartbeat; system-cron detection is stubbed/deferred.
6. **Restart-resume wiring (§3.3.4)** — on restart, the next tick's DRAIN +
   durable state resume the loop; full RESTART_NEEDED→SessionStart auto-resume is
   a follow-up.

> Tool-tier note (spec-rules §1): the deterministic tick logic is a **script**
> (`run_tick.py`); the act of *scheduling the wake* is necessarily
> **agent-mediated** (Claude Code exposes no plugin-level cron API), so the
> `/start` skill body instructs the session scheduler. This seam is inherent to
> the platform constraint in §3.3.1.

## What you'll see (installed plugin)

`/auto-maintainer:start` → tick #1 pulls the repo's open issues into `work_items`
(trace shows the count), PERSISTs them, and EXITs **IDLE**. Every ~1 min the
heartbeat re-pulls the current open issues. `/auto-maintainer:status` shows the
disposition + last-pull count. `/stop` latches STOPPED + cancels the heartbeat.

## Current behaviour

Implemented and merged (`tdd_state: test-green`). The tick-runner runs the real
route `GUARD→DRAIN→PULL→PERSIST→EXIT` (read-and-idle); the script-backed
`status.py`/`stop.py` and the `/auto-maintainer:start`/`:stop`/`:status` ship
skills compose work-intake's PULL with the loop core. Live-validated in an
installed plugin session. See `feature.json` / `docs/ROADMAP.md`.

## Agent yield/resume seam (slice: agent-adapter executor protocol)

This slice gives `run_tick` a **yield/resume seam** (DESIGN §2.8 executor
protocol) so a route that contains **agent-states** pauses at each agent-state
(emitting a rendered dispatch request) and resumes when given the dispatch
result. It consumes `agent-dispatch` and `adapter-wiring` UNCHANGED. Pure-script
routes behave EXACTLY as before — byte-for-byte the same trace, same return.

- **Backward-compatible split.** After `adapter_wiring.build_loop` resolves the
  route, `run_tick` inspects the resolved `states`: if EVERY `states[name][1]`
  is a run callable (no agent-states) the route runs via `tick_orchestrator.run`
  exactly as today and returns the EXIT disposition signal STRING. If the route
  has >=1 agent-state (`isinstance(second, adapter_wiring.AgentState)`) the
  pausable driver below runs instead.
- **Pausable driver.** A stepping loop mirroring `to.run` but agent-aware: walk
  from the current position; for a SCRIPT state run `impl(ctx)` +
  `fc.apply_result` + `resolve_next` (as `to.run` does); at an AGENT state build
  the dispatch request with agent-dispatch
  (`ad.build_envelopes(entry, slot_values, {"tick_id", "mode"}, state=<name>)`,
  each rendered with `ad.render`), journal the pending dispatch
  (record-before-act) and **checkpoint** to durable state, then return a PAUSED
  result. `run_tick` NEVER calls the Agent tool — that is the executor's job
  (a later slice).
- **Durable checkpoint** under `TICK_CHECKPOINT_KEY = "tick_checkpoint"`:
  `{next_state, slots (a full snapshot of the live TickContext slot values),
  path, signals, pending: {state, writes, schema_ref, signal_rule,
  cardinality}}`. The checkpoint is the SOLE source of truth for the paused
  dispatch (crash-safety).
- **PAUSED return contract** (a structured dict): `{"status": "paused",
  "state": <agent-state name>, "dispatches": [ {"subagent_type", "prompt"
  (rendered markdown), "writes", "schema_ref", "signal_rule", "cardinality",
  "item"? } ... ]}`. A `once` dispatch yields one dispatch with no `item`; a
  `{per_item: <path>}` dispatch yields one dispatch per resolved element, each
  carrying its `item`.
- **Resume.** `run_tick(resume_dispatch=<list of raw subagent output strings
  for the paused agent-state>)` loads the checkpoint, restores the TickContext
  slots + position, then for the paused agent-state validates each output with
  `ad.validate_output(output_text, slot_schema)` (the registered schema of the
  `writes` slot, from the known SLOT descriptors). On a validation failure it
  returns `{"status": "invalid_output", "state": <name>, "reason": <str>}` and
  leaves the checkpoint intact (re-dispatchable) — it never crashes. On success
  it `collect_outputs(...)` -> the slot value, writes it to the ctx slot,
  `compute_signal(signal_rule, slot_value)` -> the signal, `resolve_next` from
  the agent-state, and continues the driver until the next pause or the terminal.
- **Terminal.** On reaching DONE it clears `TICK_CHECKPOINT_KEY`, persists the
  per-tick ephemeral read products (#64 — only what the route produced, never a
  stale carry-forward), prints the existing one-line trace stitched across all
  segments (path/signals/work_items/work_orders/execution_plan/handoffs/mode/
  budget/route), and returns the disposition signal (a string), exactly as a
  pure-script tick does.
- **Crash-safety.** A fresh `run_tick` invocation that finds an existing
  `tick_checkpoint` (and no `resume_dispatch`) re-emits the SAME PAUSED dispatch
  request from the checkpoint (idempotent — the journal/checkpoint is the truth).
- The budget readiness gate (safety-governance) is evaluated at FRESH tick start
  only, NOT on resume. Read products stay #64 per-tick ephemeral; the budget
  stays a durable cross-tick fact.

## Known gaps / deferred

- The executor (the session-side actor that performs the Agent dispatch and
  feeds `resume_dispatch` back to `run_tick`) — a later slice; this slice only
  emits the dispatch requests and applies provided results.
- Configurable interval + route (config feature) — interval hardcoded to 1 min
  (auto-maintainer-framework#17).
- System-cron scheduler backend (§3.3.1) — slice 1 is in-session heartbeat only.
- TRIAGE/IMPLEMENT/VERIFY/INTEGRATE — the loop now PULLs (read-and-idle); acting
  on `work_items` lands with later features, at which point EXIT becomes
  work-driven (refire while actionable work remains).
- Full RESTART_NEEDED→SessionStart auto-resume (§3.3.4) — follow-up.

## Interfaces (composition)

- Depends on `tick-orchestrator` (run loop + resolve_next), `durable-state`
  (DRAIN/PERSIST/journal/state), `lifecycle-dispositions` (GUARD/EXIT/disposition).
- Declares shippable components under `ship/` for `packaging-config` to assemble
  into `plugins/auto-maintainer/`.
- Owns the `/auto-maintainer:status` skill (script-backed via `status.py`);
  `packaging-config` no longer ships a status stub. Host projects should gitignore
  the runtime dir `.auto-maintainer/`.

## Open questions

- Exact recurring-vs-one-shot scheduling shape and how `/start` records the
  active heartbeat so `/stop` can cancel it deterministically — settle in
  implementation.
