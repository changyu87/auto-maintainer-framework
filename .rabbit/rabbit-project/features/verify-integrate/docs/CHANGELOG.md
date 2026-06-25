# Changelog — verify-integrate

All notable changes to this feature are recorded here. Owner: rabbit-workflow team.

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
