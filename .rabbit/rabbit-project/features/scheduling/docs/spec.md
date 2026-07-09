---
feature: scheduling
version: 0.29.0
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API), or when the route-config CLI (Phase 4) supersedes hand-edited route.json.
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

The route is **data**, loaded + validated + resolved by `adapter-wiring`
(§3.4.3). The shipped **default route** is the read-and-idle spine:

```
GUARD → DRAIN → PULL → PERSIST → EXIT
```

- Built-in adapters are wired via the factory convention
  (`factory(runtime) -> (manifest, run)`): scheduling provides factories for
  `GUARD`/`EXIT` (lifecycle-dispositions), `DRAIN`/`PERSIST` (durable-state),
  `PULL`/`TRIAGE` (work-intake), `PRIORITIZE` (prioritize), `IMPLEMENT`
  (implement), and `VERIFY`/`GATE`/`INTEGRATE`/`CLEANUP` (verify-integrate). The
  **default adapter-map** maps every known port (incl. `TRIAGE`, `PRIORITIZE`,
  `IMPLEMENT`, `VERIFY`, `GATE`, `INTEGRATE`, `CLEANUP`) to its factory, even
  though the default route uses a subset.
  `make_verify`/`make_gate`/`make_integrate`/`make_cleanup` wrap the
  verify-integrate states (`make_gate` delegates to `verify_integrate.make_gate`,
  the cumulative regression GATE §2.2 [v2]); `make_integrate` binds the loaded
  governance `mode` so INTEGRATE merges only at `auto-merge` (and consumes
  `safety_governance.permits` + `merge_guardrails`). The full close-the-loop
  route `… IMPLEMENT → VERIFY → REVIEW → GATE → INTEGRATE → CLEANUP → PERSIST →
  EXIT` therefore wires with NO code change (all ports pre-mapped) — a pure
  `route.json` edit.
- **Override by config, not code:** a project-local
  `${CLAUDE_PROJECT_DIR}/.auto-maintainer/route.json` (and optional
  `adapters.json`) overrides the defaults. Inserting the act-side chain
  `TRIAGE → PRIORITIZE → IMPLEMENT` (PRIORITIZE reads `work_orders` and writes
  `execution_plan`; the dry-run IMPLEMENT reads `execution_plan` and writes
  `handoffs`) is a pure route.json edit — `adapter-wiring` resolves each port
  from the map and `validate_wiring` checks it at load, and all three ports are
  pre-mapped in `DEFAULT_ADAPTER_MAP`. The dry-run IMPLEMENT is INERT (no VCS /
  filesystem effect), so the act-path tick still idles: it produces `handoffs`
  but leaves no remaining work.
- **Read-and-idle:** with only read stages `EXIT` goes **IDLE** after the route
  runs; it becomes refire/work-driven once an act stage lands.
