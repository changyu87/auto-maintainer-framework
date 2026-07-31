# Changelog — verify-integrate

All notable changes to this feature are recorded here. Owner: rabbit-workflow team.

## 0.11.2 — 2026-07-31

RECONCILE tier-1 unique-worktree robustness: a leftover worktree from an abrupt
mid-rebase stop no longer wedges every subsequent tier-1 rebase.

- **`reconcile_rebase_worktree` uses a UNIQUE per-invocation worktree path.** It
  now derives the disposable integration worktree from `tempfile.mkdtemp(prefix=
  "am-reconcile-")` instead of the fixed `/tmp/am-reconcile-integration`, and runs
  `git worktree prune` (injectable runner) BEFORE `git worktree add`; the `finally`
  removes the per-invocation worktree (`git worktree remove --force` +
  `shutil.rmtree`). Previously the fixed path, orphaned by a tick killed
  mid-rebase, made every subsequent `git worktree add <fixed>` fail — the error was
  swallowed into `reconcile_result.errors` (RECONCILE is advisory), so CONFLICTING
  loop PRs were never recovered until the orphan was removed by hand. A unique path
  per invocation also lets multiple conflicting PRs be recovered in ONE tick without
  colliding, and a leftover is at worst a harmless orphaned `/tmp` dir. The fixed
  `_RECONCILE_WORKTREE_DIR` constant is retired. `ReconcileResult` schema is
  UNCHANGED; tier-1 clean-rebase / tier-2 reland behavior is otherwise unchanged;
  RECONCILE stays advisory.

## 0.11.1 — 2026-07-31

RECONCILE tier-1 URL-ref-crash fix: a URL-form `pr_ref` no longer crashes the
deterministic rebase helper.

- **`_pr_number(pr_ref)` tolerates both ref forms.** It now parses the PR number
  from BOTH the canonical `owner/repo#N` (or bare `#N`) ref AND a full GitHub PR
  URL (`…/pull/N`, trailing slash/query stripped), raising `ValueError` only when
  no number is parseable. Previously `int(pr_ref.split("#")[-1])` threw on a
  URL-form ref, so `reconcile_rebase_worktree` (tier-1) crashed every tick and a
  detected-CONFLICTING loop PR was never rebased/re-landed. A defensive backstop:
  scheduling also normalizes the seed to `owner/repo#N` at the source; either alone
  prevents the crash. No behavior change for existing `owner/repo#N` callers;
  RECONCILE stays advisory.

## 0.11.0 — 2026-07-30

RECONCILE (A) auto-merge-completion observability: an auto-merge GitHub completed
asynchronously between ticks is now surfaced with a tick-trace.

- **`ReconcileResult.auto_merged`.** RECONCILE now records `{pr_ref, issue_ref}`
  in a NEW `auto_merged` list for EVERY merged `acted_ledger` PR it sees this tick,
  UNCONDITIONALLY — whether or not the source issue is still open. This closes the
  observability gap where a PR that INTEGRATE `auto_merge_enabled` and GitHub then
  completed between ticks left NO tick-trace confirmation the merge landed (a PR
  whose issue was already auto-closed by its `Closes #<n>` keyword left `merged=0`
  forever). `auto_merged` is a pure OBSERVABILITY record — it drives no GitHub
  write and is INDEPENDENT of `closed_issues` (a merged PR whose issue is already
  closed appears in `auto_merged` but NOT `closed_issues`); the existing (A)
  still-open-issue close path is unchanged.
- **Schema (additive, non-breaking).** `RECONCILE_RESULT_SCHEMA_VERSION`
  `1.1.0` → `1.2.0`; `auto_merged` defaults to `[]` in `to_dict`/`from_dict`, so a
  `1.1.0` result deserializes unchanged. `RECONCILE_MANIFEST` reads are unchanged.
- **Cross-feature contract.** The field is exactly `auto_merged`, entries
  `{pr_ref, issue_ref}`; scheduling (a paired cycle) reads
  `reconcile_result.auto_merged` to surface the count and stamp those ledger
  entries TERMINAL so the completion is reported exactly once.

## 0.10.0 — 2026-07-30

