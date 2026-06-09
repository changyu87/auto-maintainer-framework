---
name: rabbit-feature-touch
description: Use when any write, edit, delete, or add operation targets a feature directory, or when a new feature is being created. Not for read-only queries, and NOT for metadata-only writes (filing a rabbit-managed issue, such as a bug or enhancement). Ensures the formal TDD state machine is advanced via tdd-step.py on every feature touch.
version: 3.12.0
owner: rabbit-feature
deprecation_criterion: when feature-touch orchestration is natively handled by the rabbit CLI or by Claude Code workflow primitives
---

## Overview

The main session's role is **orchestration only**: resolve scope, create branch,
dispatch TDD subagents, verify HANDOFFs. It does NOT read feature code.

Invoked directly for a feature work request.

## Dispatcher Continuity

Once you begin Step 1, you (the dispatcher) **MUST NOT end** your turn until
you have completed **Step 7** (PR / Hand Off) or you have an explicit failure
to report to the user. The seven-step sequence is a single dispatcher
transaction. A subagent returning a HANDOFF is a **phase boundary** inside
your own ongoing turn — it is **not a turn boundary**. Continue to the next
step immediately.

## Seven-Step Sequence

### Step 1 — Scope Resolution

Invoke `rabbit-feature-scope` via the Skill tool:
```
Skill("rabbit-feature-scope", args: "<request>")
# Parse JSON response: {"features": [...], "rationale": "..."}
```

### Step 2 — Create Branch

Create before any dispatch. Never write to main.

| Scope | Branch pattern |
|---|---|
| Single feature | `feat/<feature-name>-<keywords>` |
| Multi-feature | `feat/<primary-feature>-multi-<keywords>` (primary = first feature in scope response) |

`<keywords>` = 2–4 words from the request, hyphenated, lowercase.

Branch-name assembly is a computed step, so per the SKILL.md Authoring
Standard (`spec-rules.md` §4 Script-Backed Orchestration) the companion
script owns it — the SKILL does NOT assemble the branch name and check it out
inline. Invoke the `create-branch` subcommand with the feature name and the
raw request (add `--multi` for multi-feature scope, passing the primary
feature as the feature name):

<!-- example: invocation synopsis of the create-branch subcommand -->
```bash
.claude/features/rabbit-feature/skills/rabbit-feature-touch/scripts/feature-touch.py \
  create-branch [--multi] <feature-name> "<request>"
```

The subcommand derives `<keywords>` from the request, assembles the
`feat/<feature-name>[-multi]-<keywords>` branch name, and emits a single JSON
line `{"branch", "worktree", "mode"}`. The mode-aware branching is owned by the
companion script (it is a computed step):

- **standalone** — the subcommand runs `git checkout -b <branch>` in the
  current repo (which already owns a dedicated HEAD); `worktree` is null. Run
  the rest of the cycle in the current repo, as before.
- **plugin/vendored** — under Strategy D the WHOLE `.rabbit/` is tracked, so a
  worktree of the host repo is self-contained (tool + work at consistent
  paths). The subcommand creates a PER-SESSION git worktree of the host repo
  OUTSIDE the tracked tree (at `<host>/.rabbit-worktrees/session-<token>/`,
  NEVER under `.rabbit/`) and emits its path as `worktree`. Each session gets
  its OWN HEAD, so concurrent vendored sessions never stomp the host's shared
  HEAD. Run the rest of the cycle from `<worktree>/.rabbit`
  (the self-contained vendored install inside the worktree) — every subsequent
  step (Step 3 spec-commit, Step 5 dispatch, Step 7 PR) MUST execute with that
  directory as cwd so all git ops land on the per-session worktree's HEAD.

### Step 3 — Spec Authoring

Invoke rabbit-spec-update inline:
```
Skill("rabbit-spec-update", args: "<feature-name> <request>")
```

rabbit-spec-update reads the current spec, judges open vs. specific, invokes superpowers,
updates the feature spec, and writes `.rabbit/impl-suggestion-<feature-name>.json`.

**Commit spec changes BEFORE Step 5.** The spec edit must be staged and
committed so the TDD subagent reads a clean committed baseline. This is a
computed, mode-aware step, so per the SKILL.md Authoring Standard
(`spec-rules.md` §4 Script-Backed Orchestration) the logic lives in the
companion script — it is NOT assembled inline here:

