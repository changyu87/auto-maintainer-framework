---
name: rabbit-issue
version: 1.16.0
owner: rabbit-workflow team
deprecation_criterion: when GH Issues is replaced or the workflow moves to a different tracker; revisit when claude-plugins-official ships a GH Issues skill
description: Use whenever Claude detects intent to file, list, show, close, reopen, or otherwise lifecycle-manage a bug or enhancement in this repository's GitHub Issues — including casual phrasings like "file a bug", "log an enhancement", "open a feature request", "what bugs are open", "list issues for <feature>", "show issue 42", "work this bug", "close that issue", "mark issue N as not planned", or "reopen issue N". rabbit-issue is the only rabbit-managed issue surface; do NOT invoke rabbit-file or its scripts. rabbit-issue wraps the `gh` CLI to operate on GitHub Issues, honours an actionability safety guard (it refuses to close/reopen issues lacking a valid `feature:` label) so raw human-filed issues are never touched, and orchestrates the File / List / Work protocols against the three runtime scripts under `.claude/features/rabbit-issue/scripts/`. Trigger on any GH-Issues lifecycle phrasing — even when the user does not say "GitHub" or "issue" explicitly.
---

## Overview

Three modes — **File**, **List**, and **Work** — operate against GitHub
Issues via the `gh` CLI. Two issue types are supported: **bug** and
**enhancement** (GH's defaults). The target repo defaults to the upstream
rabbit-workflow repo (const `changyu87/rabbit-workflow` in `_gh.py`);
set `RABBIT_ISSUE_REPO=<owner>/<repo>` to override for forks or testing.
`gh auth status` must be green or the scripts fail loudly with an
actionable error.

rabbit-issue is the sole rabbit-managed issue surface; all lifecycle
operations route through this skill. There is no branch-backed item
storage on `origin/bug-backlog-files`. Do not invoke `rabbit-file`, its
scripts, or any `item-status.py` / `file-item.py` under
`.claude/features/rabbit-file/`.

There are no slash commands. The invocation surface is direct script
invocation:

| Mode | Script | Purpose |
|---|---|---|
| File | `python3 .claude/features/rabbit-issue/scripts/file-item.py …`       | File a new bug or enhancement |
| Work | `python3 .claude/features/rabbit-issue/scripts/item-status.py …`     | Show, close, or reopen an issue |
| List | `python3 .claude/features/rabbit-issue/scripts/list-items.py …`      | List issues with type / feature / status filters |
| Show | `python3 .claude/features/rabbit-issue/scripts/item-status.py show <N>` | Print a single issue's JSON |

The shared helper `_gh.py` resolves the repo slug and wraps `gh` calls.

---

## Label Schema

Every issue filed via `rabbit-issue` carries the type label
(`bug` / `enhancement`), plus `feature:<name>` and `priority:<…>`. The
`filed-by:` provenance label is OPTIONAL — present
only for non-human filers. They are auto-created on demand (idempotent
`gh label create … || true`) at first `file-item.py` call — there is no
separate bootstrap step. See docs/spec.md §Label schema for the full
cardinality table.

The `filed-by:` provenance label is a **fixed enum** with two non-human
values. Human is the untagged default (OMIT `--filed-by`), so a human
filing carries no `filed-by:` label; pass `--filed-by rabbit` for a
bot/wrapped rabbit script, or `--filed-by autonomous-evolve` for the
autonomous evolve loop. `file-item.py` REJECTS any value outside
`{rabbit, autonomous-evolve}` with a clear error. `filed-by:` is a
justified native-first exception — the loop and human file under the same
single GitHub identity here, so the native author cannot tell them apart;
it carries a deprecation criterion for migration to the native author once
those identities diverge. See docs/spec.md §Provenance label.

`housekeeping` is a sanctioned category label marking housekeeping-wave
work. Pass `--housekeeping` to `file-item.py` to stamp it at filing time;
omit it for non-housekeeping issues. Both labels are additive — when
present they never change the other labels. See docs/spec.md
§Housekeeping label.

`in-progress` is a sanctioned category label marking an issue as a member
of the loop's live dispatch set. Unlike `housekeeping`, it has NO
`file-item.py` flag and is NOT stamped at filing time: the autonomous
evolve loop's reconcile mirrors its dispatch journal's live set onto this
label and auto-creates it on first use via the same `ensure_labels` path.
It is additive. See docs/spec.md §In-progress label.

---

## File Protocol

When the user confirms they want to file a bug or enhancement:

1. **Resolve feature scope** — invoke `rabbit-feature-scope` to identify
   the related feature. Ask the user only if the scope is ambiguous.
2. **Collect missing fields** — ask clarifying questions for any of
   `title`, `description`, `priority`. Do not invent values.
3. **Run `file-item.py`**:
   ```bash
   python3 .claude/features/rabbit-issue/scripts/file-item.py \
     --type bug|enhancement \
     --feature <feature-name> \
     --title "..." \
     --priority <low|medium|high|critical> \
     --description "..." \
     [--filed-by <source>] \
     [--housekeeping] \
     [--parent <N>]
   ```
   See §Label Schema above for `--filed-by` and `--housekeeping`. The
   script auto-creates any missing labels, then calls `gh issue create`
   with the resolved labels attached. `--parent <N>` is OPTIONAL: when
   supplied, after the child is created it is linked as a GitHub-native
   sub-issue of parent `<N>` and the emitted JSON gains a `parent` field;
   when omitted, no link is made, no extra gh calls run, and the JSON is
   exactly `{number, url, type}`. See docs/spec.md §Sub-issue linkage.
4. **Report** the assigned issue number and URL back to the user. GH
   allocates the number; rabbit does not maintain a local counter.

---

## List Protocol

When the user wants to see open or closed issues:

<!-- example: illustrative CLI synopsis for list-items.py; bracketed args are optional, not a live step the model assembles. -->
```bash
python3 .claude/features/rabbit-issue/scripts/list-items.py \
  [--type bug|enhancement|all] \
  [--feature <feature-name>] \
  [--status open|closed]
```

Defaults: `--type all`, `--status open`, no feature filter. The script
calls `gh issue list` with the resolved label filters and prints a
deterministic, sorted summary.

Output format: `NUMBER  [TYPE]  [STATE]  [PRIORITY]  [feature:<name>]  TITLE`

Output is sorted ascending by issue number so repeated invocations
against the same repo state always print identical lines.

---

## Work Protocol

When the user asks to work, close, or reopen an issue:

1. **Fetch** — `python3 .claude/features/rabbit-issue/scripts/item-status.py show <N>`
   reads the issue from GH. If the issue is not found, inform the user
   and stop. When the issue's comment thread is also needed (e.g. to
   feed the eval subagent the full discussion), read the comment bodies
   via `gh issue view <N> --json comments`, never the human view
   `--comments` (see docs/spec.md §Reading issue comments for why).

