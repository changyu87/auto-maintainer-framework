---
feature: safety-governance
version: 0.7.0
owner: changyu87
deprecation_criterion: Superseded when trust-ladder / budget enforcement moves into a different layer than a project-local central config (config.json) consulted at tick entry, or when the config schema reaches its next breaking major (3.0.0). See spec.md / feature.json.
---

# safety-governance — Contract

```json
{
  "provides": {
    "files": [
      "Central config schema (versioned 2.2.0, machine-first: mode + work_own_filings + budget.per_day_tokens + budget.window_tz + heartbeat.interval_minutes + backoff.threshold) + load_config(project_dir) for project-local ${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json with defaults (legacy governance.json migrated once; legacy mode 'gated-merge' mapped to 'auto-merge' on load); load_governance is a thin alias; work_own_filings(config) accessor (default True, §3.11.5 default-on opt-out)",
      "MAINTAINER_REPO: a FIXED module constant ('changyu87/auto-maintainer-framework') for maintainer-self REPORT routing (§3.11.6) — not a config field",
      "Trust-ladder gate: permits(effect_kind, mode) over dry-run|propose|auto-merge (legacy 'gated-merge' tolerated, mapped to 'auto-merge')",
      "Merge guardrails (§3.8.1): merge_guardrails(pr_meta, default_branch, delete_branch) -> {ok, violations}, a pure declarative backstop below the trust ladder",
      "Budget readiness gate (per-day token ceiling, local-tz day window, null=unlimited): window rollover reset + over-ceiling idle (auto-resume, no latch), over an injectable spend seam",
      "No-AskUserQuestion->ABORTED helper: latches ABORTED + emits an escalation seam"
    ],
    "scripts": ["src/configure.py — deterministic central-config writer (load_config-modify-save of config.json; --mode/--per-day-tokens/--interval-minutes/--backoff-threshold/--describe/--show)"],
    "skills": ["ship/skills/configure/SKILL.md — /auto-maintainer:configure relay over configure.py"]
  },
  "reads": {
    "files": ["${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json (project-local override; absent => defaults; legacy governance.json migrated once)"],
    "external": []
  },
  "invokes": {"scripts": [], "agents": [], "external": []},
  "never": [
    "latches a halt disposition for budget exhaustion (budget is an auto-resuming readiness gate, not a latch; only faults/ABORTED latch)",
    "modifies lifecycle-dispositions (consumes it unchanged to latch ABORTED)",
    "performs backoff/circuit-breaker enforcement (§3.8.5) or the loopback/provenance guard (§3.11.5) — deferred to consumer milestones",
    "calls a model, the network, the filesystem (beyond the durable central config + budget state), or the wall clock except through the injectable now",
    "prompts userConfig values or implements the real issue-comment escalation sink (§3.9.3 / §3.10.1, deferred)",
    "edits files in other features"
  ]
}
```
