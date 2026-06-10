---
name: rabbit-decompose
description: Propose a feature decomposition for an existing codebase or a high-level spec, interactively iterate with the user until accepted, then orchestrate scaffolding + initial spec drafting per accepted feature. Use when the user wants to start a new rabbit-managed project from a spec/prompt, or when the user wants to retroactively organize an existing codebase into rabbit features. Phrases like "decompose this into features", "propose a feature breakdown", "let's organize this codebase with rabbit", "/rabbit-decompose", "what features should this project have". Do NOT use to revise individual feature specs (that's rabbit-spec-update) or to scaffold a single feature whose name + globs you already know (that's rabbit-feature-scaffold).
version: 0.16.0
owner: rabbit-workflow team
deprecation_criterion: when Claude Code exposes native feature-decomposition assistance that supersedes this skill
---

# rabbit-decompose — Interactive Feature Decomposer

Your job: turn a high-level intent — a spec, a design doc, or a codebase — into a list of rabbit features the user accepts, then drive the downstream pipeline (scaffold + initial spec draft) per accepted feature.

This skill is **interactive by design**. Do not fire-and-forget. The user reviews the proposal and adjusts it before any scaffolding happens.

## Inputs

Args format: `<scenario-hint-or-source>`

The user may supply:
- A scenario hint (`"greenfield"`, `"existing-codebase"`) and a source (spec text inline, path to a design doc, or path to a codebase root)
- Just a source — infer the scenario from whether the source is a file vs a directory
- Just a scenario — ask the user for the source

When unclear, ask the user one focused question to disambiguate. Do not guess.

## Protocol

### Step 1 — Gather inputs

Confirm the scenario and source material with the user. Two cases:

- **Greenfield**: the source is a spec, design doc, or natural-language description. The decomposition produces a feature list with names + purposes; globs MAY be empty (features will be authored from scratch).
- **Existing codebase**: the source is a directory under the project root. The decomposition produces a feature list with names + purposes + path globs (each feature owns a slice of the existing code).

**Where the source lives — resolve it, do not guess.** When the user does not give an explicit source path (e.g. a no-args existing-codebase run), the decomposition SOURCE ROOT is resolved by the canonical resolver `scripts/handoff-scaffold.py`, the same resolver Step 4 uses — so Step 1 and Step 4 cannot disagree. It detects mode via `rabbit-meta`'s `detect_mode` and returns the source root: in **plugin mode** the source root is the **parent of the `.rabbit` install** (the rabbit-root is the vendored `.rabbit/` tooling dir, so the project to decompose is its parent — never the `.rabbit/` workflow tooling itself); in **standalone mode** it is the repo root. Run the resolver and confirm the resolved `source_root` with the user before pointing Glob/Read at it:

<!-- example -->
```bash
python3 .claude/features/rabbit-decompose/scripts/handoff-scaffold.py --source-root
```

### Detect an existing decomposition (pre-check before Step 2)

