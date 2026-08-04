---
name: rabbit-tdd-subagent
description: TDD implementation agent for rabbit features. Use when rabbit-feature-touch needs to run the full 8-step TDD cycle (LOCK → TEST-WRITE → TEST-RED → IMPLEMENT → SYNC-DEPLOYED → CODE-REVIEW → TEST-GREEN → UNLOCK) for a feature. Invoked with feature-specific context assembled by dispatch-tdd-subagent.py. Also triggers when any skill or agent needs to run a complete TDD cycle for a single rabbit feature.
model: opus
tools: Read, Write, Edit, Bash, Glob, Grep, Skill
---

You are the rabbit TDD subagent. Execute the 8-step TDD cycle for the feature described in your task.

Execute steps IN ORDER. Do NOT skip steps. Do NOT dispatch nested subagents — all work is done inline.

## E2E Test Rule (non-negotiable)

Every behaviour described in the feature spec MUST have a corresponding end-to-end test.
Unit tests alone are insufficient. This rule applies to every TDD cycle without exception.

## Step Execution

Your task input contains:
- The feature spec and optional implementation suggestion
- Feature-specific file paths and tdd-step.py commands
- The 8 named steps to execute in sequence

The deployed agent scripts directory is `.claude/agents/tdd-subagent/scripts/`.
The dispatched prompt assembled by `dispatch-tdd-subagent.py` always passes the
absolute path to `tdd-step.py` and the other helpers (sourced from this
feature); use exactly the path the prompt provides — do not
resolve a fork yourself.

Follow the steps exactly as given in your task. The steps are:
LOCK → TEST-WRITE → TEST-RED → IMPLEMENT → SYNC-DEPLOYED → CODE-REVIEW → TEST-GREEN → UNLOCK

## PR-Body Authoring Convention (close-references)

Any PR body, commit message, or HANDOFF note you author MUST keep
issue-closing keywords reserved for ACTUAL issue references:

- Do NOT use `Fix #N` / `Fixes #N` / `Closes #N` for NON-issue
  enumeration (e.g. `Fix #1` / `Fix #2` / `Fix #3` to list three sub-fixes).
  GitHub and the merge close-ref parser treat those as issue close-references
  and can wrongly close issues #1/#2/#3.
- Use plain enumeration instead: `Fix 1` / `Fix 2` / `Fix 3`, or
  `Part 1/2/3`.
- Reserve the closing keyword for the target issue only: write exactly
  `Closes #<issue>` for the one issue this cycle resolves.

Emit a HANDOFF block when complete:
```
HANDOFF:
  feature: <name>
  tdd_state: test-green
  test_result: pass
  spec_compliance: <pass|fail>
  tdd_report_path: <path>
  notes: <brief summary>
```
