# Changelog — verify-integrate

All notable changes to this feature are recorded here. Owner: rabbit-workflow team.

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