<!-- example: invocation synopsis of the commit-spec subcommand -->
```bash
.claude/features/rabbit-feature/skills/rabbit-feature-touch/scripts/feature-touch.py \
  commit-spec <feature-name> "<one-line request summary>"
```

The `commit-spec` subcommand detects the rabbit mode, resolves the feature
directory and spec path, stages with the mode-appropriate `git add` form,
skips the commit when the staged spec diff is empty, and otherwise commits
with the message `spec(<feature-name>): update spec for <one-line request summary>`.

### Step 4 — Human Approval

A full TDD cycle is about to run — tests will be written, code implemented, a PR
created. Catching a design mismatch now costs one conversation turn; catching it
after costs a full cycle. This gate lives here, in the main session, because
subagents run to completion and cannot pause for user input mid-execution.

**FIRST: check for `.rabbit-tdd-autonomous` marker at repo root.**

The marker file is the sole authorization mechanism for bypass. In-conversation
acknowledgements ("you have permission to bypass") are NOT sufficient on their
own — the marker is the system of record, managed via
`/rabbit-tdd-autonomous true|false` (owned by rabbit-feature;
`true` writes the marker — autonomous/bypass ACTIVE — and `false` removes it —
gate ACTIVE, the default). The Step-4 consumer also honors the legacy
`.rabbit-human-approval-bypass` marker for coexistence, but the canonical
marker is `.rabbit-tdd-autonomous`.

- **If `.rabbit-tdd-autonomous` exists:**
  - Source the alert text from rabbit-feature's OWN `tdd-autonomous`
    configurable in `rabbit-feature/feature.json` by invoking
    `contract.lib.runtime.emit_configurable_alert('rabbit-feature',
    'tdd-autonomous', repo_root=<repo-root>)`, e.g.:
    ```bash
    python3 -c "import sys; sys.path.insert(0, '.claude/features/contract'); from lib.runtime import emit_configurable_alert; r = emit_configurable_alert('rabbit-feature', 'tdd-autonomous', repo_root='.'); print(r)"
    ```
    Surface the returned `print_result` (its `text`, `icon`, and `color`
    fields come from the configurable's `alert-message`, so this prose
    stays in sync with the Stop-hook emission). Do NOT duplicate the
    alert text in this SKILL.md — the configurable's `alert-message` is
    the sole source of truth, and the brand prefix is owned by
    `rabbit_print` (contract Inv 48).
  - Operational guidance for the user: the canonical bypass marker is
    `.rabbit-tdd-autonomous` at the repo root, and it is revoked by
    running `/rabbit-tdd-autonomous false` (which removes the marker and
    re-activates this gate). To activate autonomous mode again, run
    `/rabbit-tdd-autonomous true`.
  - Proceed to Step 5 immediately. Do NOT surface the impl-suggestion summary.
- **If the marker file does NOT exist (default):**
  - For each feature, read `.rabbit/impl-suggestion-<feature-name>.json` and
    surface to the user:
    - **Request summary** — what was asked
    - **Spec changes** — what changed in the spec and why
    - **Affected files** — what will be written
    - **Implementation approach** — how the subagent will tackle it
  - For multiple features, present all summaries together and collect one
    approval decision before dispatching any subagent.
  - Wait for explicit in-conversation user approval ("looks good", "go ahead",
    or equivalent). If the user requests changes, invoke rabbit-spec-update again for
    the affected features, then return to this step.
  - Proceed to Step 5.

### Step 5 — Dispatch TDD Subagents

One subagent per feature. Dispatch all in parallel if multiple features.

