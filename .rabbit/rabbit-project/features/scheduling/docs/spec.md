---
feature: scheduling
version: 0.8.0
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
     `run_tick`'s path resolution + the lifecycle API. A `--clear-only` mode
     performs ONLY the disposition decision — clear a latched `STOPPED` → `IDLE`
     (announce it), or REFUSE on `ABORTED` (exit non-zero), or no-op on
     `RUNNING`/`IDLE`/absent — and does NOT run tick #1; it exits 0 on the
     cleared/no-op cases and non-zero on the `ABORTED` refusal. This separates
     the FRESH-start latch-clear from tick #1, which the **in-session executor
     model** (DESIGN §2.8) needs: tick #1 of an AGENT route must go through the
     executor skill (which presses the `Agent` button), not start.py's
     in-process `run_tick` (which would just pause). The clear-or-refuse decision
     lives in ONE place shared by both modes (clear-only and the default
     clear+tick) — it is never duplicated or forked.
   - `src/status.py` — reads the disposition marker + the persisted `work_items`
     count (via `run_tick`'s `resolve_runtime_paths`), the **route source**
     (`default` vs the project-local `route.json` override path, #59), and the real loop
     status.
   - `src/stop.py` — writes disposition `STOPPED` (via the lifecycle-dispositions
     API) using the same runtime-path resolution. Owns the state write.
   These own ALL state operations so the skills never hand-roll Python.
4. **Shipped control skills** (`ship/skills/{start,stop,status}`):
   - `/auto-maintainer:start` (skill **v0.2.1**) — first invokes
     `start.py --clear-only` to perform ONLY the FRESH-start latch decision (clear
     a latched `STOPPED` → `IDLE`, or REFUSE on `ABORTED` and stop), then runs
     tick #1 **through the executor** by invoking the `/auto-maintainer:tick`
     skill — NOT `start.py`'s in-process `run_tick` — so an AGENT route's
     agent-state dispatches are fulfilled (DESIGN §2.8 in-session executor model).
     It then schedules a recurring ~3-min heartbeat as a **prompt** job (so the
     session is present to fulfill agent dispatches) whose prompt fires the
     `/auto-maintainer:tick` executor each interval — NOT a bare `run_tick.py`
     command, which cannot dispatch agent-states. The latch is cleared ONCE at
     start; the heartbeat does not re-clear it (re-clearing each interval would
     defeat a `/stop` that lands between heartbeats). **Interval hardcoded to
     ~3 min** for testability (#17). Heartbeat is **session-only** for slice 1
     (durable + restart-resume deferred, #31).
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
(trace shows the count), PERSISTs them, and EXITs **IDLE**. Every ~3 min the
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
  each rendered with `ad.render`) and **checkpoint** to durable state, then
  return a PAUSED result. `run_tick` NEVER calls the Agent tool — that is the
  executor's job (a later slice). The pause does NOT record any intent in the
  durable-state tick journal: that journal is the counter-reconciliation ledger
  (`drain_run` reads `target_counter` from every unconfirmed intent), and an
  agent dispatch never touches the counter. The durable checkpoint alone carries
  the paused dispatch (see below), so journaling it as well was redundant AND it
  poisoned the counter journal — an agent-dispatch intent has no `target_counter`
  and is never confirmed, so it survived into the NEXT tick's DRAIN and crashed
  it with `KeyError: 'target_counter'` (auto-maintainer-framework#109). The agent
  driver therefore writes ONLY the checkpoint, never a journal intent.
- **File-based context isolation (DESIGN §3.4.6).** The subagent's output is a
  WRITTEN FILE, never marshalled back through the orchestrator. `run_tick`
  resolves `output_dir = ${runtime_dir}/dispatch-out/` (created `mkdir -p`) and
  passes it to `ad.build_envelopes(entry, slot_values, {tick_id, mode},
  state=<name>, output_dir=output_dir)`. Each envelope carries an
  `output_contract{slot, schema, output_path}` and its rendered `## Handoff`
  section names the `output_path` and mandates writing the JSON output there. The
  orchestrator marshals NO content; the file is the sole handoff.
- **At pause: delete any stale output file.** Before returning the PAUSE,
  `run_tick` DELETES any pre-existing file at each dispatch's `output_path`. A
  stale file from a prior tick must never be misread on resume — a missing fresh
  write surfaces as `invalid_output`, never a stale read.
- **Durable checkpoint** under `TICK_CHECKPOINT_KEY = "tick_checkpoint"`:
  `{next_state, slots (a full snapshot of the live TickContext slot values),
  path, signals, output_dir, pending: {state, writes, signal_rule, cardinality,
  dispatches: [{output_path, schema}...]}}`. `output_dir` and the per-dispatch
  `output_path` are persisted so a crash-safety re-emit produces the
  byte-identical `output_path`, and so resume knows which files to read. The
  checkpoint is the SOLE source of truth for the paused dispatch (crash-safety).
- **PAUSED return contract** (a structured dict): `{"status": "paused",
  "state": <agent-state name>, "dispatches": [ {"subagent_type", "prompt"
  (rendered markdown), "writes", "output_path", "signal_rule", "cardinality",
  "item"? } ... ]}`. A `once` dispatch yields one dispatch with no `item`; a
  `{per_item: <path>}` dispatch yields one dispatch per resolved element, each
  carrying its `item` and its own distinct `output_path`.
- **Resume reads files, not a blob.** `run_tick(resume=True)` loads the
  checkpoint, restores the TickContext slots + position, then for each pending
  dispatch READS the file at its `output_path`. A MISSING output file returns
  `{"status": "invalid_output", "state": <name>, "reason": "missing output file:
  <path>"}` (re-dispatchable; checkpoint intact; never a crash). Otherwise the
  file content is validated with `ad.validate_output(content, schema)`; on invalid
  it returns the same `invalid_output` shape. On all valid it
  `collect_outputs(...)` -> the slot value, writes it to the ctx slot, persists
  the read product (#64), `compute_signal(signal_rule, slot_value)` -> the signal,
  `resolve_next` from the agent-state, and continues the driver until the next
  pause or the terminal. There is NO orchestrator-marshalled
  `dispatch-result.json` / `resume_dispatch` list input — it is superseded by
  file-reading.
- **Terminal.** On reaching DONE it clears `TICK_CHECKPOINT_KEY`, persists the
  per-tick ephemeral read products (#64 — only what the route produced, never a
  stale carry-forward), prints the existing one-line trace stitched across all
  segments (path/signals/work_items/work_orders/execution_plan/handoffs/mode/
  budget/route), and returns the disposition signal (a string), exactly as a
  pure-script tick does.
- **Crash-safety.** A fresh `run_tick` invocation that finds an existing
  `tick_checkpoint` (and no `resume`) re-emits the SAME PAUSED dispatch request
  (byte-identical `output_path`) from the checkpoint (idempotent — the checkpoint
  is the truth).
- The budget readiness gate (safety-governance) is evaluated at FRESH tick start
  only, NOT on resume. Read products stay #64 per-tick ephemeral; the budget
  stays a durable cross-tick fact.
- **The durable budget window is persisted on an agent-route pause and carried
  on resume (auto-maintainer-framework#123).** An agent route PAUSES at the first
  agent-state and RETURNS EARLY (the PAUSE / invalid_output return) before the
  terminal budget-persist block. So the fresh tick's rolled budget window would
  never be saved (durable `budget={}`, `/status` shows `win=` empty). To keep the
  budget a durable cross-tick fact on EVERY route, run_tick persists the budget
  window durably on the PAUSE / invalid_output early-return path too: it
  load-modify-saves ONLY `BUDGET_KEY` (preserving the checkpoint, the read
  products, and every other durable key), so the rolled window survives the
  pause. On resume the budget is REUSED (never re-rolled, per the FRESH-only
  gate): after `evaluate_budget` the resume branch carries the evaluated window
  forward (`new_budget_state = budget["budget_state"]`) so even a `{}` persisted
  value resolves to a real `{window_key, spent_tokens}` and the terminal persist
  records the durable window. A pure-script route is UNCHANGED — it already
  persists the window at the terminal.

## Trust-gate for acting agent-states (slice: effect-based trust ladder)

This slice trust-gates **acting agent-states** in `run_tick` (DESIGN §2.3 /
§3.8.2 trust ladder). It consumes `safety-governance` + `agent-dispatch`
UNCHANGED; edits live ONLY in scheduling (`run_tick.py`).

- **An acting agent-state** is an agent-adapter whose dispatch entry carries a
  truthy `effect` string (one of safety-governance's closed effect set
  `{implement, open_pr, merge, file}`, e.g. `effect: "implement"`). A
  **non-acting** agent-state (no `effect`, e.g. a TRIAGE adapter) is UNCHANGED —
  the trust-gate does NOT apply and it always pauses to dispatch.
- **Deterministic trust-gating in run_tick, never by the subagent.** Before
  pausing at an acting agent-state, `run_tick` computes
  `permitted = sg.permits(effect, mode)` (`mode` from the loaded governance).
  The decision is the deterministic lib's, not the model's.
  - **Not permitted (e.g. `dry-run`, where permits returns False for every
    effect):** `run_tick` does NOT pause and does NOT dispatch. It builds the
    per-dispatch items via `ad.build_envelopes(...)` (to know the work-order
    ids/cardinality) and synthesizes one **inert `planned` handoff** per item —
    `{"work_order_id": <id or null>, "status": "planned", "artifact":
    {"kind": "none", "ref": null}, "discovered_work": [], "blocked_reason":
    null}` — `ad.collect_outputs` them into the writes slot, writes the slot,
    `ad.compute_signal` the route signal, persists the read product, emits the
    `state_run`/`signal` events (the `state_run` detail notes `gated=dry-run`),
    and CONTINUES the driver. No PAUSE, no checkpoint, no spend, no subagent.
    This is the dry-run safety behaviour: inert planned handoffs, deterministic,
    the model never decides whether to act.
  - **Permitted (`propose` / `gated-merge`):** `run_tick` proceeds to the
    normal PAUSE-for-dispatch path unchanged, so the executor dispatches the real
    subagent.
- **isolation + description in the PAUSED dispatches.** When building the PAUSED
  `dispatches[]` (the permitted path), each dispatch record now also carries
  `isolation` (the dispatch entry's `isolation`, e.g. `"worktree"`, or null when
  absent) and `description` (the dispatch entry's `description` if present, else a
  default `f"{state} dispatch"`, or `f"{state}: {item}"` for a per_item dispatch).
  These let the executor call `Agent(subagent_type, description=...,
  prompt=..., isolation=...)`. `subagent_type`, `prompt`, `writes`,
  `output_path`, `signal_rule`, and `cardinality` are unchanged.
- **No budget pre-gate / acted-ledger / spend metering this slice.** ONLY the
  effect-based trust-gate (dry-run inert vs dispatch) + isolation/description in
  the PAUSED dispatches. Read products stay #64 per-tick ephemeral; the budget
  window persistence (#123) and the #109 journal-free checkpoint are unchanged.
  (The budget pre-gate, acted-ledger, and spend metering land in the next slice
  below.)

## Doer governance for acting agent-states (slice: acted-ledger + budget pre-gate + spend metering)

This slice completes the doer's `run_tick` governance for **acting agent-states**
(those whose dispatch entry carries a truthy `effect`, from the prior trust-gate
slice). On the PERMITTED (dispatch) path — `sg.permits(effect, mode)` True, e.g.
`propose` / `gated-merge` — it adds three things. ALL apply ONLY to acting
agent-states; a **non-acting** agent-state (TRIAGE, no `effect`), the dry-run
inert path, and pure-script routes are BYTE-IDENTICAL / unchanged. It consumes
`safety-governance` + `durable-state` + `agent-dispatch` + every sibling
UNCHANGED; edits live ONLY in scheduling (`run_tick.py`).

- **Acted-ledger (idempotency, §3.2.4).** A new durable cross-tick key
  `ACTED_LEDGER_KEY = "acted_ledger"` (a durable cross-tick fact like `BUDGET_KEY`,
  NOT a #64 read product) stores `{work_order_id: {"outcome": <status>, "ref":
  <artifact ref or null>}}`. `persisted_acted_ledger(state_path)` reads it
  (default `{}`). At an acting agent-state, when determining the per_item dispatch
  set, run_tick **filters OUT any `work_order_id` already present in the ledger**
  (already acted — never re-dispatch / never open a second PR). If, after
  filtering, NO items remain to dispatch, the state does NOT pause: it synthesizes
  an inert result (no handoffs to add), computes the route signal, and CONTINUES
  the driver. On **resume**, after collecting the handoffs, run_tick RECORDS each
  newly-acted item into the ledger: `ledger[work_order_id] = {"outcome":
  handoff["status"], "ref": handoff.get("artifact", {}).get("ref")}` and persists
  it (load-modify-save just `ACTED_LEDGER_KEY`, preserving all other durable keys).
  Only real, non-planned outcomes reach the resume record path.
- **Budget pre-gate.** At an acting agent-state on the permitted path, BEFORE
  pausing for dispatch, run_tick evaluates the budget window
  (`sg.evaluate_budget(gov, persisted_budget_state(...), budget_clock)`). If
  `allowed` is False (per-day exhausted), run_tick does NOT pause / dispatch — it
  synthesizes a **deferred** result (handoffs `status:"blocked"`, `blocked_reason`
  naming the budget exhaustion, for the not-yet-acted items), computes the signal,
  and continues — NO spend, NO dispatch. The items stay un-acted (NOT added to the
  ledger), so they retry on a later tick / next window. TRIAGE / read-only states
  are NOT budget-pre-gated — the pre-gate is acting-only.
- **Spend metering on resume.** The CLI `--resume` gains an optional `--spent
  <int>` (and a programmatic `spent` param on `run_tick(resume=True)`). On resume
  of an acting state, after applying the subagent outputs, run_tick
  `record_spend(budget_state, budget_clock, spent)` into the budget window and
  persists it. Default `spent` is 0 (back-compatible — the existing resume path is
  unchanged when no spend is metered).

## JSON tick CLI (slice: --step / --resume executor seam)

`run_tick.py` exposes a **JSON tick CLI** (`run_tick.main(argv)`, also the
`__main__` entrypoint) so the (later) executor skill can drive the yield/resume
loop deterministically. It is a THIN deterministic wrapper around the EXISTING
`run_tick(...)` structured returns (the yield/resume seam above) — it adds NO new
tick logic.

- **Bare invocation** (`python run_tick.py`, no `--step`/`--resume`) is
  UNCHANGED: it calls `run_tick()` and prints the one-line HUMAN trace to stdout,
  so existing pure-script bash callers keep working. The path flags
  (`--runtime-dir`/`--state`/`--journal`/`--project-dir`) are honored in bare
  mode too, but the output is the human trace, not JSON.
- **`--step`** runs the tick until the next pause or terminal and prints a SINGLE
  JSON object to stdout (nothing else on stdout):
  - done → `{"status":"done","signal":"<idle|halt|...>","trace":"<the one-line
    trace string>"}`
  - paused → `{"status":"paused","state":"<name>","dispatches":[{subagent_type,
    prompt,writes,output_path,signal_rule,cardinality,item?}...]}`
  - invalid_output → `{"status":"invalid_output","state":...,"reason":...}`
- **`--resume`** takes **NO file argument** — it calls `run_tick(resume=True)`,
  which reads each paused dispatch's subagent-WRITTEN output FILE at the
  checkpoint's `output_path`, and prints the SAME envelope shape (paused again,
  done, or invalid_output). The executor relays the rendered prompt to the
  subagent (which writes the JSON to `output_path`), then steps `--resume`.
- **stdout is PURE JSON** in `--step`/`--resume` mode (the skill parses stdout):
  the human trace line that `run_tick` writes to stdout is captured into the JSON
  `trace` field (and NOT leaked raw onto stdout). A pause emits no trace, so the
  paused/invalid_output envelopes carry no `trace` field.
- **Exit codes** (documented): `done`/`paused` → `0`; `invalid_output` (a bad
  agent output OR a missing output file) → `1`. A missing/invalid output file is
  surfaced as an `invalid_output` envelope (no crash/traceback), never a Python
  exception.
- **Runtime injection:** the path flags `--runtime-dir`/`--state`/`--journal`/
  `--project-dir` let tests point the CLI at a temp runtime; when omitted the CLI
  uses the production defaults (`resolve_runtime_paths`) exactly like bare mode.
  The PULL source stays the live `gh` CLI by default (`DEFAULT_PULL_SOURCE`) and
  is not a CLI flag; tests stub it by overriding `DEFAULT_PULL_SOURCE`.

## Shipped executor skill + echo proof subagent (slice: ship the tick executor)

scheduling now ships the two plugin assets that turn the agent yield/resume seam
into a usable, drop-in executor (DESIGN §3.4.6 / §2.8). The build's
`_copy_tree(ship_dir, plugin_root)` collects `ship/` verbatim, so these land in
the plugin tree with NO build change.

- **`ship/skills/tick/SKILL.md`** (`/auto-maintainer:tick`, the `tick` executor
  skill, **v0.3.0**) — drives `run_tick.py --step`/`--resume` and presses the
  `Agent` button at each agent-state: it steps the runner, and whenever the runner
  PAUSES at an agent-state, dispatches the named subagent(s) with the runner's
  rendered prompt until the tick completes. The skill decides nothing about the
  route — all tick logic (route, validation, slot writes, signal selection, spend
  metering, crash-safe checkpointing) lives in `run_tick.py`; the skill only
  relays dispatch requests.
  **Full dispatch parameters (#130 closed):** each PAUSED dispatch record carries
  a `description` (always) and, for an **acting** dispatch, an `isolation` value
  (e.g. `"worktree"`); the skill passes BOTH through to the `Agent` tool
  verbatim — `Agent(subagent_type, description=..., prompt=..., isolation=...)`,
  omitting `isolation` only when the record has none. Passing `description` closes
  auto-maintainer-framework#130 (the prior v0.2.0 dispatch omitted the required
  `description` arg, triggering an "Invalid tool parameters" self-correction).
  **Spend metering:** after each pause's dispatches finish, the skill sums the
  `subagent_tokens` each dispatch reported (a value observable only from the
  dispatch results, computable by no script) and carries it to the resume as
  `run_tick.py --resume --spent <sum>` (`0` when none reported), so the runner
  meters the spend against the durable budget window.
  **Subagent-writes-its-own-file (#100 fully closed):** the orchestrator marshals
  NO content. Each dispatched subagent WRITES its own output to the file named in
  the rendered prompt's `## Handoff` section, and `run_tick.py --resume` reads
  those subagent-written files itself from the checkpoint (the `--spent` value is
  the ONLY argument the executor adds to `--resume`). The skill therefore never
  writes the subagent output, never names a `dispatch-result.json`, and never
  hand-rolls serialization — keeping the executor's context clean no matter how
  large the output is.
- **`ship/agents/auto-maintainer-echo.md`** (the `auto-maintainer-echo` subagent,
  **v2.0.0**) — the domain-free PROOF echo agent: dispatched by `subagent_type` at
  an agent-state, for each input item it produces one accepted output and WRITES it
  to the file named in the prompt's `## Handoff` section, replying only with a short
  ack. It is **interface-protocol-free** (DESIGN §3.4.6): its `.md` is role-only —
  it bakes in NO schema, NO output path, and NO output format; the rendered prompt
  is the complete handoff contract (embedded schema + `output_path` + ack). It
  performs no real judgment — it exists to prove the agent-adapter executor
  end-to-end.
- **The echo-TRIAGE wiring is valid drop-in config.** A project-local
  `adapter-map.json` mapping `TRIAGE` to an agent-adapter entry that dispatches
  `subagent_type=auto-maintainer-echo` (`reads work_items`, `writes work_orders`,
  `signal nonempty_else_empty`, `cardinality once`), plus a route
  `GUARD→DRAIN→PULL→TRIAGE→PRIORITIZE→IMPLEMENT→PERSIST→EXIT` (TRIAGE agent;
  PRIORITIZE/IMPLEMENT script as today), is ACCEPTED by `adapter_wiring.build_loop`
  — TRIAGE resolves to an `AgentState` and data-readiness is satisfied — and runs
  end-to-end through `run_tick`'s yield/resume seam (the canned echo output applied
  on resume advances the tick past TRIAGE). No code change is required to wire it.

## Structured event log (slice: observability event emission)

`run_tick` now emits a **structured event log** (observability §3.9.1) to
`${runtime_dir}/events.jsonl` each tick — the machine-first record of "what the
loop did". It CONSUMES the `observability` lib UNCHANGED
(`observability.EventLog` + the closed `EVENT_KINDS` vocabulary): the EventLog
opens at `${runtime_dir}/events.jsonl` (the same `runtime_dir` the tick already
resolves; injectable for tests) and `append`s one JSON object per line. The
event `ts` reuses the tick's already-resolved tz-aware budget `now` (the injected
`now`), so the log is DETERMINISTIC — never an implicit wall clock; `seq` is
monotonic (observability assigns it via the file's line count, so a multi-
invocation agent tick keeps a single monotonic sequence).

Event emission is **purely additive**: it changes NO existing behaviour — the
one-line trace, signals, disposition, slot persistence, #64 read-product
ephemerality, the durable budget window, and all existing scheduling tests stay
green. The log is written ALONGSIDE the existing trace, never instead of it.

Events emitted (all members of `observability.EVENT_KINDS` — run_tick emits no
kind outside the closed vocabulary):

- **`tick_start`** — at the start of a FRESH tick (a `--step` with no checkpoint /
  not a resume). `detail` carries the route `source` (default vs
  `override:<path>`) + the trust `mode`.
- **`state_run` + `signal`** — one pair per visited non-terminal state, in order.
  For the **pure-script path** (`to.run`) they are derived from the returned
  `RunResult.path` + `RunResult.signals` after the run (one `state_run`/`signal`
  per visited non-terminal state). For the **agent-driver path** they are emitted
  inline as each SCRIPT state runs.
- **`pause` + `dispatch`** — when the tick pauses at an agent-state: one `pause`
  for the state, and a `dispatch` per dispatch entry (the `subagent_type` +
  `writes` in `detail`).
- **`resume`** — on a `--resume` invocation, naming the agent-state resumed.
- **`disposition`** — the resulting disposition (with the EXIT signal).
- **`tick_end`** — at the terminal/done, with the final signal + the four
  read-product counts (`work_items`/`work_orders`/`execution_plan`/`handoffs`) in
  `detail`.

Events are written in BOTH the pure-script done path and the agent-driver
done/pause paths. The budget readiness gate, slot persistence, and the trace are
untouched — only event emission is added alongside.

## Known gaps / deferred

- The executor (the session-side actor that performs the Agent dispatch and
  feeds `resume_dispatch` back to `run_tick`) now ships as the `tick` skill
  (`ship/skills/tick/SKILL.md`); `run_tick` itself still only emits the dispatch
  requests and applies provided results (it never calls the Agent tool).
- Configurable interval + route (config feature) — interval hardcoded to ~3 min
  for testability (auto-maintainer-framework#17).
- System-cron scheduler backend (§3.3.1) — slice 1 is in-session heartbeat only.
- TRIAGE/IMPLEMENT/VERIFY/INTEGRATE — the loop now PULLs (read-and-idle); acting
  on `work_items` lands with later features, at which point EXIT becomes
  work-driven (refire while actionable work remains).
- Full RESTART_NEEDED→SessionStart auto-resume (§3.3.4) — follow-up.

## Interfaces (composition)

- Depends on `tick-orchestrator` (run loop + resolve_next), `durable-state`
  (DRAIN/PERSIST/journal/state), `lifecycle-dispositions` (GUARD/EXIT/disposition).
- Consumes `observability` UNCHANGED (`EventLog` + `EVENT_KINDS`) to emit the
  per-tick structured event log to `${runtime_dir}/events.jsonl`.
- Declares shippable components under `ship/` for `packaging-config` to assemble
  into `plugins/auto-maintainer/`.
- Owns the `/auto-maintainer:status` skill (script-backed via `status.py`);
  `packaging-config` no longer ships a status stub. Host projects should gitignore
  the runtime dir `.auto-maintainer/`.

## Open questions

- Exact recurring-vs-one-shot scheduling shape and how `/start` records the
  active heartbeat so `/stop` can cancel it deterministically — settle in
  implementation.
