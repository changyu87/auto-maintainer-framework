---
feature: work-intake
version: 0.3.2
owner: changyu87
deprecation_criterion: Superseded when the tracker I/O model changes incompatibly (e.g. multi-tracker support, or the WorkItem / WorkOrder / DiscoveredIssue schema reaches a breaking major version).
---

# work-intake

## Purpose

The read-side first adapter: fetch actionable work from the tracker into the
blackboard. **Slice 1 implements the GitHub-Issues `PULL` adapter only** — each
tick fetches the repo's open issues into the `work_items` slot. This is the first
real maintainer work, replacing the `DEMO_WORK` stub. The `TRIAGE` pipeline
(normalize → validate → dedup → decompose → order → `work_orders`) is deferred to
slice 2.

> Design references: DESIGN.md §3.4.2 (GitHub-Issues PULL adapter), §2.6 (PULL
> contract: reads —, writes `work_items`, signals `OK`|`EMPTY`; the `WorkItem`
> slot schema), §3.5.1 (intake/normalize). Tool-tier: **CLI** (`gh`) — spec-rules §1.

## Paths governed

Greenfield. Code under `.../features/work-intake/src/`.

## Public surface (slice 1)

1. **`WorkItem` slot schema** — the typed shape of a tracker item:
   `{ id, number, title, body, url, state, labels: [str], author, created_at,
   updated_at }`. Machine-first, versioned (`schema_version`). Owned here
   (fsm-contracts deferred the concrete domain slot schemas to their owning
   features). Downstream features (TRIAGE/PRIORITIZE/IMPLEMENT) consume it.

2. **`PULL` state** — `run(TickContext) -> StateResult` (fsm-contracts contract).
   Fetches the configured repo's **open** issues, maps each to a `WorkItem`,
   **excludes any item the loop filed itself** (`is_loop_filed`, the §3.11.5
   loopback guard — see below), writes the surviving items to the `work_items`
   slot, and emits `OK` if any remain else `EMPTY`. Per-state manifest:
   `{ reads: [], writes: ["work_items"], emits: ["OK", "EMPTY"] }`.

3. **Injectable issue source (determinism seam)** — the production source shells
   the deterministic **`gh` CLI** (`gh issue list --state open --json
   number,title,body,url,state,labels,author,createdAt,updatedAt`), which carries
   its own auth. The source is INJECTABLE so tests pass a stub returning fixture
   issues — no network, fully deterministic (spec-rules §1: a failure is locatable
   to the fetch boundary, not a flaky live call).

4. **Repo resolution** — slice 1 resolves the target repo from the project's `gh`
   default / git remote, or an injectable `repo` argument. Explicit config
   (repo, token, label filters) is deferred to the configuration feature.

## Determinism & testability

The only non-deterministic edge (the live `gh` call) sits behind the injectable
source. Tests drive `PULL` with a stub source over fixture issues and assert the
`work_items` slot + `OK`/`EMPTY` signal; production wiring shells `gh`. No AI/prompt
tier anywhere.

## Slice 2 — TRIAGE (validity gate → work_orders)

Turn raw `work_items` into validated `work_orders`. Slice 2 implements a
**deterministic validity gate** only; richer TRIAGE is deferred.

1. **`WorkOrder` slot schema** — a validated, decision-carrying item:
   `{ id, work_item_id, title, body, url, labels, decision: accepted|rejected,
   reason, created_at }`. Machine-first, versioned. Written to the `work_orders`
   slot, consumed downstream (PRIORITIZE/IMPLEMENT, future).
2. **`TRIAGE` state** — `run(TickContext) -> StateResult`: reads `work_items`,
   applies a deterministic validity gate (well-formed = has a title; not stale =
   updated within a hardcoded window; in-scope = open, non-draft), maps each
   accepted item to a `WorkOrder` (1:1, no decompose), writes the `work_orders`
   slot, emits `OK` if any accepted else `EMPTY`. Rejected items may be recorded
   with a reason but are not forwarded. Manifest `{reads: ["work_items"],
   writes: ["work_orders"], emits: ["OK","EMPTY"]}`. Script-tier, no AI.
