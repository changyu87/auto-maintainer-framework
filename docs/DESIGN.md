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
  inbox everyone on the project files into; the loop is its single consumer —
  and, per decision 2.5, also a *producer* that files back what it discovers
  while working (section 1.3).
- **Roadmap line:** **v1 = a robust single-stream autonomous maintainer**
  (correct, crash-safe, conservative). **v2 = parallelism + smarter triage +
  self-evolution.** Anything not serving "single-stream correct" is pushed to
  v2 or later.

---

## 1. Glossary

### 1.1 The tick FSM

A single scheduled run of the loop is a **TICK**. A tick executes as a finite
state machine — the **tick FSM** — composed of **tick states** (uppercase
throughout; the terminal state is named `EXIT` for code safety — no slash). The
tick FSM runs entirely inside the `RUNNING` disposition of the outer lifecycle
machine (section 1.2).

The default shipped route is the linear spine:

```
GUARD -> DRAIN -> PULL -> TRIAGE -> PRIORITIZE -> IMPLEMENT -> VERIFY -> INTEGRATE -> CLEANUP -> PERSIST -> EXIT
```

This order is **data, not code** (see section 1.1.1 below): the arrows above are
the contents of the default `route.json`, not a hardcoded pipeline.

**The tick states:**

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
  the only state that requires a model.
- **VERIFY** (adapter) — gate the result (tests / CI / review) before
  integration.
- **INTEGRATE** (adapter) — the VCS hook: merge / release.
- **CLEANUP** (adapter) — release isolated workspaces, branch + marker hygiene.
- **PERSIST** (core) — write durable state (the backbone of resumability).
- **EXIT** (core, terminal) — emit the signal that selects the next lifecycle
  disposition: refire-now (work remains), idle (queue empty, rely on
  heartbeat), break (restart owed), or halt (stop/abort).

**Core states** (`GUARD`, `DRAIN`, `PERSIST`, `EXIT`) are owned by the loop and
not project-overridable; they are fixed **anchors**. **Adapter states**
(`PULL`, `TRIAGE`, `PRIORITIZE`, `IMPLEMENT`, `VERIFY`, `INTEGRATE`, `CLEANUP`)
are swappable ports composed freely between the anchors.

#### 1.1.1 Decoupled states + declarative routing

The tick states are **fully decoupled**: no state names another. The old
point-to-point pipe (where `PULL`'s output type was `TRIAGE`'s input type) is
replaced by a **uniform contract over a shared blackboard**.

- **Uniform state signature.** Every tick state implements the same entry
  point: `run(TickContext) -> StateResult`. A state never receives a typed
  input from a named predecessor nor hands one to a named successor.
- **`TickContext` is the blackboard** — a machine-first record carrying
  **named, schema'd, versioned slots**: `work_items`, `work_orders`,
  `execution_plan`, `handoffs`, `verdicts`, `integration_result`,
  `discoveries`, plus tick metadata (`tick_id`, budget accounting, config). A
  state reads the slots it needs and writes its products back into named slots.
- **`StateResult` is the uniform outcome envelope** —
  `{ signal, writes: { <slot>: <value> }, journal: [...] }`. The `signal` is
  drawn from a **closed, shared vocabulary** (e.g. `OK`, `EMPTY`, `BLOCKED`,
  `OWED_WORK`, `FAULT`, `RESTART_REQUIRED`, `HALT_REQUESTED`). A state reports
  *what happened*; it never decides *what runs next*.
- **Per-state manifest.** Each state declares its contract:
  `{ reads: [slots], writes: [slots], emits: [signals] }`. This is the
  machine-first realization of the bounded-scope contract (philosophy section
  2) and is what makes any custom route statically checkable.

**Routing is a declarative data file, chained by a script orchestrator.** A
per-project `route.json` defines the state set and the transition table
`(state, signal) -> next_state`; the orchestrator — a script, deterministic
(tool-tier `script`, spec-rules section 1) — loads it and loops: run the current
state, read its `signal`, resolve `next = route[state].on[signal]`, checkpoint,
repeat until the terminal state. Inserting a custom state is a data edit: add a
node, wire two edges. The transition conditions that were implicit in the old
pipeline become an **explicit, inspectable artifact**.

