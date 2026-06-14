---
feature: work-intake
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when the tracker-read model changes incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a breaking major version).
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
   writes the `work_items` slot, and emits `OK` if any items were found else
   `EMPTY`. Per-state manifest: `{ reads: [], writes: ["work_items"],
   emits: ["OK", "EMPTY"] }`.

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
- **Loopback / provenance guard** — recognizing `filed_by: autonomous-maintainer`
  so the loop never auto-consumes its own filings (§3.11.5) — belongs to
  safety-governance; deferred (and moot until `REPORT`/outbound-report exists).
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