3. **Determinism** — pure rules over the in-memory `work_items`; no network, no
   AI. The stale window is hardcoded (config deferred, #17-style).

## Shipped subagent — the TRIAGE judge

work-intake owns the TRIAGE domain and the `WorkOrder` schema, so it ships the
real triage JUDGE as a subagent: `ship/agents/auto-maintainer-triager.md`. The
build's `ship/` collection copies `ship/agents/` → the plugin's `agents/` with
NO build change, so the shipped file IS the deployed subagent definition.

- **Read-only judgment, not action.** The triager produces `work_orders` each
  carrying `decision: accepted|rejected` + a `reason`; it never modifies the
  tracker or the repo. Its tools are the read-only `Read`/`Grep`/`Glob` plus
  `Write` (to emit its output file). Enacting a decision — close a rejected
  item, implement an accepted one — is the later IMPLEMENT acting state's job,
  not the triager's.
- **Protocol-free, prompt-contracted.** The subagent definition bakes in NO
  output schema, output_path, dispatch-result filename, or file-format detail.
  Those are carried by the invocation-envelope prompt that agent-dispatch
  renders at the `TRIAGE` agent-state. The triager copies the concrete output
  example the prompt embeds.
- **Wiring.** The `TRIAGE` agent-state's adapter-map entry names this subagent
  (`subagent_type: auto-maintainer:auto-maintainer-triager`) with manifest
  `{reads: ["work_items"], writes: ["work_orders"], emits: ["OK","EMPTY"]}`. The
  TRIAGE→triager wiring validates through adapter-wiring's `build_loop` (TRIAGE
  resolves to an AgentState; data-readiness is satisfied because PULL writes the
  `work_items` TRIAGE reads).

The deterministic in-process `Triage` validity gate (Slice 2 below) and this
LLM triage judge coexist: the gate is the script-tier fast path; the judge is
the agent-tier path a project wires at the `TRIAGE` port when richer judgment is
wanted.

- **Loopback / provenance guard (§3.11.5) — enforced at PULL, by EXCLUSION (not
  by reject).** Items the loop filed itself carry the provenance stamp
  `filed_by: autonomous-maintainer` — the `filed-by:autonomous-maintainer` label
  (and the `<!-- am-dedup:... -->` body marker REPORT writes, see Slice 3). The
  v1 policy is that the maintainer does NOT auto-work its own filings: they stay
  open for human triage, preventing self-amplification. Enforcement is a
  deterministic **PULL-side EXCLUSION**: `PULL` drops any work_item for which
  `work_intake.is_loop_filed(item)` is true, so loop-filed items never become
  `work_items` / `work_orders` and the doer never touches them. This is
  deliberately NOT a TRIAGE reject — a reject would route to the doer's close
  path and CLOSE the discovery, the opposite of "leave it open for a human." The
  triager therefore never sees loop-filed items and carries no special-case for
  them. `is_loop_filed` lives in work-intake (next to the stamp it recognizes:
  the label + `<!-- am-dedup: -->` body marker that `gh_issue_file_sink` writes).

## Slice 3 — REPORT (outbound filing → DiscoveredIssue)

The **write-side mirror of PULL** (DESIGN §1.3, §2.6, §3.11): an
adapter-swappable OUTBOUND port that turns discoveries into durably-tracked
items. work-intake owns the inbound tracker I/O (PULL), so it also owns the
outbound (REPORT) port, schemas, and default GitHub filing adapter. REPORT is
**out-of-band** — NOT a routed tick state; `scheduling.run_tick` flushes
discoveries through it after the route runs (that wiring + the journaled
idempotency live in scheduling).

- **`DiscoveredIssue` schema** (machine-first, versioned; owned here):
  `{ schema_version, title, body, kind: bug|enhancement|task, severity, target:
  project|maintainer-self, dedup_key, filed_by: "autonomous-maintainer" }`.
  `dedup_key` is a stable caller-supplied key making filing idempotent; `target`
  selects the destination tracker; `filed_by` stamps loop provenance.
- **`ReportResult` schema**: `{ filed: [{dedup_key, tracker_ref, url}],
  skipped_existing: [dedup_key], errors: [{dedup_key, reason}] }`. Re-filing an
  existing `dedup_key` is a no-op that returns the prior `tracker_ref`.
