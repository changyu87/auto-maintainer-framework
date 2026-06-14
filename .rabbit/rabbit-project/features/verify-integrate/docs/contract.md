---
feature: verify-integrate
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the loop adopts a non-git VCS backend, or a model-backed verify/integrate policy replaces the deterministic gh-based gates, or when the Verdict / IntegrationResult schemas reach a breaking major version. See spec.md / feature.json.
---

# verify-integrate — Contract

```json
{
  "provides": {
    "files": [
      "Verdict slot schema (versioned, machine-first: pr_ref, url, ok, ci_state, mergeable, base, reasons)",
      "IntegrationResult slot schema (versioned: merged, skipped, errors)",
      "VERIFY state: run(TickContext) -> StateResult, reads the loop's open PRs via gh, writes verdicts, emits OK|EMPTY (read-only, deterministic)",
      "INTEGRATE state: run(TickContext) -> StateResult, reads verdicts, writes integration_result, emits OK (merges only at gated-merge, guardrail-gated)",
      "CLEANUP state: run(TickContext) -> StateResult, reads integration_result, emits OK (branch/release hygiene, idempotent)"
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
  "invokes": {"scripts": [], "agents": [], "external": ["gh", "git"]},
  "never": [
    "calls a model (all three states are deterministic gh/git script-tier)",
    "merges anywhere except at trust mode gated-merge AND only a PR that is ok AND passes merge_guardrails",
    "merges a PR with a non-default base, a dirty/conflicting tree, or failing/pending CI",
    "maintains a durable PR-ledger (GitHub is the source of truth, queried live by label)",
    "writes the tracker/issues (REPORT/work-intake owns outbound issue writes)",
    "edits files in other features"
  ]
}
```
