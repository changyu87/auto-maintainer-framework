---
feature: implement
version: 0.2.2
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

## Shipped implementer subagent (the propose-rung doer)

This feature also ships the **model-backed implementer subagent** as a
deployable artifact at `ship/agents/auto-maintainer-implementer.md` — the
`propose`-rung doer dispatched at the IMPLEMENT agent-state (DESIGN §3.6.2/§3.6.3).
It is **protocol-free**: it holds no built-in knowledge of the Handoff schema or
the output path. Its rendered prompt is the complete handoff contract (the
`## Task` / `## Inputs` / `## Handoff` envelope produced by agent-dispatch).

- **Role.** Given exactly ONE work order in its prompt, it *enacts that work
  order's triage decision*: `rejected` → close the source issue with a
  justification comment, no code change; `accepted` → implement the change
  (it owns the WHAT, DESIGN §2.1), run the project's checks, and **open a PR
  against the default branch — never merge**. If it cannot complete an accepted
  order it reports `status: blocked` and leaves no open PR.
- **Pre-handoff self-review (v2.3.0).** On the accept path, after committing and
  BEFORE `gh pr create`, the subagent runs a structured self-review against its
  OWN committed diff (it reads the actual diff, not its intent) and fixes any gap
  before opening the PR. The checklist has four lenses: **completeness** (did it
  do exactly what the issue asked — nothing more, nothing less), **quality**
  (follows existing patterns; no overbuild / YAGNI), **discipline** (only
  in-scope changes; separate problems go to `discovered_work[]`, not the diff),
  and **checks** (the project's tests/build pass on the committed change). The
  self-review is a self-check, not a licence to expand scope: an out-of-scope fix
  is surfaced as `discovered_work[]`, and an order that cannot be done cleanly
  within scope yields `status: blocked` with no open PR. (Adopted from Claude's
  superpowers `subagent-driven-development` implementer "Before reporting"
  checklist; the interactive "ask questions mid-task" behaviour is deliberately
  NOT adopted — the unattended loop's equivalent is `status: blocked`.)
- **PR provenance label (v2.1.0).** An opened PR is stamped with the
  `auto-maintainer` label (`gh pr create --label auto-maintainer`, creating the
  label if absent). This is the §3.7 hand-off seam to `verify-integrate`: VERIFY
  finds the loop's own open PRs by querying `gh pr list --label auto-maintainer
  --state open`. The label is the only coupling between IMPLEMENT and the
  VERIFY/INTEGRATE chain (no durable PR-ledger).
- **Isolation — the subagent manages its OWN worktree (v2.0.0,
  auto-maintainer-framework#143 follow-up).** It is dispatched WITHOUT the
  `isolation: "worktree"` adapter flag. That flag uses Claude Code's worktree
  isolation, which **sandboxes the subagent's file writes to the worktree** — so
  the subagent's Handoff file could not reach the shared main-workspace
  `dispatch-out/` (and for the reject path the worktree auto-cleans, deleting the
  file), breaking the file-based handoff. Instead, on the accept path the
  subagent (a coding agent with `Bash`) creates its OWN git worktree off the
  default branch OUTSIDE the repo tree (`git worktree add`), does all editing /
  committing there, opens the PR, then removes the worktree (DESIGN §3.6.2: the
  doer owns its workspace). The reject path needs no worktree. Because the
  subagent is NOT Claude-sandboxed, it CAN write its Handoff to the absolute
  `output_path`. The main checkout's branch/index/uncommitted state is never
  disturbed; `git status` on the main tree stays clean.
- **Output.** Writes its Handoff to the (absolute) file named in the prompt's
  `## Handoff` section and replies with only a short ack — its output never
  passes through the orchestrator's context.
- **Governance is external.** The subagent always acts for real when dispatched;
  the trust-ladder gate, budget window, and acted-ledger idempotency
  (DESIGN §3.8.2/§3.8.4/§3.2.4) are enforced upstream by `scheduling.run_tick`,
  which only dispatches it under `propose`+ modes. The subagent itself carries no
  mode logic.
- **Frontmatter.** Lifecycle-compliant (`version`, `owner`,
  `deprecation_criterion`), `model: opus`, and a coding toolset that includes
  `Bash` and `Write`.

This ships the subagent *definition*; the run_tick governance that arms it
(S2.1a/S2.1b) already exists. Adapter-map wiring of the IMPLEMENT state to this
subagent and live verification of the propose path are exercised at packaging
and config time.

## Invariants

- Deterministic: no model, no wall-clock, no randomness, no network, no
  filesystem. Pure function of `execution_plan`.
- Inert / propose-nothing-to-the-world: never creates a branch, PR, commit, or
  tracker change. After a tick that runs it, `git status` is clean.
- No budget cap: processes every plan entry.
- Reads only `execution_plan` (NOT `workspace`); writes only `handoffs`.
- One handoff per ordered plan entry, in plan order.
- The shipped implementer subagent `ship/agents/auto-maintainer-implementer.md`
  exists with lifecycle-compliant frontmatter (`version`, `owner`,
  `deprecation_criterion`), `model: opus`, and a coding toolset that includes
  `Bash` and `Write`.

## Deferred (NOT in this slice)

- **The model-backed implement-then-PR doer's run-side adapter** (DESIGN
  §3.6.2/§3.6.3) — the dry-run adapter remains the wired reference; the
  subagent *definition* for the `propose` rung is now shipped (see "Shipped
  implementer subagent"), but selecting it as the IMPLEMENT adapter and reading
  `workspace` is exercised at adapter-map/config time, not here.
- **TDD implementer adapter** (DESIGN §3.6.4) — rabbit's path, optional bundle.
- **Trust-ladder mode selection + budget token ceiling** (DESIGN §3.8.2/§3.8.4)
  — governance that gates the doer; lives in safety-governance.
- **Durable filing of `discovered_work` via REPORT** (DESIGN §3.11.3) — the
  dry-run adapter discovers nothing, so there is nothing to file yet.
