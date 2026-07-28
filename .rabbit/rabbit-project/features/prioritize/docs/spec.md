---
feature: prioritize
version: 0.5.0
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
4. **Serialize same-feature orders**: keep at most ONE order per target feature
   (the FIFO-first wins the slot); DEFER the rest (omit them from the plan).
5. Back-fill `status[id] = "pending"` for every kept entry.
6. Write `execution_plan` and emit `OK`.

The state is a pure function of `work_orders`: same input → byte-identical plan.

## Same-feature serialization (issue #214)

IMPLEMENT fans out one implementer per planned order **in parallel**, each
branching off the same `main`. Two orders touching the **same feature** would
each bump that feature's shared metadata (`feature.json` version,
`docs/CHANGELOG.md`, `docs/contract.md`) and regenerate the committed `plugins/`
tree off the same base — so the moment one PR lands, the others **collide** on
that shared metadata even when their code is in different functions (observed:
scheduling PRs #205/#207 collided after #208 merged; #206 survived only by
targeting a different feature). PRIORITIZE is the choke point that decides what
fans out, so it serializes here:

- It keeps at most **one** accepted order per target feature per tick (FIFO-first
  wins the slot) and **defers** the rest. A deferred order is simply absent from
  this tick's plan, so the loop re-pulls and re-prioritizes it next tick; once
  the head order's PR merges, the next same-feature order fans out then.
- **Cross-feature** orders (disjoint feature sets) stay **parallel**, so the
  non-colliding case is unaffected.
- The plan surface is **unchanged** — serialization only shrinks `ordered`; no
  `groups` key (grouping stays [v2]).

The target feature(s) are read from the order's **authoritative**
`target_feature` field, which TRIAGE stamps from the blast-radius signals (issue
#258) — a single authoritative field, no re-scraping. For orders **lacking** that
field (an older slot, or one built outside TRIAGE) PRIORITIZE **falls back** to
re-deriving the feature from the same authoritative signals, never inferred from
generic labels:

- `feature:<name>` / `component:<name>` **prefixed labels** (the maintainer's
  filing convention). Generic labels (`bug`, `enhancement`, `filed-by:*`,
  `priority:*`) are **not** feature keys, so two `enhancement`-labelled orders
  for different features stay parallel.
- a `Component:` / `Feature:` **line in the issue body**, split into a
  multi-feature radius on `+ , & /` only — **never** on the word "and" (an "and"
  split can mis-cut a feature name such as `command-and-control`).
- a conventional **title prefix** — `name:` (take the name, e.g.
  `scheduling: ...`) or `type(scope):` (take the scope, e.g.
  `feat(scheduling): ...`, `fix(work-intake): ...`). This hardens detection for
  **label-less** issues (issue #257) that carry no feature label or `Component:`
  line. A **bare** conventional-commit type used without a scope (`feat`, `fix`,
  `docs`, `chore`, `refactor`, `test`, `perf`, `build`, `ci`, `style`) names
  **no** feature, so `fix: x` and `docs: y` are not grouped (the #216
  over-serialization regression); a `type(scope):` header keys on the scope.
- a **bracket-prefix** title — `[scope@team] ...` or `[scope] ...` (a filing
  convention observed in the live pool, e.g.
  `[dci-team-atlassian-sharepoint@dci-team] jira skill ...`): the leading
  `[...]` token's `scope` part (before any `@team`) is taken as the feature key.
  This is defense-in-depth for the case TRIAGE fails to stamp `target_feature`
  and the title uses this convention (the exact live miss where six same-scope
  orders fanned out in parallel and collided); the authoritative
  `target_feature` stays the primary source and this remains fallback-only.

An order with **no provable feature** carries no shared blast radius and stays
parallel — serialization rests on a *proven* shared feature, never a guess.

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
- Identity ordering: the relative order of kept work orders is preserved.
- Same-feature safety: never more than one order per target feature in a plan;
  the target feature(s) come from the order's authoritative `target_feature`
  field (TRIAGE-stamped, #258), with a fallback re-derivation from prefixed
  labels, a `Component:` line, and a conventional title prefix
  (`name:` / `type(scope):`) — never generic labels, never a bare
  conventional-commit type, and never the "and" connector.
- `EMPTY` ⇔ the plan has zero entries; `OK` ⇔ at least one.
- Bounded scope: reads only `work_orders`; writes only `execution_plan`.

## Deferred (NOT in this slice)

- **Parallel grouping** — DESIGN §1.1 marks grouping **[v2]**; the v1
  ExecutionPlan carries no `groups`.
- **Rebase-and-rebuild merge queue** — an alternative to deferral: keep
  same-feature orders parallel and, after each merge, rebase the remaining PRs
  onto the new `main` and regenerate the built tree. Deferred; the v1 fix
  serializes in PRIORITIZE instead (no merge-queue machinery required).
- **Severity/priority re-ordering** — requires a priority key on WorkOrder
  (absent today); identity order stands until one exists.
- **Tracker-side status write** — DESIGN §1.1's "(e.g. in-progress)" implies
  marking the tracker issue in-progress, an outward effect that needs
  idempotency/journal/trust-ladder support (DESIGN §3.2.4, §3.8). Deferred to
  the safety-governance milestone; v1 back-fills status in-slot only.
