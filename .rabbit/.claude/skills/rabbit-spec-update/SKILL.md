---
name: rabbit-spec-update
description: Use when an existing feature spec needs to be revised or updated, in any context (standalone or plugin mode). Invoke as Skill("rabbit-spec-update", args: "<feature-name> <request>") from any skill, process, or directly. Auto-detects rabbit mode from .rabbit/.runtime/mode and resolves the target feature directory to .claude/features/<feature-name>/ in standalone mode or .rabbit/rabbit-project/features/<feature-name>/ in plugin mode. Reads the current spec, judges the request type, invokes superpowers as needed, updates the spec surgically, and produces an implementation suggestion file for whoever invoked it. Also use when a user asks to update, review, or revise a spec for any rabbit feature — even if they don't say "spec" explicitly (e.g., "think about what we need to build", "plan this feature", "what should change in the design", "update the design for this bug fix"). For drafting a BRAND NEW spec from scratch (no existing content), use rabbit-spec-create instead.
model: opus
version: 2.9.0
owner: rabbit-workflow team
deprecation_criterion: when Claude Code exposes native spec-lifecycle skills that supersede this feature
---

# rabbit-spec-update — Spec Revision Skill

Your job: understand the request, update the feature spec, and produce an implementation suggestion file. You are a general-purpose spec skill — any process can invoke you. You don't assume who called you or what comes next.

## Modes

