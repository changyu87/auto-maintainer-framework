---
feature: implement
version: 0.5.0
owner: changyu87
deprecation_criterion: Superseded when the model-backed implement-then-PR doer (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the Handoff schema reaches a breaking major version. See spec.md / feature.json.
---

# implement — Contract

```json
{
  "provides": {
    "files": [
      "Handoff slot schema (versioned, machine-first: work_order_id, status, artifact, discovered_work, concerns, blocked_reason, test_verdict)",
      "IMPLEMENT state (dry-run reference adapter): run(TickContext) -> StateResult, reads execution_plan, writes handoffs, emits OK|BLOCKED (deterministic, inert)",
      "validate_handoff(handoff) -> ValidationResult: deterministic validity predicate; an opened handoff is valid only with a passing script-produced test_verdict (DESIGN §3.6.3)"
    ],
    "scripts": [
      "src/test_gate.py: deterministic correctness gate (self-contained, stdlib-only, NO sibling-lib import) — runs the target feature's CONFIGURED test command via subprocess and records a machine-checkable verdict {feature, passed, returncode, summary}. Resolves the command by a direct tolerant json.load of the implement_test_command key in ${project_dir}/.auto-maintainer/config.json (the KEY owned by safety-governance's schema), three-way: null/absent=run <feature>/test/run.py (default; missing run.py = failed verdict), a command string=run it (shell, cwd=feature dir), 'none'/'skip'=skip the gate (passed=True no-op). A --test-command CLI arg overrides; --project-dir selects the config; resolution is tolerant (missing/unreadable config or key -> run.py default, never crashes)"
    ],
    "skills": []
  },
  "reads": {"files": ["<target-feature>/test/run.py or the configured implement_test_command (gate subprocess)", "${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json — the implement_test_command key ONLY, via a direct stdlib json.load (the key owned by safety-governance's schema; NOT via a safety-governance import, to keep the gate self-contained)"], "external": []},
  "invokes": {"scripts": ["src/test_gate.py (by the shipped implementer subagent on the accept path)"], "agents": [], "external": []},
  "never": [
    "calls a model (the dry-run rung is deterministic; the model-backed doer is a separate deferred adapter)",
    "creates a branch, commit, PR, or any VCS artifact",
    "reads the workspace slot or provisions an isolated worktree (deferred to the model-backed doer)",
    "the dry-run adapter writes to the tracker or filesystem (inert; git status stays clean after a tick). The test_gate.py script writes ONLY its verdict artifact to the caller-named path",
    "imposes a per-task budget cap (the token-ceiling budget lives in safety-governance)",
    "calls the wall clock, randomness, or the network",
    "edits files in other features"
  ]
}
```
