# scheduling — Changelog

## feature 0.20.0 — 2026-06-21

- **Self-healing known-port adapter-map migration (dogfood REVIEW root-cause
  fix).** A stale persisted project-local `adapter-map.json` known-port agent
  entry wired under an older template (e.g. a v0.6.0 REVIEW writing the retired
  `review_verdicts` slot with an old `output_example`) breaks the redesigned
  (FT-C/D) loop, where REVIEW writes `review_findings`.
  - `adapter_map_config.migrate_known_port_entries(adapter_map) -> adapter_map`:
    a PURE dict → dict transform. For each `(port, entry)` where
    `ad.is_agent_entry(entry)` AND `port in AGENT_PORT_TEMPLATES`, it REBUILDS the
    entry via `_build_agent_entry(port, <existing dispatch[0].subagent_type>)` —
    re-deriving `writes`/`cardinality`/`output_example`/`inputs`/`manifest`/
    `signal`/`effect`/`isolation` from the LIVE template while preserving ONLY the
    `subagent_type`. Script (string) entries, custom-port agent entries (port not
    in templates), and non-agent entries are left UNCHANGED. Returns a NEW dict
    (never mutates the input); idempotent.
  - `run_tick` passes it as the `migrate=` hook of its single `aw.build_loop(...)`
    call, so a stale entry self-heals on load BEFORE resolve+validate every tick.
    `run_tick` imports `adapter_map_config` LAZILY at the call site to avoid the
    `adapter_map_config → run_tick` circular import. A default (string)
    adapter-map is byte-for-byte unchanged.

## feature 0.19.0 — 2026-06-21

- **Redesigned-loop reconciliation (FT-E).** scheduling now consumes the
  redesigned verify-integrate (FT-C/D) + work-intake (FT-B) contracts so the
  close-the-loop route is runnable end-to-end.
  - **TRIAGE/VERIFY tick crash fixed.** `_seed_context` registers + seeds an
    empty no-risk `cross_cutting_risk` default whenever TRIAGE OR VERIFY is
    routed, and an empty `cross_check` whenever VERIFY is routed.
    `cross_cutting_risk` is added to the data-readiness `initial` set. Previously
    a TRIAGE or VERIFY tick raised `ContractError("slot 'cross_cutting_risk' is
    not registered")` and crashed the whole tick.
  - **REVIEW is ADVISORY — `review_verdicts` retired for `review_findings`.**
    `make_review` writes an EMPTY `review_findings` list (signal EMPTY); the
    `_SLOT_SCHEMAS` map, the `_seed_context` seeding (REVIEW-routed only), and the
    terminal persistence all migrate to `review_findings`.
    `REVIEW_VERDICTS_KEY` -> `REVIEW_FINDINGS_KEY` (`'review_findings'`),
    `persisted_review_verdicts` -> `persisted_review_findings`. `review_verdicts`
    is no longer seeded, mapped, or persisted.
  - **INTEGRATE is a thin merge.** It reads ONLY `verdicts` and merges each `ok`
    verdict's PR at `auto-merge` WITHOUT any review-approval read (the merge rests
    on IMPLEMENT's deterministic gate + VERIFY + guardrails + the trust ladder).
    `make_integrate`'s factory binding is unchanged.
  - **`review_findings` flush through REPORT.** The terminal REPORT flush gathers
    the `review_findings` slot as an ADDITIONAL discoveries source into
    `_flush_report`/`_gather_discoveries`; the findings (already DiscoveredIssue-
    conforming, with a stable `dedup_key`) file via `wi.file_discoveries` on the
    SAME journaled-idempotency + dedup-vs-open (`known_open=work_items`) path as
    handoff discoveries.
  - **`AGENT_PORT_TEMPLATES['REVIEW']` writes `review_findings`** (matching
    `REVIEW_MANIFEST`).
  - verify-integrate / work-intake / safety-governance are consumed UNCHANGED.

## feature 0.18.0 — 2026-06-21

