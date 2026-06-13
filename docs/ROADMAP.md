# auto-maintainer-framework — Feature Roadmap & Traceability

**Purpose:** the durable map from `DESIGN.md` → rabbit features → implementation
status. Maintained across sessions so work can resume without re-deriving the
decomposition. Update the **Status** column as features progress.

- **Source of design:** [`DESIGN.md`](DESIGN.md)
- **Decomposition scope:** v1 only (every `[v2]`/`[deferred]`/`[excluded]` item
  in DESIGN.md §3–§4 is intentionally out of the feature set below until v1
  lands).
- **Owner:** changyu87 · **GitHub account:** changyu87
- **Plugin mode:** features live under `.rabbit/rabbit-project/features/<name>/`.

## Status legend

| Status | Meaning |
|--------|---------|
| `planned` | Boundary agreed; not scaffolded yet |
| `scaffolded` | Feature dir + `feature.json` created |
| `spec-drafted` | `docs/spec.md` authored (design captured; not implemented) |
| `test-red` | Tests authored, failing (TDD) |
| `implemented` | Code passing its tests |
| `verified` | Reviewed + integrated |

## Feature traceability table

| # | Feature | DESIGN.md refs | One-line scope (v1) | Status |
|---|---------|----------------|---------------------|--------|
| 1 | `fsm-contracts` | §1.1.1, §2.6, §2.7, §3.4.1 | Blackboard slot schema, `StateResult`, signal vocabulary, per-state manifest, `route.json` schema. Pure data. | **implemented** (PR #8, merged; 22 tests) |
| 2 | `tick-orchestrator` | §1.1.1, §3.1.1, §2.7 | External router: `resolve_next`, run loop to terminal, structural validators (signal-validity, data-readiness). | **implemented** (PR #9, merged; 13 tests) |
| 3 | `lifecycle-dispositions` | §1.2, §3.1.2, §3.1.3, §3.1.4 | Disposition machine (`RUNNING`/`IDLE`/`STOPPED`/`ABORTED`/`RESTART_NEEDED`), single-writer mutex + stale detection, GUARD/EXIT anchors, host-agnostic resumption. | **implemented** (PR #20; 20 tests) |
| 4 | `durable-state` | §3.2.1–§3.2.4 | Versioned state schema, per-tick record-before-act journal, DRAIN owed-work step, PERSIST, idempotency/dedup-key. | **implemented** (PR #19; 11 tests, incl. truncate→resume exactly-once) |
| 5 | `scheduling` | §3.3.1–§3.3.4 | In-session heartbeat + script-backed `/auto-maintainer:start`/`:stop`/`:status`; route now **GUARD→DRAIN→PULL→PERSIST→EXIT** (read-and-idle, real work-intake PULL). Route is **data** (default + project-local `route.json` override via adapter-wiring §3.4.3). 1-min hardcoded (#17); system-cron + durable heartbeat deferred (#31); `/start` clears a latched STOPPED (#44); trace + status report `route=default`/`route=override:<path>` (#59). PRIORITIZE+IMPLEMENT now in `DEFAULT_ADAPTER_MAP`; `execution_plan`/`handoffs` surfaced as per-tick ephemeral read products in trace+status (PR #77). Governance loaded per tick; durable cross-tick budget window; `mode`+budget surfaced in trace/status (PR #82). | **implemented** (PRs #21/#25/#33/#41/#46/#56/#61/#77/#82; 99 tests) |
| 6 | `work-intake` | §3.4.2 (PULL), §3.5.1–§3.5.3, §3.5.5, §3.5.7, §3.5.8 | **Slice 1 (PULL):** GitHub-Issues PULL → `work_items` (gh CLI behind injectable seam). **Slice 2 (TRIAGE):** deterministic validity gate → `work_orders` (dedup-vs-closed/decompose/ordering/WHAT-gen seam = slice 3+). | **slices 1–2 implemented** (PRs #38/#41/#50; 21 tests; PULL live; TRIAGE wireable via route.json, #56) |
| 6b | `prioritize` | §1.1, §2.6 | `PRIORITIZE` adapter: deterministic `work_orders → execution_plan` (identity/FIFO order + `pending` status backfill, in-slot only). Owns `ExecutionPlan` schema. No groups (v2), no severity key (none on WorkOrder), no tracker write (deferred to safety-governance). | **implemented** (PR #74; 10 tests; wired live PR #77) |
| 7 | `implement` | §3.6.1–§3.6.5 | **Dry-run slice (trust-ladder `dry-run` rung):** `Handoff` schema + inert deterministic `execution_plan → handoffs` (status=`planned`, artifact=`none`, no model/diff/PR/cap; reads `execution_plan` only, not `workspace`). **Deferred:** model-backed implement-then-PR doer (§3.6.2/§3.6.3, `propose` rung, reads `workspace`), TDD adapter (§3.6.4), trust-ladder mode + budget gating (§3.8). | **dry-run slice implemented** (PR #76; 13 tests; wired live PR #77) |
| 8 | `verify-integrate` | §3.7.1–§3.7.4 | `VERIFY` gate `{ok,reasons[]}` (CI+test), `INTEGRATE` VCS hook (merge/release/cleanup), `CLEANUP`, idempotent release. | planned |
| 9 | `safety-governance` | §3.8.1–§3.8.5, §3.11.5 | **Slice 1:** governance config (project-local `governance.json`, machine-first), trust-ladder gate `permits()` (dry-run/propose/gated-merge, default propose), **auto-resuming budget readiness gate** (per-tick/per-day token ceiling, local-tz day window, `null`=no limit, window rollover = auto-resume, never latches), no-AskUserQuestion→ABORTED helper. **Deferred:** guardrails §3.8.1 + backoff §3.8.5 → verify-integrate; loopback §3.11.5 → outbound-report; blast-radius §3.8.6 → v2. | **slice 1 implemented** (PRs #81/#82/#88; 21 tests; wired into scheduling — mode+budget surfaced; **default daily budget = no limit** per user, finite ceiling opt-in) |
| 10 | `outbound-report` | §1.3, §2.5, §3.11.1–§3.11.4, §3.11.6, §3.11.7 | `REPORT` port + `DiscoveredIssue`/`ReportResult` schemas, default GitHub filing adapter, durable IMPLEMENT-discovery filing, idempotent journaled filing, project-vs-self routing. | planned |
| 11 | `observability` | §3.9.1–§3.9.3, §3.10.3 | Structured event log, SessionStart banner + dispatcher-persona injection, issue-comment escalation. | planned |
| 12 | `packaging-config` | §3.4.3, §3.10.1, §3.10.2, §3.10.4, §3.10.5 | **Slices 1–2:** clean plugin assembly (no `.rabbit/`) + `marketplace.json` + `ship/` collection + 5 loop libs + `/auto-maintainer:start`/`:stop`/`:status` + SessionStart persona. Later: `userConfig`, port→adapter wiring, dogfood. | **slice 2 implemented** (PRs #13/#23/#27/#34/#42/#47/#57/#62/#67/#71/#78/#83/#90; **v0.2.11**; 35 tests; ships route-as-data loop + route-source + per-tick read products #64 + status work_orders #69 + PRIORITIZE/IMPLEMENT act-path + safety_governance no-limit default, 13 libs) |
| 13 | `adapter-wiring` | §2.4, §3.4.1, §3.4.3, §3.10.2, §3.4.6 | Route-as-data: load `route.json` + `port→adapter` map (project-local override), resolve adapters via the `factory(runtime)→(manifest,run)` convention, validate wiring at load (signals + data-readiness + anchors), `build_loop`. Now also resolves/validates **agent-adapter** object entries → `(manifest, AgentState)` (PR #94). | **implemented** (PRs #54/#56/#94; 27 tests; TRIAGE wireable by config; agent entries supported) |
| 14 | `agent-dispatch` | §2.8, §3.4.6 | Deterministic helpers for the agent-adapter mechanism: schema + `is_agent_entry`/`validate_agent_adapter`, `build_envelopes`, `render` (envelope→structured markdown), `validate_output`, `collect_outputs`, `compute_signal`. Dispatches nothing (executor's job). | **implemented** (PR #93; 52 tests) |

> Note: `lifecycle-core` from the first-pass decomposition was split into #2
> `tick-orchestrator` (router) and #3 `lifecycle-dispositions` (cross-tick state)
> to keep each feature to a single, tightly-scoped TDD cycle.

## Build order (current intent)

Bit-by-bit, verifying each before the next:

1. **`fsm-contracts` + `tick-orchestrator`** — **DONE** (merged, PRs #8/#9).
   Proven by the generic, domain-free PING/PONG two-state transition test
   (fsm-contracts 22 tests; tick-orchestrator 13 tests) — FSM mechanism
   validated in isolation, zero maintainer-domain coupling.
2. `durable-state` + `lifecycle-dispositions` + `scheduling` — **DONE** (PRs
   #19/#20/#21, +#25 fix). The real loop core: a disposition-driven, journaled,
   single-writer-guarded, crash-safe, resumable tick loop driven by a 1-min
   in-session heartbeat; demo route GUARD→DRAIN→DEMO_WORK→PERSIST→EXIT with a
   persisted counter. Loop mechanics real; the *work* (DEMO_WORK) still stubbed.
3. `work-intake` **PULL slice 1 — DONE + LIVE** (PRs #38/#41; shipped v0.2.3):
   the installed loop now pulls the repo's real open issues into `work_items`
   (read-and-idle). Next: TRIAGE (work-intake slice 2) → `implement` →
   `verify-integrate`. `implement` is the first stage that *acts* on work
   (replaces read-and-idle with real ticks).
4. `safety-governance`, `outbound-report`, `observability` — cross-cutting.
5. `packaging-config` — **slices 1–2 DONE** (PRs #13/#23/#27, v0.2.1): clean
   plugin + marketplace + `ship/` collection now ships the loop core
   (`/auto-maintainer:start`/`:stop`/`:status`). Install/update via the GitHub
   flow. Remaining slices (`userConfig`, port→adapter wiring, dogfood) follow
   once the adapter features exist.

6. **Route-as-data + adapter wiring (`adapter-wiring`, §3.4.3) — DONE** (PRs
   #54/#56/#57, shipped v0.2.5). The framework is now genuinely ports-and-adapters
   *at runtime*: route + adapters are data; a project-local `route.json` wires/
   reorders/swaps adapter states with no code (proven live — TRIAGE inserted by
   config produces `work_orders`). Load-time validation rejects bad wiring.
**Read products are per-tick ephemeral** (#64, PRs #65/#66/#67, v0.2.7): a tick
reports only what THIS tick's route produced — a route without TRIAGE reports
`work_orders=0`, never a stale count carried from an earlier TRIAGE tick.

7. **Act-side seam (`prioritize` + dry-run `implement`) — DONE** (PRs
   #74/#76/#77, shipped v0.2.9). The pipeline now reaches
   `work_orders → PRIORITIZE → execution_plan → IMPLEMENT → handoffs`, all
   deterministic. IMPLEMENT is the trust-ladder **`dry-run`** rung (spec-faithful:
   `propose` actually opens PRs, §2.3): inert handoffs, no model/diff/PR, `git`
   stays clean. An override route `TRIAGE→PRIORITIZE→IMPLEMENT` activates it with
   no code change; default route stays read-and-idle. Budget was correctly
   removed from IMPLEMENT — the real budget is a **token ceiling** (§3.8.4) owned
   by safety-governance, not a per-task cap.

8. **`safety-governance` slice 1 — DONE** (PRs #81/#82/#83, shipped v0.2.10).
   Governance config + trust-ladder gate + auto-resuming budget readiness gate
   (local-tz day, `null`=no limit, never latches — budget exhaustion idles and
   resumes at the next window, no human `/start`) + no-AskUserQuestion→ABORTED.
   Loaded per tick; `mode`+budget surfaced in trace/status. Enforcement of
   act-skip is the acting adapter's job (consults `permits`/budget) — lands with
   the doer. Guardrails/backoff deferred to verify-integrate; loopback to
   outbound-report.

9. **Execution-model pivot — DESIGN §2.8 + §3.4.6** (PRs #85/#86/#87). Settled how
   LLM states run: the loop is **in-session, script-driven** (warm-only; supersedes
   the headless §3.1.4 / system-cron §3.3.1 ambition). States are two kinds —
   **script-states** (`run(ctx)`) and **agent-states** (a model is needed). The
   script drives the route; at an agent-state it emits a rendered invocation
   envelope and **yields**; the session presses the `Agent` button (decides
   nothing), output is validated against the slot schema and written back, the
   script resumes (journal = pause/resume). Serial composition = **chain states**
   (reuse the slot handoff); an agent-adapter only adds "one subagent or a parallel
   set." This **replaces** the earlier subprocess/`claude -p` doer idea.

10. **Budget default = no limit — DONE** (PRs #88/#89/#90, shipped v0.2.11). Per
    user decision, `DEFAULT_GOVERNANCE.budget.per_day_tokens` is `null` (no limit)
    by default; a finite ceiling is opt-in (`governance.json`, later `userConfig`
    §3.10.1). Default tick renders `budget=0/none`.

**Agent-adapter mechanism (§3.4.6) — deterministic engine DONE:**
1. **`agent-dispatch` feature — DONE** (PR #93, 52 tests): schema + render() + validate + envelopes + signal.
2. **`adapter-wiring` agent entries — DONE** (PR #94, 27 tests): resolves/validates agent objects → `(manifest, AgentState)`.
3. **`scheduling` run_tick yield/resume seam — DONE** (PR #95, 111 tests): agent route → PAUSED{dispatches} + checkpoint; `resume_dispatch` validates+applies+continues; crash-safe; pure-script routes unchanged.

4. **Executor skill + domain-free proof — DONE & LIVE-PROVEN** (PRs #97/#98/#99,
   shipped **v0.2.12**). `scheduling` tick CLI (`--step/--resume` JSON, PR #97);
   the `/auto-maintainer:tick` executor skill + `auto-maintainer-echo` proof
   subagent shipped (PR #98, skill-creator-validated); packaging v0.2.12 (PR #99).
   **Live proof:** a tick paused at an agent-state TRIAGE, the session dispatched
   `auto-maintainer:auto-maintainer-echo` (a real subagent), its output was
   validated against the `work_orders` slot schema and written, and the script
   resumed to `done` — `work_orders=4` produced by a subagent, `git` clean. The
   §2.8 executor protocol is proven end-to-end in a real install. (Plugin agents
   are namespaced: `subagent_type` must be `auto-maintainer:<name>`.)

**Agent-adapter mechanism (§3.4.6) — COMPLETE & live-proven (v0.2.12).**

**Next:**
5. **Heartbeat automation follow-up** — rework `/start` + the heartbeat to a
   **prompt-cron** firing `/auto-maintainer:tick` (so the loop runs the executor
   automatically, not just a manual `/tick`); + harden the tick skill's
   resume-marshalling (#100, script-backed not hand-rolled python).
6. **Wire real TRIAGE / IMPLEMENT as agent-adapters** + ship default triager/implementer
   subagents (the `propose` rung). Then `verify-integrate` (guardrails §3.8.1 + backoff
   §3.8.5), `outbound-report` (loopback §3.11.5), `observability` (escalation §3.9.3).

## Deferred (NOT in the v1 feature set)

Per DESIGN.md §3 tags and §4: parallel dispatch, recursive decompose, dedup-vs-open
(§3.5.4), state compaction (§3.2.5), adapter SDK (§3.4.4), built-in non-GitHub
trackers (§3.4.5), non-git VCS adapters (§3.7.5), blast-radius/learned scope
(§3.8.6), richer escalation sinks (§3.9.4), self-evolution (§3.10.6),
bidirectional cross-tracker sync (§3.11.8).

## Known design notes to resolve during spec work

- **`PRIORITIZE` state**: present in the §1.1 spine but omitted from the §2.6
  adapter-contract table. Currently folded under `work-intake`/`lifecycle`
  scope; confirm its home when its spec is drafted.