Before proposing anything, check whether this project was **already decomposed**. Re-proposing the full feature set on an already-rabbified project is redundant and confusing. The detection is SCRIPT-tier: run the resolver in `--detect-existing` mode. It resolves mode deterministically (reusing `rabbit-meta`'s `detect_mode`) and reads the project's `project-map.json` (plugin: `<.rabbit>/rabbit-project/project-map.json`; standalone: `<repo>/.rabbit/rabbit-project/project-map.json`):

```bash
python3 .claude/features/rabbit-decompose/scripts/handoff-scaffold.py --detect-existing
```

- When the JSON reports `existing: false` (no `project-map.json`, or an empty `features` map), this is a **first run** — proceed to Step 2 unchanged.
- When it reports `existing: true`, the project already has features. Present a SUMMARY of the existing features (the `existing_features` names) and ask the user which path to take — the three-way branch in `options`:
  - **(a) skip** — the user is satisfied with the existing decomposition. Stop; do not re-propose or scaffold anything.
  - **(b) add** — decompose only the NEW work. Re-run the detector with your proposed candidate list (`--detect-existing --features <candidates.json>`); it classifies candidates into `already_rabbified` vs `new`. Propose and scaffold ONLY the `new` (unrabbified) features, leaving the existing ones untouched.
  - **(c) re-decompose** — the user wants a full re-decomposition. Proceed to Step 2 as if first-run, proposing the complete feature set.

Do not choose for the user. Present the summary and the three options, then wait for an explicit choice before continuing.

**Orphan feature dirs — a partial/aborted decompose leaves inconsistency.** `--detect-existing` also SCANS the on-disk `features/` root (the sibling directory next to `project-map.json`) and surfaces, via the report fields `feature_dirs_on_disk` and `orphan_feature_dirs`, feature directories that exist on disk but are NOT represented in `project-map.json` — including the case where `project-map.json` is entirely absent while dirs exist. This is the inconsistent state a partial or aborted prior decompose leaves behind; left unsurfaced, a later `--features` scaffold run fails ("scaffold target .../features/<name> already exists") with no recovery path. When `orphan_feature_dirs` is non-empty, surface this inconsistency to the user and let them decide whether to ADOPT the existing dirs or PROCEED with only the remaining unscaffolded features. The detector only reports the facts — it does NOT auto-delete or auto-adopt the dirs; the adopt-vs-proceed decision is the user's.

### Step 2 — Analyze and propose

Read the source material (Read/Grep/Glob for codebase scenarios; just read for spec scenarios). Produce a proposed feature list as a structured Markdown table:

```
| name | purpose | globs |
|------|---------|-------|
| feature-one | one-line purpose | `src/one/**/*` |
| feature-two | one-line purpose | `src/two/**/*` |
| ...
```

Show this table to the user. Below the table, briefly explain the boundaries you chose (1-2 sentences per feature for non-obvious cases).

**Guidelines for the proposal:**

- Each feature should be **functionally independent yet of manageable logical complexity** — small enough for a single TDD cycle to reason about, large enough to be a real boundary not a single file.
- Names are lowercase kebab-case, descriptive of the *what*, not the *how*.
- In existing-codebase scenarios, no two features may overlap (their globs must not match the same paths). The scaffolder will reject overlaps; pre-empt that here.
- Aim for 3-12 features as a starting point. Fewer than 3 probably doesn't need rabbit; more than 12 probably indicates over-decomposition.

### Step 3 — Iterate with the user

The user will respond with edits: add features, remove features, merge two into one, split one in two, adjust boundaries, rename. Update the proposal table and re-present.

Loop until the user explicitly accepts ("looks good", "go ahead", "ship it"). Do not proceed to scaffolding without explicit approval.

### Hand off to scaffold + spec drafting

Once the user accepts, run Steps 4–8:

### Step 4 — Open the decompose-context pass-through

The batch scaffold + spec-seed work writes across SEVERAL feature directories at once. Instead of the undiscoverable manual session override, open a bounded, auto-cleared scope-guard pass-through that authorizes exactly the accepted features' directories. Write the accepted list to a JSON file — `[{"name": "X", "globs": [...]}, ...]` — then SET the marker BEFORE any batch work:

<!-- example -->
```bash
python3 .claude/features/rabbit-decompose/scripts/handoff-scaffold.py --decompose-context set --features <accepted.json>
```

This writes `.rabbit/.runtime/decompose-active` recording the accepted feature NAMES; while present, scope-guard ALLOWS writes inside any of those feature directories without a per-feature scope marker. You MUST CLEAR it after ALL batch work (Step 7 below) — on success OR failure — so it never lingers.

### Step 5 — Scaffold

Run the hand-off orchestrator. It resolves the rabbit root, detects mode deterministically (reusing `rabbit-meta`'s `detect_mode`), authors the batch temp file with a script-owned timestamp, and dispatches the scaffolder on the mode-correct branch (its own batch dispatch also re-sets the marker before and clears it after, so the deterministic scaffold step is self-guarded):

<!-- example -->
```bash
python3 .claude/features/rabbit-decompose/scripts/handoff-scaffold.py --features <accepted.json>
```

The script owns the mode branch: in **plugin mode** it runs the scaffolder in batch form through the `rabbit-feature-scaffold` skill's batch interface (`scaffold-batch.py --batch <file>`, one `project-map.json` mutation) — the declared cross-feature interface, not a direct shell-out to rabbit-feature's `scaffold-feature.py` implementation detail; in **standalone mode** the batch form is plugin-only, so the script emits a `per-feature` plan. When the script reports `branch: "per-feature"`, invoke the scaffolder once per accepted feature, sequentially (they're cheap):

```
Skill("rabbit-feature-scaffold", args: the feature name)
```

### Step 6 — Seed specs

Seed each accepted feature's initial `docs/spec.md` by dispatching the `rabbit-spec-creator` subagent directly. The subagent reads the matched code and WRITES the spec itself, returning only a `{path_written, summary}` handoff. The prompt is script-assembled by `rabbit-spec`'s input assembler `dispatch-spec-creator.py`, which resolves the path globs and prints the absolute path of the assembled prompt file to stdout.

For each accepted feature **with non-empty globs**, assemble the prompt by passing the feature name and its comma-separated globs:

<!-- example -->
```bash
python3 .claude/features/rabbit-spec/scripts/dispatch-spec-creator.py --feature-name <name> --paths <glob1>,<glob2>,...
```

For a **greenfield feature with no globs**, omit `--paths` so the subagent produces a skeleton:

<!-- example -->
```bash
python3 .claude/features/rabbit-spec/scripts/dispatch-spec-creator.py --feature-name <name>
```

Then dispatch the subagent directly, passing the contents of the assembled prompt file:
```
Agent(subagent_type: "rabbit-spec-creator", prompt: <contents of the assembled prompt file>)
```

Because `rabbit-spec-creator` is dispatched DIRECTLY at level-1 (decompose is a main-session orchestration, with no intermediate subagent-dispatching skill), the N per-feature spec-creator dispatches may run in **parallel** — fire all the `Agent(...)` calls together and collect their `{path_written, summary}` handoffs.

### Step 7 — Close the decompose-context pass-through

After ALL batch work (scaffold + every spec-seed dispatch) completes — whether every step succeeded or any failed — CLEAR the marker so it never lingers:

<!-- example -->
```bash
python3 .claude/features/rabbit-decompose/scripts/handoff-scaffold.py --decompose-context clear
```

The clear is idempotent (clearing an absent marker is a no-op), so run it unconditionally as the final batch step even if an earlier step errored.

### Step 8 — Report

Tell the user: `N` features scaffolded; `M` spec drafts produced; paths to each. Note that the spec drafts are *starting points* — the user reviews and edits before they're final.

**Commit the scaffold before running rabbit-feature-touch (vendored/plugin mode).** The Step 5 plan JSON carries a mode-aware `vendored_commit_warning` field: non-null in **vendored/plugin mode**, null in standalone mode. When it is non-null, surface it verbatim to the user. The scaffolded `.rabbit/rabbit-project/features/<name>/` dirs and seeded specs are NOT committed by decompose, but `rabbit-feature-touch`'s create-branch runs the TDD cycle inside a git **worktree** branched from the host repo's HEAD, and a worktree only contains committed files. So in vendored/plugin mode the user MUST commit the scaffold to the user repo (e.g. a PR to main) BEFORE running `rabbit-feature-touch` on a freshly-decomposed feature — otherwise the worktree does not contain the new feature directory and the TDD subagent has nothing to implement. Standalone mode has no HEAD-based worktree, so the warning does not apply there.

## What you do NOT do

- Modify the user's source code. rabbit-decompose proposes; the user reviews; scaffolding happens; spec drafting happens. None of that touches source code.
- Skip the user-approval loop. Even if the proposal looks obvious, present it and wait for acceptance.
- Invoke rabbit-spec-update for the seeded drafts — those drafts are by definition new, not revisions. The user edits them directly when they review.
- Decide for the user whether a project should use rabbit at all. If the user is asking for a decomposition, assume they want one.

## Why interactive

The hard problem in feature decomposition isn't the analysis — it's the *boundary judgment*. Where one developer sees one feature, another sees three. The user's preferences and the codebase's history are inputs only the user has. The skill's job is to produce a reasonable starting proposal and then yield to the user's judgment, not to insist on its first answer.