**A route validator (script) guards flexibility with three deterministic
checks**, run before any tick:

1. **Anchor invariants** (crash-safety, non-negotiable): entry is `GUARD`;
   `DRAIN` precedes every adapter state; `PERSIST` precedes `EXIT`; `EXIT` is
   the sole terminal. Core anchors are fixed; adapter states compose between
   them.
2. **Signal validity**: every `on` key is in that state's declared `emits`, and
   every transition target exists.
3. **Data-readiness**: on every path reaching a state, each slot it `reads` was
   `written` by a predecessor. A route that runs IMPLEMENT before
   `execution_plan` exists fails validation statically, not at runtime.

`REPORT` (section 1.3) is **not** a routed tick state — it stays out-of-band,
flushed from the `discoveries` slot at journaled points by the orchestrator.

### 1.2 Lifecycle dispositions

The loop's coarse, cross-tick operating condition is its **disposition** — the
outer state machine, kept terminologically distinct from the inner tick FSM's
*states* (section 1.1). The tick FSM executes entirely within the `RUNNING`
disposition; every other disposition is a between-tick or halt condition.

| Disposition | Meaning | Caused by | Auto-resumes on next heartbeat? | Surfacing |
|---|---|---|---|---|
| `RUNNING` | A tick FSM is actively executing | — | n/a | — |
| `IDLE` | Healthy, no work right now (queue empty) | the loop, normally | **Yes** | silent |
| `STOPPED` | Intentional pause | a **human** explicit stop | **No** — holds until a human resumes | neutral |
| `ABORTED` | Fault halt (safety violation / hard blocker) | the loop self-halting | **No** — holds until a human investigates | **alarm** |
| `RESTART_NEEDED` | Cannot proceed until Claude restarts | self-modification / platform limit | **Yes**, after the restart | prompt |

Key distinctions: **IDLE auto-resumes; STOPPED and ABORTED both latch.** STOPPED
is a deliberate human choice (neutral, resume at will); ABORTED is an
involuntary fault (alarms, demands investigation). They are kept distinct so a
fault never masquerades as a normal pause. The terminal tick state `EXIT` emits
the signal that selects the next disposition (refire / idle / break / halt).

### 1.3 Outbound reporting model (loop as producer)

The loop is not only the tracker's single *consumer* (section 0); it is also a
*producer*. A maintainer that works a codebase inevitably discovers NEW problems
— a bug adjacent to the work order, a broken test harness noticed during VERIFY,
a flaw in an adapter, a defect in the maintainer's own tooling. These
discoveries must be captured as durable, trackable items, not lost in-tick.

Two discovery sources feed one outbound sink:

- **Implementer discoveries** — the IMPLEMENT adapter surfaces follow-on work in
  `Handoff.discovered_work[]` (section 2.6). v1 *durably files* these through the
  REPORT port rather than only re-queuing them in memory.
- **Orchestrator / state discoveries** — the dispatcher and any tick state
  (`GUARD` … `CLEANUP`) may emit a `DiscoveredIssue` to the per-tick
  `discoveries` slot on `TickContext`. The slot is flushed through the REPORT
  port.

REPORT is the write-side mirror of PULL: an adapter-swappable outbound port,
invoked out-of-band — it is NOT a routed tick state in the spine of section 1.1.
Filing is an outward effect, so it obeys the same record-before-act,
exactly-once journal discipline as every other side effect (section 3.2.4): each
`DiscoveredIssue` carries a stable `dedup_key`, the intent is journaled before
the REPORT call, and a re-run or DRAIN replay never files a duplicate.

**Reporting is distinct from escalation.** Escalation (section 3.9.3) posts a
comment on the *triggering* issue to ask a human to look at *this* work. REPORT
*creates a new tracked item* for a *different* discovery. They are complementary
outbound channels, not the same one.

---

## 2. Pivotal decisions (the forks, resolved)