2. **Eval subagent** — dispatch a read-only default-model subagent with
   the fetched issue JSON and the affected feature's `docs/spec.md`.
   It returns:
   - **Verdict**: `valid` (still relevant and reproducible) or
     `stale/invalid` with reason.
   - **Test gap analysis**: names the existing tests covering the
     affected behaviour and lists any missing tests that the implementer
     should add before turning RED → GREEN. A bug fix without an
     accompanying regression test is incomplete — surface this gap up
     front so the user can authorize the test work.

   The subagent is read-only: it does NOT edit code, file new items, or
   touch issue state.

3. **User-decision gate** — brief the user with the verdict, the test
   gap, and a clear recommendation:
   - Summarize verdict and reasoning.
   - Recommend close-without-work (if stale/invalid) or proceed (if
     valid).
   - Do NOT invoke `rabbit-feature-touch` until the user confirms.

4. **If the user chooses close without work**:
   ```bash
   python3 .claude/features/rabbit-issue/scripts/item-status.py close <N> \
     --reason not-planned \
     --reason-text "<a specific, concrete justification, >= 50 chars>" \
     --comment "<why>"
   ```
   `not-planned` is the correct reason for closing stale or invalid
   issues; the script translates it to gh's `not planned` at the CLI
   boundary, yielding `state_reason = not_planned`. The safety guard
   below still applies.

   `--reason-text` is **required** for `--reason not-planned`: at least 50
   characters and free of reflexive-deferral boilerplate, which the script
   rejects (see docs/spec.md §Lifecycle for the rejected-phrase list).
   Write a concrete reason that names *why* the issue is stale or invalid
   (e.g. what replaced it), not a generic deferral.