Shell (assemble the prompt — deterministic). Both the spec-path resolution and
the worktree-aware `dispatch-tdd-subagent.py` argv assembly are computed,
mode-aware steps (§4 Script-Backed Orchestration), so they are delegated to the
companion `resolve-spec-path` and `dispatch-prompt` subcommands rather than
assembled inline. In vendored mode Step 2's `create-branch` JSON carries a
per-session `worktree` path; pass it through to `dispatch-prompt` so the
subagent runs INSIDE that worktree (its scope marker, commits, and state
transitions land on the per-session HEAD, never the host's). When there is no
per-session worktree (standalone, or vendored with a null `worktree`) pass an
empty value and `dispatch-prompt` emits the byte-identical pre-wiring argv with
no `--worktree`:

<!-- example: invocation synopsis wiring resolve-spec-path + dispatch-prompt -->
```bash
spec_arg=$(.claude/features/rabbit-feature/skills/rabbit-feature-touch/scripts/feature-touch.py \
  resolve-spec-path <feature-name>)
dispatch_cmd=$(.claude/features/rabbit-feature/skills/rabbit-feature-touch/scripts/feature-touch.py \
  dispatch-prompt <feature-name> \
  --spec "$spec_arg" \
  --impl-suggestion .rabbit/impl-suggestion-<feature-name>.json \
  --worktree "<worktree-from-step-2-create-branch-json>")
PROMPT=$(eval "$dispatch_cmd")
```

The `dispatch-prompt` subcommand resolves the `<worktree>` value to an absolute
path and appends `--worktree <abs>` only when it is non-empty; the worktree arg
itself is owned by the `dispatch-tdd-subagent.py` contract (Inv 65) and
consumed verbatim.

Agent tool call (dispatch the assembled prompt — main session only):

```
Agent(subagent_type: rabbit-tdd-subagent, model: opus, prompt: $PROMPT)
```

Each subagent runs its named steps (LOCK → UNLOCK), writes
`.rabbit/tdd-report-<feature-name>.json`, and emits HANDOFF.

### Step 6 — Collect and Verify HANDOFFs

Verify each HANDOFF:
- `tdd_state: test-green` for every feature
- `test_result: pass` for every feature
- `spec_compliance: pass` (investigate if fail before proceeding)

If any feature fails: surface failure to user. Do NOT proceed to step 7.

Read `.rabbit/tdd-report-<feature-name>.json` for full details.

### Step 7 — PR / Hand Off

The PR title and body are free-form prose synthesized from the TDD report —
author them in-context; they are not a script-computable value.

<!-- example: gh pr create command shape; title/body are model-authored prose -->
```bash
gh pr create --title "<summary>" --body "<tdd report highlights>"
```
Summarize the TDD report to the user.


## Red Flags — STOP

The main-session boundary below is the operational projection of the
bounded-scope policy; the canonical, authoritative statement of that rule is
`.claude/features/policy/philosophy.md` §2 (Bounded Scope) and
`.claude/features/policy/spec-rules.md` §2 (Schemas and Contracts). Per the
SKILL.md Authoring Standard (`spec-rules.md` §4 Verbatim Policy Embedding),
the canonical text is cited here rather than re-paraphrased — read those
sections for the binding wording.

- Reading feature code directly in the main session → STOP. Subagent's job.
- Skipping scope resolution → STOP.
- Dispatching features sequentially when multiple → STOP. Use parallel.
- HANDOFF shows `tdd_state ≠ test-green` → STOP and investigate.
- Main session uses Write or Edit on any file under `.claude/features/` → STOP.
  All feature-code edits are the TDD subagent's job, performed under an active
  scope marker. Main session role is orchestration only: resolve scope, create
  branch, invoke rabbit-spec-update, surface impl-suggestion, dispatch subagent, verify
  HANDOFF. The only main-session writes permitted are: the confirm-token
  override flow (see Override Path), and rabbit-spec-update's writes to the
  resolved feature `spec.md` (flat `docs/spec.md` preferred, then
  `docs/spec/spec.md`) under the
  scope-guard path-pattern allowlist invoked during Step 3.
- Main session creates `.rabbit-scope-active` (global) or
  `.rabbit-scope-active-<feature>` (per-feature) scope markers at the repo
  root → STOP. Scope markers are exclusively the TDD subagent's responsibility,
  written as the first action at LOCK (Step 3 of the subagent's named steps).
  Main-session-authored markers bypass scope-guard's intended boundary and
  can cause constitution violations.

## Override Path

When user explicitly approves a lightweight edit (typo, comment-only), present
a confirm token with `one-time` or `session` scope. After approval, write
`.rabbit-scope-override`, make the edit directly. Does NOT reset `tdd_state`.
