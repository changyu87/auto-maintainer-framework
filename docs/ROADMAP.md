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
| 5 | `scheduling` | §3.3.1–§3.3.4 | In-session heartbeat + script-backed `/auto-maintainer:start`/`:stop`/`:status`; route now **GUARD→DRAIN→PULL→PERSIST→EXIT** (read-and-idle, real work-intake PULL). 1-min hardcoded (#17); system-cron + durable heartbeat deferred (#31). `/start` clears a latched STOPPED via script-backed `start.py` (#44). | **implemented** (PRs #21/#25/#33/#41/#46; 40 tests) |
| 6 | `work-intake` | §3.4.2 (PULL), §3.5.1–§3.5.3, §3.5.5, §3.5.7, §3.5.8 | **Slice 1 (PULL):** GitHub-Issues PULL → `work_items` (gh CLI behind injectable seam). **Slice 2 (TRIAGE):** deterministic validity gate → `work_orders` (dedup-vs-closed/decompose/ordering/WHAT-gen seam = slice 3+). | **slices 1–2 implemented** (PRs #38/#41/#50; 21 tests; PULL live, TRIAGE not yet wired) |
| 7 | `implement` | §3.6.1–§3.6.5 | `Handoff` schema, mandatory worktree isolation (direct L1 dispatch), default implement-then-PR, optional TDD adapter, long-run handling. | planned |
| 8 | `verify-integrate` | §3.7.1–§3.7.4 | `VERIFY` gate `{ok,reasons[]}` (CI+test), `INTEGRATE` VCS hook (merge/release/cleanup), `CLEANUP`, idempotent release. | planned |
| 9 | `safety-governance` | §3.8.1–§3.8.5, §3.11.5 | Guardrails, trust ladder (dry-run/propose/gated-merge), no-AskUserQuestion→ABORTED, budget caps, backoff/circuit-breaker, loopback/provenance guard. | planned |
| 10 | `outbound-report` | §1.3, §2.5, §3.11.1–§3.11.4, §3.11.6, §3.11.7 | `REPORT` port + `DiscoveredIssue`/`ReportResult` schemas, default GitHub filing adapter, durable IMPLEMENT-discovery filing, idempotent journaled filing, project-vs-self routing. | planned |
| 11 | `observability` | §3.9.1–§3.9.3, §3.10.3 | Structured event log, SessionStart banner + dispatcher-persona injection, issue-comment escalation. | planned |
| 12 | `packaging-config` | §3.4.3, §3.10.1, §3.10.2, §3.10.4, §3.10.5 | **Slices 1–2:** clean plugin assembly (no `.rabbit/`) + `marketplace.json` + `ship/` collection + 5 loop libs + `/auto-maintainer:start`/`:stop`/`:status` + SessionStart persona. Later: `userConfig`, port→adapter wiring, dogfood. | **slice 2 implemented** (PRs #13/#23/#27/#34/#42/#47; **v0.2.4**; 23 tests) |

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

The real adapter spine (`work-intake` → `implement` → `verify-integrate`) and the
cross-cutting features remain the next real work — they replace DEMO_WORK with
genuine maintainer ticks.

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