- **REPORT dedup-vs-open wiring (#224, DESIGN §3.5.4).** `_flush_report` now
  passes the tick's PULLed open `work_items` to `work_intake.file_discoveries`
  as `known_open`, so a discovery whose subject duplicates an already-open issue
  is skipped (not filed as duplicate noise — the live #222/#223-vs-#209/#210
  case). The open-duplicate skips fold into the `reported=<filed>/<skipped>`
  surface and the dry-run would-file count excludes them. work-intake +
  safety-governance are consumed unchanged except for the added `known_open`
  arg. New tests in `test/test_report_flush_e2e.py` cover skip/file/dry-run with
  open work_items present.

## contract 0.11.0 — 2026-06-21

- **Acted-ledger re-entry: re-attempt a still-valid issue when its auto-PR is
  closed (§3.8.5-symmetric leak fix, auto-maintainer-framework#204).** The
  durable acted-ledger records every acted work order as `opened`/`closed` and
  the IMPLEMENT per_item filter skips an already-acted work order forever
  (idempotency — no duplicate PR). But when a human CLOSED an auto-PR (rejecting
  the work) and left the issue OPEN, the loop treated it as done forever and
  never re-attempted — a leak (the same class as the `blocked`-leak fixed at
  contract 0.8.x). The only remedy was manual `durable-state.json` surgery
  (deleting the acted-ledger entry). This closes the leak, symmetric with the
  backoff re-entry.
  - **Act-time issue state recorded.** An `opened` acted-ledger entry now stores
    `acted_at_updated_at` (the source issue's `updated_at` at act time):
    `ledger[wo] = {outcome: "opened", ref: <pr_url>, acted_at_updated_at:
    <issue.updated_at>}`. `_record_acted_ledger` resolves it via the dispatched
    work_orders' work_order_id -> work_item_id map. Back-compatible: a pre-#204
    entry (no pin) reads as `null` and can never re-enter (stays locked).
  - **Re-entry rule.** In the IMPLEMENT acted-ledger filter, beside the existing
    backoff skip-deferred-unchanged check, an already-`opened` work order
    RE-ENTERS (its ledger entry CLEARED, the item re-dispatched) when BOTH (a) its
    PR `ref` is CLOSED-AND-NOT-MERGED AND (b) the issue's current `updated_at` has
    ADVANCED past `acted_at_updated_at`. It stays LOCKED otherwise: merged (done),
    still-open PR (pending review), or closed-but-issue-unchanged (the human
    closed it without a redo — respect it, no thrash).
  - **Injectable PR-state seam.** New `gh_pr_state_source(pr_ref, repo=None,
    runner=subprocess.run)` + `DEFAULT_PR_STATE_SOURCE`, mirroring
    verify-integrate's `gh_open_pr_source`: shells `gh pr view <ref> --json
    state,mergedAt` and returns `{state, merged}` so the closed/merged check is
    deterministic + unit-testable. `run_tick(pr_state_source=...)` overrides it.
    The PR is queried ONLY for entries whose `updated_at` advanced (bounds the gh
    calls to changed issues); a raising/malformed source stays locked and never
    crashes the tick.
  - Net behaviour: **close an auto-PR + update the issue -> the loop re-attempts
    with the new guidance; close it + touch nothing -> the loop leaves it alone.**
    Removes the manual ledger surgery. Edits live ONLY in scheduling
    (`run_tick.py`); all sibling features are consumed UNCHANGED.

## contract 0.10.0 — 2026-06-21

- **Durable heartbeat + SessionStart auto-resume (#31, DESIGN §3.3.2).**
  The warm-only heartbeat (a session-scheduled prompt) ended with the Claude
  session and did not auto-resume next session. It is now DURABLE — not by
  keeping a clock alive (a plugin cannot) but by persisting a durable
  **loop-intent** marker and re-arming the in-session heartbeat from a
  `SessionStart` hook on the next session.
  - **New lib `heartbeat.py`** owns two runtime-dir markers alongside the
    lifecycle disposition/lock markers (which it only READS, never edits):
    `loop-intent` (set `running` by `/start`, cleared by `/stop`) and
    `last-resume-session` (the cross-session arm-once dedup), plus the pure
    decision `should_auto_resume(runtime_dir, session_id)` — True only when
    intent is `running`, the loop is NOT latched `STOPPED`/`ABORTED` and NOT owed
    a `RESTART_NEEDED`, and this session has not already armed. The decision is a
    pure function of on-disk state + session id, so it is deterministic and
    unit-testable with no session/clock/scheduler.
  - **Dedup ownership is on the SessionStart path, NOT `/start` (the #31 core
    fix).** `record_loop_intent` (the `/start` path) does NOT clear the
    resume-dedup. The SessionStart hook is what asks the session to run `/start`;
    if `/start` cleared the dedup, a SECOND SessionStart in the SAME session
    (SessionStart fires on startup / resume / `/clear` / compact) would re-arm a
    DUPLICATE heartbeat. Only `mark_resumed` (hook, on a True decision) and
    `clear_loop_intent` (the `/stop` path, which ends the arming epoch) ever
    write/clear the dedup. Regression test:
    `hook-arm → /start → 2nd SessionStart same session → assert no 2nd arm`.
  - **`start.py` (v0.4.0)** records the durable loop-intent after the
    latch-clear/refuse succeeds (in BOTH default and `--clear-only` modes; a
    refused `ABORTED` start records nothing) — without clearing the dedup.
    **`stop.py` (v0.2.0)** clears the loop-intent AND the dedup (so a stopped
    loop does NOT auto-resume, and a `/start` in the same session can re-arm) in
    addition to latching `STOPPED`.
  - **New shipped SessionStart hook `session-start-resume.py`** (scheduling
    `ship/hooks/`): reads intent + disposition, asks `should_auto_resume`, stamps
    the dedup, and emits `additionalContext` instructing the session to re-run
    `/auto-maintainer:start` to re-arm the heartbeat — at most once per session.
    A hook never breaks the session (any error -> silent). Registered as a second
    `SessionStart` command in the shipped `hooks.json` (packaging-config asset),
    alongside the persona hook.
  - **RESTART_NEEDED is BLOCKED, not driven.** A latched `RESTART_NEEDED` blocks
    auto-resume (the safe choice — the loop is never silently re-armed behind the
    human's back). This is NOT the DESIGN §3.3.4 RESTART_NEEDED→SessionStart
    *resume-drive* flow, which remains **deferred**.
  - **build_plugin.py** ships `heartbeat.py` into `lib/` with the plain self-path
    bootstrap (resolving its sibling `lifecycle_dispositions` from `lib/` alone);
    the hook resolves `heartbeat` from `../lib`. Build stays deterministic +
    byte-stable. scheduling consumes lifecycle-dispositions UNCHANGED.

> Numbering note: contract.md advanced 0.7.0 → 0.8.0 → 0.9.0 during the
> configurables overhaul (the wiring-config CLIs `route_config.py` /
> `adapter_map_config.py`, PRs #193-198) without dedicated CHANGELOG entries for
> 0.8.0/0.9.0; that work is described in those PRs and in spec.md. This entry
> resumes the changelog at the next contract version, **0.10.0**.
- **Per-tick event-log `tick_id` that discriminates ticks (#112).** Previously
  every event stamped `tick_id` = the work `counter`, which a read-only route
  never bumps — so every read-only tick collapsed to `tick_id=0` and the field
  could not distinguish ticks. The fix introduces a durable monotonic per-tick
  counter, SEPARATE from the work counter:
  - **`TICK_ID_COUNTER_KEY = "tick_id_counter"`** + `persisted_tick_id_counter(
    state_path) -> int` (default `0`; first FRESH tick assigns `1`, never `0`).
  - **`_assign_tick_id(state_path, resume, checkpoint)`** assigns THIS
    invocation's id: minted ONCE (counter+1) at a FRESH tick, and READ BACK from
    the durable checkpoint on a `--resume` / crash-safety re-emit. The id is
    stamped into `TICK_CHECKPOINT_KEY` at a PAUSE so a single tick's `--step` →
    `--resume` pair shares ONE id.
  - The id is therefore (1) DISTINCT across ticks AND (2) STABLE across a single
    tick's `--step` → `--resume` on ALL routes including the agent route — whose
    `--step`/`--resume` are SEPARATE processes that inject no `now`. It is
    deliberately NOT derived from the wall clock/`now` (which would split one
    logical tick into two ids; the reason the superseded PR #201 was wrong).
  - The same `tick_id` seeds the rendered agent-dispatch `{tick_id}` slot, the
    structured event log, and the human trace. Removes the dead `tick-0` fallback
    (the `counter`-defaulting-to-0 reads).
  - Purely additive otherwise: the walk, signals, disposition, slot persistence,
    #64 ephemerality, and the budget window are unchanged. Tests: a `--step` →
    `--resume` pair WITHOUT injecting `now` (production parity) asserts a single
    shared `tick_id`; consecutive read-only ticks and consecutive agent ticks
    assert DISTINCT ids; the counter advances once per logical tick (not per
    invocation).

## contract 0.7.0 — 2026-06-10

- **Doer governance for ACTING agent-states: acted-ledger + budget pre-gate +
  spend metering.** Completes `run_tick`'s governance for agent-states that
  perform outward effects (a truthy `effect`, from the prior trust-gate slice) on
  the PERMITTED (dispatch) path. ALL three apply ONLY to acting agent-states; a
  non-acting agent-state (TRIAGE), the dry-run inert path, and pure-script routes
  are byte-identical / unchanged.
  - **Acted-ledger (idempotency, §3.2.4).** New durable cross-tick key
    `ACTED_LEDGER_KEY = "acted_ledger"` + `persisted_acted_ledger(state_path)` ->
    `{work_order_id: {outcome, ref}}` (default `{}`). When building the per_item
    dispatch set, run_tick FILTERS OUT any `work_order_id` already in the ledger
    (already acted — never re-dispatch / no second PR). If after filtering NO items
    remain, the acting state does NOT pause (synthesizes an inert result, computes
    the signal, continues). On resume each newly-acted handoff is RECORDED into the
    ledger (`ledger[work_order_id] = {outcome: handoff["status"], ref:
    handoff.get("artifact",{}).get("ref")}`) via load-modify-save of ONLY
    `ACTED_LEDGER_KEY` (preserving every other durable key).
  - **Budget pre-gate.** BEFORE pausing at an acting agent-state, run_tick
    evaluates the budget window (`sg.evaluate_budget`); if `allowed` is False
    (per_day exhausted) it does NOT pause/dispatch — it synthesizes a DEFERRED
    result (handoffs `status:"blocked"`, `blocked_reason` naming the budget
    exhaustion, for the not-yet-acted items), computes the signal, continues — NO
    spend, NO dispatch; the items stay un-acted (NOT in the ledger) so they retry
    next window. TRIAGE / read-only states are NOT budget-pre-gated.
  - **Spend metering on resume.** The CLI `--resume` gains an optional `--spent
    <int>` (and a programmatic `spent` param on `run_tick(resume=True)`, default 0,
    back-compatible). On resume of an acting state, after applying the subagent
    outputs, run_tick `sg.record_spend(...)` into the budget window and persists it.
  - The acted-ledger + budget window are durable cross-tick facts (load-modify-save
    preserving other keys), never #64-ephemeral. scheduling consumes
    safety-governance (`evaluate_budget` / `record_spend` / `permits`) +
    durable-state + agent-dispatch + every sibling UNCHANGED.

## contract 0.6.3 — 2026-06-10

- **Trust-gate for ACTING agent-states (DESIGN §2.3 / §3.8.2 trust ladder).**
  `run_tick` now deterministically trust-gates agent-states that perform outward
  effects. An ACTING agent-state is an agent-adapter whose dispatch entry carries
  a truthy `effect` string (safety-governance's closed effect set
  `{implement, open_pr, merge, file}`).
  - Before pausing at an acting agent-state, `run_tick` computes
    `permitted = sg.permits(effect, mode)` (`mode` from the loaded governance) —
    the deterministic lib's decision, never the model's.
  - **dry-run (not permitted):** `run_tick` does NOT pause or dispatch. It builds
    the per-dispatch items via `ad.build_envelopes(...)` and synthesizes one INERT
    `planned` handoff per item (`{work_order_id, status:"planned", artifact:
    {kind:"none", ref:null}, discovered_work:[], blocked_reason:null}`),
    `ad.collect_outputs` them into the writes slot, writes it, computes the route
    signal, persists the read product, emits `state_run`/`signal` events
    (`state_run` detail notes `gated=dry-run`), and CONTINUES the driver. No
    PAUSE, no checkpoint, no spend, no subagent.
  - **propose / gated-merge (permitted):** the normal PAUSE-for-dispatch path runs
    unchanged so the executor dispatches the real subagent.
  - A NON-acting agent-state (no `effect`, e.g. a TRIAGE adapter) is UNCHANGED —
    the trust-gate does not apply and it always pauses to dispatch.
- **isolation + description in the PAUSED dispatches.** Each paused dispatch
  record now also carries `isolation` (the dispatch entry's `isolation`, e.g.
  `"worktree"`, else null) and `description` (the dispatch entry's `description`
  else a default `f"{state} dispatch"` / `f"{state}: {item}"`), so the executor
  can call `Agent(subagent_type, description=..., prompt=..., isolation=...)`.
- ONLY the effect-based trust-gate + isolation/description this slice; no budget
  pre-gate / acted-ledger / spend metering (next sub-slice). Read products stay
  #64 per-tick ephemeral; the #123 budget persistence and the #109 journal-free
  checkpoint are unchanged. scheduling consumes safety-governance + agent-dispatch
  + all sibling features UNCHANGED.

## contract 0.6.2 — 2026-06-14

- **#123 fixed — the durable budget window is now persisted on agent-route
  ticks.** On an agent route the fresh `--step` rolled the budget window but
  `_run_agent_tick` PAUSED and `run_tick` RETURNED EARLY (the PAUSE /
  invalid_output return) BEFORE the terminal budget-persist block, so the window
  was never saved (durable `budget={}`, `/status` showed `win=` empty). And on
  `--resume` the `is_resume` branch read the persisted window (`{}` because the
  pause never saved it) without carrying the evaluated window forward, so it
  persisted `{}` again.
  - Fix 1: `run_tick` now persists the budget window durably on the PAUSE /
    invalid_output early-return path (load-modify-save ONLY `BUDGET_KEY`,
    preserving the checkpoint, read products, and every other durable key), so
    the fresh tick's rolled window survives the pause.
  - Fix 2: on resume, after `evaluate_budget`, the evaluated window is carried
    forward (`new_budget_state = budget["budget_state"]`) so even a `{}`
    persisted value resolves to a real `{window_key, spent_tokens}`; the budget
    is REUSED, never re-rolled, per the FRESH-only gate.
  - The budget window now survives across two agent ticks in the same runtime
    dir; a `now` on a later local day rolls the window over (window_key advances,
    spent resets) on the next fresh tick. Pure-script route budget persistence is
    UNCHANGED (regression-guarded).
- scheduling consumes safety-governance and all sibling features UNCHANGED; edits
  live ONLY in scheduling (`src/run_tick.py` + tests + docs).
- Closes #123.

## contract 0.6.1 — 2026-06-10

- **#100 fully closed — no orchestrator content-marshalling remains.** Shipped
  three reworked, skill-creator-validated plugin assets into `ship/` VERBATIM
  (collected by the build's `_copy_tree(ship_dir, plugin_root)`, NO build change):
  - `ship/agents/auto-maintainer-echo.md` → **v2.0.0**, now
    **interface-protocol-free** (DESIGN §3.4.6). Its `.md` is role-only with
    frontmatter `tools: [Write]`; it bakes in NO schema, NO output path, and NO
    output format. The rendered prompt is the complete handoff contract (embedded
    schema + `output_path` + ack); for each input item it produces one accepted
    output and WRITES it to the file the prompt names, replying with only a short
    ack.
  - `ship/skills/tick/SKILL.md` → **v0.2.0**, subagent-writes-its-own-file. The
    skill marshals NO content: each dispatched subagent writes its own output file
    and `run_tick.py --resume` (taking **NO file argument**) reads those files
    itself from the checkpoint. The removed `dispatch-result.json` marshalling and
    the `Write`-the-output step are gone.
  - `ship/skills/start/SKILL.md` → **v0.2.1**, the recurring heartbeat interval is
    now **~3 minutes** (for testability; configurability still deferred — #17). It
    still clears the latch via `start.py --clear-only`, runs tick #1 through the
    `/auto-maintainer:tick` executor, and schedules a recurring prompt heartbeat.
- Updated the ship-asset e2e tests to the new contracts (echo protocol-free,
  tick subagent-writes-file + `--resume` no arg, start ~3-min) and kept the
  echo-TRIAGE wiring test green.
- scheduling consumes `run_tick.py`/`start.py`/`status.py` logic and all sibling
  features UNCHANGED; edits live ONLY in scheduling (`ship/` assets + tests + docs).
- Closes #100.

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
