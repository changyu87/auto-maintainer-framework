# Rabbit Workflow

- **Feature-oriented:** everything in this workflow falls into a feature scope; all edits require a feature touch.
- **Scope-protected:** files under `.claude/` cannot be edited directly; use the override switch with explicit human approval and caution.
- **Drift-protected:** the Stop hook checks every change and warns on drift; CLAUDE.md is repeatedly re-injected to prevent context drift.
- **Subagent-driven:** the TDD subagent enables concurrent feature touches with a full test-driven development cycle.
- Feature touches are token-heavy; use judgment to choose between a trivial direct edit (with override) and a full feature touch.

You are the dispatcher. Orchestrate subagents and use rabbit skills to work on features, bugs, and backlogs. Do not directly edit any scope-protected file without explicit human confirmation and approval.

@.claude/features/policy/coding-rules.md
@.claude/features/policy/philosophy.md
@.claude/features/policy/spec-rules.md
