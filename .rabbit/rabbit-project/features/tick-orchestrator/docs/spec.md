---
feature: tick-orchestrator
version: 0.1.0
owner: changyu87
deprecation_criterion: Retired/superseded when the declarative-route execution model is replaced, or folded into a larger lifecycle-core runtime that the project migrates to.
---

# tick-orchestrator

## Purpose

The **external router/runner** that executes a `route.json` over states
conforming to `fsm-contracts`. It is the **only** component that reads
`route.json`; no state knows it exists. This first milestone is a **minimal
slice**: transition resolution + a run loop to a terminal state + structural
route validation. Journaling, checkpointing, and lifecycle dispositions are
deferred.

> Design references: DESIGN.md §1.1.1 ("Routing is a declarative data file,
> chained by a script orchestrator"), §3.1.1 (orchestrator + route validator),
> §2.7. Tool-tier: `script` (deterministic), spec-rules §1.

## Paths governed

Greenfield — authored from scratch.

## Public surface

1. **`resolve_next(route, state, signal) -> next_state`** — pure transition
   resolution. The decoupling core: maps `(state, signal)` via route *data* to
   the successor. **States never call this**; the router does.

2. **The run loop** — load route → run the current state's `run(ctx)` → read
   `StateResult.signal` → apply `StateResult.writes` to `TickContext` →
   `resolve_next` → repeat until a terminal state is reached. Deterministic,
   script-tier.

3. **Structural route validators** (run before a tick):
   - **signal-validity**: every `on` key ∈ that state's declared `emits`; every
     transition target exists. (§1.1.1 check 2)
   - **data-readiness**: on every path reaching a state, each slot it `reads`
     was `written` by a predecessor. (§1.1.1 check 3)

## Decoupling guarantee (the property this feature exists to provide)

Inserting state C between A and B = author C + edit `route.json` (repoint A's
edge to C, add C→B). **Zero edits to A or B.** The router is the sole reader of
`route.json`; states only emit signals from their declared closed set. This is
the direct answer to the point-to-point coupling problem: external routing, not
neighbor-naming.

## Conformance test (this milestone's proof)

A **generic, domain-free** two-state fixture proving end-to-end transition:

- Slot: `count` (integer), initial `0`.
- State `PING`: reads `count`, writes `count+1`; emits `GO` if `count < 2`,
  else `STOP`.
- State `PONG`: reads `count`, writes `count+1`; emits `GO` (always).
- `route.json`: `PING --GO--> PONG`, `PONG --GO--> PING`,
  `PING --STOP--> END` (terminal).
- Expected run from `count=0`:
  `PING (0→1, GO) → PONG (1→2, GO) → PING (2→ STOP) → END`.
- Asserts: blackboard slot read/write, the `StateResult` envelope, signal
  emission from a closed set, route resolution, loop re-entry, terminal halt.

These states have **zero maintainer-domain meaning** — they validate the
*mechanism*, not the spec. They live in this feature's `test/` as fixtures and
are **never promoted to features**.

## Current behaviour

Implemented and merged (`tdd_state: test-green`). `resolve_next`, the run loop,
and the structural validators (`validate_signals`, `validate_data_readiness`) are
in `src/` with 13 passing tests. See `feature.json` / `docs/ROADMAP.md`.

## Known gaps / deferred (explicit boundaries)

- **Per-tick journal / record-before-act / skip-on-resume** (§3.2.2) → deferred
  to `durable-state`.
- **Checkpointing / crash-safe resumption** (§3.1.4) → deferred.
- **Disposition selection** (`RUNNING`/`IDLE`/`STOPPED`/`ABORTED`/
  `RESTART_NEEDED`) + **single-writer mutex** (§1.2, §3.1.2/3.1.3) → deferred to
  `lifecycle-dispositions`.
- **Anchor-invariant validation** (GUARD entry, DRAIN-before-adapters,
  PERSIST-before-EXIT) (§3.1.1 check 1) → deferred (spine-specific; would block
  the generic two-state test).
- **`REPORT` out-of-band flush** (§1.3) → deferred to `outbound-report`.

## Open questions

- Where does the validator run — once at config time, or before every tick?
  (Design says "before any tick"; for now it is invoked explicitly by the test.)
- Terminal-state representation in `route.json`: an explicit `terminal: [...]`
  list vs. a state with no out-edges.

## Dependencies

- **`fsm-contracts`** — consumes `TickContext`, `StateResult`, the signal
  vocabulary, the per-state manifest, and the `route.json` schema.
