# auto-maintainer-framework — Design Plan

**Status:** draft. **Form factor:** Claude Code plugin. **Name:** "auto" =
**automatic** AND **autonomous**.

---

## 0. Scope & form factor (decided)

- **Claude Code plugin**, distributed via a Claude plugin marketplace so
  installation is a standard `/plugin install`. Non-Claude-Code platforms are
  out of scope (a separate future discussion).
- **One plugin instance maps 1:1 to one project.** Exactly one maintainer loop
  runs at a time per project (single-writer). The work tracker is the shared
  inbox everyone on the project files into; the loop is its single consumer.
- **Roadmap line:** **v1 = a robust single-stream autonomous maintainer**
  (correct, crash-safe, conservative). **v2 = parallelism + smarter triage +
  self-evolution.** Anything not serving "single-stream correct" is pushed to
  v2 or later.

---

## 1. Glossary

### 1.1 Tick phase pipeline

A single scheduled run of the loop is a **TICK**. Its phases (uppercase
throughout; the final phase is named `EXIT` for code safety — no slash):

```
GUARD -> DRAIN -> PULL -> TRIAGE -> PRIORITIZE -> IMPLEMENT -> VERIFY -> INTEGRATE -> CLEANUP -> PERSIST -> EXIT
```

- **GUARD** (core) — entry gate: honor STOPPED / ABORTED / RESTART_NEEDED;
  enforce single-writer mutual exclusion (stale-marker detection).
- **DRAIN** (core) — finish any owed work left by a prior truncated tick BEFORE
  pulling new work (crash-safety).
- **PULL** (adapter) — fetch the actionable work queue from the tracker.
- **TRIAGE** (adapter) — turn raw items into validated, deduplicated,
  decomposed, ordered work orders.
- **PRIORITIZE** (adapter) — decide order and (v2) parallel grouping; back-fill
  status (e.g. in-progress).
- **IMPLEMENT** (adapter) — dispatch an isolated coding agent per work order;
  the only phase that requires a model.
- **VERIFY** (adapter) — gate the result (tests / CI / review) before
  integration.
- **INTEGRATE** (adapter) — the VCS hook: merge / release.
- **CLEANUP** (adapter) — release isolated workspaces, branch + marker hygiene.
- **PERSIST** (core) — write durable state (the backbone of resumability).
- **EXIT** (core) — decide the next action: refire-now (work remains), idle
  (queue empty, rely on heartbeat), break (restart owed), or halt
  (stop/abort).

**Core phases** (`GUARD`, `DRAIN`, `PERSIST`, `EXIT`) are owned by the loop and
not project-overridable. **Adapter phases** (`PULL`, `TRIAGE`, `PRIORITIZE`,
`IMPLEMENT`, `VERIFY`, `INTEGRATE`, `CLEANUP`) are swappable ports.

### 1.2 Lifecycle state machine

| State | Meaning | Caused by | Auto-resumes on next heartbeat? | Surfacing |
|---|---|---|---|---|
| `RUNNING` | A tick is actively executing | — | n/a | — |
| `IDLE` | Healthy, no work right now (queue empty) | the loop, normally | **Yes** | silent |
| `STOPPED` | Intentional pause | a **human** explicit stop | **No** — holds until a human resumes | neutral |
| `ABORTED` | Fault halt (safety violation / hard blocker) | the loop self-halting | **No** — holds until a human investigates | **alarm** |
| `RESTART_NEEDED` | Cannot proceed until Claude restarts | self-modification / platform limit | **Yes**, after the restart | prompt |

Key distinctions: **IDLE auto-resumes; STOPPED and ABORTED both latch.** STOPPED
is a deliberate human choice (neutral, resume at will); ABORTED is an
involuntary fault (alarms, demands investigation). They are kept distinct so a
fault never masquerades as a normal pause.

---

## 2. Pivotal decisions (the forks, resolved)

- **D1 — Where the WHAT-intelligence lives: implement-heavy by default.**
  TRIAGE emits a validated *decision + pointer*; the IMPLEMENT agent generates
  the spec/WHAT itself. *Rationale:* lowest prerequisite — a project needs no
  spec subsystem to start. A spec-first TRIAGE is a pluggable adapter, not the
  default. **[v1 default + seam]**
- **D2 — Parallelism / scope-conflict model: deferred.** v1 is **serial-only**.
  *Rationale:* safe parallel autonomy needs a per-project scope/conflict model
  (the deepest generality gap); serial is immediately useful and avoids it.
  **[v2]**
- **D3 — Trust default: `propose`.** Ship modes `dry-run` / `propose` /
  `gated-merge`; default `propose` (implement + open PR, never auto-merge).
  *Rationale:* auto-merge is the scariest action; make trust opt-in and
  graduated. **[v1]**