RECONCILE (B) conflict-recovery: a genuinely-CONFLICTING loop PR is now recovered
even when GitHub still reports its live mergeability as UNKNOWN at tick-top.

- **Prior-verdict race-breaker.** RECONCILE runs FIRST in the route (tick-top),
  when a just-invalidated loop PR is most likely to still report
  `mergeable=UNKNOWN` even after the bounded poll — so the (B) ladder, which
  required a settled live `CONFLICTING`, never fired and the conflict lingered. The
  previous tick's VERIFY — which ran later, once mergeability settled — already
  recorded the hard CONFLICTING. `Reconcile` now reads a NEW OPTIONAL
  `prior_verdicts` slot (the previous tick's VERIFY verdicts, a List of verdict
  dicts identical in shape to the `verdicts` slot, seeded by scheduling's
  `make_reconcile`) and the (B) conflict determination becomes LIVE-PREFERRED with
  a prior-verdict race-breaker: a settled live `mergeable` is authoritative
  (MERGEABLE ⇒ left alone/never force-pushed, CONFLICTING ⇒ ladder), and ONLY on a
  live `UNKNOWN` (poll exhausted) does it consult `prior_verdicts`, treating the PR
  as CONFLICTING iff the prior verdict for that `pr_ref` was CONFIRMED-CONFLICTING
  (a hard `mergeable=CONFLICTING`, DISTINCT from a transient DEFERRED/UNKNOWN
  verdict — centralized in the pure `_is_confirmed_conflicting_verdict`).
- **Non-breaking + additive.** `prior_verdicts` is added to `RECONCILE_MANIFEST`
  reads but read OPTIONALLY (absent/empty ⇒ exactly today's live-read-only
  behavior, so the un-wired route still validates). RECONCILE stays advisory
  (never raises; per-entry fault → `errors`; emits only OK) and trust-gated
  (mutating recovery only at `permits("merge", mode)`). (A) merged-PR issue-close
  and (C) same-issue dedup are unchanged.
- New tests: the pure discriminator (hard CONFLICTING vs DEFERRED/MERGEABLE/absent),
  live UNKNOWN + prior CONFIRMED-CONFLICTING ⇒ tier-1 rebase / tier-2 re-land, live
  UNKNOWN + prior DEFERRED ⇒ left alone, live UNKNOWN + absent/empty prior ⇒ left
  alone (back-compat), live MERGEABLE + prior CONFLICTING ⇒ NOT touched (live
  preferred), and live CONFLICTING settled ⇒ ladder regardless of prior. Housekeep
  doc baseline re-anchored (spec.md 569 → 616) for the prior_verdicts spec content.

## 0.9.3 — 2026-07-24

REVIEW noise suppression: the advisory reviewer no longer files merge-conflict /
cross-PR-collision issues.

- **Reviewer scope narrowed to the PR's own diff.** In the live loop the
  `auto-maintainer-reviewer` filed backlog issues like "PR #780 version bump 0.3.4
  collides with sibling PR #779 (same shared files)" — a MERGE-STATE / cross-PR
  collision that RECONCILE (conflict-recovery) and the VERIFY/INTEGRATE merge gates
  already own. That's duplicate noise racing RECONCILE. `ship/agents/auto-maintainer-reviewer.md`
  now carries an explicit EXCLUSION: emit findings ONLY about the logical/functional
  quality of the code in THIS PR's own `base..head` diff (correctness, security,
  error handling, missing tests, code-quality defects), judged from the diff alone —
  NEVER about merge conflicts, mergeability, or version-bump/shared-file collisions
  with sibling PRs. REVIEW stays advisory + non-gating; the `review_findings` schema,
  dispatch wiring, and severity floor are unchanged.

## 0.9.2 — 2026-07-24

Hotfix: RECONCILE's conflict-recovery ladder was blind to a freshly-invalidated
loop PR.