### 2.1 Where the WHAT-intelligence lives — implement-heavy by default

TRIAGE emits a validated *decision + pointer*; the IMPLEMENT agent generates
the spec/WHAT itself. *Rationale:* lowest prerequisite — a project needs no
spec subsystem to start. A spec-first TRIAGE is a pluggable adapter, not the
default. **[v1 default + seam]**

### 2.2 Parallelism / scope-conflict model — deferred

v1 is **serial-only**. *Rationale:* safe parallel autonomy needs a per-project
scope/conflict model (the deepest generality gap); serial is immediately useful
and avoids it. **[v2]**

### 2.3 Trust default — `propose`

Ship modes `dry-run` / `propose` / `gated-merge`; default `propose` (implement +
open PR, never auto-merge). *Rationale:* auto-merge is the scariest action; make
trust opt-in and graduated. **[v1]**

### 2.4 Extensibility — ports-and-adapters via script contracts

Each adapter state is a script with a typed slot contract (the slots it reads
and writes on `TickContext`, section 2.6); project config maps port -> adapter;
default adapters ship for GitHub + git + generic-implement. **[v1]**

### 2.5 The loop is a producer, not only a consumer

Section 0 frames the loop as the tracker's single consumer; 2.5 adds the
symmetric write side. The loop files newly discovered bugs/tasks back into a
tracker through a dedicated outbound REPORT port (section 2.6; section 3.11),
provenance-stamped so it never re-pulls and amplifies its own filings, and
routed so that defects in the *maintainer itself* land in a separate maintainer
tracker from defects in the *project*. *Rationale:* a maintainer that cannot
record what it finds silently drops real defects; a consumer-only model has
nowhere to put them. **[v1]**

### 2.6 Adapter contracts (slots, signals, manifests)

Every tick state implements the uniform signature `run(TickContext) ->
StateResult` (section 1.1). What used to be a point-to-point port signature is
now expressed as **which blackboard slots a state reads and writes**, plus the
**signals it emits**. The seven adapter states and their slot contracts:

```
state       reads                       writes                signals (typical)
PULL        —                           work_items            OK | EMPTY
TRIAGE      work_items                  work_orders           OK | EMPTY
PRIORITIZE  work_orders                 execution_plan        OK | EMPTY
IMPLEMENT   execution_plan, workspace   handoffs              OK | BLOCKED
VERIFY      handoffs                    verdicts              OK | FAULT
INTEGRATE   verdicts                    integration_result    OK
CLEANUP     integration_result          —                     OK
```

The slot **schemas** — the typed shape of `WorkItem`, `WorkOrder` (validated,
dedup'd, decomposed, ordered; + decisions), `ExecutionPlan` (order + parallel
groups + status backfill), `Handoff` (status, artifact=branch|pr,
discovered_work[], blocked_reason), `Verdict` (ok: bool, reasons[]),
`IntegrationResult` — are the load-bearing contracts. Their content is unchanged
from the old signatures; they are now carried as named slots on `TickContext`
rather than as a pipe between neighbors. A state knows only the slot schemas and
the closed signal vocabulary; it never knows another state's identity or
implementation.

`REPORT` is **not** a routed tick state: it is the outbound counterpart to the
`PULL` slot fill (section 1.3, decision 2.5), invoked out-of-band by the
orchestrator from the `discoveries` slot — never a node in the route — and its
adapter owns how a `DiscoveredIssue` becomes a tracked item (the default adapter
files a GitHub Issue):

```
REPORT      : DiscoveredIssue[]        -> ReportResult   (outbound; files new tracked items, provenance-stamped, idempotent by dedup_key)
```

`Handoff` is the load-bearing seam between the loop and any project's
implementer (TDD subagent, plain-PR agent, spec-then-code agent). The core
knows only these slot schemas; it never knows an adapter's implementation.

The two outbound schemas carried by REPORT:

