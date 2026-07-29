---
feature: verify-integrate
version: 0.9.3
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult / ReconcileResult schemas reach a breaking major version.
---

# verify-integrate

## Purpose

The act-side CLOSE of the loop (DESIGN §1.1, §3.7): after `IMPLEMENT` opens a PR,
`VERIFY` gates it (mergeability + base; CI optional), `INTEGRATE` merges it (the
VCS hook), and `CLEANUP` does branch/release hygiene. These are the stages between
"a PR exists" and "the work is landed and tidied".

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
  green, then merged in tick N+k — no slot carries PRs across ticks.

## Slot contract (DESIGN §2.6, refined)

```
state      reads                            writes              signals
VERIFY     cross_cutting_risk (gh: open PRs) verdicts, cross_check  OK | EMPTY
REVIEW     verdicts                         review_findings     OK | EMPTY
GATE       verdicts (+ config)              gate_results        OK
INTEGRATE  verdicts, gate_results           integration_result  OK
CLEANUP    integration_result              —                    OK
```

The acting pipeline runs `… VERIFY → REVIEW → GATE → INTEGRATE → CLEANUP …`
(GATE inserted between REVIEW and INTEGRATE; the route edge is wired by
`scheduling`).

## Schemas (owned here, machine-first, versioned)

- **`Verdict`** — one per open loop PR: `{ schema_version, pr_ref, url, ok,
  ci_state: passing|pending|failing|unknown, mergeable: bool, base, reasons: [str],
  orphaned: bool }`.
  `ok` is the conservative AND of the BLOCKING conditions: mergeable AND
  base == default branch. CI is RECORDED (`ci_state`) but is OPTIONAL — it no
  longer gates `ok` (DESIGN §3.7.1/§3.7.2: the correctness gate lives in
  IMPLEMENT). `reasons` explains a non-ok verdict, and DISTINGUISHES a transient
  DEFERRED mergeability (`mergeable=UNKNOWN` still unresolved after VERIFY's
  bounded poll — see below) from a real CONFLICTING conflict: a deferred verdict
  carries the reason `mergeability not yet determined (mergeable=UNKNOWN) —
  deferred to a later tick` (still `ok=False`, so INTEGRATE skips it, but it reads
  as a transient defer the next tick's refire re-evaluates, NOT the permanent
  `not mergeable (mergeable=CONFLICTING)` failure). `orphaned` is True when the
  loop PR's **closing issue is CLOSED** — the driver work is resolved or
  abandoned, so the PR will never merge and must be CLOSED (not merged, not left
  to linger); an orphaned verdict is forced `ok=False` so GATE skips it and
  INTEGRATE closes it (see below). `orphaned` defaults False (a PR with no closing
  issue, or an unresolvable one, is conservatively NOT treated as orphaned).
- **`CrossCheck`** — the conditional cross-feature complement-run result
  (§3.7.6): `{ schema_version, ran: bool, reason, results: [{feature, passed,
  returncode, summary}] }`. `ran` is True only when TRIAGE flagged
  `cross_cutting_risk`; `results` is one entry per at-risk feature whose run.py
  was run.
- **`IntegrationResult`** — `{ schema_version, merged: [{pr_ref, url}],
  skipped: [{pr_ref, reason}], errors: [{pr_ref, reason}], gate_failed: [{pr_ref,
  issue_ref, reason}], closed_orphaned: [{pr_ref, issue_ref}] }`. Idempotent: an
  already-merged PR leaves the open set, so a re-run never double-merges. Each
  `merged` entry's `url` is derived from its `pr_ref` (`owner/repo#number` →
  `.../owner/repo/pull/number`; bare `#number` uses the configured repo; neither
  → `''`) so a successful merge is observable, not an empty link. `gate_failed`
  records PRs INTEGRATE did not merge because their GATE result failed (a comment
  was posted on their issue instead). `closed_orphaned` records loop PRs INTEGRATE
  CLOSED because their driver issue is closed (orphaned verdicts) — the
  convergence guarantee that a stale/superseded loop PR whose issue was closed
  does not linger open forever and keep the loop refiring.
- **`GateResult`** — one per REVIEW-passed PR the GATE state gated:
  `{ schema_version, pr_ref, issue_ref, passed: bool, reason: null | "regression"
  | "conflict" | "load-bearing", failure_summary }`. GATE runs the configured
  `regression_command`
  CUMULATIVELY (DESIGN §2.2 [v2]): in a disposable integration worktree from
  current `main`, PRs are merged in deterministic order and the regression is run
  after each merge, so PR *k* is validated on top of `main` + the already-passed
  *1..k-1*; a failing/conflicting PR is rolled out, EXCLUDED, and recorded.
  `regression_command` null ⇒ GATE is a no-op PASS (every `passed=True`,
  `reason=null`). `failure_summary` is a BOUNDED tail of the command output
  (empty on pass).