5. **If the user confirms to proceed**:
   - Invoke `rabbit-feature-touch` via its **default full seven-step TDD
     cycle** (NOT the lightweight Override Path), passing the issue
     title + body as the request text (no special CLI flag, no "issue
     mode" — `rabbit-feature-touch` does NOT need to know about GH
     Issues at all; tracking and closure are rabbit-issue's concern).
   - The TDD cycle's implementation commit should include `Fixes #N` in
     the commit message so GitHub auto-closes the issue with
     `state_reason = completed` and auto-links the SHA in the Timeline
     once the PR merges to the default branch.
   - **After touch returns successfully — rabbit-issue verifies closure:**
     - **Auto-close path** — if the impl commit included `Fixes #N` AND
       the PR merged to the default branch, GH already closed the
       issue (`gh issue view <N>` shows `closed`). Nothing more to do.
     - **Fallback path** — otherwise (e.g., squash-merge stripped the
       trailer, the PR merged via the web UI without it, or the work
       landed via direct commit), rabbit-issue explicitly closes:
       ```bash
       python3 .claude/features/rabbit-issue/scripts/item-status.py close <N> \
         --reason completed \
         --commit-sha <sha> \
         --comment "TDD cycle complete in <commit-sha>"
       ```
       `--reason completed` requires **exactly one** deliverable proof.
       For landed work pass `--commit-sha <sha>`, which must resolve to a
       real commit in the local git repo — "completed" can only be
       asserted when work actually landed. Pass the SHA of the commit
       that carried the fix.
       Always verify state with `gh issue view <N>` before the
       fallback close so an already-closed issue is not re-closed.
   - **Research SMALL-outcome close** (the comment-only deliverable):
     when a research item's deliverable is findings appended as a COMMENT
     on the request issue (no landed commit), close `completed` with the
     comment URL instead of a commit SHA:
     ```bash
     python3 .claude/features/rabbit-issue/scripts/item-status.py close <N> \
       --reason completed \
       --findings-comment-url https://github.com/<owner>/<repo>/issues/<N>#issuecomment-<id>
     ```
     `--findings-comment-url` is validated against the GitHub
     issue-comment URL shape and is mutually exclusive with
     `--commit-sha`; the URL is persisted as the close comment so the
     closed issue links to its findings. See docs/spec.md §Lifecycle.

---

## Safety Invariants

These guard the boundary between human-filed and rabbit-filed issues,
and between rabbit and the GH API.

- **Actionability guard** — `item-status.py close` and
  `item-status.py reopen` refuse to act on issues that are NOT
  actionable, i.e. that lack a valid `feature:<name>` label. A raw,
  hand-filed GitHub issue with no labels stays out of rabbit's
  automation reach. Do not work around the guard by slapping a
  `feature:` label on a human's issue without their consent; ask first.
- **Human-filing enforcement (Issue Form)** — a native GitHub Issue Form
  (`.github/ISSUE_TEMPLATE/file-item.yml`) makes `feature` and `priority`
  REQUIRED dropdowns for a human web filing, and a companion workflow
  (`.github/workflows/issue-form-autolabel.yml`) stamps `feature:<x>` /
  `priority:<y>` from the answers on issue open — so a hand-filed issue
  lands actionable rather than label-less. The form's option sets mirror
  `file-item.py`'s enums (a drift guard holds them in sync). The form and
  workflow are governed deployed surfaces (`publish_file` manifest
  entries); `file-item.py` remains the enforcement path for bot/loop
  filings, which forms do not reach. See docs/spec.md
  §Human-filing enforcement.
- **`gh auth` required** — every script checks `gh auth status` and
  fails with an actionable error if authentication is not green. Do
  not fall back to unauthenticated calls.
- **Upstream rabbit-workflow target** — the target repo defaults to the
  const `changyu87/rabbit-workflow`. Override via the `RABBIT_ISSUE_REPO`
  environment variable (e.g. `RABBIT_ISSUE_REPO=myfork/rabbit-workflow`
  for fork contributors). The skill never consults the cwd's git remote —
  bugs about rabbit always go to rabbit's repo, regardless of where the
  user invokes from.

---

## Lifecycle

GH issue state is binary; `state_reason` distinguishes the close path.

- `state` ∈ {`open`, `closed`}
- `state_reason` ∈ {`completed`, `not_planned`, `null`}
  - `completed` — closed after a TDD fix (default close reason for work
    that landed). Set automatically by `Fixes #N` auto-close, or
    manually via the `--reason completed --commit-sha <sha>` fallback
    (the SHA must be a real local commit). A research SMALL-outcome item
    whose deliverable is a linked comment closes `completed` via
    `--findings-comment-url <url>` instead (mutually exclusive with
    `--commit-sha`; the URL must be a GitHub issue-comment URL).
  - `not_planned` — closed without work (stale or invalid); requires a
    specific `--reason-text` (>= 50 chars, no boilerplate).
  - `null` — pre-rabbit closures or external closes; not produced by
    this skill.
- Reopen restores `state = open` with `state_reason = reopened`.

---

## Scripts Reference

| Script | Purpose |
|---|---|
| `file-item.py` | File a new bug or enhancement (auto-creates labels); `--filed-by <rabbit\|autonomous-evolve>` stamps the matching `filed-by:` label (omit for human; other values rejected); `--housekeeping` stamps the `housekeeping` category label; `--parent <N>` links the child as a GitHub-native sub-issue of `<N>` (omit for no link) |
| `item-status.py` | `show <N>` / `close <N>` / `reopen <N>` (actionability guard enforced on close/reopen — refuses issues lacking a valid `feature:` label; `close --reason completed` requires exactly one of `--commit-sha` or `--findings-comment-url`, `close --reason not-planned` requires `--reason-text`) |
| `list-items.py` | List with `--type`, `--feature`, `--status` filters; deterministic sort |
| `_gh.py` | Shared helper — repo slug discovery, `gh` invocation wrappers |
