---
name: rabbit-feature-scaffold
description: Scaffold a new rabbit feature directory with the standard skeleton (feature.json, docs/spec.md, docs/contract.md, test/run.py) in standalone mode, or scaffold a per-project plugin feature under .rabbit/rabbit-project/features/<name>/ with path-glob mapping in plugin mode. Adds N features in one shot via a batch input (a JSON file path, or an inline ';'-separated list) in plugin mode. Use when the user asks to create, scaffold, or initialize a new rabbit feature — phrases like "create a new feature", "scaffold a feature called X", "/rabbit-feature-scaffold", "set up a new rabbit feature", "bootstrap a feature dir", "scaffold these features in a batch". Invoke as Skill("rabbit-feature-scaffold", args: "<feature-name>") in standalone mode, Skill("rabbit-feature-scaffold", args: "<feature-name> <path-glob> [<path-glob>...]") in plugin single mode, or Skill("rabbit-feature-scaffold", args: "--batch <features.json>") / Skill("rabbit-feature-scaffold", args: "--list \"<name> [glob ...]; ...\"") in plugin batch mode.
version: 1.10.0
owner: rabbit-workflow team
deprecation_criterion: When this skill's scaffolding step is absorbed into a native `rabbit-feature` CLI subcommand or into the rabbit CLI itself.
---

# rabbit-feature-scaffold — Feature Scaffolding Skill

Your job: create a new rabbit feature directory that conforms to the contract feature's structure rules, then verify it.

## Modes

This skill has two invocation modes. The mode is auto-detected from
`<repo>/.rabbit/.runtime/mode` (written at SessionStart by rabbit-meta's
`write_mode_marker`):

- **Standalone mode** (default; marker absent or contains `standalone`).
  Scaffolds a conforming rabbit-self feature directory under
  `.claude/features/<feature-name>/`.
- **Plugin mode** (marker contains `plugin`). Scaffolds a per-project
  feature under `<repo>/.rabbit/rabbit-project/features/<feature-name>/`
  and registers it in
  `<repo>/.rabbit/rabbit-project/project-map.json` with a list of
  user-code path globs.

Each mode adds a single feature OR a batch of features. Both the single
and the batch surface are the skill's declared interface — callers
(including `rabbit-decompose`) invoke this skill rather than shelling out
to the scaffolder directly. The batch surface is plugin-mode only and is
described in the "Batch mode" subsections below.

## Inputs

**Standalone mode** — Args format: `<feature-name>`

- **feature-name**: lowercase kebab-case identifier (e.g., `rabbit-foo`,
  `my-tool`). The underlying scaffolder enforces naming rules and rejects
  invalid names.

**Plugin mode** — Args format: `<feature-name> [<path-glob>...]`

- **feature-name**: same naming rules as standalone.
- **path-glob**: zero or more shell-style glob patterns, relative to the
  user-project root (the directory containing `.rabbit/`). Globs are
  OPTIONAL — a bare `<feature-name>` scaffolds a greenfield feature that
  owns no existing paths yet (symmetric with standalone mode); such
  a feature is scaffolded without a `project-map.json` glob registration.
  Recursive globs (`**/*.py`) are supported. When one or more globs ARE
  supplied, the scaffolder validates each glob against three rules and
  aborts on any failure:
  - Boundary: the glob's literal anchor must resolve under the
    user-project root (no `../../etc/**`).
  - Non-empty match: the union of matches must include at least one
    filesystem path (typo guard).
  - No overlap: no existing feature in `project-map.json` may already
    claim any matched path; on conflict the error names the incumbent.

**Plugin batch mode** — Args format: `--batch <features.json>` OR
`--list "<name> [glob ...]; <name> [glob ...]; ..."`

- **`--batch <features.json>`**: a path to a JSON array file. Each entry is
  an object `{"name": "<feature-name>", "globs": ["<glob>", ...]}`; `globs`
  is OPTIONAL (empty/absent scaffolds a greenfield feature). The whole array
  is scaffolded in one shot, with a single `project-map.json` mutation.
- **`--list "<entries>"`**: an inline, `;`-separated batch. Each entry is a
  whitespace-separated `<name> [glob ...]`. The companion script normalizes
  this into the same JSON-array shape before delegating.

Per-entry naming, glob-validation, overlap, and project-map schema rules are
identical to single plugin mode. The batch is pre-validated then scaffolded
sequentially; a late filesystem failure leaves earlier entries committed
(no transactional rollback).

## Protocol

The skill is a thin wrapper around an existing scaffolder. Follow these
steps in order; do not skip the validation step in standalone mode.

### Step 1 — Scaffold the directory

**Standalone mode.** Shell out to the rabbit-feature scaffolder, pointing
it at the rabbit features root and passing the requested feature name:

<!-- example: invocation synopsis of scaffold-feature.py (standalone form) -->
```bash
python3 .claude/features/rabbit-feature/scripts/scaffold-feature.py \
  .claude/features <feature-name>
```

The script creates `.claude/features/<feature-name>/` with the standard
skeleton: `feature.json`, `docs/spec.md`, `docs/contract.md`,
`test/run.py`. It exits non-zero on invalid names or if the target already
exists — surface that error to the caller and stop.

**Plugin mode.** Shell out to the same scaffolder, but use the plugin
form (the script detects plugin mode from `.rabbit/.runtime/mode` itself,
so no flag is needed):

<!-- example: invocation synopsis of scaffold-feature.py (plugin form) -->
```bash
python3 .claude/features/rabbit-feature/scripts/scaffold-feature.py \
  <feature-name> [<path-glob>...]
```

