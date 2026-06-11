---
feature: work-intake
version: 0.1.0
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

## Current behaviour

None yet — `tdd_state: spec` (this is the seeded spec; implementation follows
this cycle).

## Known gaps / deferred

- **TRIAGE pipeline** (validity gate, dedup-vs-closed, 1-level decompose,
  dependency ordering, WHAT-generation seam) → `work_orders` — slice 2 (§3.5).
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