- **D4 — Extensibility: ports-and-adapters via script contracts.** Each adapter
  phase is a script with a typed input -> output contract; project config maps
  port -> adapter; default adapters ship for GitHub + git + generic-implement.
  **[v1]**

### 2.1 Adapter port contracts (shape)

```
PULL        : ()                       -> WorkItem[]
TRIAGE      : WorkItem[]               -> WorkOrder[]   (validated, dedup'd, decomposed, ordered) + decisions
PRIORITIZE  : WorkOrder[]              -> ExecutionPlan (order + parallel groups + status backfill)
IMPLEMENT   : (WorkOrder, Workspace)   -> Handoff       (status, artifact=branch|pr, discovered_work[], blocked_reason)
VERIFY      : Handoff                  -> Verdict        (ok: bool, reasons[])
INTEGRATE   : Verdict[]                -> IntegrationResult
CLEANUP     : IntegrationResult        -> ()
```

`Handoff` is the load-bearing seam between the loop and any project's
implementer (TDD subagent, plain-PR agent, spec-then-code agent). The core
knows only these schemas; it never knows an adapter's implementation.

---

## 3. Feature areas (with adopt/defer decisions)

Decision tags: **[v1]** adopt now, **[v2]** next version, **[deferred]** later,
**[excluded]** out of scope.

### A. Lifecycle Core
- **A1** The TICK phase spine (section 1.1). **[v1]**
- **A2** Lifecycle state machine (section 1.2): states + transitions, each
  durably encoded by a marker/state. **[v1]**
- **A3** Single-writer mutual exclusion (running-guard + stale-marker
  detection). **[v1]**
- **A4** Host-agnostic resumption contract — identical behavior on fresh
  (headless) or warm (in-session) context; on-disk state is the only source of
  truth. **[v1]**