- **Injectable filing sink (determinism seam, mirrors `gh_issue_source`).** The
  production `gh_issue_file_sink(discovery, repo=None) -> {tracker_ref, url}`
  shells `gh issue create` with the title/body, the provenance label
  `filed-by:autonomous-maintainer`, and a `<!-- am-dedup:<dedup_key> -->` marker
  appended to the body (so a later PULL/TRIAGE — and a dedup re-scan — can
  recognize the filing). The sink is INJECTABLE so tests pass a stub (no
  network, deterministic; a failure is locatable to the file boundary).
  - **It ENSURES the provenance label exists first (live-found bug).**
    `gh issue create --label <L>` FAILS if label `L` is absent in the repo — and
    a fresh repo has no `filed-by:autonomous-maintainer` label, so every filing
    errored (silently, since `file_discoveries` catches sink errors). The sink
    therefore first runs `gh label create filed-by:autonomous-maintainer
    --description "filed by the autonomous maintainer"` (idempotent — a non-zero
    "already exists" exit is tolerated, NOT raised) before the issue create. Both
    `gh` calls honor `--repo` and the injectable `runner`.
- **`file_discoveries(discoveries, sink, known_dedup_keys) -> ReportResult`** —
  pure orchestration: for each `DiscoveredIssue`, if its `dedup_key` is in
  `known_dedup_keys` it goes to `skipped_existing` (no sink call); otherwise the
  sink is invoked and the result recorded in `filed`; a sink exception is caught
  and recorded in `errors` (filing one bad discovery never aborts the batch).
  Deterministic given the injected sink + known set; performs no I/O of its own.
- **Target routing (§3.11.6).** `target: project` files into the repo PULL reads
  (the default); `target: maintainer-self` files into a configured maintainer
  tracker (a different repo, the dogfood case §3.10.5). The sink maps `target` →
  the destination repo; v1 ships both with `project` as the default and the
  maintainer repo supplied via runtime/config.
- **Trust interaction (§3.11.7).** Filing is the `file` effect. `file_discoveries`
  itself is pure (it always files through the sink it is handed); the
  trust-ladder GATE lives in `scheduling.run_tick`, which only calls
  `file_discoveries` when `safety_governance.permits("file", mode)` — at
  `dry-run` the intent is logged, not filed; at `propose`/`gated-merge` it files.

## Current behaviour

Slice 1 (PULL) implemented and merged (`tdd_state: test-green`) — `WorkItem` +
`PULL` adapter, live in the loop. Slice 2 (TRIAGE validity gate → `work_orders`)
implemented and merged. This cycle ships the `auto-maintainer-triager` subagent
(the real TRIAGE judge) and proves the TRIAGE→triager agent-adapter wiring
validates.

## Known gaps / deferred

- **TRIAGE — slice 2 (this cycle):** deterministic validity gate → `work_orders`.
  **Slice 3+ deferred:** dedup-vs-closed (§3.5.3), 1-level decompose (§3.5.5),
  dependency ordering (§3.5.7), WHAT-generation/spec seam (§3.5.8, the AI seam).
- **Loopback / provenance guard (§3.11.5)** — IMPLEMENTED: `work_intake.is_loop_filed`
  recognizes the provenance stamp and `PULL` EXCLUDES loop-filed items (they never
  enter the pipeline; they stay open for humans). NOT a TRIAGE reject. Opt-in
  "let the loop work its own filings" stays deferred.
- **`PRIORITIZE`** (execution_plan) — separate state, deferred.
- **Non-GitHub trackers**, label/filter config, pagination tuning — deferred.

## Interfaces (composition)

- Implements the fsm-contracts state contract so `tick-orchestrator` runs `PULL`
  as a route state.
- Writes the `work_items` slot consumed downstream (TRIAGE/PRIORITIZE/IMPLEMENT,
  future).
- Invokes the external `gh` CLI (declared in the contract). Once integrated,
  `scheduling`'s route swaps `DEMO_WORK` → `PULL` (a separate integration step:
  scheduling route + packaging rebuild).

## Open questions

- Exact `WorkItem` field set vs. what TRIAGE will need — keep minimal now; extend
  when TRIAGE lands.
- How "actionable" is defined at PULL vs. TRIAGE (slice 1 pulls all open issues;
  validity/actionability filtering is TRIAGE's job).
