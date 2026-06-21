---
name: "rabbit-housekeep"
description: "Run a measured verify-or-flag housekeeping wave over the CONSUMING PROJECT's declared features. Default DOC dimension; opt-in --code dimension simplifies a feature's src/."
version: 0.9.0
owner: "rabbit-workflow team"
deprecation_criterion: "when housekeeping is provided natively by the rabbit CLI as a first-class measured-reduction subcommand"
template_version: 1.0.0
---

> **rabbit-workflow command (`/rabbit-housekeep`)** — invoke when the user
> wants to slim/clean/reduce their project's docs, scrub dead prose, or run a
> housekeeping pass over the project they are building with rabbit.

## Purpose

`/rabbit-housekeep` is the user-facing entry point to the `rabbit-housekeep`
skill. It runs the coding-rules §6 verification-based cleanup
(prove-dead / keep-live / flag-unverifiable) in complexity-sized waves across
the **consuming project's** declared features — NOT rabbit-workflow's own
framework features.

This command is a thin entry point: it resolves scope deterministically, then
hands off to the skill, which owns the wave mechanics (measure, verify-or-flag,
decompose, dispatch).

---

## Usage

```
/rabbit-housekeep [<target>] [--code] [--no-automerge]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `<target>` | No | A single project feature name, several names, or `--repo`/`all` for the whole project. Omit to housekeep every consuming-project feature. |
| `--code` | No | Opt into the CODE dimension: simplify and dead-code-prune the target feature's `src/` instead of slimming doc surfaces. Defaults to the DOC dimension when omitted. |
| `--no-automerge` | No | Opt OUT of auto-merge: create each wave's PR and leave it open for you to merge by hand. By default a wave's PR is auto-merged to `main` on green gates (mechanical, fully gated); a wave that fails any gate always leaves its PR open. |

---

## Steps

1. Resolve the in-scope feature set with the deterministic companion script
   `.claude/features/rabbit-housekeep/scripts/resolve-housekeep-scope.py list`.
   The script is mode-aware: in a vendored install it returns the consuming
   project's features under `rabbit-project/features/*` and EXCLUDES rabbit's
   own framework features under `.claude/features/*`; in a standalone install
   it returns `.claude/features/*`. This anchors the wave on the consuming
   project, not rabbit's self-repo (issue #1179).
2. Invoke the `rabbit-housekeep` skill against the resolved target. The skill
   sizes the work into waves, measures before/after with
   `scripts/measure-reduction.py`, runs the verify-or-flag protocol, and
   decomposes cross-feature scope into per-feature sub-issues worked through
   the governed TDD path.
3. The skill creates each wave's PR for the audit trail and, unless
   `--no-automerge` was passed, auto-merges it to `main` on green gates
   (gated by `scripts/wave-automerge.py`); a wave that fails any gate leaves
   its PR open. Report the honest verdict (measured reduction when content was
   removed, or a no-op / already-clean outcome when nothing was dead), any
   `housekeeping`-tagged sub-issues filed for unverifiable items, which wave PRs
   were merged vs left open, and confirmation that behavior was preserved (the
   existing test suite stayed green; load-bearing tokens survived).

This command runs in the MAIN session. The skill it invokes is
subagent-dispatching (it dispatches the TDD subagent and files sub-issues), so
per the SKILL.md authoring standard's "No Subagent-Dispatching Skill Inside
Agent()" rule it MUST NOT be wrapped in an `Agent(...)` call; parallelize by
dispatching the underlying TDD subagent directly at level-1.

---

## Examples

```
# Housekeep a single project feature (doc dimension, default)
/rabbit-housekeep user-auth

# Housekeep the whole consuming project
/rabbit-housekeep --repo

# Simplify and dead-code-prune a feature's src/ (opt-in code dimension)
/rabbit-housekeep user-auth --code
```
