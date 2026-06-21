---
feature: verify-integrate
version: 0.2.0
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult / ReviewVerdict schemas reach a breaking major version. See spec.md / feature.json.
---

# verify-integrate — Contract

```json
{
  "provides": {
    "files": [
      "Verdict slot schema (versioned, machine-first: pr_ref, url, ok, ci_state, mergeable, base, reasons)",
      "ReviewVerdict slot schema (versioned, machine-first: pr_ref, approved, severity, findings[]) — the model-backed REVIEW gate's output (#209)",
      "REVIEW_MANIFEST/REVIEW_SIGNALS + REVIEW_VERDICTS_SLOT + is_review_approved(): REVIEW is a NON-ACTING agent-state (reads verdicts, writes review_verdicts, emits OK|EMPTY) dispatched to the auto-maintainer-reviewer subagent; the schema/manifest live here, the dispatch is wired in scheduling",
      "IntegrationResult slot schema (versioned: merged, skipped, errors)",
      "VERIFY state: run(TickContext) -> StateResult, reads the loop's open PRs via gh, writes verdicts, emits OK|EMPTY (read-only, deterministic)",
      "INTEGRATE state: run(TickContext) -> StateResult, reads verdicts + review_verdicts, writes integration_result, emits OK (merges only at gated-merge, guardrail-gated, AND only a review-APPROVED PR)",
      "CLEANUP state: run(TickContext) -> StateResult, reads integration_result, emits OK (branch/release hygiene, idempotent)",
      "ship/agents/auto-maintainer-reviewer.md — the model-backed reviewer subagent (spec-compliance + code-quality over the PR base..head diff)"
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
    "calls a model from its SCRIPT states (VERIFY/INTEGRATE/CLEANUP stay deterministic gh/git script-tier; REVIEW is the one model-backed state, a non-acting agent-state dispatched by scheduling)",
    "merges anywhere except at trust mode gated-merge AND only a PR that is ok AND review-APPROVED AND passes merge_guardrails",
    "merges a PR with a non-default base, a dirty/conflicting tree, or failing/pending CI",
    "maintains a durable PR-ledger (GitHub is the source of truth, queried live by label)",
    "writes the tracker/issues (REPORT/work-intake owns outbound issue writes)",
    "edits files in other features"
  ]
}
```
