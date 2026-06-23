---
feature: verify-integrate
version: 0.4.0
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult / ReviewVerdict schemas reach a breaking major version. See spec.md / feature.json.
---

# verify-integrate — Contract

```json
{
  "provides": {
    "files": [
      "Verdict slot schema (versioned, machine-first: pr_ref, url, ok, ci_state, mergeable, base, reasons)",
      "review_findings slot schema + REVIEW_FINDINGS_SLOT + review_finding_record(): the ADVISORY REVIEW state's output (DESIGN §3.7.7) — a list of records each conforming EXACTLY to work-intake's DiscoveredIssue.to_dict (schema_version, title, body, kind, severity, target, dedup_key, filed_by), so REPORT files them unchanged",
      "ReviewVerdict slot schema (retained, versioned: pr_ref, approved, severity, findings[], evidence{files_examined[], rationale}) + review_evidence_valid(rv) + batch_is_untrustworthy(review_verdicts): deterministic evidence validators consumed by scheduling + the packaging-config release gate; NO LONGER a merge gate (REVIEW is advisory)",
      "REVIEW_MANIFEST/REVIEW_SIGNALS + REVIEW_VERDICTS_SLOT: REVIEW is a NON-ACTING agent-state (reads verdicts, writes review_findings, emits OK|EMPTY) dispatched to the auto-maintainer-reviewer subagent; the schema/manifest live here, the dispatch is wired in scheduling",
      "IntegrationResult slot schema (versioned: merged, skipped, errors)",
      "VERIFY state: run(TickContext) -> StateResult, reads the loop's open PRs via gh, writes verdicts, emits OK|EMPTY (read-only, deterministic)",
      "INTEGRATE state: run(TickContext) -> StateResult, reads verdicts (thin — no review_verdicts coupling), writes integration_result, emits OK (merges only at auto-merge, guardrail-gated)",
      "CLEANUP state: run(TickContext) -> StateResult, reads integration_result, emits OK (branch/release hygiene, idempotent)",
      "ship/agents/auto-maintainer-reviewer.md — the ADVISORY quality reviewer subagent (code-review + code-simplify lenses over the PR base..head diff; emits review_findings, never merges/approves)"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {
    "files": [
      "safety_governance.permits(effect, mode) — the trust-ladder gate (merge effect)",
      "safety_governance.merge_guardrails(pr_meta, default_branch) -> {ok, violations} — §3.8.1 declarative guardrails"
    ],
    "external": [
      "gh CLI — pr list (the loop's open auto-maintainer-labelled PRs), pr checks (CI rollup), pr merge --merge --delete-branch"
    ]
  },
  "invokes": {"scripts": [], "agents": ["auto-maintainer-reviewer (the REVIEW agent-state's subagent; dispatched by the scheduling executor, not called directly here)"], "external": ["gh", "git"]},
  "never": [
    "calls a model from its SCRIPT states (VERIFY/INTEGRATE/CLEANUP stay deterministic gh/git script-tier; REVIEW is the one model-backed state, a non-acting ADVISORY agent-state dispatched by scheduling)",
    "merges anywhere except at trust mode auto-merge AND only a PR that is ok AND passes merge_guardrails (REVIEW is advisory — INTEGRATE does NOT gate merge on a review approval)",
    "merges a PR with a non-default base, a dirty/conflicting tree, or failing/pending CI",
    "maintains a durable PR-ledger (GitHub is the source of truth, queried live by label)",
    "writes the tracker/issues (REPORT/work-intake owns outbound issue writes; REVIEW only PRODUCES review_findings records conforming to work-intake's DiscoveredIssue schema)",
    "edits files in other features"
  ]
}
```