- `DiscoveredIssue` — `{ title, body, kind: bug|enhancement|task, severity:
  low|medium|high|critical, origin: {tick_state, tick_id, work_order_id?}, target:
  project|maintainer-self, dedup_key, filed_by: autonomous-maintainer }`. The
  `body` is machine-first / structured, not free prose. `target` routes the
  item (project tracker vs. the maintainer's own tracker); `dedup_key` makes
  filing idempotent; `filed_by` stamps loop provenance so PULL/TRIAGE can
  recognize and not auto-consume the loop's own output.
- `ReportResult` — `{ filed: [{dedup_key, tracker_ref, url}], skipped_existing:
  [dedup_key], errors: [{dedup_key, reason}] }`. Re-filing an existing
  `dedup_key` is a no-op that returns the prior `tracker_ref`.

### 2.7 Decoupled tick states + declarative routing

The tick FSM (section 1.1) is built from fully decoupled states behind one
uniform contract (`run(TickContext) -> StateResult`) over a schema'd blackboard,
chained by a script orchestrator that reads a per-project `route.json`
transition table, and enforced by a route validator (anchor invariants, signal
validity, data-readiness). *Rationale:* point-to-point phase coupling makes the
spine rigid and the transition conditions implicit; a uniform slot contract plus
a declarative route makes states independently testable, lets a project insert
or reorder adapter states by editing data (not code), and turns the transition
table into an inspectable, statically-validated artifact — all while staying
machine-first and deterministic (script-tier). Core states remain
non-overridable anchors. The runtime (orchestrator + validator) belongs to the
lifecycle core; the schemas (slots, signals, `StateResult`, per-state manifest,
`route.json`) belong to the port-contract layer. **[v1]**

### 2.8 Execution model — in-session, model-orchestrated

The tick runs inside a **live Claude Code session**, which is the tick
**executor**. Tick states are two kinds:

- **script-states** — deterministic `run(ctx)` callables (tool-tier `script`):
  `GUARD`, `DRAIN`, `PULL`, `PRIORITIZE`, `PERSIST`, `EXIT`, and any deterministic
  adapter.
- **agent-states** — states that require a model (`TRIAGE`, `IMPLEMENT`; sections
  2.1, 3.5, 3.6). The session dispatches subagent(s) via the `Agent` tool per a
  declarative **dispatch schema** (tool-tier `spec`) and writes their structured
  output to the blackboard slot.

The deterministic route resolver + validator (section 1.1.1) still own control
flow; the session walks the route and, per state kind, either runs the script or
dispatches the agent-adapter. Subagents run one level below the orchestrator
(L0 session → L1 subagent); a dispatched subagent never dispatches another (the
2-level nesting cap, section 3.6.2).

*Rationale:* the only states that need a model cannot be reached from a
deterministic script — a `run(ctx)` cannot invoke the `Agent` tool. Inverting
control so the **session orchestrates and calls the scripts** (rather than a
headless script that shells out to a model) deletes the subprocess/headless
transport entirely and uses Claude Code's native subagent dispatch.

*Consequence — warm-only:* the loop runs only while a session for the project is
open. It is autonomous in **what** it decides and does, not in **when** it runs.

*Supersedes:* section 3.1.4's headless branch (behavior is warm-only; on-disk
state remains the sole source of truth for resume-after-reopen) and section
3.3.1's system-cron branch (the in-session durable heartbeat is the sole
scheduler). Section 0's "autonomous" is re-scoped to "autonomous within an open
session." **[v1]**

---

## 3. Feature areas (with adopt/defer decisions)

Decision tags: **[v1]** adopt now, **[v2]** next version, **[deferred]** later,
**[excluded]** out of scope.

### 3.1 Lifecycle Core
- **3.1.1** The tick FSM (section 1.1): the uniform state contract, the script
  orchestrator that chains states via `route.json`, and the route validator
  (anchor invariants, signal validity, data-readiness). **[v1]**
- **3.1.2** Lifecycle dispositions (section 1.2): dispositions + transitions,
  each durably encoded by a marker/state. **[v1]**
- **3.1.3** Single-writer mutual exclusion (running-guard + stale-marker
  detection). **[v1]**
