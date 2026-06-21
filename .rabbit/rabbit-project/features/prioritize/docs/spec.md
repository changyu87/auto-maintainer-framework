---
feature: prioritize
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when ordering ceases to be deterministic (e.g. a model-backed prioritizer adapter replaces the default), or when the ExecutionPlan schema reaches a breaking major version.
---

# prioritize

## Purpose

The `PRIORITIZE` adapter state (DESIGN §1.1, §2.6): turn the validated
`work_orders` produced by TRIAGE into a concrete, ordered `execution_plan`
that the downstream `IMPLEMENT` state consumes. This is the third adapter in
the read-side pipeline (`PULL → work_items → TRIAGE → work_orders →
PRIORITIZE → execution_plan`).

PRIORITIZE is **deterministic** — it is one of the spec's non-model states.
It decides execution order and back-fills a per-order status into the plan.
It performs **no outward effect**: it reads and writes only blackboard slots.

## Slot contract (DESIGN §2.6)

```
state       reads        writes          signals
PRIORITIZE  work_orders  execution_plan  OK | EMPTY
```

- **reads** `work_orders` (the array of accepted WorkOrder dicts from TRIAGE).
- **writes** `execution_plan`.
- **emits** `OK` when the plan contains at least one entry; `EMPTY` when there
  are no accepted work orders to plan.

## ExecutionPlan schema (this feature owns it)

Machine-first, versioned (the producer owns its slot schema, mirroring how
work-intake owns WorkItem/WorkOrder).

```json
{
  "schema_version": "1.0.0",
  "ordered": ["<work_order_id>", "..."],
  "status": {"<work_order_id>": "pending"}
}
```

- `ordered` — the execution sequence: a list of `work_order_id`s.
- `status` — the back-filled per-order status map; every planned order starts
  `pending` (DESIGN §1.1 "back-fill status"). This status is an **in-slot,
  machine-first field only** — it is NOT written to the tracker (see Deferred).

## Behaviour

1. Read `work_orders` from `TickContext`.
2. Consider only orders with `decision == "accepted"` (rejected orders carry no
   execution intent). If none, emit `EMPTY` with an empty plan
   (`ordered=[]`, `status={}`).
3. Order the accepted orders by **identity / FIFO** — preserve the order TRIAGE
   produced (TRIAGE already owns dependency ordering, DESIGN §3.5.7). The
   WorkOrder schema carries no severity/priority field today, so v1 introduces
   no re-ordering key of its own.
4. **Serialize same-feature orders** (same-feature serialization, #214). Walking
   the accepted orders in FIFO order, keep at most **one** order per
   blast-radius feature: the FIFO-first order claims its feature(s), and any
   later order that shares **any** feature with an already-kept order is
   **deferred** (dropped from this plan). Cross-feature orders (disjoint feature
   sets) are all kept and fan out in parallel.
5. Back-fill `status[id] = "pending"` for every ordered (kept) entry.
6. Write `execution_plan` and emit `OK` when at least one order was kept, else
   `EMPTY`.

The state is a pure function of `work_orders`: same input → byte-identical plan.

### Why serialize same-feature orders (#214)

`IMPLEMENT` fans out **one implementer per ordered work_order in parallel**,
each branching from the same `main`. Two work_orders that touch the **same
feature** each bump that feature's **shared metadata** (`feature.json` version,
`docs/CHANGELOG.md`, `docs/contract.md`) and regenerate the committed `plugins/`
tree, so the moment one auto-PR lands the others **conflict** on that shared
metadata — even when their actual code is in different functions. This is the
concrete v1 instance of the deferred "parallel scope/conflict model" (DESIGN
§2.2 / §3.8.6). PRIORITIZE is the choke point that decides what fans out, so it
serializes here: a deferred order is simply absent from this tick's plan — it is
never acted, never recorded in the acted-ledger, so the loop re-pulls +
re-prioritizes it on a later tick; once the head order's PR merges, the next
same-feature order becomes the head and fans out then. The non-colliding
cross-feature case stays parallel.

### How a work_order's feature is inferred

The feature is derived **deterministically** from the order's declared
blast-radius (no model, no I/O):

1. Every feature named in a `Component:` (or `Component.`) line of the order's
   `body` — the convention the maintainer's own issues already use (e.g. a
   trailing `Component: scheduling.`). A value naming several features (e.g.
   `verify-integrate + scheduling`) is split on `+`/`,`/`&`/`and`/`/` so the
   order serializes against **each**. Matching is case- and trailing-punctuation
   insensitive.
2. Else every **label** on the order (labels often name the component).
3. Else a **per-order unique bucket** keyed on the order id — an order with no
   declarable feature is never serialized against another (the safe default:
   serialize only on a *proven* shared feature; it stays parallel).

## Adapter factory convention

PRIORITIZE is wired as a route-as-data adapter (adapter-wiring's
`factory(runtime) -> (StateManifest, run_callable)` convention), consumed by
`scheduling.run_tick` via `DEFAULT_ADAPTER_MAP`. Its manifest declares
`reads=["work_orders"]`, `writes=["execution_plan"]`, `emits=["OK", "EMPTY"]`
so the route validator's data-readiness check (DESIGN §1.1.1) statically
rejects any route that runs PRIORITIZE before `work_orders` exists, or
IMPLEMENT before `execution_plan` exists.

## Invariants

- Deterministic: no model call, no wall-clock read, no randomness, no network.
- No outward effect: reads/writes blackboard slots only; never touches the
  tracker or the filesystem.
- Identity ordering: the relative order of the **kept** work orders is preserved
  (FIFO; no severity/priority re-ordering).
- Same-feature serialization: at most one kept order per blast-radius feature
  per tick; same-feature surplus is deferred (absent from the plan), never
  re-ordered or dropped permanently. Cross-feature orders stay parallel.
- `EMPTY` ⇔ the plan has zero entries; `OK` ⇔ at least one.
- Bounded scope: reads only `work_orders`; writes only `execution_plan`.

## Deferred (NOT in this slice)

- **Parallel grouping (explicit `groups`)** — DESIGN §1.1 marks the `groups`
  surface **[v2]**; the plan still carries no `groups` key. Same-feature
  serialization (#214) achieves conflict-free fan-out by *shrinking* `ordered`
  to one-per-feature, not by adding a grouping surface.
- **Rebase-and-rebuild merge queue** — keeping the deferred same-feature orders
  in-flight and rebasing each onto the new `main` after a merge (a merge-queue
  concept) is left for a later slice; v1 simply re-pulls + re-prioritizes the
  deferred orders next tick.
- **Severity/priority re-ordering** — requires a priority key on WorkOrder
  (absent today); identity order stands until one exists.
- **Tracker-side status write** — DESIGN §1.1's "(e.g. in-progress)" implies
  marking the tracker issue in-progress, an outward effect that needs
  idempotency/journal/trust-ladder support (DESIGN §3.2.4, §3.8). Deferred to
  the safety-governance milestone; v1 back-fills status in-slot only.