On success the scaffolder creates
`<repo>/.rabbit/rabbit-project/features/<feature-name>/` with
`feature.json`, `docs/spec.md`, and `docs/contract.md`. When one or more
globs are supplied, it also registers the new feature in
`<repo>/.rabbit/rabbit-project/project-map.json` (schema-validated
against `.claude/features/contract/schemas/project-map.json.schema.json`
before write); a greenfield (globless) feature owns no paths yet and is
NOT registered in `project-map.json`. On glob-validation failure
(boundary, empty-match, or overlap) it exits non-zero — surface that
error to the caller and stop.

After a successful plugin-mode scaffold, the script prints a `NEXT:`
block to stdout naming rabbit-spec's input assembler
`dispatch-spec-creator.py` and the `rabbit-spec-creator` subagent the
caller should dispatch directly to seed `docs/spec.md`. Capture that
block and follow it (see Step 3 below); the scaffolder itself never
dispatches the rabbit-spec-creator subagent.

**Plugin batch mode.** To add several features in one shot, invoke the
companion `scaffold-batch.py` script (the declared skill-level batch
interface). It validates and normalizes the input, then delegates to
`scaffold-feature.py --batch` so a single `project-map.json` mutation
covers the whole set. Do NOT shell out to `scaffold-feature.py --batch`
directly — the companion script is the boundary callers (including
`rabbit-decompose`) use:

<!-- example: invocation synopsis of the scaffold-batch.py companion (batch forms) -->
```bash
# JSON-file form
python3 .claude/features/rabbit-feature/skills/rabbit-feature-scaffold/scripts/scaffold-batch.py \
  --batch <features.json>

# Inline-list form (';'-separated "<name> [glob ...]" entries)
python3 .claude/features/rabbit-feature/skills/rabbit-feature-scaffold/scripts/scaffold-batch.py \
  --list "<name> [glob ...]; <name> [glob ...]"
```

The companion script also handles single mode (`<name> [glob ...]`), so a
caller that does not know ahead of time whether it is scaffolding one or N
features can route every invocation through it. On any per-entry validation
failure it exits non-zero (mirroring the scaffolder's codes) — surface that
error to the caller and stop.

### Step 2 — Validate the scaffold

**Standalone mode only.** After the scaffolder succeeds, confirm the new
directory conforms by calling the contract feature's CLI shim around
`validate_feature`:

<!-- example: invocation synopsis of the validate-feature.py CLI shim -->
```bash
python3 .claude/features/contract/scripts/validate-feature.py .claude/features/<feature-name>
```

The shim exits 0 on pass, 1 on validation error, and 2 on bad invocation; it
prints the per-check messages to stdout on pass or stderr on fail. If
validation fails, do not silently continue; surface the failure so the caller
can decide whether to delete the partial scaffold or fix it in place.

**Plugin mode.** Skip this step — plugin-mode features live under
`.rabbit/rabbit-project/` and do not participate in the rabbit-self
audit surface. Glob-validation and project-map schema-validation already
ran inside the scaffolder.

### Step 3 — Dispatch rabbit-spec-creator (plugin mode only)

In plugin mode, the scaffolder's stdout ends with a `NEXT:` block of the
form:

```text
NEXT: seed docs/spec.md by dispatching the rabbit-spec-creator
subagent directly. Assemble its prompt with rabbit-spec's input
assembler (it prints the assembled prompt-file path to stdout):
  python3 .claude/features/rabbit-spec/scripts/dispatch-spec-creator.py \
    --feature-name <feature-name> \
    --paths '<glob1>,<glob2>,...'
then dispatch the subagent with the assembled prompt:
  Agent(subagent_type: "rabbit-spec-creator", prompt: <contents of the assembled prompt file>)
```

Follow it: run `dispatch-spec-creator.py` to print the assembled
prompt-file path, then dispatch the `rabbit-spec-creator` subagent
DIRECTLY with the prompt's contents. The subagent reads the matched code
and WRITES `docs/spec.md` itself, returning a `{path_written, summary}`
handoff. The user flow is therefore two steps: (1) invoke this skill to
scaffold; (2) dispatch `rabbit-spec-creator` to draft the initial spec
body.

In standalone mode this step does not apply — skip to Step 4. (Standalone
features can still dispatch `rabbit-spec-creator` if a seeded draft is
desired, but the scaffolder does not print the `NEXT:` block in that
mode.)

### Step 4 — Report the result

Report the absolute path of the new feature directory and a brief summary of
what was created. In plugin mode, also surface the path of the updated
`project-map.json` and the spec-creator dispatch command from the
`NEXT:` block. Keep the report short — the caller already knows the request;
they just need confirmation and a path to navigate to.

## What You Do NOT Do

- Do not edit the generated skeleton files. The scaffolder is the source of
  truth for what a fresh feature looks like; if you want to change it, change
  the scaffolder, not the post-scaffold output.
- Do not invoke other skills or dispatch subagents. This skill is a small
  wrapper; it has no need to call out to the `rabbit-spec-creator` subagent,
  `rabbit-feature-touch`, or anything else. The caller decides what comes next.
- Do not skip the `validate_feature` check. A scaffold that doesn't pass
  contract validation is a latent bug — fail loudly instead.

## Notes

- The post-scaffold check uses `contract.lib.checks.validate_feature` (via
  the `validate-feature.py` CLI shim) so this skill stays in sync with
  whatever conformance rules `contract` owns — no rules are duplicated here.