- **RECONCILE missed transient `mergeable=UNKNOWN`.** RECONCILE runs at the start
  of the tick and read PR state via `gh_pr_state_source`, which (unlike VERIFY's
  `gh_open_pr_source` since 0.9.1) did NOT poll a transient `mergeable=UNKNOWN`.
  Right after a sibling PR merged and invalidated a loop PR, GitHub reports
  `UNKNOWN` momentarily, so `_reconcile_one`'s `(B)` branch (which requires
  `mergeable == "CONFLICTING"`) silently skipped it — no tier-1 rebase, no tier-2
  re-land. The conflict survived, VERIFY (which polls) later marked it CONFLICTING,
  INTEGRATE skipped it, and REVIEW filed noise "sibling collision" issues (live:
  #779/#780 left untouched by RECONCILE). Fix: `gh_pr_state_source` now resolves a
  transient `UNKNOWN` via the same bounded `poll_mergeability` poll
  (`MERGEABILITY_POLL_ATTEMPTS`/`MERGEABILITY_POLL_INTERVAL_S`; injectable runner +
  sleep) when the PR is OPEN and not merged — a MERGED PR short-circuits, a
  still-`UNKNOWN` result defers to the next tick. `_reconcile_one` is unchanged; it
  now receives a settled value, so the (B) ladder engages on genuinely-conflicting
  PRs.
- New tests: `gh_pr_state_source` polls UNKNOWN→CONFLICTING (bounded, no-op sleep),
  stays-UNKNOWN returns UNKNOWN (no crash), a MERGED PR does not poll; and an e2e
  that RECONCILE runs the tier-1 rebase on a PR whose mergeability settles to
  CONFLICTING.

## 0.9.1 — 2026-07-24

Hotfix: the v0.9.0 RECONCILE same-issue dedup (C) crashed the whole tick on stock
`gh` (2.69.0).

- **Wrong gh query.** `gh_open_pr_closing_issue_source` requested
  `gh pr list --label auto-maintainer --state open --json
  number,url,closingIssuesReferences`, but `closingIssuesReferences` is a
  `gh pr view`-only `--json` field — invalid on `gh pr list` — so gh exited
  non-zero and (via `check=True`) aborted the tick in RECONCILE. Now the source
  LISTS with only supported fields (`--json number,url`) and resolves each PR's
  first closing-issue ref via the existing `gh_closing_issue_ref` (`gh pr view <n>
  --json closingIssuesReferences`). Same `[{pr_ref, url, issue_ref}]` shape;
  PRs closing no issue excluded.
- **Advisory fault-isolation.** The dedup step (source call + per-group work) is
  now wrapped: any fault is recorded under `reconcile_result.errors`
  (`{ref: "dedup", reason}`) and the tick CONTINUES with dedup as a no-op —
  restoring the invariant that RECONCILE is advisory and never aborts the tick.
- New tests: a command-shape assertion that the `pr list` argv never requests
  `closingIssuesReferences`, and a fault-isolation test that a raising source is
  recorded under errors while (A)/(B) outcomes and the OK signal are unaffected.

## 0.9.0 — 2026-07-24

Two auto-merge reliability + convergence fixes surfaced by the live test.

- **VERIFY resolves transient `mergeable=UNKNOWN` (auto-merge in-tick).** GitHub
  computes mergeability asynchronously, so a freshly-opened PR is reported
  `mergeable=UNKNOWN` the tick it is opened and `derive_verdict` previously folded
  `UNKNOWN` into the same hard `not mergeable` failure as `CONFLICTING` — so the
  loop could never auto-merge a PR in the tick it created it (live: 10/11 fresh PRs
  skipped `not mergeable (mergeable=UNKNOWN)` despite green CI). VERIFY's open-PR
  source now RESOLVES a transient `UNKNOWN` with a BOUNDED poll
  (`MERGEABILITY_POLL_ATTEMPTS` re-queries of `gh pr view <n> --json mergeable`,
  `MERGEABILITY_POLL_INTERVAL_S` apart; both the subprocess runner AND the sleep
  are injectable — no network/wall-clock in tests) until it settles to
  `MERGEABLE`/`CONFLICTING`. A still-`UNKNOWN` result is a DEFERRED verdict
  (`ok=False`, reason `mergeability not yet determined (mergeable=UNKNOWN) —
  deferred to a later tick`), DISTINCT from the permanent
  `not mergeable (mergeable=CONFLICTING)` failure.
- **RECONCILE same-issue open-PR dedup (C).** When the implementer's prompt-tier
  supersede is denied by the session permission classifier, two open
  `auto-maintainer` PRs can close the SAME still-open issue (live: issue #650 with
  both #744 and #754). RECONCILE now sources the LIVE open loop-PR set
  (`gh_open_pr_closing_issue_source` → `gh pr list --json
  closingIssuesReferences`), groups by closing-issue ref, and for each group with
  >1 open PR whose issue is still OPEN keeps the highest-numbered PR and CLOSES the
  rest via the existing PR-close sink — the deterministic backstop that runs inside
  `run_tick`'s already-approved subprocess. Recorded in the new
  `ReconcileResult.deduped` (`RECONCILE_RESULT_SCHEMA_VERSION` → `1.1.0`,
  additive). Never closes the sole PR for an issue, a closed-issue group, or across
  issues; trust-gated at `auto-merge`.

## 0.7.1 — 2026-06-21

Observability fix: a merged `IntegrationResult` entry now carries the PR's web
link instead of `url:''` (a successful merge previously looked like nothing
happened).

- **`_pr_url(pr_ref, repo)` (new, pure).** Derives the PR web URL:
  `owner/repo#number` → `https://github.com/owner/repo/pull/number`; a bare
  `#number` ref uses the configured `repo` (`owner/repo`); when neither yields an
  `owner/repo` it returns `''`. Never raises for URL derivation.
- **`gh_pr_merge_sink` now returns the derived url.** The `gh pr merge <number>
  --merge --delete-branch [--repo]` call (`check=True`) is UNCHANGED — a failed
  merge still raises `CalledProcessError`; only the returned entry changes from
  `{pr_ref, url:''}` to `{pr_ref, url:_pr_url(pr_ref, repo)}`.
- `Integrate.run` and the merged-entry schema are unchanged (only the url value).

## 0.7.0 — 2026-06-21

Fix the FT-D `features_root` self-containment bug packaging-config's
self-containment guard caught, and conservatively gate an unverifiable
cross-cutting batch (§3.7.1).

- **No source-tree default for `features_root`.** Deleted the
  `_DEFAULT_FEATURES_ROOT` constant (the `dirname(dirname(dirname(__file__)))`
  computation) and its comment. `feature_run_py_path(feature, features_root)` now
  REQUIRES a non-None `features_root` (pure path-join; raises on None) — the
  shipped, self-contained plugin lib cannot assume its own on-disk layout, so the
  caller (scheduling) injects the locator at runtime.
- **Conservative gate when unverifiable (§3.7.1).** When
  `cross_cutting_risk.risk=True` but `features_root` is unconfigured, the
  complement CANNOT run, so `Verify.run` now GATES: `cross_check` records
  `ran=False` with reason `complement run skipped: features_root not configured —
  cross-cutting risk unverifiable`, and EVERY verdict is marked `ok=False` with
  that same reason. A flagged cross-cutting batch that cannot be verified must
  never auto-merge — loud and recorded, never silent.
- **Self-contained source.** The shipped `verify_integrate.py` now carries NO
  `rabbit-project` and NO `.rabbit` path substring; a test asserts this.

## 0.6.0 — 2026-06-21

VERIFY becomes THIN + gains the conditional cross-feature complement run
(DESIGN §3.7.1, §3.7.2, §3.7.6 — FT-D of the loop redesign).

- **Thin VERIFY (CI optional).** `derive_verdict`'s `ok` is now
  `mergeable AND base == default_branch` — the hard CI requirement is DROPPED from
  `ok`. `ci_state` is still DERIVED and RECORDED on the `Verdict` (informational
  defense-in-depth), but a pending/unknown/failing CI no longer flips `ok` and no
  longer contributes a `reasons` entry. The correctness gate lives in IMPLEMENT
  (FT-A runs the touched feature's run.py before a PR opens); the loop's own CI is
  a hollow byte-compile gate a pending run would otherwise wedge merges on.
- **Conditional cross-feature complement run (§3.7.6).** `Verify.run` now READS
  the `cross_cutting_risk` slot (work-intake's `CrossCuttingRisk`). When
  `risk=True` it runs, via an INJECTABLE complement-runner
  (`default_complement_runner`, self-contained — shells each named feature's
  `test/run.py` via subprocess, modeled on FT-A's `test_gate.py`, NOT an import of
  `implement`), the run.py of each at-risk feature. A deterministic injectable
  `feature_run_py_path()` resolver locates each run.py so tests never hit a real
  sibling suite or the network. Results land in the new versioned `cross_check`
  slot (`CrossCheck`: `{ran, reason, results[{feature, passed, returncode,
  summary}]}` + `CROSS_CHECK_SLOT`). When `risk=False`/absent, NO complement runs
  and `cross_check` records `ran=False` (thin).
- **Complement GATE.** If ANY complement suite FAILS, every verdict this tick is
  marked `ok=False` with a specific cross-feature-break reason (failing feature +
  the triager's overlap reason), so INTEGRATE merges nothing from a batch that
  breaks an at-risk sibling. All pass (or `risk=False`) → verdicts unaffected.
- **Manifest.** `VERIFY_MANIFEST.reads` gains `cross_cutting_risk`; `writes` gains
  `cross_check` (keeps `verdicts`). Signals stay OK|EMPTY.
- **Re-baselined housekeep doc fixture** for this wave's load-bearing doc growth
  (the cross_check schema + the §3.7.6 complement behavior).
- Out of scope (FT-E): scheduling registering/seeding `cross_cutting_risk` /
  `cross_check` and `route.json`. Until then VERIFY's tests inject the slot
  directly and the runtime tolerates its absence (thin path).

## 0.5.0 — 2026-06-23

REVIEW becomes ADVISORY; INTEGRATE becomes a THIN guardrailed merge
(DESIGN §3.7.3, §3.7.7, §3.7.8, §3.7.9 — FT-C of the loop redesign).

- **INTEGRATE thin merge.** `Integrate.run` now merges every `ok` verdict whose
  guardrails pass AND `permits('merge', mode)` is True. The review-approval
  coupling is REMOVED: INTEGRATE no longer reads `review_verdicts`, no longer
  calls `is_review_approved` / `_review_skip_reason`, and has no untrustworthy
  short-circuit. `INTEGRATE_MANIFEST.reads` drops `review_verdicts` (reads only
  `verdicts`). The trust-ladder `permits` gate and `merge_guardrails` are
  unchanged.
- **REVIEW advisory.** Replaced the merge-gating `review_verdicts` output with a
  `review_findings` slot — a list of records conforming EXACTLY to work-intake's
  `DiscoveredIssue.to_dict` (schema_version, title, body, kind, severity, target,
  dedup_key, filed_by). Added the versioned `REVIEW_FINDINGS_SLOT` descriptor and
  `review_finding_record(...)` builder. `REVIEW_MANIFEST` now writes
  `review_findings`; signals stay OK|EMPTY.
- **Reviewer agent rewrite (major).** `ship/agents/auto-maintainer-reviewer.md`
  is now an advisory quality reviewer (NOT a merge gate): it reads `gh pr diff`,
  applies the code-review and code-simplify lenses, and emits material findings
  (kind + severity, stable dedup_key) with a severity floor. It touches no code,
  never merges, never approves/blocks. The two superpowers lens bodies are
  inlined verbatim (re-sync on upstream change).
- **Retained.** `ReviewVerdict`, `REVIEW_VERDICTS_SLOT`,
  `review_evidence_valid`, `batch_is_untrustworthy` are KEPT (consumed by
  scheduling + the packaging-config release gate); they are no longer a merge
  gate. `is_review_approved` and `_review_skip_reason` were removed as orphaned
  by the INTEGRATE change (proven dead by repo-wide grep).
- Out of scope (FT-D/FT-E): scheduling wiring of `review_findings` into REPORT,
  VERIFY thinning, and `route.json`. After this change `review_findings` is
  produced but not yet auto-filed; the merge path still works.

## 0.4.0 and earlier

See git history. Prior versions gated INTEGRATE on the model-backed REVIEW
approval (#209) with the #255 evidence-gated trust backstop.
