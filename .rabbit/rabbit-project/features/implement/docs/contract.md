---
feature: implement
version: 0.4.0
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
      "src/test_gate.py: deterministic correctness gate — runs a target feature's test/run.py via subprocess and records a machine-checkable verdict {feature, passed, returncode, summary} (self-contained, no rabbit-framework runtime dependency)"
    ],
    "skills": []
  },
  "reads": {"files": ["<target-feature>/test/run.py (gate subprocess)"], "external": []},
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
