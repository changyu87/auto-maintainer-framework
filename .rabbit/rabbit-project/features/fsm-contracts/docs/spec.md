---
feature: fsm-contracts
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the tick-FSM contract schema reaches a breaking major version — e.g. when the v2 parallelism tier (DESIGN §2.2) introduces scope/conflict slots that change the TickContext shape.
---

# fsm-contracts

## Purpose

Define the machine-first **contract layer** for the tick FSM: the schemas that
every tick state and the external router conform to. Pure data shapes and
structural validation rules — **zero execution logic, zero maintainer-domain
payloads**. This is the seam every state-feature and the orchestrator depend on.

> Design references: DESIGN.md §1.1.1 (decoupled states + declarative routing),
> §2.6 (adapter contracts — slots, signals, manifests), §2.7, §3.4.1.

## Paths governed

Greenfield — authored from scratch. No globs registered yet; this feature will
own the contract schema modules + JSON schemas once implemented.

## Public surface (the contracts this feature defines)

1. **`TickContext` blackboard** — a machine-first record of **named, typed,
   versioned slots**. A state reads the slots it needs and writes its products
   back into named slots. The slot registry is **open/extensible**: consumers
   (real maintainer states, or test fixtures) register their own slots. The
   contract defines the *slot mechanism*, not a fixed slot set. (§1.1.1)

2. **`StateResult` envelope** — `{ signal, writes: { <slot>: <value> },
   journal: [...] }`. The uniform outcome of every state. A state reports *what
   happened*; it never decides *what runs next*. (§1.1.1)

3. **Closed signal vocabulary** — a state emits a symbol from a **declared,
   closed set**. The *mechanism* treats the set as data: each state declares
   its signals via the manifest's `emits`. The v1 maintainer *instance* fixes
   its set to `OK | EMPTY | BLOCKED | OWED_WORK | FAULT | RESTART_REQUIRED |
   HALT_REQUESTED`; a generic test may declare its own (e.g. `GO | STOP`).
   "Closed and declared" is the contract; the specific members are an instance
   choice. (§1.1.1)

4. **Per-state manifest** — each state declares `{ reads: [slots],
   writes: [slots], emits: [signals] }`. The machine-first realization of
   bounded scope (philosophy §2). **No state names another state.** (§1.1.1)

5. **`route.json` schema** — the transition-table shape: a state set, the
   `(state, signal) -> next_state` edge table, and the terminal-state marker.
   Routing is **data, not code**, and lives **outside** every state. This
   feature owns the *shape* of `route.json`; the resolver that *executes* it is
   `tick-orchestrator`. (§1.1.1)

## Uniform state signature (contract, not implementation)

Every tick state implements `run(TickContext) -> StateResult`. A state never
receives a typed input from a named predecessor nor hands one to a named
successor. It reads named slots, does work, returns a `StateResult`.

This is the **decoupling guarantee**: inserting a state C between A and B is a
`route.json` data edit (repoint A's edge to C, add C→B) plus authoring C —
**neither A's nor B's code is touched.** Point-to-point coupling (where A names
B) is explicitly rejected.

## Current behaviour

None yet — feature is in `tdd_state: spec`. Schemas not authored.

## Known gaps / deferred (explicit boundaries)

- **Concrete maintainer domain slot schemas** — `WorkItem`, `WorkOrder`,
  `ExecutionPlan`, `Handoff`, `Verdict`, `IntegrationResult`,
  `DiscoveredIssue`/`ReportResult` (§2.6) — are deferred to their owning
  features (`work-intake`, `implement`, `verify-integrate`, `outbound-report`).
  This feature defines the slot *mechanism*, not the maintainer's concrete slot
  payloads. Keeping them out is what makes the contract domain-free and testable
  on arbitrary states.
- **Anchor-invariant validation** (GUARD is entry, DRAIN-before-adapters,
  PERSIST-before-EXIT) is **spine-specific policy** and is NOT owned here — it
  belongs to the lifecycle core (§3.1.1). Excluding it is what lets two
  non-domain states pass the structural contract.
- **The transition resolver and the run loop are runtime, not contract** —
  owned by `tick-orchestrator`. This feature only defines the `route.json`
  *shape* the resolver consumes.
- **Slot versioning / migration policy** (§3.2.1 versioned durable state) —
  deferred.

## Open questions

- Encoding of slot type declarations: JSON Schema per slot vs. a lighter typed
  registry? (Leaning JSON Schema for machine-first validation.)
- Does the per-state manifest live alongside each state's code or inside
  `route.json`? (Leaning: declared by the state, aggregated by the router for
  validation.)

## Relationship to `tick-orchestrator` (first milestone)

`tick-orchestrator` consumes these schemas to run the generic **two-state
ping-pong conformance test** (`PING`/`PONG` over a `count` slot, signals
`GO`/`STOP`). That test proves the contract works on states with **zero
maintainer-domain meaning** — validating the mechanism, not the spec. See
`tick-orchestrator/docs/spec.md`.