### B. State, Resumability & Idempotency
- **B1** Versioned durable state schema (single JSON, semver'd). **[v1]**
- **B2** Per-tick journal (record-before-act; drives skip-on-resume). **[v1]**
- **B3** DRAIN owed-work entry step (finish a truncated prior tick first).
  **[v1]** *(non-negotiable for crash-safety)*
- **B4** Idempotency / dedup-key convention for every outward effect. **[v1]**
  *(correctness, not optional: e.g. record "PR opened for #N" BEFORE the agent
  call, so a re-run never opens a second PR for the same item)*
- **B5** State compaction/rotation (bound growth over months). **[v2]**
  *Rationale:* only matters after long runtime; not a day-1 correctness issue.

### C. Scheduling, Heartbeat & Restart
- **C1** Scheduler detection (system cron where available, else an in-session
  durable heartbeat). **[v1]** *(forced by the platform: a plugin cannot
  install its own clock)*
- **C2** Heartbeat install/uninstall bootstrap — the "configure" step wires the
  clock; the user authorizes it. **[v1]**
- **C3** Immediate-refire decision (work-remains -> one-shot; queue-empty ->
  idle) with at-most-one-refire dedup. **[v1]**
- **C4** Restart-and-resume (RESTART_NEEDED marker -> SessionStart auto-resume).
  **[v1]** *(forced by the platform: self-modifying code needs a restart; also
  the hook for self-evolution)*

### D. Phase Ports & Adapters
- **D1** Typed port contracts for all adapter phases (section 2.1). **[v1]**
- **D2** Default adapters: GitHub-Issues `PULL`, generic `IMPLEMENT`, git+PR
  `INTEGRATE`. **[v1]**
- **D3** Override mechanism (project config maps each port to a script).
  **[v1]**
- **D4** Adapter SDK + authoring docs. **[v2]** *Rationale:* v1 ships working
  defaults; formal SDK ergonomics follow once contracts are battle-tested.
- **D5** Built-in non-GitHub trackers (Jira / Linear). **[deferred]**
  *Rationale:* the *port* exists in v1; extra built-in adapters are additive.

### E. TRIAGE Pipeline
- **E1** Intake / normalize to a canonical WorkItem. **[v1]**
- **E2** Validity gate (well-formed, in-scope, non-spam, not stale). **[v1]**
- **E3** Dedup vs closed (already-resolved guard); back with native
  mark-as-duplicate where available. **[v1]**
- **E4** Dedup vs open (merge overlapping open items into one). **[v2]**
  *Rationale:* useful but a known gap even in rabbit today; not a correctness
  blocker.
- **E5** Decompose (one level) + parent/child linkage via native sub-issues.
  **[v1]**
- **E6** Recursive decompose + atomicity test. **[v2]** *Rationale:* one level
  covers the common case; recursion adds convergence complexity.
- **E7** Dependency / ordering via native issue dependencies. **[v1]**
- **E8** WHAT-generation / spec adapter (per D1; default off, rabbit wires it
  on). **[v1 seam, default off]**

### F. IMPLEMENT Contract
- **F1** `Handoff` schema — the swap seam (section 2.1). **[v1]**
- **F2** Subagent isolation = hard rule + per-dispatch isolated workspace
  (worktree). **[v1]** *(also: the 2-level nesting cap means the loop dispatches
  the implementer directly, never wrapped in another agent)*
- **F3** Default implementer = generic implement-then-PR. **[v1]**
- **F4** TDD implementer as an optional bundled adapter (rabbit's path).
  **[v1, optional]** *Rationale:* keeps TDD available without making it a
  prerequisite.
- **F5** Long-run / partial-completion handling (label-before-dispatch
  visibility; resumable mid-implement). **[v1]**

### G. VERIFY & INTEGRATE
- **G1** VERIFY gate = `{ok, reasons[]}`, conservative default (no merge unless
  explicitly green). **[v1]**
- **G2** Default gates: CI status + test pass. **[v1]**
- **G3** INTEGRATE = pluggable VCS hook covering merge + release + branch
  cleanup. **[v1]**
- **G4** Idempotent release / tag (create-if-not-exists). **[v1]**
- **G5** Non-git VCS adapters (GitLab MR / Gerrit). **[deferred]**
  *Rationale:* port exists; extra backends are additive.

### H. Safety, Authority & Governance
- **H1** Declarative guardrails (red-flags as config the host enforces: never
  merge a wrong base, never delete a non-matching branch, never merge a dirty
  tree). **[v1]**
- **H2** Trust ladder (`dry-run` / `propose` / `gated-merge`, per D3). **[v1]**
- **H3** No `AskUserQuestion` in autonomous mode -> ABORTED marker + escalation
  instead of blocking prompts. **[v1]**
- **H4** Budget caps (per-tick / per-day token ceiling, hard stop). **[v1]**
  *Rationale:* an unbounded loop is a financial hazard; needs a real ceiling,
  not judgment.
- **H5** Backoff / circuit-breaker (consecutive-failure + per-item defer counts
  -> stop thrashing). **[v1]**
- **H6** Blast-radius caps / learned scope inference. **[v2]** *Rationale:*
  basic guardrails (H1) suffice for serial v1; inference is hard and tied to the
  parallel tier.

### I. Observability & Surfacing
- **I1** Structured, tail-able event log — source of truth for "what did the
  loop do", readable by other sessions. **[v1]**
- **I2** SessionStart banner + dispatcher-persona injection (the CLAUDE.md
  substitute, since a plugin has no always-on context). **[v1]**
- **I3** Escalation channel = issue-comment by default. **[v1]**
- **I4** Slack / email / webhook escalation. **[v2]** *Rationale:* issue-comment
  needs no extra integration; richer sinks are additive.

### J. Config, Persona & Packaging
- **J1** `userConfig` prompted at enable (tracker token, mode, budget). **[v1]**
- **J2** Project-local config file (port -> adapter wiring; read via
  `CLAUDE_PROJECT_DIR`). **[v1]**
- **J3** Persona via SessionStart hook + skill bodies (the CLAUDE.md
  substitute). **[v1]**
- **J4** `.claude-plugin/plugin.json` + marketplace layout + install / configure
  / run UX. **[v1]** *(Claude marketplace compliance is a v1 requirement, for
  ease of user install.)*
- **J5** Dogfood: rabbit-workflow as adapter #1 (prove the loop runs rabbit's
  TDD / scope features through the ports). **[v1 validation goal]**
- **J6** Self-evolution (the plugin evolving its own code) + reload / restart.
  **[v2]** *Rationale:* depends on C4 being solid first; high-risk, not needed
  to "maintain a project".

---

## 4. Explicitly excluded

Multi-repo / one-loop-many-projects; non-Claude-Code platforms; parallel
dispatch (-> v2); recursive decomposition (-> v2); learned scope inference
(-> v2); self-evolution (-> v2).

---

## 5. Concern-coverage check

| Concern | Home | Decision |
|---|---|---|
| State / resumability | B1-B3 | v1 |
| Idempotency / exactly-once | B4 | v1 |
| Scheduling / lifecycle | C1-C3, A2 | v1 |
| Safety / authority / policy | H1-H3 | v1 |
| Budget governance | H4 | v1 |
| Backoff / circuit-breaker | H5 | v1 |
| Observability + escalation | I1-I3 | v1 |
| Config / adapter wiring | D3, J1-J2 | v1 |
| WHAT-intelligence location | D1, E8 | v1 (implement-heavy default) |
| Scope / conflict (parallelism) | H6, D2 | **v2** |
| Trust ladder | H2 | v1 |
| Fresh-vs-warm context | A4 | v1 |
| Self-evolution + restart | C4 (mech), J6 (feature) | C4 v1 / J6 v2 |
| Dogfood entanglement | J5 | v1 goal |
| Config approachability | J1-J2 | v1 |
| Multi-repo / scale | — | excluded |
