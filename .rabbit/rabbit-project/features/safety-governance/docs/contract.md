---
feature: safety-governance
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the governance config schema reaches a breaking major version, or when trust-ladder / budget enforcement moves into a different layer than a project-local governance config consulted at tick entry. See spec.md / feature.json.
---

# safety-governance — Contract

```json
{
  "provides": {
    "files": [
      "Governance config schema (versioned, machine-first: mode + budget) + loader for project-local ${CLAUDE_PROJECT_DIR}/.auto-maintainer/governance.json with defaults",
      "Trust-ladder gate: permits(effect_kind, mode) over dry-run|propose|gated-merge",
      "Budget readiness gate (per-tick/per-day token ceiling, local-tz day window, null=unlimited): window rollover reset + over-ceiling idle (auto-resume, no latch), over an injectable spend seam",
      "No-AskUserQuestion->ABORTED helper: latches ABORTED + emits an escalation seam"
    ],
    "scripts": [],
    "skills": []
  },
  "reads": {
    "files": ["${CLAUDE_PROJECT_DIR}/.auto-maintainer/governance.json (project-local override; absent => defaults)"],
    "external": []
  },
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "latches a halt disposition for budget exhaustion (budget is an auto-resuming readiness gate, not a latch; only faults/ABORTED latch)",
    "modifies lifecycle-dispositions (consumes it unchanged to latch ABORTED)",
    "performs declarative VCS guardrails (§3.8.1), backoff/circuit-breaker (§3.8.5), or the loopback/provenance guard (§3.11.5) — deferred to consumer milestones",
    "calls a model, the network, the filesystem (beyond durable budget state), or the wall clock except through the injectable now",
    "prompts userConfig values or implements the real issue-comment escalation sink (§3.9.3 / §3.10.1, deferred)",
    "edits files in other features"
  ]
}
```