- **3.1.4** Host-agnostic resumption contract — identical behavior on fresh
  (headless) or warm (in-session) context; on-disk state is the only source of
  truth. **[v1]** *(Superseded by section 2.8: execution is warm-only; on-disk
  state remains the sole source of truth for resume-after-reopen.)*

### 3.2 State, Resumability & Idempotency
- **3.2.1** Versioned durable state schema (single JSON, semver'd). **[v1]**
- **3.2.2** Per-tick journal (record-before-act; drives skip-on-resume). **[v1]**
- **3.2.3** DRAIN owed-work entry step (finish a truncated prior tick first).
  **[v1]** *(non-negotiable for crash-safety)*
- **3.2.4** Idempotency / dedup-key convention for every outward effect. **[v1]**
  *(correctness, not optional: e.g. record "PR opened for #N" BEFORE the agent
  call, so a re-run never opens a second PR for the same item)*
- **3.2.5** State compaction/rotation (bound growth over months). **[v2]**
  *Rationale:* only matters after long runtime; not a day-1 correctness issue.

### 3.3 Scheduling, Heartbeat & Restart
- **3.3.1** Scheduler detection (system cron where available, else an in-session
  durable heartbeat). **[v1]** *(forced by the platform: a plugin cannot
  install its own clock)* *(Superseded by section 2.8: the in-session durable
  heartbeat is the sole scheduler; the system-cron branch is dropped.)*
- **3.3.2** Heartbeat install/uninstall bootstrap — the "configure" step wires the
  clock; the user authorizes it. **[v1]**
- **3.3.3** Immediate-refire decision (work-remains -> one-shot; queue-empty ->
  idle) with at-most-one-refire dedup. **[v1]**
- **3.3.4** Restart-and-resume (RESTART_NEEDED marker -> SessionStart auto-resume).
  **[v1]** *(forced by the platform: self-modifying code needs a restart; also
  the hook for self-evolution)*

### 3.4 State Ports & Adapters
- **3.4.1** Typed contracts for all adapter states (section 2.6): the
  `TickContext` slot schemas, the closed signal vocabulary, the `StateResult`
  envelope, the per-state read/write/emit manifest, and the `route.json`
  schema. **[v1]**
- **3.4.2** Default adapters: GitHub-Issues `PULL`, generic `IMPLEMENT`, git+PR
  `INTEGRATE`. **[v1]**
- **3.4.3** Override + routing mechanism (project config maps each port to a
  script; `route.json` defines the transition table and may insert or reorder
  adapter states between the core anchors). **[v1]**
- **3.4.4** Adapter SDK + authoring docs. **[v2]** *Rationale:* v1 ships working
  defaults; formal SDK ergonomics follow once contracts are battle-tested.
- **3.4.5** Built-in non-GitHub trackers (Jira / Linear). **[deferred]**
  *Rationale:* the *port* exists in v1; extra built-in adapters are additive.

### 3.5 TRIAGE Pipeline
- **3.5.1** Intake / normalize to a canonical WorkItem. **[v1]**
- **3.5.2** Validity gate (well-formed, in-scope, non-spam, not stale). **[v1]**
- **3.5.3** Dedup vs closed (already-resolved guard); back with native
  mark-as-duplicate where available. **[v1]**
- **3.5.4** Dedup vs open (merge overlapping open items into one). **[v2]**
  *Rationale:* useful but a known gap even in rabbit today; not a correctness
  blocker.
- **3.5.5** Decompose (one level) + parent/child linkage via native sub-issues.
  **[v1]**
- **3.5.6** Recursive decompose + atomicity test. **[v2]** *Rationale:* one level
  covers the common case; recursion adds convergence complexity.
- **3.5.7** Dependency / ordering via native issue dependencies. **[v1]**
- **3.5.8** WHAT-generation / spec adapter (per decision 2.1; default off, rabbit
  wires it on). **[v1 seam, default off]**

### 3.6 IMPLEMENT Contract
- **3.6.1** `Handoff` schema — the swap seam (section 2.6). **[v1]**
- **3.6.2** Subagent isolation = hard rule + per-dispatch isolated workspace
  (worktree). **[v1]** *(also: the 2-level nesting cap means the loop dispatches
  the implementer directly, never wrapped in another agent)*
- **3.6.3** Default implementer = generic implement-then-PR. **[v1]**
- **3.6.4** TDD implementer as an optional bundled adapter (rabbit's path).
  **[v1, optional]** *Rationale:* keeps TDD available without making it a
  prerequisite.
- **3.6.5** Long-run / partial-completion handling (label-before-dispatch
  visibility; resumable mid-implement). **[v1]**

### 3.7 VERIFY & INTEGRATE
- **3.7.1** VERIFY gate = `{ok, reasons[]}`, conservative default (no merge unless
  explicitly green). **[v1]**
- **3.7.2** Default gates: CI status + test pass. **[v1]**
- **3.7.3** INTEGRATE = pluggable VCS hook covering merge + release + branch
  cleanup. **[v1]**
- **3.7.4** Idempotent release / tag (create-if-not-exists). **[v1]**
- **3.7.5** Non-git VCS adapters (GitLab MR / Gerrit). **[deferred]**
  *Rationale:* port exists; extra backends are additive.

### 3.8 Safety, Authority & Governance
- **3.8.1** Declarative guardrails (red-flags as config the host enforces: never
  merge a wrong base, never delete a non-matching branch, never merge a dirty
  tree). **[v1]**
- **3.8.2** Trust ladder (`dry-run` / `propose` / `gated-merge`, per decision
  2.3). **[v1]**
- **3.8.3** No `AskUserQuestion` in autonomous mode -> ABORTED marker + escalation
  instead of blocking prompts. **[v1]**
- **3.8.4** Budget caps (per-tick / per-day token ceiling, hard stop). **[v1]**
  *Rationale:* an unbounded loop is a financial hazard; needs a real ceiling,
  not judgment.
- **3.8.5** Backoff / circuit-breaker (consecutive-failure + per-item defer counts
  -> stop thrashing). **[v1]**
- **3.8.6** Blast-radius caps / learned scope inference. **[v2]** *Rationale:*
  basic guardrails (3.8.1) suffice for serial v1; inference is hard and tied to the
  parallel tier.

### 3.9 Observability & Surfacing
- **3.9.1** Structured, tail-able event log — source of truth for "what did the
  loop do", readable by other sessions. **[v1]**
- **3.9.2** SessionStart banner + dispatcher-persona injection (the CLAUDE.md
  substitute, since a plugin has no always-on context). **[v1]**
- **3.9.3** Escalation channel = issue-comment by default. **[v1]**
- **3.9.4** Slack / email / webhook escalation. **[v2]** *Rationale:* issue-comment
  needs no extra integration; richer sinks are additive.

### 3.10 Config, Persona & Packaging
- **3.10.1** `userConfig` prompted at enable (tracker token, mode, budget). **[v1]**
- **3.10.2** Project-local config file (port -> adapter wiring; read via
  `CLAUDE_PROJECT_DIR`). **[v1]**
- **3.10.3** Persona via SessionStart hook + skill bodies (the CLAUDE.md
  substitute). **[v1]**
- **3.10.4** `.claude-plugin/plugin.json` + marketplace layout + install / configure
  / run UX. **[v1]** *(Claude marketplace compliance is a v1 requirement, for
  ease of user install.)*
- **3.10.5** Dogfood: rabbit-workflow as adapter #1 (prove the loop runs rabbit's
  TDD / scope features through the ports). **[v1 validation goal]**
- **3.10.6** Self-evolution (the plugin evolving its own code) + reload / restart.
  **[v2]** *Rationale:* depends on 3.3.4 being solid first; high-risk, not needed
  to "maintain a project".

### 3.11 Outbound Discovery & Reporting

The producer side of decision 2.5 (section 1.3). A v1 capability implemented
across existing feature areas — the REPORT contract belongs to the port-contract
layer (section 3.4), a default filing adapter sits alongside the GitHub PULL
adapter (3.4.2), and the loopback guard belongs to safety (section 3.8). No new
top-level feature is required.

- **3.11.1** REPORT port + `DiscoveredIssue` / `ReportResult` schemas (section 2.6).
  The outbound seam, adapter-swappable like every other port. **[v1]**
- **3.11.2** Default filing adapter — creates a tracker item (GitHub Issue) from a
  `DiscoveredIssue`, mirroring the GitHub-Issues PULL adapter (3.4.2). **[v1]**
- **3.11.3** Durable filing of IMPLEMENT discoveries — `Handoff.discovered_work[]`
  (3.6.1) is filed via REPORT, not merely re-queued in memory, so a discovery not
  worked the same tick is never lost. **[v1]**
- **3.11.4** Idempotent, journaled filing — every `DiscoveredIssue` carries a
  `dedup_key`; the REPORT intent is recorded in the per-tick journal before the
  call (3.2.4), so a crash + DRAIN replay or a refire never files a duplicate.
  **[v1]**
- **3.11.5** Loopback / provenance guard — every loop-filed item is stamped
  `filed_by: autonomous-maintainer`. The validity and closed-dedup gates
  (3.5.2/3.5.3) MUST recognize the stamp; v1 policy is that the loop does NOT
  auto-work its own filings — they land for human triage unless explicitly
  opted in — preventing self-amplification. Owned by safety-governance
  (section 3.8). **[v1]**
- **3.11.6** Tracker routing — `DiscoveredIssue.target` selects the destination:
  `project` defects go to the project tracker PULL reads; `maintainer-self`
  defects (bugs in the loop's own adapters or tooling — the dogfood case 3.10.5)
  go to a configured maintainer tracker, a different repo. **[v1]**
- **3.11.7** Trust-ladder interaction — filing is a non-destructive write: permitted
  at `propose` and `gated-merge`; at `dry-run` the intent is logged, not filed
  (3.8.2). **[v1]**
- **3.11.8** Rich cross-tracker routing rules and bidirectional sync (mirroring
  state changes back from the maintainer tracker into the project view).
  **[v2]** *Rationale:* v1 needs only one-way filing with static project/self
  routing; richer routing is additive.

---

## 4. Explicitly excluded

Multi-repo / one-loop-many-projects; non-Claude-Code platforms; parallel
dispatch (-> v2); recursive decomposition (-> v2); learned scope inference
(-> v2); self-evolution (-> v2); bidirectional cross-tracker sync (-> v2, 3.11.8).

---

## 5. Concern-coverage check

| Concern | Home | Decision |
|---|---|---|
| Tick-FSM decoupling + routing | 1.1, 2.7, 3.1.1, 3.4.1, 3.4.3 | v1 |
| State / resumability | 3.2.1-3.2.3 | v1 |
| Idempotency / exactly-once | 3.2.4 | v1 |
| Scheduling / lifecycle | 3.3.1-3.3.3, 3.1.2 | v1 |
| Safety / authority / policy | 3.8.1-3.8.3 | v1 |
| Budget governance | 3.8.4 | v1 |
| Backoff / circuit-breaker | 3.8.5 | v1 |
| Observability + escalation | 3.9.1-3.9.3 | v1 |
| Config / adapter wiring | 3.4.3, 3.10.1-3.10.2 | v1 |
| WHAT-intelligence location | 2.1, 3.5.8 | v1 (implement-heavy default) |
| Outbound discovery / loop-as-producer | 2.5, 3.11.1-3.11.7 | v1 |
| Loop-filing loopback safety | 3.11.5 | v1 |
| Scope / conflict (parallelism) | 3.8.6, 2.2 | **v2** |
| Trust ladder | 3.8.2 | v1 |
| Fresh-vs-warm context | 3.1.4 | v1 |
| Self-evolution + restart | 3.3.4 (mech), 3.10.6 (feature) | 3.3.4 v1 / 3.10.6 v2 |
| Dogfood entanglement | 3.10.5 | v1 goal |
| Config approachability | 3.10.1-3.10.2 | v1 |
| Multi-repo / scale | — | excluded |
