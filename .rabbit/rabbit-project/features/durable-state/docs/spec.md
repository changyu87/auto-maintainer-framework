---
feature: durable-state
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the durable-state schema reaches a breaking major version (e.g. v2 adds compaction/rotation, DESIGN §3.2.5) or when the persistence layer is replaced.
---

# durable-state

## Purpose

The backbone of resumability: a **versioned durable state file**, a **per-tick
record-before-act journal**, the **DRAIN** owed-work entry state, the **PERSIST**
state, and an **idempotency / dedup-key** convention for outward effects. Makes a
tick crash-safe — a truncated tick is finished (not redone) on the next run.

> Design references: DESIGN.md §3.2.1–§3.2.4, §1.1 (DRAIN/PERSIST core states),
> §3.1.4 (on-disk state is the only source of truth).

## Paths governed

Greenfield. Code under `.../features/durable-state/src/`. The durable state +
journal files live at a runtime path chosen by the loop core (passed in;
defaults under the project runtime dir). NOT shipped state — the user's project
owns its own runtime state file.

## Public surface

1. **`DurableState`** — load/save a single semver'd JSON document
   (`{schema_version, ...slots}`). On-disk file is the sole source of truth
   (§3.1.4). Atomic write (temp + rename) so a crash never leaves a torn file.
   Holds the maintainer's cross-tick state (for the slice: a `counter` + last-tick
   metadata).
2. **Journal** — append-only per-tick journal with **record-before-act**: the
   intent of an outward/mutating effect is journaled BEFORE the effect runs, each
   intent carrying a stable **dedup_key**. Drives skip-on-resume.
3. **`DRAIN` state** — `run(TickContext) -> StateResult` (fsm-contracts contract).
   Entry step that **finishes owed work from a prior truncated tick** before any
   new work: scans the journal for intents recorded-but-not-confirmed and
   completes/reconciles them idempotently (a target value is re-applied, never
   re-incremented). Emits `OK` (nothing owed, or owed work drained).
4. **`PERSIST` state** — `run(TickContext) -> StateResult`. Writes the durable
   state from `TickContext` slots to disk (the resumability backbone). Emits `OK`.
5. **Idempotency / dedup convention** — every outward effect records its intent
   (with `dedup_key`) before acting, so a re-run or DRAIN replay never double-acts
   (§3.2.4).

Per-state manifests for DRAIN and PERSIST (fsm-contracts `{reads, writes, emits}`).

## Crash-safety contract (the battle-test property)

If a tick is truncated after journaling an intent but before PERSIST, the next
tick's DRAIN replays the journal and brings durable state to the intended value
**exactly once** (no double-count, no lost update). Verified by a truncate→resume
test, not just inspection.

## Current behaviour

None yet — `tdd_state: spec`.

## Known gaps / deferred

- State compaction / rotation (§3.2.5) — v2.
- The disposition machine, GUARD/EXIT, mutex — owned by `lifecycle-dispositions`
  (orthogonal: disposition marker vs. state journal).
- The route, heartbeat, demo work — owned by `scheduling`.

## Interfaces (composition)

- Implements the fsm-contracts state contract for DRAIN/PERSIST so the
  `tick-orchestrator` runs them as route anchors.
- Consumed by `scheduling` (route includes DRAIN + PERSIST) and by
  `lifecycle-dispositions` resumption (reads durable state as source of truth).

## Open questions

- Exact on-disk location of the state + journal files (project runtime dir vs a
  configured path) — settle when `scheduling` wires the concrete loop; for tests,
  a temp path is injected.