- **`review_findings` record** — one MATERIAL finding the advisory reviewer
  emits, conforming EXACTLY to work-intake's `DiscoveredIssue.to_dict`:
  `{ schema_version, title, body, kind, severity, target, dedup_key, filed_by }`.
  `kind` in {bug, enhancement, chore}; `target` = `project`; `filed_by` =
  `autonomous-maintainer`; `dedup_key` is stable (`review:<pr_ref>:<slug>`) so
  REPORT files it idempotently. `review_finding_record(...)` builds one.
- **`ReviewVerdict`** — the retained `{ schema_version, pr_ref, approved,
  severity, findings, evidence }` schema. It is NO LONGER a merge gate; the
  deterministic `review_evidence_valid` / `batch_is_untrustworthy` validators
  ship on (consumed by scheduling + the packaging-config release gate).

## REVIEW is advisory (DESIGN §3.7.7 — no longer a merge gate)

REVIEW is a non-acting agent-state: the `auto-maintainer-reviewer` subagent reads
each open loop PR's base..head diff (`gh pr diff`) and emits MATERIAL quality
findings as durable `review_findings` records (respecting a severity floor — no
nitpicks). It writes the `review_findings` slot and emits `OK` when there were
PRs to review else `EMPTY`. REVIEW NEVER gates merge: the merge decision rests on
IMPLEMENT's deterministic run.py gate plus VERIFY + guardrails + the trust ladder,
so a lazy reviewer costs only missed quality notes, never an unsafe merge (this
structurally defuses the #255 rubber-stamp danger). The findings are filed as
backlog issues by the downstream REPORT port and fixed on a later tick.

**Scope boundary — REVIEW reviews the PR's OWN diff, never merge state.** The
reviewer emits findings ONLY about the LOGICAL and FUNCTIONAL quality of the code
in THIS PR's own `base..head` diff — correctness bugs, broken/missing behavior,
security, error handling, missing tests for the changed code, clear code-quality
defects — judged from the diff ALONE. It MUST NOT emit findings about merge
conflicts, rebase/mergeability state, or version-bump / shared-file COLLISIONS
between this PR and sibling/other open loop PRs, nor any cross-PR or merge-state
concern: those are owned deterministically by RECONCILE (conflict-recovery ladder)
and the VERIFY/INTEGRATE merge gates, and a REVIEW finding about them is duplicate
noise that races RECONCILE (observed: it filed "PR #x version bump collides with
sibling #y"). REVIEW never inspects how a PR relates to other open PRs.

## VERIFY (slice 1 — THIN, DESIGN §3.7.1/§3.7.2/§3.7.6)

- Lists the loop's open PRs via an INJECTABLE source (production: `gh pr list
  --label auto-maintainer --state open --json number,url,headRefName,baseRefName,
  mergeable,statusCheckRollup`; tests pass a stub — the determinism seam, mirror
  of `work_intake.gh_issue_source`).
- **Transient-`UNKNOWN` mergeability resolution (bounded poll).** GitHub computes
  a PR's mergeability ASYNCHRONOUSLY, so a PR opened this tick is almost always
  reported `mergeable=UNKNOWN` the moment VERIFY lists it — indistinguishable, at
  the raw-string level, from a real `CONFLICTING`. Treating `UNKNOWN` as a hard
  not-mergeable failure means the loop can NEVER auto-merge a PR in the tick it was
  opened (observed live: 10/11 fresh PRs skipped `not mergeable (mergeable=UNKNOWN)`
  despite passing CI). So BEFORE deriving a verdict, the open-PR source RESOLVES a
  transient `UNKNOWN`: for any PR whose `mergeable` is `UNKNOWN`, it re-queries gh
  (`gh pr view <n> --json mergeable`) with a BOUNDED retry — `MERGEABILITY_POLL_ATTEMPTS`
  attempts (default a small constant, e.g. 3) with `MERGEABILITY_POLL_INTERVAL_S`
  seconds between attempts (default a short constant, e.g. 2) — until it settles to
  `MERGEABLE` or `CONFLICTING`. BOTH the subprocess `runner` AND the `sleep`
  function are INJECTABLE so tests drive the poll deterministically with no network
  and no wall-clock wait. A `MERGEABLE`/`CONFLICTING` result derives the verdict as
  today. If it is STILL `UNKNOWN` after the bounded retries, the verdict is
  DEFERRED — `ok=False` with the transient `mergeability not yet determined
  (mergeable=UNKNOWN) — deferred to a later tick` reason (distinct from the
  permanent CONFLICTING failure), so INTEGRATE skips it and the tick's refire
  re-evaluates it next tick. This is orthogonal to the CI cross-tick model above:
  mergeability settles in seconds (polled here); CI stays async across ticks.
- For each PR derives a `Verdict`: `ci_state` from the status-check rollup
  (all SUCCESS → passing; any FAILURE → failing; any PENDING → pending; none →
  unknown), `mergeable` from gh's `mergeable` field, `base` from `baseRefName`;
  `ok = mergeable AND base==<default branch>`. CI is RECORDED but OPTIONAL: a
  pending/unknown/failing CI does NOT flip `ok` and contributes no `reasons`
  entry. The hard correctness gate lives in IMPLEMENT (FT-A runs the touched
  feature's run.py before the PR opens); the loop's own CI is a hollow
  byte-compile gate a pending run would otherwise wedge every merge on.
- **Conditional cross-feature complement run (§3.7.6).** VERIFY reads the
  `cross_cutting_risk` slot (work-intake's `CrossCuttingRisk`: `{risk, features,
  reason}`). When `risk` is True it runs, via an INJECTABLE complement-runner
  (default: shell each named feature's `test/run.py` via subprocess, self-contained
  here — modeled on FT-A's `test_gate.py`, NOT an import of `implement`; a
  deterministic feature-root resolver locates each run.py so tests
  never hit a real sibling suite or the network), the run.py of EACH feature in
  `cross_cutting_risk.features`. Results land in the versioned `cross_check` slot.
  The `features_root` is a runtime-injected locator with NO source-tree default:
  the resolver REQUIRES a `features_root` and the caller (scheduling) injects it.
  If ANY complement FAILS, every verdict this tick is marked `ok=False` with a
  specific cross-feature-break reason (naming the failing feature + the triager's
  overlap reason), so INTEGRATE merges nothing from a batch that breaks an at-risk
  sibling. **Conservative gate when unverifiable (§3.7.1):** when `risk` is True
  but the `features_root` is NOT configured, the complement CANNOT run, so VERIFY
  conservatively GATES — `cross_check` records `ran=False` with reason
  `complement run skipped: features_root not configured — cross-cutting risk
  unverifiable`, and EVERY verdict is marked `ok=False` with that same reason — a
  flagged batch that cannot be verified must NEVER auto-merge. When `risk` is
  False (or the slot is absent), NO complement runs and `cross_check` records
  `ran=False` — VERIFY stays thin; verdicts reflect only mergeable+base.
- **Orphaned-PR detection (convergence, DESIGN §3.7).** For each open loop PR,
  VERIFY resolves the state of the PR's **closing issue** via an **injectable
  resolver** (production: `gh_closing_issue_state` — resolves the closing-issue
  ref, then `gh issue view <n> --json state`). When that issue is **CLOSED**, the
  verdict is marked `orphaned=True` and forced `ok=False` with reason `driver
  issue <ref> is closed — orphaned loop PR (will be closed by INTEGRATE)`. This is
  a READ only — VERIFY still never mutates GitHub; INTEGRATE performs the close.
  Resolution is CONSERVATIVE: a PR that closes no issue, or whose issue state
  cannot be resolved (any resolver fault), is left `orphaned=False` (never closed
  on uncertainty). Because `gh pr list --label auto-maintainer` scopes VERIFY to
  loop-authored PRs, only the loop's own PRs are ever flagged orphaned.
- Writes the `verdicts` and `cross_check` slots; emits `OK` if any open PRs were
  found else `EMPTY`.
- Read-only w.r.t. GitHub: VERIFY never merges, closes, or writes to GitHub.
  Always runs at every trust mode (it only reports).

## GATE (cumulative regression gate, DESIGN §2.2 [v2])

Between REVIEW and INTEGRATE. Deterministic, **script-tier**; the self-contained
regression gate that replaces reliance on external CI. Reads `verdicts` (the open
loop PRs) + `regression_command` from the central config
(`safety_governance.load_config` / the `regression_command` accessor). Writes
`gate_results` (one `GateResult` per gated PR). Emits `OK`.

- **No-op when unconfigured.** `regression_command` is `null` ⇒ every PR passes
  (`passed=True, reason=null`); a project that has not configured a gate merges
  exactly as before. GATE never blocks by absence.
- **Cumulative integration.** Otherwise create a **DISPOSABLE integration git
  worktree at current `main`** (via `git worktree add`; the loop's real checkout
  is never touched, `main` is never merged to). For each `ok` verdict PR in a
  **deterministic order** (execution-plan / PR-number): merge the PR head into the
  integration worktree with the **same strategy INTEGRATE uses** (`--no-ff` merge
  commit) so the validated tree equals the merged tree.
  - **textual conflict** → `GateResult{passed:False, reason:"conflict"}`; abort
    the merge; **EXCLUDE** the PR (not carried forward); continue.
  - **clean merge** → **doc-surface load-bearing-token survival** check (below),
    then run `regression_command` (**injectable runner**; cwd = the integration
    worktree; capture output). exit 0 → `passed:True`, KEEP the merge as the base
    for the next PR; nonzero → `passed:False, reason:"regression"`,
    `failure_summary` = bounded output tail, and **ROLL BACK** the merge
    (`git reset --hard` to the pre-merge commit); continue.
  - Remove the worktree when done (always, even on error).
- **Setup robustness — never gate against a stale or wrong tree.** The
  integration worktree lives at a FIXED path (`/tmp/am-gate-integration` by
  default), so a crashed prior tick can leave it behind. Before `git worktree
  add`, GATE proactively clears any stale leftover (best-effort `git worktree
  remove --force` then `git worktree prune`) so a leftover never wedges every
  subsequent tick. GATE then checks the `worktree add` RETURN CODE: on failure it
  writes an EMPTY `gate_results` list (a setup failure is not any PR's fault) so
  INTEGRATE merges nothing and posts NO gate-fail marker — the tick converges to
  idle and retries cleanly next tick, rather than false-failing every PR into the
  park threshold. Inside the per-PR loop GATE checks the `git fetch origin
  pull/<n>/head` RETURN CODE before merging: on a fetch failure it returns
  `GateResult{passed:False, reason:"fetch-failed"}` for that PR WITHOUT merging,
  so a failed fetch can never silently merge the PREVIOUS PR's stale `FETCH_HEAD`
  into the cumulative tree (which would produce a verdict for the wrong tree).
- **Doc-surface load-bearing-token survival (issue #353).** Feature test suites
  do NOT assert doc prose, so a doc-reduction PR that over-deletes a load-bearing
  token (a schema field, a symbol/script name, an invariant, a cross-reference) can
  pass the line-count baseline and auto-merge. On each clean merge, BEFORE the
  regression, GATE asserts every token a doc-touched feature DECLARES load-bearing
  still appears in its post-change doc surfaces (`docs/spec.md`,
  `docs/contract.md`, `skills/*/SKILL.md`). A feature declares must-survive tokens
  in `test/load_bearing_tokens.json` (`{"tokens": [...]}`); GATE reads the PR's
  changed doc surfaces (`git diff --name-only`), maps them to features, and checks
  each feature's BASE (pre-merge) declared set — NOT the PR's own copy, so a PR
  cannot bypass the gate by dropping a token AND its declaration together (issue
  #392) — against the merged worktree. A dropped token ⇒ `GateResult{passed:False,
  reason:"load-bearing"}` (named in `failure_summary`); the merge is ROLLED BACK
  and the PR EXCLUDED. Wired from the central
  `doc_check_features_root` config key ONLY (a repo-relative root; fully DECOUPLED
  from `features_root` so VERIFY's complement locator never silently toggles this
  gate, #391) — unset/absolute ⇒ off. Opt-in + doc-scoped: no declared tokens, or
  a PR touching no doc surface, are unaffected.
- Each PR is thus validated on top of the prior GATE-passed PRs, catching
  **semantic conflicts** (clean merges that break together). Residual: intra-tick
  order-dependence (deterministic). GATE writes only the disposable worktree — no
  merge to `main`, no GitHub writes.

## INTEGRATE (slice 2)

- THIN merge (DESIGN §3.7.3). Reads `verdicts` AND `gate_results` (no
  review-approval coupling — REVIEW is advisory). A verdict is merged only when it
  is `ok` AND its `GateResult.passed` is True; it then consults the **guardrails**
  (`safety_governance.merge_guardrails`, §3.8.1) and, if clean, merges via an
  injectable merge sink. Records `IntegrationResult`. Non-ok verdicts and
  guardrail violations go to `skipped`.
- **Merge is MERGE-QUEUE-AWARE at `auto-merge` mode.** A branch protected by a
  **GitHub merge queue** REJECTS a merge-method flag: `gh pr merge <pr> --merge`
  (or `--squash`/`--rebase`) is refused because the queue owns the merge method,
  and a direct immediate merge is not allowed at all — the PR must be added to the
  queue via `gh pr merge <pr> --auto` (no method flag). Passing `--merge` to a
  queue branch is exactly what made a full batch of INTEGRATE merges fail with an
  opaque non-zero exit. The production merge sink therefore, in `auto-merge` mode,
  **detects a merge queue on the PR's base branch** (GraphQL
  `repository.mergeQueue(branch: <base>)` — non-null ⇒ queue) and branches:
  - **Queue present →** `gh pr merge <pr> --auto` — **no method flag and no
    `--delete-branch`** (the queue owns the method, per its `ALLGREEN`/config, and
    branch deletion is handled by the queue + the repo's `delete_branch_on_merge`).
    This adds the PR to the merge queue; the queue merges it when all required
    checks are green. Recorded in `IntegrationResult.auto_merge_enabled` (a
    **pending success**, NOT an error).
  - **No queue →** the method-specified path: try an immediate
    `gh pr merge <pr> --merge --delete-branch`; if that fails because the PR is
    not yet mergeable (required checks still pending), enable native auto-merge
    with `gh pr merge <pr> --auto --merge --delete-branch`. An immediate merge →
    `merged`; an enabled auto-merge → `auto_merge_enabled`.
  - The sink runs gh with **captured stderr** and, on a non-zero exit, records the
    **gh stderr text** in `IntegrationResult.errors` (`{pr_ref, reason}`) — never
    a bare "exit status 1" — so an access failure (403/404 — e.g. the session's
    gh account lacks repo access), a queue-method conflict, or a transient gh
    error is diagnosable from the log.
  - `auto_merge_enabled` PRs stay OPEN until the queue/auto-merge merges them; the
    source issue closes then via the PR's `Closes #<n>`. Re-work is already
    prevented by the acted-ledger `opened`-lock (an `opened` work order re-enters
    only when its PR is CLOSED-and-not-merged; a pending PR is OPEN, so it stays
    locked — the loop never supersedes it). `INTEGRATION_RESULT_SCHEMA_VERSION`
    already carries the `auto_merge_enabled` field (no further bump).
  This is `auto-merge`-mode-only: at `dry-run`/`propose` INTEGRATE never merges or
  enables auto-merge (the would-merge intent is logged; a human merges).
- **GATE-failed PRs are NOT merged.** For each `GateResult.passed=False`,
  INTEGRATE posts a **machine-readable failure comment** on the PR's linked issue
  (an **injectable comment sink**; production: `gh issue comment <issue> --body
  <structured>`), resolving the issue from the PR's closing-issue reference. The
  comment carries a FIXED marker + `{pr_ref, reason, failure_summary}` so a later
  tick's TRIAGE reads it deterministically (the Phase-2 retry/threshold model).
  These PRs are recorded in `IntegrationResult.gate_failed`.
- **Orphaned loop PRs are CLOSED, not merged (convergence).** For each verdict
  with `orphaned=True` (its driver issue is closed — see VERIFY), INTEGRATE CLOSES
  the PR via an **injectable close sink** (production: `gh pr close <pr>
  --delete-branch` with a machine-readable explanatory comment) and records it in
  `IntegrationResult.closed_orphaned`. This closes the convergence gap where a
  stale/superseded loop PR whose issue was already closed lingers open forever
  (never superseded, since supersede only fires on issue-retry) and keeps the loop
  refiring. The close is checked FIRST (before the merge/skip disposition) and is
  **trust-gated by `permits("merge", mode)`** exactly like merge: only at
  `auto-merge` does INTEGRATE actually close; at `dry-run`/`propose` the
  would-close intent is recorded under `skipped` (a human closes it). A
  close-sink fault does not wedge the tick (recorded under `errors`).
- **Trust-gated by `permits("merge", mode)`** (§3.8.2): merge is permitted ONLY
  at `auto-merge`. At `dry-run` and the default `propose`, INTEGRATE is a NO-OP
  that logs the would-merge intent — a human merges. Arming autonomous merge is
  an explicit `/auto-maintainer:configure --mode auto-merge` (the legacy name
  `gated-merge` is still accepted and stored as `auto-merge`).
- Emits `OK`.

## CLEANUP (slice 2)

- Reads `integration_result`; ensures merged PRs' branches are deleted (folded
  into `--delete-branch`; an explicit sweep covers the residual case) and
  performs an idempotent release/tag if configured (create-if-not-exists).
  Emits `OK`. Deterministic, idempotent.

## RECONCILE-support (cross-tick leftover-PR reconciliation, DESIGN §3.7 convergence)

`Reconcile` is a deterministic, **script-tier** class (mirroring `Integrate`) that
`scheduling`'s `make_reconcile` adapter wraps into a route state run BEFORE `PULL`.
The state wiring, the `route.json`/`adapter-map.json` edit, and the durable
acted-ledger read are `scheduling`/`packaging-config` concerns landed in a later
wave — **NOT owned here**. This feature owns the reconcile LOGIC and the
`ReconcileResult` schema.

It reconciles the PREVIOUS tick's leftover PRs so a merged-but-open issue, or a
loop PR left CONFLICTING after a sibling merged, never lingers. Reconcile is
**ADVISORY**: its manifest emits ONLY `OK`; a fault on any single entry is
recorded, never raised — RECONCILE never blocks the tick.

Its issue-close and comment writes are an OWNED, trust-gated GitHub convergence
write, extending INTEGRATE's existing issue-comment (gate-fail) and PR-close
(orphaned) writes. It does NOT file NEW issues — outbound issue FILING remains
REPORT/work-intake's (contract `never`).

### Inputs (injected by scheduling)

- an `acted_ledger` slot — the durable acted-ledger entries with recorded
  `outcome == "opened"`, each carrying `{work_order_id, pr_ref, issue_ref, repo}`.
  Scheduling loads the ledger from durable state and seeds this slot; Reconcile
  never reads durable state itself.
- the injectable seams below; ALL GitHub/git effects run behind them so the class
  is unit-testable with fakes and touches no network in tests.

### Injectable seams

- `gh_pr_state_source(pr_ref, repo) -> {state, merged, mergeable}` — the EXISTING
  PR-state read, surfacing `mergeable` (MERGEABLE|CONFLICTING|UNKNOWN). It RESOLVES
  a transient `mergeable=UNKNOWN` via the SAME bounded poll VERIFY's
  `gh_open_pr_source` uses (reusing `poll_mergeability`, `MERGEABILITY_POLL_ATTEMPTS`
  / `MERGEABILITY_POLL_INTERVAL_S`; injectable `runner` + `sleep`): when the PR is
  OPEN and NOT merged and `mergeable` is `UNKNOWN`, it re-queries until the value
  settles to MERGEABLE/CONFLICTING, so RECONCILE's (B) conflict detection is NOT
  blind to a just-invalidated loop PR (GitHub reports UNKNOWN transiently right
  after a sibling merge). A MERGED PR short-circuits (no poll — merge state is
  final). A still-`UNKNOWN` result after the bounded poll is returned as-is, so
  `_reconcile_one`'s `CONFLICTING` check stays False and the entry is DEFERRED to
  the next tick (never a crash, never a permanent not-conflicting).
- `gh_issue_state_source(issue_ref, repo) -> {state}` — the source issue's
  open/closed state (reuse VERIFY's closing-issue resolver seam).
- `gh_issue_close_sink(issue_ref, repo, comment) -> None` — NEW injectable sink:
  closes an issue with a machine-readable comment naming the PR.
- `gh_open_pr_closing_issue_source(repo) -> [{pr_ref, url, issue_ref}]` — NEW
  injectable source for same-issue dedup (C): the LIVE open loop-PR set with each
  PR's first closing-issue ref. Production: LIST with only supported fields — `gh
  pr list --label auto-maintainer --state open --json number,url` — then for each
  listed PR resolve its first closing-issue ref by delegating to the EXISTING
  `gh_closing_issue_ref` (which uses `gh pr view <n> --json closingIssuesReferences`,
  the supported form). `closingIssuesReferences` is NOT a valid `gh pr list` `--json`
  field (only `gh pr view` supports it), so requesting it on the list would abort
  the tick on stock `gh` — hence list-then-per-PR-view. A PR whose closing-issue ref
  is None (closes no issue) is EXCLUDED. Runner injectable; no network in tests.
- the EXISTING injectable PR-close sink and issue-comment sink, and the EXISTING
  GATE integration-worktree helper (fetch a PR head, merge/rebase onto a fresh
  base), reused for the tier-1 rebase.

### (A) Merged-PR issue-close fallback

For each `opened` ledger entry, query `gh_pr_state_source`. If the PR is
**MERGED** and its source issue is **still OPEN** (`gh_issue_state_source`), close
the issue via `gh_issue_close_sink` with a comment naming the merged PR — a
deterministic FALLBACK for when the PR's `Closes #<n>` keyword did not fire or the
project opts out of keyword-closing. NEVER touch a human-closed issue: ONLY a
MERGED-PR-with-still-OPEN-issue is closed. Idempotent — each close is reported so
scheduling records it in the ledger and a later tick never re-comments.

### (B) Conflict-recovery ladder

For each `opened` entry whose PR is OPEN and **CONFLICTING** (`mergeable ==
CONFLICTING` — a sibling merged and invalidated it):

- **TIER 1 — deterministic rebase (no model, ~zero tokens).** In a disposable
  integration worktree (the GATE worktree helper), fetch the PR head and rebase it
  onto fresh `origin/<default-branch>`. If it rebases CLEAN, force-push the rebased
  branch so the PR is mergeable again and re-enters VERIFY/GATE/INTEGRATE next tick
  with NO implementer run. Recorded in `reconcile_result.rebased`.
- **TIER 2 — re-land fallback.** If the rebase hits a real textual conflict
  (semantic resolution is the implementer's job, not determinism — spec-rules §1),
  close the PR via the EXISTING PR-close sink and comment the source issue with the
  PR ref via the EXISTING comment sink, so the acted-ledger re-entry gate re-lands
  it next tick (PR CLOSED-and-not-merged + issue `updated_at` advanced by the
  comment). Recorded in `reconcile_result.relanded`.

### (C) Same-issue open-PR dedup (deterministic supersede backstop)

The `implement` doer's subagent has a best-effort, PROMPT-tier "supersede a prior
same-issue PR" step (`gh pr close`), but it can be DENIED by the maintainer
session's per-command permission classifier — as it was in the live test, leaving
TWO open `auto-maintainer`-labelled PRs closing the SAME still-open issue (e.g.
issue #650 open with both #744 and #754). Left alone, both could auto-merge for one
issue. RECONCILE runs before PULL INSIDE `run_tick`'s already-approved subprocess,
so a close it performs bypasses that per-command classifier — the deterministic
backstop (spec-rules §1: script > prompt) the prompt-tier supersede cannot
guarantee.

Dedup sources the LIVE open loop-PR set (NOT the `acted_ledger`, which holds only
the latest `opened` entry per work order and would miss a re-dispatch's orphaned
first-run PR) via an INJECTABLE source (production: LIST the open loop PRs with only
supported `--json` fields — `gh pr list --label auto-maintainer --state open --json
number,url` — then resolve EACH PR's FIRST closing-issue ref via the EXISTING
`gh_closing_issue_ref` (`gh pr view <n> --json closingIssuesReferences`); a PR that
closes no issue is excluded from dedup). `closingIssuesReferences` is a `gh pr
view`-only field — it is NOT valid on `gh pr list`, so it must NEVER be requested on
the list (doing so aborts the whole tick on stock `gh`). It GROUPS the open PRs by
closing-issue ref and, for each group with **more
than one** open PR whose **source issue is still OPEN** (`gh_issue_state_source`),
KEEPS exactly one — the **highest PR number** (the loop-tracked / newest re-land) —
and CLOSES every other PR in the group via the EXISTING PR-close sink (`gh pr close
<n> --delete-branch` + a machine-readable comment naming the kept PR as the
superseding same-issue re-land). Recorded in `reconcile_result.deduped`. Dedup NEVER
touches the sole PR for an issue, NEVER touches a group whose issue is CLOSED (an
orphaned duplicate whose issue closed is INTEGRATE's/orphan path's concern, and a
merged-issue is (A)'s), and NEVER touches PRs across different issues.

**Advisory fault-isolation (the whole dedup step).** The entire dedup step — the
one `gh_open_pr_closing_issue_source` call AND the per-group close work — is wrapped
so ANY fault (a gh error from the source, an unresolvable ref, a close-sink failure)
is recorded under `reconcile_result.errors` (e.g. `{ref: "dedup", reason: <str>}`)
and the tick CONTINUES with dedup degraded to a no-op. A dedup fault NEVER raises
and NEVER aborts the tick — RECONCILE stays advisory (emits only `OK`). This closes
the regression where a source-call fault propagated and crashed the whole tick.

**Trust-gated exactly like INTEGRATE.** The mutating acts (issue-close,
force-push, PR-close, same-issue dedup PR-close) run ONLY at `permits("merge",
mode)` (i.e. `auto-merge`); at `dry-run`/`propose` the would-act intent is recorded
under `skipped` and a human acts. A single-entry fault is recorded under `errors`
and never wedges the tick.

### `ReconcileResult` slot (owned here, versioned, machine-first)

`{ schema_version, closed_issues: [{issue_ref, pr_ref}], rebased: [{pr_ref}],
relanded: [{pr_ref, issue_ref}], deduped: [{pr_ref, issue_ref, kept_pr_ref}],
skipped: [{ref, reason}], errors: [{ref, reason}] }`. `deduped` records the
same-issue duplicate PRs (C) closed this tick — each entry names the closed
`pr_ref`, its `issue_ref`, and the `kept_pr_ref` that superseded it — kept SEPARATE
from `closed_issues`/`rebased`/`relanded`. `RECONCILE_MANIFEST` reads `acted_ledger`,
writes `reconcile_result`, emits only `OK`. `RECONCILE_RESULT_SCHEMA_VERSION` is
`1.1.0` (additive: the `deduped` list was added to the `1.0.0` shape).

## Guardrails (consumed from safety-governance)

- **Merge guardrails (§3.8.1)** — `safety_governance.merge_guardrails(pr_meta,
  default_branch) -> {ok, violations}`: never-merge-wrong-base (base != default),
  never-merge-dirty (`mergeable` is CONFLICTING/unknown), never-delete a
  non-matching branch. INTEGRATE refuses any PR with violations.

## Adapter factory convention

Wired as route-as-data adapters (the `factory(runtime) -> (StateManifest,
run_callable)` convention), consumed by `scheduling.run_tick` via new
`make_verify` / `make_integrate` / `make_cleanup` factories + `DEFAULT_ADAPTER_MAP`
entries. The route option extends to `...IMPLEMENT → VERIFY → INTEGRATE →
CLEANUP → PERSIST → EXIT`.

## Invariants

- VERIFY is read-only w.r.t. GitHub (no GitHub writes); INTEGRATE merges ONLY at
  `auto-merge` and ONLY a PR that is `ok` AND passes guardrails.
- REVIEW's findings are scoped to the PR's OWN diff (logical/functional/quality
  defects in the changed code); REVIEW NEVER files findings about merge conflicts,
  mergeability state, or cross-PR version-bump/shared-file collisions — those are
  owned by RECONCILE + the VERIFY/INTEGRATE merge gates, not REVIEW.
- VERIFY's `ok` is mergeable+base only (CI recorded, not gating); a failing
  cross-feature complement flips every verdict `ok=False`.
- VERIFY resolves a transient `mergeable=UNKNOWN` via a BOUNDED, injectable-runner
  + injectable-sleep poll before deriving the verdict; a still-`UNKNOWN` result is
  a DEFERRED verdict (`ok=False`, transient reason) DISTINCT from a permanent
  `CONFLICTING` failure — a real `CONFLICTING` stays a hard failure. The poll is
  bounded (never loops unboundedly) and adds no network/wall-clock cost in tests.
- Deterministic given the injected `gh` + complement-runner seams; no model, no
  wall-clock beyond gh.
- Idempotent: a merged PR leaves the open set, so re-running never double-merges;
  release/tag is create-if-not-exists.
- Cross-tick: the open-PR set is sourced LIVE from GitHub (the `auto-maintainer`
  label), never a durable ledger.
- RECONCILE is advisory (emits only `OK`, never blocks the tick), deterministic
  given its injected seams, idempotent (a merged-PR issue-close is reported so it
  never re-comments), and mutates GitHub ONLY at `auto-merge` (trust-gated exactly
  like INTEGRATE). It closes ONLY a MERGED-PR-with-still-OPEN issue — never a
  human-closed one — and its tier-1 rebase force-pushes ONLY the loop's own PR
  branch; a real textual conflict falls back to re-land (never a silent semantic
  auto-merge).
- RECONCILE's PR-state read (`gh_pr_state_source`) RESOLVES a transient
  `mergeable=UNKNOWN` via the same bounded poll as VERIFY before the (B)
  CONFLICTING decision, so a just-invalidated loop PR (UNKNOWN right after a
  sibling merge) is not silently missed by the conflict-recovery ladder. A
  still-UNKNOWN result after the poll defers the entry to the next tick (never a
  crash, never treated as permanently not-conflicting); a MERGED PR does not poll.
- RECONCILE same-issue dedup (C) closes a duplicate loop PR ONLY when MORE THAN
  ONE open `auto-maintainer` PR closes the SAME still-OPEN issue; it keeps the
  highest-numbered PR and closes the rest via the existing PR-close sink. It NEVER
  closes the sole PR for an issue, NEVER touches a group whose issue is closed, and
  NEVER crosses issues; it is trust-gated (`auto-merge` only) and records closures
  in `reconcile_result.deduped`. Its open-PR source LISTS with only supported
  `gh pr list` `--json` fields (`number,url`) and resolves each closing-issue ref
  via `gh_closing_issue_ref` (`gh pr view`) — it NEVER requests
  `closingIssuesReferences` on `gh pr list` (an invalid field that would abort the
  tick). The entire dedup step is fault-isolated: any fault is recorded under
  `errors` and the tick CONTINUES (RECONCILE never crashes on a dedup fault).

## Deferred (NOT v1)

- Non-git VCS backends (GitLab MR / Gerrit) — §3.7.5, port exists, additive.
- Model-backed VERIFY (review-quality judgment beyond CI) — v1 is CI+mergeable.
- Rich release pipelines / changelog generation.
- **Backoff (§3.8.5)** — a consecutive-failure counter that defers/escalates a
  PR after K failing verdicts is NOT implemented in v1: re-checking a red PR is
  cheap and never merges, so VERIFY simply re-checks every tick until the PR
  becomes mergeable (the cross-tick model above). The K threshold exists today
  only as a `safety-governance` config knob (`backoff.threshold`); wiring it into
  a durable cross-tick counter — plus any circuit-breaker tuning — is deferred.

## Interfaces (composition)

- Implements the fsm-contracts state contract; `tick-orchestrator` runs the three
  as route states; `scheduling` wires them.
- Consumes `safety-governance` (`permits("merge", …)` + `merge_guardrails`).
- Reads the loop's open PRs (stamped `auto-maintainer` by the `implement` doer)
  via the `gh` CLI; merges via `gh`.
- Declares no shippable `ship/` components of its own; its states ship as libs.
