---
feature: prioritize
version: 0.1.0
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
4. Back-fill `status[id] = "pending"` for every ordered entry.
5. Write `execution_plan` and emit `OK`.

The state is a pure function of `work_orders`: same input → byte-identical plan.

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
- Identity ordering: the relative order of accepted work orders is preserved.
- `EMPTY` ⇔ the plan has zero entries; `OK` ⇔ at least one.
- Bounded scope: reads only `work_orders`; writes only `execution_plan`.

## Deferred (NOT in this slice)

- **Parallel grouping** — DESIGN §1.1 marks grouping **[v2]**; the v1
  ExecutionPlan carries no `groups`.
- **Severity/priority re-ordering** — requires a priority key on WorkOrder
  (absent today); identity order stands until one exists.
- **Tracker-side status write** — DESIGN §1.1's "(e.g. in-progress)" implies
  marking the tracker issue in-progress, an outward effect that needs
  idempotency/journal/trust-ladder support (DESIGN §3.2.4, §3.8). Deferred to
  the safety-governance milestone; v1 back-fills status in-slot only.