This skill has two operating modes. The mode is auto-detected from
`<repo_root>/.rabbit/.runtime/mode` (written at SessionStart by
rabbit-meta's `write_mode_marker`):

- **Standalone mode** (default; marker absent or contains `standalone`).
  The target feature lives under `.claude/features/<feature-name>/`.
- **Vendored mode** (marker contains `vendored` or the legacy `plugin`).
  The target feature lives under
  `.rabbit/rabbit-project/features/<feature-name>/`.

Dual-accept BOTH vendored-marker spellings for the vendored branch. The
canonical value is `vendored`; the older value `plugin` is still honoured
during the coexistence window — this is the same
`_VENDORED_MODES = ("vendored", "plugin")` idiom every contract reader uses.
Treat a marker of `vendored` exactly as you would `plugin`: resolve to the
vendored feature_root below, never let it fall through to the standalone
path. Only when the marker contains `standalone` (or is absent) do you take
the standalone branch. The legacy `plugin` acceptance is dropped only once no
install carries the older marker spelling.

Define `feature_root` as the resolved prefix for the rest of this skill
body — i.e. `.claude/features/<feature-name>/` in standalone mode and
`.rabbit/rabbit-project/features/<feature-name>/` in vendored mode (marker
`vendored` or legacy `plugin`). Every
Read/Edit/Write reference to the target feature's spec.md, contract.md,
feature.json, or implementation files below uses `<feature_root>` as the
prefix. The impl-suggestion path at
`<repo_root>/.rabbit/impl-suggestion-<feature-name>.json` (Step 5) is
mode-agnostic and is NOT prefixed by `<feature_root>`.

### Spec-file layout (canonical flat docs/)

The in-feature spec-file layout is the canonical flat `docs/` layout,
resolved INDEPENDENTLY of the mode prefix above:

- `spec_path`: `<feature_root>/docs/spec.md`.
- `contract_path`: `<feature_root>/docs/contract.md`.
- `feature.json` lives at `<feature_root>/feature.json`.

Every `spec.md` / `contract.md` reference in the steps below means
`<spec_path>` / `<contract_path>` resolved as above.

## Inputs

Args format: `[--intent-only] <feature-name> <request-or-item-description>`

- **feature-name**: the rabbit feature to update (e.g., `tdd-subagent`, `rabbit-issue`)
- **request**: the user's raw request, or a rabbit-managed issue description
- **`--intent-only`** (optional flag): opt into the intent-only / no-commit
  mode. When present, the skill EMITS the spec-reduction intent and stops
  short of applying the spec edit. See the **Intent-Only Mode** section
  below. When absent (the default), behaviour is unchanged: the skill edits
  and writes the spec (Step 4) and writes the impl-suggestion file (Step 5).

## Intent-Only Mode (`--intent-only`)

This is an additive, opt-in mode. It is OFF by default — when the
`--intent-only` flag is absent, the skill behaves exactly as it always
has (edit + write the spec at Step 4, then write the impl-suggestion file
at Step 5). The default path is unchanged and backward-compatible.

When `--intent-only` IS present, the skill COMPUTES the spec-reduction
intent but does NOT apply it:

- It runs Step 1 (Read), Step 2 (Judge), and Step 3 (Superpowers) exactly
  as in the default flow — these are read-only/analysis steps that produce
  the intent.
- It SHORT-CIRCUITS Step 4: it does NOT edit and does NOT write the target
  `docs/spec.md`, and it does NOT commit anything. The on-disk
  `docs/spec.md` stays byte-identical. The actual spec edit is deferred to
  whoever consumes the intent (e.g. a downstream TDD subagent that will
  author the `docs/spec.md` change under its own scope marker).
- Instead of writing the impl-suggestion FILE (Step 5), it EMITS the same
  payload as JSON on stdout. The emitted payload reuses the existing
  impl-suggestion schema VERBATIM (the exact object shape documented in
  Step 5 — same `schema_version`, `feature`, `generated_at`,
  `request_summary`, `spec_changes`, `implementation_approach`,
  `affected_files`, `key_invariants`, and optional `owner` / `deprecation`
  fields). Do NOT invent a new schema; the only difference from the default
  mode is the SINK (stdout JSON, not the `.rabbit/impl-suggestion-*.json`
  file) and the absence of the spec edit/commit.

In short: intent-only = "compute and emit the intent, edit nothing, commit
nothing." Default = "edit the spec, write the impl-suggestion file."

## Step 1 — Read Current State

Before forming any opinion, you MUST Read the target feature's
`<spec_path>` (the feature's canonical flat `docs/spec.md`) via the Read tool
in this session (see the **Modes** section above for how `<feature_root>`,
`<spec_path>`, and `<contract_path>` are resolved).
Reading is mandatory comprehension, not optional context-gathering — it
lets you understand current invariants, numbering, and section structure
before mutating, and it satisfies Claude Code's per-session file-state
guard that rejects Edit tool calls on files not previously Read
in-session. Skipping this Read causes silent `File must be read first`
tool errors at Step 4.

You MAY also read any other file inside the resolved `<feature_root>/`
directory. Examples of what you should typically read include:

1. The feature's contract (if present): `<contract_path>`
2. The feature manifest: `<feature_root>/feature.json`
3. Any existing implementation files under `<feature_root>/scripts/`,
   `<feature_root>/skills/`, `<feature_root>/hooks/`,
   `<feature_root>/commands/`, or `<feature_root>/agents/` — read
   freely; you are not writing to these.

The list above is illustrative, not exhaustive. Read anything in the
feature directory that helps you understand what already exists, so you
don't re-spec implemented behavior.

If the resolved `<feature_root>/` does not exist, abort immediately
with an error message naming the missing feature directory (include the
mode you detected and the resolved path). Do NOT silently create or
scaffold a new feature — that is rabbit-project's job.

## Step 2 — Judge Request Type

Decide whether the request is **open-ended** or **specific**:

| Signal | Classification |
|--------|---------------|
| Vague goal, exploratory language, "what should we do about X", no clear acceptance criteria | Open-ended |
| Bug fix, enhancement task, bounded change with clear outcome, "add invariant N", "rename X to Y" | Specific |

When in doubt, lean toward **specific** — the brainstorming superpower adds cost and time, so only invoke it when the user genuinely needs design exploration.

## Step 3 — Invoke Superpowers

**If open-ended:**
```
Skill("superpowers:brainstorming")
```
Then, once design is settled:
```
Skill("superpowers:writing-plans")
```

**If specific:**
```
Skill("superpowers:writing-plans")
```
Only. No brainstorming needed when the change is already well-defined.

Both superpowers run under your current model context (opus). Do not dispatch a subagent — this skill runs inline.

## Step 4 — Update the Spec

**INTENT-ONLY GUARD:** If the `--intent-only` flag was passed (see the
**Intent-Only Mode** section above), SKIP this step entirely — do NOT edit
or write `docs/spec.md` and do NOT commit. Proceed straight to emitting the
intent JSON on stdout per the Intent-Only Mode section (the Step 5 payload
shape). The rest of this step applies only to the default (edit + write)
mode.

**PRE-CONDITION:** You must have already Read the target
`<spec_path>` (the feature's canonical flat `docs/spec.md`; see
**Modes** above for how `<feature_root>` and `<spec_path>` are resolved)
in Step 1 of this same
session. The Claude Code Edit tool will reject any Edit on a file not
previously Read in-session — this is not optional, it is a harness-enforced
contract. If for any reason you arrive at Step 4 without having Read
the spec.md in this session, Read it now before proceeding to the
Edit/Write call below.

Edit `<spec_path>` (the same canonical flat `docs/spec.md` you resolved and
Read in Step 1) to reflect what the request requires. Be
surgical:
- For specific requests: add/modify only the affected invariants or surface entries
- For open-ended requests: apply the full design outcome from the superpowers above

Do not touch implementation files — your scope is the spec only.

## Step 5 — Write impl-suggestion File

**INTENT-ONLY GUARD:** If the `--intent-only` flag was passed, do NOT write
this file — EMIT the identical payload as JSON on stdout instead (see the
**Intent-Only Mode** section above). The schema below is the same in both
modes; only the sink differs.

Write `.rabbit/impl-suggestion-<feature-name>.json` (create `.rabbit/` if it doesn't exist):

```json
{
  "schema_version": "1.0.0",
  "feature": "<feature-name>",
  "generated_at": "<iso 8601 UTC timestamp, e.g. 2026-05-18T03:42:11Z>",
  "request_summary": "<one sentence: what was asked>",
  "spec_changes": "<one paragraph: what changed in the spec and why>",
  "implementation_approach": "<narrative: how you suggest implementing the spec changes>",
  "affected_files": ["<repo-relative path 1>", "<repo-relative path 2>"],
  "key_invariants": ["<invariant text that directly constrains implementation>"],
  "owner": "<optional: named accountable individual or team>",
  "deprecation": "<optional: end-of-life criterion if the suggestion creates a new artifact>"
}
```

Field notes:

- `generated_at` MUST be ISO 8601 UTC in the form `YYYY-MM-DDTHH:MM:SSZ`.
- `affected_files` lists the repo-relative paths the implementer will
  modify (or create) to satisfy the suggestion. No globs; concrete paths
  only. Files that are only read (not modified) are excluded.
- `owner` and `deprecation` are optional fields per the impl-suggestion
  schema. Include them when the suggestion introduces a new artifact whose
  lifecycle must be tracked.

Make `implementation_approach` genuinely useful — it should save any downstream implementer from having to re-derive the approach from scratch.

## What You Do NOT Do

- Run tests
- Write implementation code
- File bugs or enhancements
- Invoke skills other than the superpowers invoked in Step 3
  (`superpowers:brainstorming` and `superpowers:writing-plans`). Your
  output is the impl-suggestion file; what happens next is the caller's
  concern.
