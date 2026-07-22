---
feature: verify-integrate
version: 0.5.0
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult schemas reach a breaking major version.
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
  IMPLEMENT). `reasons` explains a non-ok verdict. `orphaned` is True when the
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

## VERIFY (slice 1 — THIN, DESIGN §3.7.1/§3.7.2/§3.7.6)

- Lists the loop's open PRs via an INJECTABLE source (production: `gh pr list
  --label auto-maintainer --state open --json number,url,headRefName,baseRefName,
  mergeable,statusCheckRollup`; tests pass a stub — the determinism seam, mirror
  of `work_intake.gh_issue_source`).
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
  injectable merge sink (production: `gh pr merge <pr> --merge --delete-branch`).
  Records `IntegrationResult`. Non-ok verdicts and guardrail violations go to
  `skipped`.
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
- VERIFY's `ok` is mergeable+base only (CI recorded, not gating); a failing
  cross-feature complement flips every verdict `ok=False`.
- Deterministic given the injected `gh` + complement-runner seams; no model, no
  wall-clock beyond gh.
- Idempotent: a merged PR leaves the open set, so re-running never double-merges;
  release/tag is create-if-not-exists.
- Cross-tick: the open-PR set is sourced LIVE from GitHub (the `auto-maintainer`
  label), never a durable ledger.

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
