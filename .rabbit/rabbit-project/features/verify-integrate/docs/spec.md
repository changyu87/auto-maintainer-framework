---
feature: verify-integrate
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult schemas reach a breaking major version.
---

# verify-integrate

## Purpose

The act-side CLOSE of the loop (DESIGN §1.1, §3.7): after `IMPLEMENT` opens a PR,
`VERIFY` gates it (CI + mergeability), `INTEGRATE` merges it (the VCS hook), and
`CLEANUP` does branch/release hygiene. These are the stages between "a PR exists"
and "the work is landed and tidied".

All three are **script-tier, deterministic `gh`** — no model. VERIFY is read-only
and always safe; INTEGRATE performs the single highest-stakes action in the system
(autonomous merge to the default branch) and is therefore the most tightly gated.

> Design references: DESIGN §3.7 (VERIFY gate `{ok, reasons[]}`, conservative
> default; INTEGRATE = merge+release+branch cleanup; idempotent release; CLEANUP),
> §2.6 (slot contract), §3.8.1 (declarative merge guardrails), §3.8.2 (trust
> ladder), §3.8.5 (backoff). Tool-tier: **CLI** (`gh`/`git`) — spec-rules §1.

## The cross-tick model (refines DESIGN §2.6)

DESIGN §2.6's slot contract has `VERIFY` read the same-tick `handoffs`. But a PR's
CI runs **asynchronously**: it is never green the tick the PR is opened, so a
same-tick VERIFY→INTEGRATE could never merge the loop's own PRs. The resolved
model: **GitHub is the source of truth for the loop's open PRs** — there is NO
durable PR-ledger to drift.

- The `IMPLEMENT` doer stamps every PR it opens with the `auto-maintainer` label.
- `VERIFY` each tick QUERIES `gh pr list --label auto-maintainer --state open` —
  the loop's currently-open PRs, regardless of which tick opened them.
- A PR opened in tick N is therefore re-checked every tick until its CI goes
  green, then merged in tick N+k. Cross-tick correctness falls out of querying
  GitHub live; no slot carries PRs across ticks.

So `VERIFY`'s effective input is "the loop's open PRs" (queried), not only the
just-produced `handoffs`. The §2.6 `handoffs → verdicts → integration_result`
shape is preserved as the in-tick slot flow; the PR SET is sourced from `gh`.

## Slot contract (DESIGN §2.6, refined)

```
state      reads                writes              signals
VERIFY     (gh: open PRs)       verdicts            OK | EMPTY
INTEGRATE  verdicts             integration_result  OK
CLEANUP    integration_result  —                    OK
```

## Schemas (owned here, machine-first, versioned)

- **`Verdict`** — one per open loop PR: `{ schema_version, pr_ref, url, ok,
  ci_state: passing|pending|failing|unknown, mergeable: bool, base, reasons: [str] }`.
  `ok` is the conservative AND: CI passing AND mergeable AND base == default
  branch. `reasons` explains a non-ok verdict.
- **`IntegrationResult`** — `{ schema_version, merged: [{pr_ref, url}],
  skipped: [{pr_ref, reason}], errors: [{pr_ref, reason}] }`. Idempotent: an
  already-merged PR leaves the open set, so a re-run never double-merges.

## VERIFY (slice 1)

- Lists the loop's open PRs via an INJECTABLE source (production: `gh pr list
  --label auto-maintainer --state open --json number,url,headRefName,baseRefName,
  mergeable,statusCheckRollup`; tests pass a stub — the determinism seam, mirror
  of `work_intake.gh_issue_source`).
- For each PR derives a `Verdict`: `ci_state` from the status-check rollup
  (all SUCCESS → passing; any FAILURE → failing; any PENDING → pending; none →
  unknown), `mergeable` from gh's `mergeable` field, `base` from `baseRefName`;
  `ok = ci_state==passing AND mergeable AND base==<default branch>`.
- Writes the `verdicts` slot; emits `OK` if any open PRs were found else `EMPTY`.
- Read-only: VERIFY never merges, closes, or writes to GitHub. Always runs at
  every trust mode (it only reports).

## INTEGRATE (slice 2)

- Reads `verdicts`. For each `ok` verdict, consults the **guardrails**
  (`safety_governance.merge_guardrails`, §3.8.1) and, if clean, merges via an
  injectable merge sink (production: `gh pr merge <pr> --merge --delete-branch`).
  Records `IntegrationResult`. Non-ok verdicts and guardrail violations go to
  `skipped`.
- **Trust-gated by `permits("merge", mode)`** (§3.8.2): merge is permitted ONLY
  at `gated-merge`. At `dry-run` and the default `propose`, INTEGRATE is a NO-OP
  that logs the would-merge intent — a human merges. Arming autonomous merge is
  an explicit `/auto-maintainer:configure --mode gated-merge`.
- Emits `OK`.

## CLEANUP (slice 2)

- Reads `integration_result`; ensures merged PRs' branches are deleted (folded
  into `--delete-branch`; an explicit sweep covers the residual case) and
  performs an idempotent release/tag if configured (create-if-not-exists).
  Emits `OK`. Deterministic, idempotent.

## Guardrails & backoff (consumed from safety-governance)

- **Merge guardrails (§3.8.1)** — `safety_governance.merge_guardrails(pr_meta,
  default_branch) -> {ok, violations}`: never-merge-wrong-base (base != default),
  never-merge-dirty (`mergeable` is CONFLICTING/unknown), never-delete a
  non-matching branch. INTEGRATE refuses any PR with violations.
- **Backoff (§3.8.5)** — a PR whose VERIFY keeps failing is not retried forever;
  after K consecutive failing verdicts it is deferred/escalated (minimal v1;
  re-checking a red PR is cheap and never merges, so this only bounds noise).

## Adapter factory convention

Wired as route-as-data adapters (the `factory(runtime) -> (StateManifest,
run_callable)` convention), consumed by `scheduling.run_tick` via new
`make_verify` / `make_integrate` / `make_cleanup` factories + `DEFAULT_ADAPTER_MAP`
entries. The route option extends to `...IMPLEMENT → VERIFY → INTEGRATE →
CLEANUP → PERSIST → EXIT`.

## Invariants

- VERIFY is read-only (no GitHub writes); INTEGRATE merges ONLY at `gated-merge`
  and ONLY a PR that is `ok` AND passes guardrails.
- Deterministic given the injected `gh` seams; no model, no wall-clock beyond gh.
- Idempotent: a merged PR leaves the open set, so re-running never double-merges;
  release/tag is create-if-not-exists.
- Cross-tick: the open-PR set is sourced LIVE from GitHub (the `auto-maintainer`
  label), never a durable ledger.

## Deferred (NOT v1)

- Non-git VCS backends (GitLab MR / Gerrit) — §3.7.5, port exists, additive.
- Model-backed VERIFY (review-quality judgment beyond CI) — v1 is CI+mergeable.
- Rich release pipelines / changelog generation.
- Aggressive backoff / circuit-breaker tuning (§3.8.5) beyond the minimal counter.

## Interfaces (composition)

- Implements the fsm-contracts state contract; `tick-orchestrator` runs the three
  as route states; `scheduling` wires them.
- Consumes `safety-governance` (`permits("merge", …)` + `merge_guardrails`).
- Reads the loop's open PRs (stamped `auto-maintainer` by the `implement` doer)
  via the `gh` CLI; merges via `gh`.
- Declares no shippable `ship/` components of its own; its states ship as libs.