- **Per-tick read products (#64):** the four read products
  `work_items`/`work_orders`/`execution_plan`/`handoffs` are EPHEMERAL — each tick
  they reflect ONLY what THIS tick's route produced (PULL writes `work_items`;
  TRIAGE, if routed, writes `work_orders`; PRIORITIZE, if routed, writes
  `execution_plan`; IMPLEMENT, if routed, writes `handoffs`). They are NOT carried
  forward across ticks: a route without TRIAGE reports `work_orders=0`, and a
  route without PRIORITIZE/IMPLEMENT reports `execution_plan=0 handoffs=0`, never a
  stale count. All four are surfaced in the tick trace and in `status.py` (always
  shown, including 0, #69), in the order
  `work_items work_orders execution_plan handoffs`. Durable state keeps cross-tick
  facts; the read-product snapshot is overwritten every tick.
- **Producible read-product slots are SEEDED EMPTY (skipped-state safety).**
  `_seed_context` not only registers but WRITES a schema-valid empty default for
  every producible read-product slot it registers (`work_orders`,
  `execution_plan`, `handoffs`, `verdicts`, `gate_results`,
  `integration_result`). `gate_results` (verify-integrate's GATE product) is
  seeded EMPTY when GATE is routed, so INTEGRATE reading it and a GATE-skipped
  route both stay crash-free. A route may
  SKIP a producing state via a signal branch — e.g. `VERIFY EMPTY → PERSIST`
  skips INTEGRATE/CLEANUP, or `TRIAGE EMPTY → VERIFY` skips IMPLEMENT — so that
  state never writes its slot. Without a seeded default, the terminal's
  `ctx.read(<slot>)` (run to persist the #64 snapshot) raised a `ContractError`
  and crashed the whole tick. Seeding empties makes a skipped producer persist
  its product EMPTY (the #64-correct value) instead of crashing; a state that
  DOES run overwrites its seeded empty. The empties also flow through the agent
  checkpoint/restore so a multi-resume tick is equally safe.

## Redesigned-loop reconciliation (FT-E): cross-cutting risk, advisory REVIEW, thin INTEGRATE

Reconciles scheduling with the redesigned verify-integrate + work-intake
contracts (loop redesign FT-A/B/C/D) so the close-the-loop route runs end-to-end.
verify-integrate + work-intake + safety-governance are consumed UNCHANGED; edits
live ONLY in `run_tick.py` + `adapter_map_config.py`.

- **VERIFY reads `cross_cutting_risk`, writes `cross_check` (FT-B/D §3.5.9/§3.7.6).**
  `_seed_context` registers + seeds an empty no-risk `cross_cutting_risk` default
  when TRIAGE **or** VERIFY is routed (a VERIFY-without-TRIAGE route still has the
  slot to read), and an empty `cross_check` (`ran=False`) when VERIFY is routed.
  `cross_cutting_risk` is in the data-readiness `initial` set. Without it a TRIAGE
  or VERIFY tick crashed with `ContractError("slot 'cross_cutting_risk' is not
  registered")`.
- **REVIEW is ADVISORY — `review_verdicts` retired for `review_findings` (FT-C
  §3.7.7).** REVIEW reads `verdicts` and writes the advisory `review_findings`
  slot (DiscoveredIssue-conforming records with stable `dedup_key`s). REVIEW is
  NO LONGER a merge gate. `_seed_context` seeds `review_findings` only when REVIEW
  is routed; `review_verdicts` is no longer seeded/mapped/persisted.
  `persisted_review_findings` replaces `persisted_review_verdicts`;
  `REVIEW_VERDICTS_KEY` -> `REVIEW_FINDINGS_KEY`.
- **Advisory REVIEW ALWAYS flows to INTEGRATE (`always_ok`).** Because REVIEW is
  advisory, it must NEVER branch the loop's flow on how many findings it produced.
  Its `AGENT_PORT_TEMPLATES['REVIEW']['signal_rule']` is `always_ok` (the
  closed-vocabulary rule in `agent_dispatch.compute_signal` that returns `OK` for
  any slot value), so a clean review (ZERO `review_findings`) emits `OK` and
  continues to INTEGRATE rather than EMPTY-branching past it. The default
  `make_review` no-op likewise emits `OK` while still writing an EMPTY
  `review_findings` list. REVIEW's declared `emits` stay `[OK, EMPTY]`
  (verify-integrate's `REVIEW_MANIFEST`, UNCHANGED) so a seeded route that still
  carries a `REVIEW--EMPTY-->PERSIST` edge keeps passing `validate_signals` —
  that EMPTY edge is now valid-but-dead, and no `route.json` change is required.
  `migrate_known_port_entries` re-derives a stale REVIEW entry from this template,
  so a healed entry carries `signal_rule='always_ok'` automatically.
- **INTEGRATE is a THIN merge (FT-C/D).** Reads ONLY `verdicts`; merges each `ok`
  verdict's PR via `sg.permits('merge', mode)` + `merge_guardrails` with NO
  review-approval read (the merge rests on IMPLEMENT's gate + VERIFY + guardrails
  + the trust ladder). An ok PR merges at `auto-merge` even with no findings;
  `propose` records the would-merge intent under `skipped`. The `make_integrate`
  factory binding is unchanged.
- **`review_findings` flush through REPORT.** At the terminal the flush gathers
  the `review_findings` slot as an ADDITIONAL discoveries source into
  `_gather_discoveries`/`_flush_report`, beside `handoffs[].discovered_work`.
  Being DiscoveredIssue-conforming, they file via `wi.file_discoveries` on the
  SAME journaled-idempotency + dedup-vs-open (`known_open=work_items`) path — NOT
  a parallel mechanism. `AGENT_PORT_TEMPLATES['REVIEW']` writes `review_findings`.

## Governance wiring (slice 1: load + surface + persist)

scheduling consumes `safety-governance` UNCHANGED to make the maintainer loop
governance-aware. This slice **loads + surfaces + persists** governance state;
enforcement of act-skip is **deferred** to the acting doer (next milestone).

- **Load once per tick.** `run_tick` calls `sg.load_config(project_dir)`
  (project-local `${project_dir}/.auto-maintainer/config.json`, else the
  documented defaults; `load_governance` remains a thin alias) and threads the
  loaded config into the factory `runtime` dict under a `governance` key — so
  acting adapters can consult `permits`/budget — without disturbing the existing
  runtime keys (`project_dir`/`runtime_dir`/`source`/`now`). It also READS
  `backoff.threshold` (default 5) from this config for the backoff gate, and the
  `/start` heartbeat reads `heartbeat.interval_minutes` (default 3) for its
  cadence (both owned by safety-governance, consumed here via the contract).
- **Durable, cross-tick budget window.** A durable key `budget` stores
  `{window_key, spent_tokens}`. Each tick resolves a tz-aware `now` (the injected
  `now` when tz-aware, else the host local-aware `datetime.now().astimezone()`),
  loads the prior budget state (default `{}`), calls
  `sg.evaluate_budget(gov, budget_state, now, tick_spend=<injected, default 0>)`,
  and PERSISTS the returned `budget_state`. The lib performs the window rollover /
  auto-resume: a `now` on a later local day advances `window_key` and resets
  `spent_tokens`. `tick_spend` is `0` in production; tests inject it. The budget
  window is a durable CROSS-TICK fact, **not** a per-tick ephemeral read product
  (#64): a tick within the same window carries accumulated spend forward — only a
  window rollover resets it.
- **Surface in the trace AND status.py** (always shown, #69 style): `mode=<mode>`
  and a compact `budget=<spent>/<ceiling-or-"none"> win=<window_key>` field
  (`none` = a null/unlimited per_day ceiling), placed after the existing fields.
  When `evaluate_budget` returns `allowed=False`, a `budget_paused=<reason>`
  indicator is appended (e.g. `budget_paused=per_day_exhausted`).
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
   defines the built-in adapter **factories** + the embedded conservative
   `DEFAULT_ROUTE` and `DEFAULT_ADAPTER_MAP` fallbacks, RESOLVES the active
   default by reading the shipped `default-config/route.json` / `adapter-map.json`
   FRESH when present (else the embedded constant; §"Default-config resolution"),
   then calls `adapter_wiring.build_loop(<default_route>, <default_map>, runtime,
   …)` to load (project-local override else that default)
   → resolve → validate → `(route, states)`, runs `tick_orchestrator.run(...)`
   over a `TickContext` seeded from durable state, prints a tick trace (tick
   number, state path, `work_items`/`work_orders` counts, resulting disposition,
   and the **route source** — `default` vs the project-local override path, #59),
   and returns/persists the outcome. One invocation = one tick. `PULL`'s issue
   source is the live `gh` CLI in production but **injectable** so tests pass a
   stub (no network).
2. **PULL integration (read-and-idle)** — the route uses work-intake's `PULL`
   state (writes `work_items`). After PULL + PERSIST, `EXIT` selects **IDLE** (no
   act stage yet), so the heartbeat re-pulls next interval rather than the loop
   busy-firing.
3. **Control scripts** (deterministic, script-tier — spec-rules §1):
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
     lives in ONE place shared by both modes.
   - `src/status.py` — reads the disposition marker + the persisted `work_items`
     count (via `run_tick`'s `resolve_runtime_paths`), the **route source**
     (`default` vs the project-local `route.json` override path, #59), and the
     real loop status.
   - `src/stop.py` — writes disposition `STOPPED` (via the lifecycle-dispositions
     API) using the same runtime-path resolution. Owns the state write.
   These own ALL state operations so the skills never hand-roll Python.
4. **Shipped control skills** (`ship/skills/{start,stop,status}`):
   - `/auto-maintainer:start` (skill **v0.3.0**) — first invokes
     `start.py --clear-only` to perform ONLY the FRESH-start latch decision (clear
     a latched `STOPPED` → `IDLE`, or REFUSE on `ABORTED` and stop), then runs
     tick #1 **through the executor** by invoking the `/auto-maintainer:tick`
     skill — NOT `start.py`'s in-process `run_tick` — so an AGENT route's
     agent-state dispatches are fulfilled (DESIGN §2.8 in-session executor model).
     It then schedules a recurring heartbeat as a **prompt** job (so the
     session is present to fulfill agent dispatches) whose prompt fires the
     `/auto-maintainer:tick` executor each interval — NOT a bare `run_tick.py`
     command, which cannot dispatch agent-states. The latch is cleared ONCE at
     start; the heartbeat does not re-clear it (re-clearing each interval would
     defeat a `/stop` that lands between heartbeats). **The interval is
     config-driven** — `start.py` emits the configured `heartbeat.interval_minutes`
     (default 3, from the central config) and the `/start` skill schedules at that
     cadence. The in-session heartbeat ends with the session, but the loop is
     **durable across sessions** (§3.3.2, #31): `start.py` records a durable
     **loop-intent** marker (via `heartbeat.py`) when it clears the latch, and the
     plugin's `SessionStart` auto-resume hook re-arms the heartbeat on the next
     session (point 6 below). `start.py` records the intent but does NOT clear the
     cross-session resume-dedup, so a second `SessionStart` in the same session
     cannot re-arm a duplicate heartbeat.
   - `/auto-maintainer:stop` — invokes `stop.py` (latch STOPPED **and clear the
     durable loop-intent**, so the next session's `SessionStart` hook does not
     auto-resume) then cancels the heartbeat (CronDelete).
   - `/auto-maintainer:status` — invokes `status.py` and reports the real
     disposition + last-pull `work_items` count.
   Only the heartbeat scheduling (CronCreate/CronDelete) is agent-mediated (no
   plugin-level cron API); every state operation is a script.
5. **Scheduler detection (§3.3.1)** — slice 1 uses the in-session durable
   heartbeat; system-cron detection is stubbed/deferred.
6. **Durable heartbeat + SessionStart auto-resume (§3.3.2, #31).** The
   warm-only heartbeat is made durable across sessions by a durable **loop-intent**
   marker + a `SessionStart` hook, owned by **`heartbeat.py`**:
   - `loop-intent` (`running`) is set by `/start` and cleared by `/stop`; it is
     the durable bit that survives a session ending. `last-resume-session` is the
     at-most-one-refire dedup across sessions.
   - The pure decision `heartbeat.should_auto_resume(runtime_dir, session_id)` is
     True only when intent is `running`, the disposition is **not** latched
     `STOPPED`/`ABORTED` and **not** owed `RESTART_NEEDED`, and this session has
     not already armed.
   - The shipped `SessionStart` hook (`session-start-resume.py`, registered in
     `hooks.json` alongside the persona hook) asks that decision and, when True,
     stamps the dedup and emits `additionalContext` telling the session to re-run
     `/auto-maintainer:start` to re-arm the in-session heartbeat — at most once per
     session. **The dedup is owned by the SessionStart path, not `/start`**: the
     hook is what asks the session to run `/start`, so `start.py` must NOT clear
     the dedup (else a second `SessionStart` in the same session — SessionStart
     fires on startup / resume / `/clear` / compact — would re-arm a duplicate
     heartbeat). A hook never breaks the session (any error → silent).
   - A latched `RESTART_NEEDED` **blocks** auto-resume (the safe, conservative
     gate — the loop is never silently re-armed). This is NOT the full
     §3.3.4 `RESTART_NEEDED`→`SessionStart` resume-**drive** flow (which would
     proactively resume after a restart); that remains **deferred**. On restart,
     the next tick's DRAIN + durable state still resume the loop.

> Tool-tier note (spec-rules §1): the deterministic tick logic is a **script**
> (`run_tick.py`); the act of *scheduling the wake* is necessarily
> **agent-mediated** (Claude Code exposes no plugin-level cron API), so the
> `/start` skill body instructs the session scheduler. This seam is inherent to
> the platform constraint in §3.3.1.

## What you'll see (installed plugin)

`/auto-maintainer:start` → tick #1 pulls the repo's open issues into `work_items`
(trace shows the count), PERSISTs them, and EXITs **IDLE**. Every
`heartbeat.interval_minutes` (default 3) the heartbeat re-pulls the current open
issues. `/auto-maintainer:status` shows the disposition + last-pull count.
`/stop` latches STOPPED + cancels the heartbeat.

## Current behaviour

Implemented and merged (`tdd_state: test-green`). The tick-runner runs the real
route `GUARD→DRAIN→PULL→PERSIST→EXIT` (read-and-idle); the script-backed
`status.py`/`stop.py` and the `/auto-maintainer:start`/`:stop`/`:status` ship
skills compose work-intake's PULL with the loop core. Live-validated in an
installed plugin session. See `feature.json`.

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
  executor's job. The pause does NOT record any intent in the durable-state tick
  journal: that journal is the counter-reconciliation ledger (`drain_run` reads
  `target_counter` from every unconfirmed intent), and an agent dispatch never
  touches the counter. The durable checkpoint alone carries the paused dispatch,
  so journaling it as well would poison the counter journal — an agent-dispatch
  intent has no `target_counter` and is never confirmed, so it would survive into
  the NEXT tick's DRAIN and crash it with `KeyError: 'target_counter'`
  (auto-maintainer-framework#109). The agent driver therefore writes ONLY the
  checkpoint, never a journal intent.
- **File-based context isolation (DESIGN §3.4.6).** The subagent's output is a
  WRITTEN FILE, never marshalled back through the orchestrator. `run_tick`
  resolves `output_dir = ${runtime_dir}/dispatch-out/` (created `mkdir -p`) and
  passes it to `ad.build_envelopes(entry, slot_values, {tick_id, mode},
  state=<name>, output_dir=output_dir)`. Each envelope carries an
  `output_contract{slot, schema, output_path}` and its rendered `## Handoff`
  section names the `output_path` and mandates writing the JSON output there. The
  orchestrator marshals NO content; the file is the sole handoff.
- **The `output_dir` MUST be ABSOLUTE (auto-maintainer-framework#143).**
  `run_tick` absolutizes it
  (`output_dir = os.path.abspath(os.path.join(runtime_dir, "dispatch-out"))`) so
  every dispatch's `output_path` is an absolute path resolved identically no
  matter the subagent's cwd.
- **Acting file-handoff agents self-isolate; NO template declares harness
  `isolation: "worktree"` (auto-maintainer-framework#335).** The file-based
  handoff REQUIRES the subagent to write its output JSON into the SHARED
  main-workspace `dispatch-out/`. An acting agent (e.g. IMPLEMENT) already
  isolates its code changes in its OWN git worktree that it creates and removes
  per its agent contract, and it runs with its cwd = the main workspace so the
  handoff lands where the orchestrator reads it. Declaring harness
  `isolation: "worktree"` on such an agent instead relocates its cwd OFF the
  main workspace, and the handoff then never reaches `dispatch-out/` — the
  orchestrator reads a MISSING file and re-dispatches, re-running the act (a
  second PR / a second issue-close). Therefore `AGENT_PORT_TEMPLATES` entries
  declare NO `isolation` (the default resolves to `None`), acting agents provide
  their own worktree isolation, and the adapter-wiring layer REJECTS any agent
  dispatch that declares both a file-based handoff and harness
  `isolation: "worktree"`. Non-isolated dispatches (e.g. TRIAGE) likewise run
  with cwd = the main workspace.
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
  "state": <agent-state name>, "dispatches": [ {"subagent_type", "prompt_path"
  (a FILE holding the rendered envelope), "writes", "output_path", "signal_rule",
  "cardinality", "item"? } ... ]}`. A `once` dispatch yields one dispatch with no
  `item`; a `{per_item: <path>}` dispatch yields one dispatch per resolved
  element, each carrying its `item` and its own distinct `output_path`. The
  rendered invocation envelope is delivered by **file reference** (`prompt_path`),
  NOT inline — see "File-referenced dispatch prompt" below.
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
  never be saved. To keep the budget a durable cross-tick fact on EVERY route,
  run_tick persists the budget window durably on the PAUSE / invalid_output
  early-return path too: it load-modify-saves ONLY `BUDGET_KEY` (preserving the
  checkpoint, the read products, and every other durable key), so the rolled
  window survives the pause. On resume the budget is REUSED (never re-rolled, per
  the FRESH-only gate): after `evaluate_budget` the resume branch carries the
  evaluated window forward (`new_budget_state = budget["budget_state"]`) so even a
  `{}` persisted value resolves to a real `{window_key, spent_tokens}` and the
  terminal persist records the durable window. A pure-script route is UNCHANGED.

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
  - **Permitted (`propose` / `auto-merge`):** `run_tick` proceeds to the
    normal PAUSE-for-dispatch path unchanged, so the executor dispatches the real
    subagent.
- **isolation + description in the PAUSED dispatches.** Each PAUSED dispatch
  record (the permitted path) carries `isolation` (the entry's `isolation`, e.g.
  `"worktree"`, or null) and a `description`, so the executor can call
  `Agent(subagent_type, description=..., prompt=..., isolation=...)`.
  `subagent_type`, `prompt`, `writes`, `output_path`, `signal_rule`, and
  `cardinality` are unchanged. The `description` NAMES the issue/PR each subagent
  works on so parallel subagents (an IMPLEMENT per-item fan-out, a REVIEW once
  dispatch over several PRs) show DISTINCT names, not one generic
  `<state> dispatch`. The pure `_dispatch_description(dispatch_entry, name, env)` +
  `_dispatch_refs(env)` derive it, branching on cardinality:
  - **per_item** (`env` carries `item`, e.g. `wo-owner/repo#275`) → ALWAYS name
    the item ref so parallel per-item subagents are DISTINCT. The base is the
    explicit `dispatch_entry['description']` when present, else the state name;
    the item ref (`#` + the substring after the LAST `#`; a dict item prefers
    `pr_ref`/`number`/`id`) is APPENDED → `auto-maintainer implement #275`
    (explicit base) / `IMPLEMENT #275` (name base). When the item yields NO
    derivable ref → the explicit description verbatim when present, else
    `f"{name} dispatch"`. The explicit description is a PREFIX, never the
    verbatim whole, on the per_item path (auto-maintainer-framework#280).
  - **once** (no `item`) → (0) an explicit `dispatch_entry['description']` wins
    verbatim; else scan `env['inputs']` list-of-dicts for `pr_ref` (REVIEW
    verdicts) or `number` (TRIAGE work_items) → `REVIEW #276, #277` /
    `TRIAGE #275, #276` (de-duped, order-preserving, capped at six with
    `+K more`); else the `f"{state} dispatch"` fallback.
- **No budget pre-gate / acted-ledger / spend metering this slice.** ONLY the
  effect-based trust-gate (dry-run inert vs dispatch) + isolation/description in
  the PAUSED dispatches. Read products stay #64 per-tick ephemeral; the budget
  window persistence (#123) and the #109 journal-free checkpoint are unchanged.

## Doer governance for acting agent-states (slice: acted-ledger + budget pre-gate + spend metering)

This slice completes the doer's `run_tick` governance for **acting agent-states**
(those whose dispatch entry carries a truthy `effect`). On the PERMITTED
(dispatch) path — `sg.permits(effect, mode)` True, e.g. `propose` / `auto-merge`
— it adds three things. ALL apply ONLY to acting agent-states; a **non-acting**
agent-state (TRIAGE, no `effect`), the dry-run inert path, and pure-script routes
are BYTE-IDENTICAL / unchanged. It consumes `safety-governance` + `durable-state`
+ `agent-dispatch` + every sibling UNCHANGED; edits live ONLY in scheduling
(`run_tick.py`).

- **Acted-ledger (idempotency, §3.2.4).** A durable cross-tick key
  `ACTED_LEDGER_KEY = "acted_ledger"` (a durable cross-tick fact like `BUDGET_KEY`,
  NOT a #64 read product) stores `{work_order_id: {"outcome": <status>, "ref":
  <artifact ref or null>, "acted_at_updated_at": <issue updated_at at act time or
  null>}}`. `persisted_acted_ledger(state_path)` reads it (default `{}`). At an
  acting agent-state, when determining the per_item dispatch set, run_tick
  **filters OUT any `work_order_id` already present in the ledger** (already acted —
  never re-dispatch / never open a second PR), EXCEPT a work order that RE-ENTERS
  (see below). If, after filtering, NO items remain to dispatch, the state does NOT
  pause: it synthesizes an inert result (no handoffs to add), computes the route
  signal, and CONTINUES the driver. On **resume**, after collecting the handoffs,
  run_tick RECORDS each newly-acted item into the ledger: `ledger[work_order_id] =
  {"outcome": handoff["status"], "ref": handoff.get("artifact", {}).get("ref"),
  "acted_at_updated_at": <the source issue's current updated_at>}` and persists it
  (load-modify-save just `ACTED_LEDGER_KEY`, preserving all other durable keys).
  Only real, non-planned outcomes reach the resume record path.
- **Acted-ledger re-entry (§3.8.5-symmetric leak fix,
  auto-maintainer-framework#204).** A still-valid issue whose auto-PR a human
  CLOSED (rejecting the work) but left OPEN must not be abandoned forever — that is
  a leak, the same class as the `blocked`-leak. So an already-`opened` work order
  RE-ENTERS the per_item dispatch set (its acted-ledger entry CLEARED, the item
  re-dispatched), beside the existing backoff skip-deferred-unchanged check, when
  BOTH: (a) the source issue's current `updated_at` has ADVANCED past
  `acted_at_updated_at`, AND (b) its recorded PR `ref` is **CLOSED-AND-NOT-MERGED**
  (queried via the **injectable** `gh_pr_state_source` / `DEFAULT_PR_STATE_SOURCE`,
  mirroring verify-integrate's `gh_open_pr_source`; `run_tick(pr_state_source=...)`
  overrides it). It stays **LOCKED** otherwise: merged (done), still-open PR
  (pending review), or closed-but-issue-unchanged (the human closed it without a
  redo — respect it, no thrash). The PR is queried ONLY for entries whose
  `updated_at` advanced (bounds the `gh` calls to changed issues); a
  raising/malformed source stays locked and never crashes the tick. Net: **close
  an auto-PR + update the issue -> the loop re-attempts with the new guidance;
  close it + touch nothing -> the loop leaves it alone**.
- **Budget pre-gate.** At an acting agent-state on the permitted path, BEFORE
  pausing for dispatch, run_tick evaluates the budget window
  (`sg.evaluate_budget(gov, persisted_budget_state(...), budget_clock)`). If
  `allowed` is False (per-day exhausted), run_tick does NOT pause / dispatch — it
  synthesizes a **deferred** result (handoffs `status:"blocked"`, `blocked_reason`
  naming the budget exhaustion, for the not-yet-acted items), computes the signal,
  and continues — NO spend, NO dispatch. The items stay un-acted (NOT added to the
  ledger), so they retry on a later tick / next window. TRIAGE / read-only states
  are NOT budget-pre-gated — the pre-gate is acting-only.
- **Spend metering on resume (ALL agent-state resumes, not just acting).** The
  CLI `--resume` gains an optional `--spent <int>` (and a programmatic `spent`
  param on `run_tick(resume=True)`). On resume of **any** agent-state, after
  applying the subagent outputs, run_tick `record_spend(budget_state,
  budget_clock, spent)` into the budget window and persists it. The budget is a
  **token ceiling over ALL model spend in the loop** (DESIGN §3.8.4), so a
  non-acting **TRIAGE** resume's spend is metered too, not only the acting doer's.
  The executor passes the per-pause summed `subagent_tokens` on each `--resume`, so
  each resume records its own pause's spend with NO double-counting (a tick with a
  TRIAGE pause then an IMPLEMENT pause records both, cumulatively). Default
  `spent` is 0 (back-compatible). NOTE: spend metering (counting model usage) is
  distinct from the acting-only **budget pre-gate** above (which gates only acting
  dispatches from STARTING when the window is already exhausted).

## File-referenced dispatch prompt (slice: prompt-by-file-reference)

The dispatch's rendered invocation envelope is delivered by **file reference**
(`prompt_path`), symmetric with the already-file-based subagent OUTPUT. The PAUSE
previously emitted each dispatch's rendered envelope INLINE as
`dispatches[].prompt` (`ad.render(env)`) in the `--step` stdout JSON; for a large
envelope (an IMPLEMENT dispatch with the work-order body + issue comments) stdout
was truncated, forcing the orchestrator to RE-READ the whole output to relay the
prompt. The INPUT is now symmetric with outputs.

- **`_pause_result` writes the rendered envelope to a file.** For each dispatch it
  WRITES `ad.render(env)` to a deterministic ABSOLUTE file under `output_dir`,
  named parallel to `output_path` (`<state>-<di>-<ii>.json` →
  `<state>-<di>-<ii>.prompt.md`), sets `prompt_path`, and DROPS the inline
  `prompt` — the orchestrator receives only the path. It is written at the PAUSE
  (where stale OUTPUT files are cleared) and OVERWRITTEN each emit, so a
  crash-safety re-emit reproduces the SAME `prompt_path` with byte-identical
  content (rendered from the durable checkpoint round-trip, like `output_path`).
- **The `--step`/`--resume` JSON dispatch entries carry `prompt_path`** (a small
  absolute path) and NOT the multi-KB inline prompt, so stdout stays small (no
  truncation). All other fields (`subagent_type`, `description`, `isolation`,
  `output_path`, `writes`, `cardinality`, `item`) are UNCHANGED.
- **The envelope CONTENT is unchanged** — only its delivery (file vs inline)
  changes; `ad.render` and the subagent agent `.md` files are untouched. The
  executor dispatches each entry with a SHORT reference telling the subagent its
  envelope is the file at `prompt_path`, to read it IN FULL and follow it
  literally. The orchestrator does NOT open/read `prompt_path`; the SUBAGENT
  reads it (symmetric with outputs).

## JSON tick CLI (slice: --step / --resume executor seam)

`run_tick.py` exposes a **JSON tick CLI** (`run_tick.main(argv)`, also the
`__main__` entrypoint) so the executor skill can drive the yield/resume loop
deterministically. It is a THIN deterministic wrapper around the EXISTING
`run_tick(...)` structured returns — it adds NO new tick logic.

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
    prompt_path,writes,output_path,signal_rule,cardinality,item?}...]}`
    (the rendered envelope is delivered by FILE REFERENCE via `prompt_path`, NOT
    inline — so `--step` stdout stays small and is never truncated)
  - invalid_output → `{"status":"invalid_output","state":...,"reason":...}`
- **`--resume`** takes **NO file argument** — it calls `run_tick(resume=True)`,
  which reads each paused dispatch's subagent-WRITTEN output FILE at the
  checkpoint's `output_path`, and prints the SAME envelope shape (paused again,
  done, or invalid_output). The executor dispatches the subagent with a SHORT
  reference to the dispatch's `prompt_path` (the subagent reads the envelope file
  itself and writes the JSON to `output_path`), then steps `--resume`.
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

scheduling ships the two plugin assets that turn the agent yield/resume seam into
a usable, drop-in executor (DESIGN §3.4.6 / §2.8). The build's
`_copy_tree(ship_dir, plugin_root)` collects `ship/` verbatim, so these land in
the plugin tree with NO build change.

- **`ship/skills/tick/SKILL.md`** (`/auto-maintainer:tick`, the `tick` executor
  skill, **v0.4.0**) — drives `run_tick.py --step`/`--resume` and presses the
  `Agent` button at each agent-state: it steps the runner, and whenever the runner
  PAUSES at an agent-state, dispatches the named subagent(s) with the runner's
  rendered prompt until the tick completes. The skill decides nothing about the
  route — all tick logic (route, validation, slot writes, signal selection, spend
  metering, crash-safe checkpointing) lives in `run_tick.py`; the skill only
  relays dispatch requests.
  **Step/resume protocol (v0.4.0).** The advance rule is unambiguous: `--step` is
  called EXACTLY ONCE (the first runner command of the tick), the advance after
  EVERY dispatch is `--resume`, and `--step` is used again ONLY to re-emit a pause
  after an `invalid_output`. Running `--step` mid-tick (instead of `--resume`)
  after a dispatch skips the resume that applies the subagent's output and fires
  the terminal REPORT flush — so discoveries go unfiled (`reported=0/0`); the
  runner's idempotency absorbs the rest (no duplicate PR), but the missed resume
  is the hazard the v0.4.0 wording eliminates.
  **Full dispatch parameters (#130 closed):** each PAUSED dispatch record carries
  a `description` (always) and, for an **acting** dispatch, an `isolation` value
  (e.g. `"worktree"`); the skill passes BOTH through to the `Agent` tool
  verbatim — `Agent(subagent_type, description=..., prompt=..., isolation=...)`,
  omitting `isolation` only when the record has none.
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
  exists to prove the agent-adapter executor end-to-end.
- **The echo-TRIAGE wiring is valid drop-in config.** A project-local
  `adapter-map.json` mapping `TRIAGE` to an agent-adapter entry that dispatches
  `subagent_type=auto-maintainer-echo` (`reads work_items`, `writes work_orders`,
  `signal nonempty_else_empty`, `cardinality once`), plus a route
  `GUARD→DRAIN→PULL→TRIAGE→PRIORITIZE→IMPLEMENT→PERSIST→EXIT` (TRIAGE agent;
  PRIORITIZE/IMPLEMENT script as today), is ACCEPTED by `adapter_wiring.build_loop`
  — TRIAGE resolves to an `AgentState` and data-readiness is satisfied — and runs
  end-to-end through `run_tick`'s yield/resume seam. No code change is required.

## Structured event log (slice: observability event emission)

`run_tick` emits a **structured event log** (observability §3.9.1) to
`${runtime_dir}/events.jsonl` each tick — the machine-first record of "what the
loop did". It CONSUMES the `observability` lib UNCHANGED
(`observability.EventLog` + the closed `EVENT_KINDS` vocabulary): the EventLog
opens at `${runtime_dir}/events.jsonl` (the same `runtime_dir` the tick already
resolves; injectable for tests) and `append`s one JSON object per line. The
event `ts` reuses the tick's already-resolved tz-aware budget `now`, so the log
is DETERMINISTIC — never an implicit wall clock; `seq` is monotonic
(observability assigns it via the file's line count, so a multi-invocation agent
tick keeps a single monotonic sequence).

Every event carries a **per-tick `tick_id`** that DISCRIMINATES ticks (#112). It
is a durable monotonic counter (`tick_id_counter` in durable state, SEPARATE from
the work `counter` — a read-only route never bumps the work counter, so it cannot
distinguish read-only ticks). The id is assigned ONCE, at FRESH tick start
(incrementing the counter; the first tick is `1`, never `0`), and carried through
the tick via the durable checkpoint: it is stamped into `TICK_CHECKPOINT_KEY` at a
PAUSE and READ BACK on a `--resume` / crash-safety re-emit. It is therefore (1)
DISTINCT across ticks AND (2) STABLE across a single tick's `--step` → `--resume`
— on ALL routes including the agent route, where `--step` and `--resume` are
separate processes that inject no `now`, so the id is deliberately NOT derived
from the wall clock/`now`.

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
  `RunResult.path` + `RunResult.signals` after the run. For the **agent-driver
  path** they are emitted inline as each SCRIPT state runs.
- **`pause` + `dispatch`** — when the tick pauses at an agent-state: one `pause`
  for the state, and a `dispatch` per dispatch entry (the `subagent_type` +
  `writes` in `detail`).
- **`resume`** — on a `--resume` invocation, naming the agent-state resumed.
- **`disposition`** — the resulting disposition (with the EXIT signal).
- **`tick_end`** — at the terminal/done, with the final signal + the four
  read-product counts (`work_items`/`work_orders`/`execution_plan`/`handoffs`) in
  `detail`, the REPORT counts (`reported_filed`/`reported_skipped`/
  `reported_errored`), the INTEGRATE merge results (`merged` count,
  `integrate_skipped` count, `integrate_errored` count, and `merged_refs` — the
  list of merged `pr_ref`s, from the `integration_result` read product), and a
  `refire` boolean disambiguating idle-because-no-work (`false`) from
  refire-because-work-remains (`true`). The one-line trace also gains a compact
  `merged=<n>` token (plus `integrate_errored=<n>` only when `> 0`). All existing
  detail keys + trace fields are preserved; a route with no INTEGRATE shows
  `merged=0`/`merged_refs=[]`.

Events are written in BOTH the pure-script done path and the agent-driver
done/pause paths. The budget readiness gate, slot persistence, and the trace are
untouched — only event emission is added alongside.

## Outbound REPORT flush (slice: discovered_work → tracker)

`run_tick` flushes the tick's discoveries through `work-intake`'s REPORT port
(DESIGN §1.3, §3.11) so follow-on work the doer surfaces becomes durably-tracked
items instead of being dropped. REPORT is **out-of-band — NOT a routed state**:
the flush runs at the tick TERMINAL (the `done` path), AFTER the route completed
and the read products are known, on BOTH the pure-script and agent-driver done
paths. It consumes `work_intake` (`DiscoveredIssue` / `file_discoveries` /
`gh_issue_file_sink`) and `safety_governance` UNCHANGED.

- **Sources of discoveries.** The flush gathers raw discovery dicts from (a) the
  tick's `handoffs` — each handoff's `discovered_work[]` — and (b) an optional
  per-tick `discoveries` slot on `TickContext` any state may append to. v1's
  primary source is `handoffs.discovered_work`.
- **Normalization.** Each raw dict is completed into a `DiscoveredIssue`
  (work_intake schema): `filed_by="autonomous-maintainer"`; `target` defaults to
  `project`; `kind` defaults to `task`; missing `dedup_key` is DERIVED
  deterministically (a stable hash of `title`+`body`, optionally prefixed by the
  source `work_order_id`) so the same discovery always yields the same key.
- **Durable idempotency ledger.** A durable cross-tick key
  `REPORT_LEDGER_KEY = "report_ledger"` (a durable fact like `ACTED_LEDGER_KEY` /
  `BUDGET_KEY`, NOT a #64 read product) maps `{dedup_key: {tracker_ref, url}}`.
  `persisted_report_ledger(state_path)` reads it (default `{}`). Discoveries whose
  `dedup_key` is already a ledger key are skipped (filed in a prior tick — never
  re-file). The flush passes the ledger keys as `file_discoveries`'
  `known_dedup_keys`, and after filing RECORDS each `filed` entry back into the
  ledger (load-modify-save just `REPORT_LEDGER_KEY`, preserving all other durable
  keys). This is the journaled-idempotency guarantee (§3.11.4): a refire / DRAIN
  replay never files a duplicate.
- **Trust gate (§3.11.7).** Filing is the `file` effect. The flush computes
  `sg.permits("file", mode)`: at `dry-run` it does NOT file — it logs the intent
  (the would-file count) and leaves the ledger untouched so a later armed tick
  files them; at `propose`/`auto-merge` it files via `file_discoveries`.
- **Injectable sink seam.** `DEFAULT_REPORT_SINK = work_intake.gh_issue_file_sink`
  (mirrors `DEFAULT_PULL_SOURCE`); tests override it with a stub so no network.
  The sink's destination repo is resolved per `DiscoveredIssue.target`:
  `project` → the project repo (gh default); `maintainer-self` → the **fixed**
  `safety_governance.MAINTAINER_REPO` (the upstream maintainer repo) — **never**
  the project repo, with **no fallback** (§3.11.6). The former
  `governance.maintainer_repo` config field is gone.
- **Surfacing — including ERRORS (never silent).** The trace and `status.py` show
  `reported=<filed>/<skipped>` (always, #69 style), and when any discovery FAILED
  to file the trace appends `report_errors=<n>`; the `tick_end` event `detail`
  carries `reported_filed` / `reported_skipped` / `reported_errored`.
  `_flush_report` returns the errored count too (from `ReportResult.errors`). A
  filing error (e.g. a missing tracker label) is caught into `ReportResult.errors`
  but must not look like "no discoveries", so filing errors are now visible. (NO
  new event kind — the terminal `tick_end` carries the errored count.)
- **Unchanged.** A tick with NO discoveries flushes nothing (`reported=0/0`) and
  is otherwise byte-identical. Read products stay #64 per-tick ephemeral; the
  REPORT ledger + budget window + acted-ledger are the durable cross-tick facts.

## Loopback opt-out wiring — PULL honors `work_own_filings` (§3.11.5)

`make_pull(runtime)` threads safety-governance's §3.11.5 loopback toggle into
work-intake's `Pull`: it constructs
`wi.Pull(source=source, work_own_filings=sg.work_own_filings(runtime['governance']))`.
`runtime['governance']` is the per-tick loaded central config; `work_own_filings`
defaults `True` when absent. The injectable `source` binding is unchanged.
safety-governance + work-intake are consumed UNCHANGED — the edit lives ONLY in
`make_pull`. Default / explicit-`true` → the bound `Pull` INCLUDES loop-filed
items (the loop works its own discoveries). An explicit `work_own_filings: false`
config → the bound `Pull` EXCLUDES loop-filed items at PULL so they stay open for
human triage (the exclusion logic is work-intake's, consumed unchanged).

## Backoff: bounded-retry → escalate → defer for blocked work orders (§3.8.5)

A valid work order the doer reports `blocked` must be **worked toward an end, not
silently leaked, and must never halt the loop** (DESIGN §3.8.5). This slice adds
that to `run_tick`'s acting-state governance. It consumes `observability`
(`escalate`) UNCHANGED; edits live ONLY in scheduling.

- **The leak fix.** `_record_acted_ledger` records ONLY *completed* outcomes
  (`opened` / `closed`) into the acted-ledger — **never `blocked`**. Previously a
  blocked work order was written to the acted-ledger and then filtered out as
  "already acted" forever (silent leak). A blocked item now stays retryable.
- **Backoff ledger (durable, keyed on `work_item_id`).** `BACKOFF_LEDGER_KEY =
  "backoff"` maps `{work_item_id: {blocked_count, deferred_at_updated_at}}` (a
  durable cross-tick fact like the acted-ledger). `persisted_backoff_ledger` reads
  it (default `{}`).
- **On resume of an acting state**, for each handoff (mapped from
  `work_order_id` → `work_item_id` via the dispatched work_orders):
  - `opened`/`closed` → recorded in the acted-ledger (existing) AND its backoff
    entry is cleared (a success resets the counter).
  - `blocked` → `backoff[work_item_id].blocked_count += 1`. If the count reaches
    the configured threshold (`backoff.threshold`, default 5, read from the
    central config via `sg.load_config`), the item is **deferred**:
    `deferred_at_updated_at` is set to the item's current issue `updated_at`
    (looked up from the tick's `work_items`), and an **escalation** is posted via
    `observability.escalate(<issue ref>, "auto-maintainer attempted N times,
    blocked: <reason>; needs human attention", …)` through an injectable sink
    (tests stub it; never raises). Below the threshold the item is NOT deferred —
    it simply retries next tick.
- **IMPLEMENT per_item filter — skip deferred-unchanged.** In addition to
  skipping already-acted work_orders (existing acted-ledger filter), the per_item
  set now also drops any work_order whose `work_item_id` is **deferred** in the
  backoff ledger AND whose issue `updated_at` (looked up from the tick's
  `work_items`) **equals** `deferred_at_updated_at` (deferred + unchanged → no
  re-dispatch, no thrash). If the issue's `updated_at` has **advanced** (a human
  commented / edited / relabelled / reopened), the item is NOT skipped — it
  **re-enters**, and its backoff entry is reset (`blocked_count → 0`, deferral
  cleared) so it gets a fresh K attempts. This is the durable, GitHub-native,
  session-independent retry trigger (no manual control).
- **Strictly per-item; never loop-halting.** Backoff only suppresses re-dispatch
  of the one deferred item; the tick loop runs all other work and reaches its
  terminal normally. A *systemic* fault remains the separate `ABORTED` path
  (§3.8.3), untouched here.
- **End states.** A valid work order ends either **implemented** (`opened`/
  `closed`) or **escalated-to-human + deferred** (K honest attempts, a visible
  issue comment, issue stays open) — never silently dropped.

## Skip-unchanged re-triage (§3.5.3)

The triager re-judges every open issue every tick — wasteful when an issue is
already handled and unchanged. This slice adds a durable **triage memory** so
the loop only re-triages NEW or CHANGED issues. Edits live ONLY in scheduling
(`run_tick.py`).

- **Triage memory (durable, keyed on `work_item_id`).** `TRIAGE_MEMORY_KEY =
  "triage_memory"` maps `{work_item_id: {updated_at, status}}`, where `status` is
  `done` (the doer opened/closed it) or `deferred` (backoff). It is recorded at
  the acting-state resume, alongside the acted/backoff ledgers, from the same
  `work_order_id → work_item_id` mapping + the item's current issue `updated_at`
  (looked up from the tick's `work_items`).
- **Filter at TRIAGE dispatch.** When `run_tick` builds the dispatch for an
  agent-state whose read slots include `work_items` (i.e. TRIAGE), it FILTERS the
  `work_items` fed to the subagent: an item is dropped iff
  `triage_memory[work_item_id].status ∈ {done, deferred}` AND its current
  `updated_at` EQUALS the remembered `updated_at` (handled + unchanged). NEW
  items (not in memory), CHANGED items (advanced `updated_at`), and `active`
  items (accepted but not yet done — still being worked) are ALWAYS re-triaged,
  so a valid issue in flight is never starved. The SAME `updated_at` change
  signal that re-enters a deferred item (§3.8.5 backoff) also re-triages it.
- **Surfacing.** The persisted `work_items` read product stays the full PULL set
  (accurate); the trace adds a `triaged=<judged>/<pulled>` token so the skip is
  visible.
- **Unchanged.** With an empty triage memory (first run) nothing is skipped —
  byte-identical to today. Pure-script routes and non-`work_items` agent-states
  are unaffected.

## Wiring config CLIs (route + adapter-map) — §3.4.3 / §3.10.2

scheduling owns the maintainer's `DEFAULT_ROUTE` + `DEFAULT_ADAPTER_MAP` and every
port's runtime details, so it also ships the two guided **wiring CLIs** that let a
user edit the project-local override files without hand-writing JSON. Both VALIDATE
through `adapter-wiring` before writing — `adapter-wiring` stays the dependency-free
validator; scheduling supplies the defaults + per-port knowledge those CLIs need.

### route CLI — `src/route_config.py` + `/auto-maintainer:route`

- `src/route_config.py` (deterministic, script-tier — spec-rules §1): a
  load-modify-VALIDATE-save of `${project_dir}/.auto-maintainer/route.json`.
  - `--show` — print the active route (override `route.json` if present, else
    `DEFAULT_ROUTE`) + its source via `route_source` (#59), a readable derivative
    of the machine-first route.
  - Deterministic edit ops — insert/append/remove a state; add/remove an edge
    `{state, signal, next}`. Each edit is applied to the route dict and then
    VALIDATED by building the loop (`adapter_wiring.build_loop` over the edited
    route + the active adapter-map: resolve + signals + data-readiness + anchor
    invariants) BEFORE writing. A failing edit is REJECTED (non-zero exit, file
    NOT written) — an invalid route can never be saved.
  - `--describe` — emit a machine-first catalog of the current states/edges + the
    editable operations (for the skill to drive).
- `/auto-maintainer:route` skill — **recommends keeping the default route**; if the
  user wants to insert/reorder/remove states (e.g. enable the close-the-loop
  chain), it walks them through the change and calls `route_config.py` to validate
  + write. Dispatches NO subagent.

### adapter-map CLI — `src/adapter_map_config.py` + `/auto-maintainer:adapter-map`

- `src/adapter_map_config.py` (deterministic): a load-modify-VALIDATE-save of
  `${project_dir}/.auto-maintainer/adapter-map.json`.
  - `--show` — print the active map (override else `DEFAULT_ADAPTER_MAP`).
  - Set a port to a script factory address (string) OR to an **agent** entry. For
    an agent entry on a **KNOWN agent-capable port** the user supplies **only the
    `subagent_type`**; the CLI fills the rest from `AGENT_PORT_TEMPLATES[port]` —
    `writes` slot, `cardinality`, `effect` (present only for acting ports), and a
    concrete `output_example` (a concrete example value in the slot's top-level
    type — NEVER a schema descriptor, per the agent-adapter rule). For an
    unknown/custom port the CLI additionally requires `writes` + (if acting)
    `effect` + an `output_example`, since they cannot be inferred.
  - The resulting map is VALIDATED by resolving it
    (`adapter_wiring.resolve_states` / `build_loop`, which deep-validates agent
    entries via `agent-dispatch`) BEFORE writing; an invalid entry is REJECTED
    (no write).
- `/auto-maintainer:adapter-map` skill — **recommends the default map**; to wire an
  agent to a port the user gives the `subagent_type` (+ port), the skill calls
  `adapter_map_config.py` which fills the entry + validates + writes. Dispatches
  NO subagent.

### `AGENT_PORT_TEMPLATES` (scheduling-owned)

A table mapping each known agent-capable port (e.g. `TRIAGE`, `IMPLEMENT`) →
`{writes, cardinality, effect?, output_example}`, built from the ports' own slot
owners (work-intake `WORK_ORDERS_SLOT`, implement `HANDOFFS_SLOT`, …) so a bare
`subagent_type` is enough to produce a valid agent entry. This per-port knowledge
is why the adapter-map CLI lives in scheduling (which imports those slot owners),
not in dependency-free adapter-wiring.

### Self-healing known-port migration — `migrate_known_port_entries`

A persisted project-local `adapter-map.json` known-port agent entry wired under
an OLDER template version carries a RETIRED `writes` slot (e.g. a v0.6.0 REVIEW
entry writing the retired `review_verdicts` slot, which the redesigned loop
(FT-C/D) replaced with `review_findings`). scheduling owns `AGENT_PORT_TEMPLATES`
+ `_build_agent_entry`, so it owns the pure migration
`migrate_known_port_entries(adapter_map) -> adapter_map` (in
`adapter_map_config.py`) that heals such RETIRED-slot entries against the LIVE
template on load. It is a PURE dict → dict transform (NEW dict, no input
mutation, idempotent), and **SURGICAL, not blanket** (#279): a blanket re-derive
of EVERY known-port agent entry would CLOBBER valid customizations — the dogfood
IMPLEMENT writes the still-valid `handoffs` slot but reads `work_orders` (NOT the
template's `execution_plan`) in a NO-PRIORITIZE route, and a blanket re-derive
would rewrite its `inputs` to `execution_plan` (never produced), raising
`adapter-wiring` `WiringError`. So: compute `valid_writes =
{tmpl['writes'] for tmpl in AGENT_PORT_TEMPLATES.values()}`. For each
`(port, entry)`: REBUILD via
`_build_agent_entry(port, <entry's existing dispatch[0].subagent_type>)` —
re-deriving every template field (writes / cardinality / output_example / inputs /
manifest / signal / effect / isolation) and preserving ONLY the `subagent_type` —
ONLY when `ad.is_agent_entry(entry)` AND `port in AGENT_PORT_TEMPLATES` AND the
entry's `dispatch[0]['writes']` is NOT in `valid_writes` (a RETIRED slot).
Otherwise the entry is returned UNCHANGED (valid-writes customizations, script
strings, custom-port agent entries, and non-agent entries are all preserved). It
heals REVIEW (`review_verdicts` → `review_findings`) while PRESERVING IMPLEMENT
(`handoffs` untouched, custom `work_orders` read kept).

It is wired into `run_tick` as the `migrate=` hook of the single
`aw.build_loop(DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime, ...)` call (running on
the loaded map AFTER `load_adapter_map`, BEFORE `resolve_states`), so a stale
persisted entry self-heals on every tick BEFORE resolve+validate. Because
`adapter_map_config` imports `run_tick`, `run_tick` imports `adapter_map_config`
LAZILY at the call site (a deferred `import adapter_map_config` inside the tick
body, never a module-level import, which would be circular).

## Stale/incompatible tick-checkpoint discard-and-fresh (upgrade-in-flight crash fix)

The adapter-map migration above self-heals the live CONFIG, but a durable
**`tick_checkpoint`** paused at an agent-state under an OLDER wiring BYPASSES it:
its `pending.writes` names a slot the current (migrated, seeded) wiring NO LONGER
registers — e.g. a v0.7.0 checkpoint paused at REVIEW with
`pending.writes='review_verdicts'`, the slot the loop redesign (FT-C/D) retired
in favour of `review_findings`. Before this slice the runner re-emitted the stale
dispatch on `--step` and, on `--resume`, applied the subagent output to the
unregistered slot — raising `fc.ContractError("slot 'review_verdicts' is not
registered")` and CRASHING `run_tick`. `start.py --clear-only` clears the
disposition latch, NOT the checkpoint, so `/auto-maintainer:start` could not
recover. The runner now treats such a checkpoint as STALE: discard it and run a
fresh tick.

- **Pure predicate `_checkpoint_compatible(checkpoint, ctx) -> bool`.** True when
  the checkpoint is empty/absent OR its pending dispatch `writes` slot is in
  `ctx.registered_slots()` (the freshly-seeded current wiring). False when the
  pending writes slot is unregistered (a retired/renamed slot after an upgrade).
  It is pure and deterministic — no wall clock, no network.
- **Discard-and-fresh guard in `_run_agent_tick`.** After loading the checkpoint
  (`persisted_tick_checkpoint`) and BEFORE the resume / crash-safety-re-emit
  branches, run_tick seeds a fresh ctx (the same `ctx_seed` used downstream) and
  checks `_checkpoint_compatible`. When the checkpoint is present but INCOMPATIBLE
  it `_clear_checkpoint(state_path)` to drop the stale `TICK_CHECKPOINT_KEY`,
  records the discard (the stale pending `state` + `writes`) on the `tick_start`
  event detail (`checkpoint_discarded`), sets the local checkpoint to `{}`, and
  FORCES the fresh path (resume=False semantics) so the tick re-walks from GUARD
  and re-pauses at the agent-state with the CURRENT migrated writes slot. This
  applies on BOTH `--step` (no resume) and `--resume` (resume=True): a stale
  checkpoint NEVER reaches `_resume_agent_state` / `_emit_pause_from_checkpoint`.
  (The new event detail stays within observability's closed `EVENT_KINDS` — it is
  a `tick_start` detail field, not a new event kind.)
- **Safe, idempotent re-walk.** The fresh re-walk after discard relies on the
  EXISTING acted-ledger / re-entry guards (§3.2.4, #204) so re-running
  PULL..IMPLEMENT does not double-act an already-open PR.
- A COMPATIBLE checkpoint (pending writes registered) is UNCHANGED — still
  re-emitted on `--step` and resumed on `--resume` exactly as before.

## Immediate-refire when actionable work remains (§3.3.3)

Owner-requested enhancement: the loop should run the **next tick IMMEDIATELY**
when actionable work remains, instead of waiting the full heartbeat interval.
This completes the immediate-refire mechanism (DESIGN §3.3.3). It consumes
`work-intake` (Triage) + `lifecycle-dispositions` (Exit) + `durable-state`
UNCHANGED; edits live ONLY in scheduling (`run_tick.py` + the tick executor
skill).

- **Pure POOL predicate
  `_work_remains(work_items, triage_memory, backoff_ledger, threshold, now)`.**
  The refire question is a POOL check: *"would next tick's TRIAGE have any
  classify-valid, non-blocked issue to judge/act on?"* — NOT a narrow
  committed-work check. The predicate first computes
  `candidates = _filter_triage_work_items(work_items, triage_memory)` — the SAME
  §3.5.3 skip-filter TRIAGE applies, which drops done/deferred-AND-unchanged items
  and KEEPS new/changed/active ones. It returns True iff ANY candidate is BOTH:
  - **TRIAGE-acceptable** — the deterministic work-intake validity gate accepts
    it (`wi.Triage(now=now).classify(item)[0] == "accepted"`: well-formed +
    open + not-stale, keyed off the injectable `now`). An invalid / closed /
    stale / malformed item never counts.
  - **NOT blocked-and-unchanged** — blocked-and-unchanged means the item's
    backoff entry has `blocked_count >= threshold` AND the item's `updated_at` is
    NOT strictly after `entry.deferred_at_updated_at` (a deferred + unchanged
    item is inert; re-dispatching it would thrash, §3.8.5). A blocked item whose
    `updated_at` ADVANCED past the pin IS actionable again and counts.

  Reusing the §3.5.3 skip-filter GUARANTEES no busy-loop: a done/deferred-unchanged
  or persistently-rejected (closed) item is filtered out and never refires; a
  blocked item only refires until the backoff threshold, then goes inert. It is
  pure and deterministic — no wall clock (the reference time is the injected
  `now`), no network, no durable-state read.

- **EXIT wrapped with immediate-refire (`make_exit`).** `make_exit(runtime)`
  WRAPS `lifecycle-dispositions`' `Exit`. Its `run(ctx)` reads `tick_outcome` +
  `work_items` + `state_path` from ctx and loads the durable triage memory
  (`persisted_triage_memory(state_path)`) + backoff ledger
  (`persisted_backoff_ledger(state_path)`) from `state_path = ctx.read("state_path")`.
  When the inner outcome is `empty` AND the route has an **acting agent-state** (a
  dispatch entry carrying a truthy `effect`, threaded onto the runtime dict as
  `has_acting_agent` — the gate KEPT so the read-and-idle DEFAULT route + the
  dry-run inert IMPLEMENT still IDLE) AND
  `_work_remains(work_items, triage_memory, backoff, threshold, now)` is True, it
  rewrites `tick_outcome` to `work-remains` so the inner EXIT selects
  `RUNNING`/`refire`; otherwise the inner EXIT runs unchanged. It overrides
  **ONLY** the `empty` outcome — a `restart`/`fault` (or already-`work-remains`)
  outcome is delegated UNCHANGED. The refire keys on the POOL, not on this tick's
  committed orders. `threshold` is `_backoff_threshold(runtime["governance"])`;
  `now` is `runtime.get("now")`. EXIT's manifest stays `reads=[tick_outcome,
  work_items, state_path]`, `writes=[tick_outcome]`, `emits` = the inner Exit's
  refire/idle/break/halt; `work_items` is in the data-readiness `initial` set (it
  is seeded unconditionally, so every route leaves it readable for EXIT).

  The acting-route gate lives in `make_exit` (not in the pure predicate) because
  `_work_remains` is the route-agnostic POOL predicate; whether the loop SHOULD
  refire on a workable pool depends on the route actually having an acting stage to
  do the work next tick (the dry-run inert IMPLEMENT / read-and-idle spine leaves no
  real work, so it idles, per spec).

- **Back-compat.** The read-and-idle **DEFAULT_ROUTE** spine + any **empty-pool**
  tick still IDLE: with no acting agent-state present `has_acting_agent` is False
  and the override never fires. All existing scheduling tests stay green.

- **The tick executor skill loops on `refire`.** `ship/skills/tick/SKILL.md`
  documents the refire-loop: when a completed tick's final signal is `refire`,
  run ANOTHER tick immediately, looping until a non-refire signal
  (`idle`/`halt`/`break`). The cron heartbeat remains the safety net.

## Release-needed detection — the human-release-owed signal (#319)

The auto-maintainer is **NOT self-deployable**: `run_tick` performs NO build,
NO commit, NO push, and NO regeneration of the committed plugin tree. The loop
merges PRs **server-side** (INTEGRATE), so when a merge touches SHIPPED feature
source the committed plugin tree drifts from the bumped version a release would
carry. A **human release is owed** to keep the plugin version 1:1 with the
shipped bytes. scheduling surfaces that explicitly so the operator is not left
guessing.

- **`_release_needed(integration_result, project_dir, files_source, repo)` — a
  pure detector, no action.** It returns True iff (a)
  `_self_deploy_repo_root(project_dir)` resolves — the maintained project IS the
  framework's own checkout (the only place a self-release is meaningful, detected
  via `build_plugin.SELF_DEPLOY_MARKER`) — AND (b) >=1 PR in
  `integration_result.merged` has a diff that touches SHIPPED source
  (`build_plugin.touches_shipped_src` over the injectable `files_source`'s files
  for each merged PR). It fires on the **merged shipped-src change alone** — it
  takes NO governance / `self_deploy` knob. A docs/test-only merge, a tick that
  merged nothing, and a maintained project that is not the framework checkout all
  return False (and, for the no-merge / not-framework cases, never query the files
  source — keeping the `gh` calls off non-self-deploy projects).
- **Surfaced, never acted on.** When True, the one-line trace carries a
  `release_needed` token and the `tick_end` event `detail` carries
  `release_needed: true`; otherwise the token is absent and the detail boolean is
  False. No self-deploy / version-bump / commit / push fields ride the trace or
  event.
- **Seams kept for the detector only.** `gh_pr_files_source` /
  `DEFAULT_PR_FILES_SOURCE` (the injectable per-PR changed-files source) and
  `_self_deploy_repo_root` (the own-checkout detector) are KEPT solely to feed
  `_release_needed`. `build_plugin` is imported ONLY for
  `bp.touches_shipped_src` + `bp.SELF_DEPLOY_MARKER`; it is absent in a normal
  install (not shipped), so the detector no-ops there.

## Known gaps / deferred

- The executor (the session-side actor that performs the Agent dispatch and
  feeds `resume_dispatch` back to `run_tick`) ships as the `tick` skill
  (`ship/skills/tick/SKILL.md`); `run_tick` itself still only emits the dispatch
  requests and applies provided results (it never calls the Agent tool).
- Configurable **route** + **adapter-map** via the `/auto-maintainer:route` and
  `/auto-maintainer:adapter-map` CLIs — IMPLEMENTED (see "Wiring config CLIs"
  above). The tick **interval is config-driven** (`heartbeat.interval_minutes`,
  default 3) — #17 resolved.
- System-cron scheduler backend (§3.3.1) — slice 1 is in-session heartbeat only.
- TRIAGE/IMPLEMENT/VERIFY/INTEGRATE — the loop PULLs (read-and-idle) and, on an
  ACTING route, EXIT is now **work-driven**: it refires while actionable work
  remains (see "Immediate-refire when actionable work remains" above). The
  read-and-idle default route still idles.
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

## Default-config resolution (read shipped default FRESH, #337)

The aggressive operational default (`mode: auto-merge` + the full acting route +
the agent adapters) ships as the plugin's `default-config/{config,route,
adapter-map}.json` (built by packaging-config, refreshed every release). The
runtime READS those files FRESH on each start as the default handed to the
resolver — it does NOT copy them into the runtime dir.

`run_tick` resolves `default_route` / `default_map` by reading the shipped
`default-config/route.json` / `adapter-map.json` when present, else the embedded
conservative `DEFAULT_ROUTE` / `DEFAULT_ADAPTER_MAP` constant (the source-tree /
no-plugin safety fallback). The shipped dir is `<plugin_root>/default-config/`
(sibling of `lib/`); in the source tree that sibling is absent, so the
conservative constant is used. safety-governance's `load_config` resolves the
config default the same way (shipped `default-config/config.json` fresh, else
`DEFAULT_GOVERNANCE`).

There is **NO seed-once copy** — `start.py` does NOT copy `default-config` into
`.auto-maintainer/` (the retired `seed_default_config`). Because the default is
read fresh, a release that changes a shipped default reaches an existing install
automatically (no frozen seeded copy — the #337 staleness fix). A user override
at `${CLAUDE_PROJECT_DIR}/.auto-maintainer/<file>` still WINS over the shipped
default (unchanged override-else-default in `load_route` / `load_adapter_map` /
`load_config`). An existing install that seeded a copy under a prior version
clears that file to adopt the fresh shipped default.
