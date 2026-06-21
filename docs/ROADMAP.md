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
| 5 | `scheduling` | §3.3.1–§3.3.4 | In-session heartbeat + script-backed `/auto-maintainer:start`/`:stop`/`:status`; route now **GUARD→DRAIN→PULL→PERSIST→EXIT** (read-and-idle, real work-intake PULL). Route is **data** (default + project-local `route.json` override via adapter-wiring §3.4.3). 1-min hardcoded (#17); system-cron + durable heartbeat deferred (#31); `/start` clears a latched STOPPED (#44); trace + status report `route=default`/`route=override:<path>` (#59). PRIORITIZE+IMPLEMENT now in `DEFAULT_ADAPTER_MAP`; `execution_plan`/`handoffs` surfaced as per-tick ephemeral read products in trace+status (PR #77). Governance loaded per tick; durable cross-tick budget window; `mode`+budget surfaced in trace/status (PR #82). **Doer governance (PRs #131/#132):** acting agent-states (dispatch entry carries truthy `effect`) get a trust-gate (dry-run→inert planned handoffs, propose+→dispatch with `isolation`+`description`), acted-ledger idempotency, budget pre-gate, and `--resume --spent` spend metering. **Tick executor skill v0.3.0 (PR #134, #130 closed):** passes `description`+`isolation` to Agent and meters summed `subagent_tokens` via `--spent`. | **implemented** (PRs #21/#25/#33/#41/#46/#56/#61/#77/#82/#131/#132/#134; 193 tests) |
| 6 | `work-intake` | §3.4.2 (PULL), §3.5.1–§3.5.3, §3.5.5, §3.5.7, §3.5.8 | **Slice 1 (PULL):** GitHub-Issues PULL → `work_items` (gh CLI behind injectable seam). **Slice 2 (TRIAGE):** deterministic validity gate → `work_orders` (dedup-vs-closed/decompose/ordering/WHAT-gen seam = slice 3+). | **slices 1–2 implemented** (PRs #38/#41/#50; 21 tests; PULL live; TRIAGE wireable via route.json, #56) |
| 6b | `prioritize` | §1.1, §2.6 | `PRIORITIZE` adapter: deterministic `work_orders → execution_plan` (identity/FIFO order + `pending` status backfill, in-slot only). Owns `ExecutionPlan` schema. No groups (v2), no severity key (none on WorkOrder), no tracker write (deferred to safety-governance). | **implemented** (PR #74; 10 tests; wired live PR #77) |
| 7 | `implement` | §3.6.1–§3.6.5 | **Dry-run slice (trust-ladder `dry-run` rung):** `Handoff` schema + inert deterministic `execution_plan → handoffs` (status=`planned`, artifact=`none`, no model/diff/PR/cap; reads `execution_plan` only, not `workspace`). **Propose-rung doer subagent (PR #133, v0.2.19):** protocol-free coding agent at `ship/agents/auto-maintainer-implementer.md` (tools Read/Grep/Glob/Edit/Write/Bash, model opus) — enacts each work order's triage decision: rejected→close issue+justify, accepted→implement+open PR (NEVER merge), blocked→no open PR; worktree isolation; writes own Handoff; governance (trust/budget/idempotency) enforced upstream in run_tick. **Deferred:** TDD adapter (§3.6.4), full resumable-mid-implement (§3.6.5). | **dry-run slice + propose-rung doer subagent implemented** (PRs #76/#133; 13+17 tests; doer **LIVE-PROVEN** v0.2.21: accept→PR, reject→close, idempotent) |
| 8 | `verify-integrate` | §3.7.1–§3.7.4, §3.8.1 | The act-side CLOSE of the loop, all SCRIPT-tier (deterministic `gh`). **Cross-tick model:** GitHub is the source of truth for the loop's open PRs (no durable ledger) — the implementer stamps PRs with the `auto-maintainer` label; **VERIFY** queries `gh pr list --label auto-maintainer --state open` each tick → a `Verdict` per PR (`ok` = CI passing AND mergeable AND base==default). **INTEGRATE** merges `ok` verdicts via `gh pr merge --merge --delete-branch` ONLY at `gated-merge` AND when `safety_governance.merge_guardrails` passes (dry-run/propose = NO-OP → skipped). **CLEANUP** v1-thin (branch cleanup folded into `--delete-branch`; release deferred). Wired into scheduling (`make_verify`/`make_integrate`/`make_cleanup`); merge guardrails §3.8.1 live in safety-governance. **REVIEW gate (#209, feature v0.3.0, plugin v0.4.0):** a model-backed correctness/quality gate between VERIFY and INTEGRATE — a NON-acting `REVIEW` agent-state (reads `verdicts`, writes the versioned `review_verdicts` slot; `ReviewVerdict {approved, severity, findings[]}`) dispatched to the new `auto-maintainer-reviewer` subagent (spec-compliance "right thing, nothing more/less, read the actual diff" + code-quality over the PR base..head diff). INTEGRATE now ANDs `is_review_approved` into its merge condition (un-/not-approved → `skipped` with the findings as reason, re-attempts next tick); conservative default = not-approved. Wired in scheduling (`make_review` default no-op + `AGENT_PORT_TEMPLATES['REVIEW']`). | **implemented** (PRs #158/#159/#160/#161/#162/#163/#164/#209; VERIFY 18 + guardrails 12 + INTEGRATE/CLEANUP/REVIEW 44 + wiring 11 tests; shipped v0.2.24; REVIEW gate v0.4.0; **LIVE-PROVEN** under gated-merge: green CI PR auto-merged + branch deleted) |
| 9 | `safety-governance` | §3.8.1–§3.8.5, §3.11.5 | **Slice 1:** governance config (project-local `governance.json`, machine-first), trust-ladder gate `permits()` (dry-run/propose/gated-merge, default propose), **auto-resuming budget readiness gate** (per-tick/per-day token ceiling, local-tz day window, `null`=no limit, window rollover = auto-resume, never latches), no-AskUserQuestion→ABORTED helper. **Deferred:** guardrails §3.8.1 + backoff §3.8.5 → verify-integrate; loopback §3.11.5 → outbound-report; blast-radius §3.8.6 → v2. **Slice 2 — config writer (PR #135, feature v0.2.0):** `src/configure.py` (deterministic load-modify-save of `governance.json`: validates mode via `permits` closed set, budget ceilings int-or-none/null, preserves keys, `--show`, exit-2 on invalid) + `/auto-maintainer:configure` skill (thin relay, the doer's arming surface). Lib default mode unchanged (`propose`). | **slices 1–2 implemented** (PRs #81/#82/#88/#135; 21+10 tests; mode+budget surfaced; **default daily budget = no limit**; userConfig writer ships) |
| 10 | `outbound-report` (REPORT, spread per §3.11 — no new feature) | §1.3, §2.5, §3.11.1–§3.11.7 | Implemented as a SPREAD across existing features (DESIGN §3.11 "no new top-level feature"): **work-intake** owns the `DiscoveredIssue`/`ReportResult` schemas + injectable `gh_issue_file_sink` (provenance `filed-by:autonomous-maintainer` label + `<!-- am-dedup:KEY -->` marker) + pure `file_discoveries` + `is_loop_filed`; PULL EXCLUDES loop-filed items (§3.11.5 loopback — exclusion, NOT reject). **scheduling** run_tick flushes `handoffs[].discovered_work` out-of-band at the terminal → durable `REPORT_LEDGER_KEY` idempotency, `permits("file",mode)` trust gate (dry-run logs, propose+ files), `reported=<filed>/<skipped>` surfaced. | **implemented** (PRs #154/#155/#156; shipped v0.2.23, fixed + re-shipped v0.2.28 (sink ensures the provenance label exists; report_errors surfaced); **LIVE-PROVEN** v0.2.28: implementer discoveries filed as issues #189/#190, idempotent + loopback-excluded next tick. maintainer_repo routing added v0.2.25 (PR #174)) |
| 11 | `observability` | §3.9.1–§3.9.3, §3.10.3 | Structured event log, SessionStart banner + dispatcher-persona injection, issue-comment escalation. | planned |
| 12 | `packaging-config` | §3.4.3, §3.10.1, §3.10.2, §3.10.4, §3.10.5 | **Slices 1–2:** clean plugin assembly (no `.rabbit/`) + `marketplace.json` + `ship/` collection + 5 loop libs + `/auto-maintainer:start`/`:stop`/`:status` + SessionStart persona. Later: `userConfig`, port→adapter wiring, dogfood. | **slice 2 implemented** (PRs #13/#23/#27/#34/#42/#47/#57/#62/#67/#71/#78/#83/#90; **v0.2.19**; 55 tests; ships route-as-data loop + route-source + per-tick read products #64 + status work_orders #69 + PRIORITIZE/IMPLEMENT act-path + the IMPLEMENT-doer arsenal (implementer agent + `/auto-maintainer:configure` skill + tick skill v0.3.0 + `lib/configure.py`), 14 libs) |
| 13 | `adapter-wiring` | §2.4, §3.4.1, §3.4.3, §3.10.2, §3.4.6 | Route-as-data: load `route.json` + `port→adapter` map (project-local override), resolve adapters via the `factory(runtime)→(manifest,run)` convention, validate wiring at load (signals + data-readiness + anchors), `build_loop`. Now also resolves/validates **agent-adapter** object entries → `(manifest, AgentState)` (PR #94). | **implemented** (PRs #54/#56/#94; 27 tests; TRIAGE wireable by config; agent entries supported) |
| 14 | `agent-dispatch` | §2.8, §3.4.6 | Deterministic helpers for the agent-adapter mechanism: schema + `is_agent_entry`/`validate_agent_adapter`, `build_envelopes`, `render` (envelope→structured markdown), `validate_output`, `collect_outputs`, `compute_signal`. Dispatches nothing (executor's job). | **implemented** (PR #93; 52 tests) |
| 15 | `observability` | §3.9.1, §3.9.3 | Structured append-only event log (`events.jsonl`, versioned, injectable clock) + escalation channel (issue-comment via injectable `gh` sink, un-stubs §3.8.3). `run_tick` emits events each tick (PR #104). | **implemented** (PRs #103/#104; 10+11 tests; event log live in the loop) |

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

11. **Observability + heartbeat automation — DONE** (PRs #103/#104/#105/#106/#107,
    shipped **v0.2.13**). New `observability` feature (#15): structured event log
    (`events.jsonl`) + escalation channel; `run_tick` emits events each tick.
    `/start` reworked to a prompt-cron heartbeat that drives the **executor**
    (`start.py --clear-only` + tick-1 via `/auto-maintainer:tick`), and the tick
    skill's resume-marshalling hardened (#100 closed). The loop now **auto-runs
    the executor** while a session is open, and records what it did.
    *Escalation channel is built but its live trigger (a would-block on a
    specific work order) lands with the doer.*

12. **Protocol-free subagents + file-based context isolation — DONE** (PRs
    #113/#114/#115/#116/#117, shipped **v0.2.15**; #100/#109 closed). DESIGN §3.4.6
    now mandates: **subagent definitions are interface-free** (role + capability
    only); the **rendered prompt is the complete, self-contained handoff
    contract** — embedded output schema + `output_path` + one-line ack. The
    subagent **writes its own output file**; the executor reads it on `--resume`
    (no arg) — so subagent output **never passes through the orchestrator
    context** (isolation). Echo agent rewritten protocol-free (`tools:[Write]`);
    tick skill stops marshalling content; agent-adapter entries carry
    `output_schema`. Heartbeat hardcoded to **3 min** (testability). Also fixed
    #109 (agent-tick journal intent crashing DRAIN) — 3 consecutive heartbeat
    ticks proven clean live.

**Next:**
6. **Real IMPLEMENT doer + `userConfig`** — swap the dry-run/echo for a genuine
   `propose`-rung implementer subagent (writes code + opens a PR, worktree
   isolation, consults `permits`/budget — the real token spender, which finally
   exercises governance + the escalation trigger); `userConfig` (§3.10.1) prompts
   mode/budget/token at enable, now that its knobs bite. This is where the loop
   starts doing real maintenance work.
7. Then a real **TRIAGE** subagent, `verify-integrate` (guardrails §3.8.1 +
   backoff §3.8.5), `outbound-report` (REPORT + loopback §3.11.5).

## Configurables overhaul — DONE (2026-06-20, plugin v0.3.0, PRs #193–#198)

A user-driven refactor of the whole config surface, spec-checked first (DESIGN
§3.8.4/§3.8.5/§3.3.2/§3.10.1/§3.10.2/§3.11.6 edited, PR #193):

1. **Central `config.json`** (schema 2.0.0) replaces the scattered `governance.json`
   (rename-and-migrated; `load_config` + `load_governance` alias). safety-governance
   owns it. (PR #194)
2. **Per-tick budget removed** — only `per_day_tokens` remains. (PR #194)
3. **Fixed `MAINTAINER_REPO`** constant (`changyu87/auto-maintainer-framework`) —
   `maintainer-self` discoveries always route upstream, never the project tracker,
   no fallback; the `maintainer_repo` config field is gone. (PRs #194/#195/#196)
4. **Config-driven knobs** — `heartbeat.interval_minutes` (default 3, #17 resolved)
   + `backoff.threshold` (default 5), owned by safety-governance, read by scheduling.
   (PRs #194/#195)
5. **Guided CLIs** — `/auto-maintainer:configure --setup` walk-through (over the
   `configure.py --describe` catalog), and `/auto-maintainer:route` +
   `/auto-maintainer:adapter-map` wiring editors (in scheduling — owns defaults +
   per-port knowledge; validate via adapter-wiring before writing; adapter-map's
   "agent-type-only" fills from `AGENT_PORT_TEMPLATES`). (PR #197)
6. **Re-shipped** plugin **v0.3.0** (PR #198; route_config/adapter_map_config libs +
   the new skills; stale `test_version_bumped_to_0_2_28` fixed). LIVE TEST PENDING
   (GitHub marketplace flow): `--setup`, `/route`, `/adapter-map`, governance.json→config.json
   migration, a tick reading interval(3)/threshold(5), maintainer-self routing.

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
