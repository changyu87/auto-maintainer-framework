---
feature: safety-governance
version: 0.12.0
owner: changyu87
deprecation_criterion: Superseded when trust-ladder / budget enforcement moves into a different layer than a project-local central config (config.json) consulted at tick entry, or when the config schema reaches its next breaking major (3.0.0). See spec.md / feature.json.
---

# safety-governance — Contract

```json
{
  "provides": {
    "files": [
      "Central config schema (versioned 2.8.0, machine-first: mode + work_own_filings + regression_command + doc_check_features_root + implement_test_command + issue_filter + budget.per_day_tokens + budget.window_tz + heartbeat.interval_minutes + backoff.threshold) + load_config(project_dir) for project-local ${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json with defaults (legacy governance.json migrated once; legacy mode 'gated-merge' mapped to 'auto-merge' on load; schema_version is loader-owned metadata EXCLUDED from the 3-way field-merge — always normalized to the current GOVERNANCE_SCHEMA_VERSION, never a spurious conflict/stale-preserve); load_governance is a thin alias; work_own_filings(config) accessor (default True, §3.11.5 default-on opt-out); issue_filter(config) accessor (pure DNF-label + title_pattern normalizer, default no-filter = pull all open issues, §work-intake PULL); issue_filter_apply_labels(config) accessor (pure: the FIRST non-empty AND-group of issue_filter.labels — the labels a loop-FILED discovery issue must carry to be re-pullable by a later label-filtered PULL; [] when no filter; consumed by scheduling REPORT flush -> work-intake filing sink); implement_test_command(config) accessor (raw value, None default = run <feature>/test/run.py; a command string = run it; the sentinel 'none'/'skip' = skip the IMPLEMENT test-gate; consumed by §implement test_gate.py)",
      "MAINTAINER_REPO: a FIXED module constant ('changyu87/auto-maintainer-framework') for maintainer-self REPORT routing (§3.11.6) — not a config field",
      "Trust-ladder gate: permits(effect_kind, mode) over dry-run|propose|auto-merge (legacy 'gated-merge' tolerated, mapped to 'auto-merge')",
      "Merge guardrails (§3.8.1): merge_guardrails(pr_meta, default_branch, delete_branch) -> {ok, violations}, a pure declarative backstop below the trust ladder",
      "Budget readiness gate (per-day token ceiling, local-tz day window, null=unlimited): window rollover reset + over-ceiling idle (auto-resume, no latch), over an injectable spend seam",
      "No-AskUserQuestion->ABORTED helper: latches ABORTED + emits an escalation seam"
    ],
    "scripts": ["src/configure.py — deterministic central-config writer (load_config-modify-save of config.json; --mode/--per-day-tokens/--interval-minutes/--backoff-threshold/--regression-command/--doc-check-features-root/--features-root/--work-own-filings/--issue-labels (DNF: comma=AND, semicolon=OR)/--issue-title-pattern/--implement-test-command (empty clears to null; 'none'/'skip' preserved verbatim as the skip sentinel)/--show; --describe emits the field catalog with a loop-stage per knob; --preflight emits the read-only gh-auth + resolved-repo probe for --setup onboarding — issue_filter values validated through this feature's issue_filter normalizer)"],
    "skills": ["ship/skills/configure/SKILL.md — /auto-maintainer:configure relay over configure.py, including the guided --setup onboarding (preflight -> confirm repo -> loop-stage-ordered walk -> opt-in route/adapter -> apply -> offer /start)"]
  },
  "reads": {
    "files": ["${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json (project-local override; absent => defaults; legacy governance.json migrated once)"],
    "external": []
  },
  "invokes": {"scripts": [], "agents": [], "external": ["gh auth status (configure.py --preflight: read-only auth probe for the --setup onboarding)", "gh repo resolution for --preflight (the gh-default / git-remote repo the loop would maintain; read-only, e.g. gh repo view --json nameWithOwner)"]},
  "never": [
    "latches a halt disposition for budget exhaustion (budget is an auto-resuming readiness gate, not a latch; only faults/ABORTED latch)",
    "modifies lifecycle-dispositions (consumes it unchanged to latch ABORTED)",
    "performs backoff/circuit-breaker enforcement (§3.8.5) or the loopback/provenance guard (§3.11.5) — deferred to consumer milestones",
    "calls a model, the network, the filesystem (beyond the durable central config + budget state), or the wall clock except through the injectable now — EXCEPT configure.py --preflight, which shells read-only gh (auth status + repo resolution) for the --setup onboarding and writes nothing",
    "prompts userConfig values or implements the real issue-comment escalation sink (§3.9.3 / §3.10.1, deferred)",
    "edits files in other features"
  ]
}
```
