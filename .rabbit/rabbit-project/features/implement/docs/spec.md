---
feature: implement
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the model-backed implement-then-PR doer (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the Handoff schema reaches a breaking major version.
---

# implement

## Purpose

The `IMPLEMENT` adapter state (DESIGN §1.1, §2.6) — the first tick state that
*acts* on work. This feature ships the **dry-run reference adapter**: it turns
the `execution_plan` produced by PRIORITIZE into a list of `handoffs`, **without
performing any work**. It is deterministic and inert — no model, no diff, no
branch, no PR, no tracker write, no filesystem effect.

This is the **`dry-run` rung of the trust ladder** (DESIGN §2.3, §3.8.2:
`dry-run` / `propose` / `gated-merge`). Its job is to prove the act-side seam —
the `Handoff` schema, the `execution_plan → handoffs` slot wiring, the signal,
and the per-tick surfacing — with ZERO repo risk. The model-backed
implement-then-PR doer (the `propose` rung, DESIGN §3.6.3) is a separate,
swappable adapter deferred to a later milestone (see Deferred).

## Slot contract (DESIGN §2.6)

```
state      reads           writes    signals
IMPLEMENT  execution_plan  handoffs  OK | BLOCKED
```

- **reads** `execution_plan` ONLY. DESIGN §2.6 also lists `workspace` for
  IMPLEMENT, but `workspace` is the isolated worktree consumed by the
  model-backed doer (DESIGN §3.6.2). The dry-run adapter does no isolated code
  work, so it deliberately does NOT read `workspace` — keeping the route
  validator's data-readiness check (DESIGN §1.1.1) satisfiable without any
  predecessor writing `workspace`.
- **writes** `handoffs`.
- **emits** `OK` (handoffs produced, or the plan was empty), `BLOCKED` only when
  a plan entry is malformed and cannot be turned into a handoff.

## Handoff schema (this feature owns it)

Machine-first, versioned (DESIGN §3.6.1, §2.6 — the load-bearing seam between
the loop and any implementer).

```json
{
  "schema_version": "1.0.0",
  "work_order_id": "<id>",
  "status": "planned",
  "artifact": {"kind": "none", "ref": null},
  "discovered_work": [],
  "blocked_reason": null
}
```

- `status` — `planned` for the dry-run rung (no work performed). The schema's
  value space anticipates the doer's `opened` / `blocked` / `partial`, but the
  dry-run adapter only ever emits `planned`.
- `artifact` — `{kind, ref}`. DESIGN §2.6 lists `branch|pr`; the dry-run rung
  adds `none` (no artifact was created). `ref` is null for `none`.
- `discovered_work` — follow-on items the implementer surfaces (DESIGN §1.3,
  §3.11.3). The dry-run adapter discovers nothing → always empty.
- `blocked_reason` — null unless the handoff is blocked.

## Behaviour

1. Read `execution_plan` from `TickContext`.
2. For each `work_order_id` in `execution_plan.ordered` (in order), emit one
   handoff with `status="planned"`, `artifact={"kind":"none","ref":null}`,
   `discovered_work=[]`, `blocked_reason=null`.
3. Process the WHOLE plan — there is no budget cap (a per-task cap is not the
   spec's budget; the real token-ceiling budget, DESIGN §3.8.4, lives in
   safety-governance and only bites the model-backed doer).
4. Write `handoffs` and emit `OK`. An empty plan yields `handoffs=[]`, `OK`.
   A malformed entry (missing/empty id) yields a `BLOCKED` handoff for that
   entry with `blocked_reason` set, and the state signal is `BLOCKED`.

The state is a pure function of `execution_plan`: same input → byte-identical
handoffs.

## Adapter factory convention

Wired as a route-as-data adapter (adapter-wiring's
`factory(runtime) -> (StateManifest, run_callable)` convention), consumed by
`scheduling.run_tick` via `DEFAULT_ADAPTER_MAP`. Manifest declares
`reads=["execution_plan"]`, `writes=["handoffs"]`,
`emits=["OK", "BLOCKED"]`.

## Invariants

- Deterministic: no model, no wall-clock, no randomness, no network, no
  filesystem. Pure function of `execution_plan`.
- Inert / propose-nothing-to-the-world: never creates a branch, PR, commit, or
  tracker change. After a tick that runs it, `git status` is clean.
- No budget cap: processes every plan entry.
- Reads only `execution_plan` (NOT `workspace`); writes only `handoffs`.
- One handoff per ordered plan entry, in plan order.

## Deferred (NOT in this slice)

- **The model-backed implement-then-PR doer** (DESIGN §3.6.2/§3.6.3) — the
  `propose` rung: dispatches an isolated subagent in a `workspace` worktree,
  writes code, opens a PR (never auto-merges). Reads `workspace`. A separate
  swappable adapter for a later milestone.
- **TDD implementer adapter** (DESIGN §3.6.4) — rabbit's path, optional bundle.
- **Trust-ladder mode selection + budget token ceiling** (DESIGN §3.8.2/§3.8.4)
  — governance that gates the doer; lives in safety-governance.
- **Durable filing of `discovered_work` via REPORT** (DESIGN §3.11.3) — the
  dry-run adapter discovers nothing, so there is nothing to file yet.
