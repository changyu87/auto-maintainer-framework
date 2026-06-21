---
name: rabbit-housekeep
description: Run measured verify-or-flag housekeeping against a target — a single feature, a set of features, or the whole project — in complexity-sized waves. The wave targets the CONSUMING PROJECT's declared features, not rabbit's own framework. Each wave proves-it-dead-or-flags every claim, measures before/after line counts, mandates ACTUAL removal (not rewording), and preserves named load-bearing tokens. Also enforces the spec-rules §4 Script-Backed Orchestration standard as a script-tier verify-or-flag dimension: it scans SKILL/agent/command bodies for non-script-backed orchestration steps and flags each. Cross-feature or project-wide scope is decomposed into per-feature sub-issues, each worked through the governed TDD path. Use when the user wants to slim/clean/reduce a feature's docs or their project, remove dead prose, scrub historical burden, check that orchestration is script-backed, or run a housekeeping pass. Phrases like "housekeep this feature", "slim the specs", "run a reduction wave", "clean up dead prose", "check script-backed orchestration", "/rabbit-housekeep". Do NOT use to author new behavior (that's rabbit-feature-touch) or to propose a feature decomposition for a greenfield project (that's rabbit-decompose).
version: 0.5.1
owner: rabbit-workflow team
deprecation_criterion: when housekeeping is provided natively by the rabbit CLI as a first-class measured-reduction subcommand
---

# rabbit-housekeep — Measured Verify-or-Flag Housekeeping in Waves

Your job: turn a housekeeping intent — "slim this feature", "scrub dead prose
repo-wide", "reduce the specs" — into a MEASURED reduction with zero behavior
loss. Rewording without removing is a failure; deleting a load-bearing token
is a regression. This skill enforces both guards: it measures the reduction
with a deterministic script and asserts named load-bearing tokens survive.

## The governing policy (embedded verbatim)

Every claim in scope is resolved by the prove-it-dead-or-flag protocol from
`coding-rules.md` §6, embedded here VERBATIM per the SKILL.md authoring
standard's Verbatim Policy Embedding rule; canonical source
`@.claude/features/policy/coding-rules.md`. Do not paraphrase it — apply it.

<!-- BEGIN VERBATIM coding-rules.md §6 -->
## 6. Cleanup: Prove It Dead or Flag It

**A cleanup pass removes dead-but-plausible content, not just syntactically
tagged historical burden — and never silently keeps the uncertain.**

A cleanup pass is done only when every claim in scope has been resolved by a
deterministic VERIFICATION check, not by judgment. For each claim, run the
matching check:

- **path reference** → `find` it across the repo; zero matches = dead.
- **function / flag / script / symbol** → `grep` for callers/usages; none =
  dead.
- **described behavior** → a reachable code path and/or a test exercising it;
  neither = dead.
- **cross-feature claim** → inspect the other feature directly.

Apply the action table to each result:

| Verification result            | Action                                      |
| ------------------------------ | ------------------------------------------- |
| **Proven dead** (check empty)  | DELETE with confidence.                     |
| **Proven live** (check finds it)| KEEP.                                       |
| **Unverifiable** (no cheap check)| FLAG: file a `housekeeping`-tagged sub-issue naming the file, the sentence, and why it could not be verified. |

**Annotate-and-continue.** An unverifiable sentence is flagged as a separate
sub-issue and the pass CONTINUES. One uncertain sentence never stalls a
feature's cleanup.
<!-- END VERBATIM coding-rules.md §6 -->

## The script-backed-orchestration standard (embedded verbatim)

Housekeeping also enforces the spec-rules §4 **Script-Backed Orchestration**
standard, embedded here VERBATIM per the SKILL.md authoring standard's Verbatim
Policy Embedding rule; canonical source `@.claude/features/policy/spec-rules.md`.
Do not paraphrase it — apply it.

<!-- BEGIN VERBATIM spec-rules.md §4 Script-Backed Orchestration -->
- **Script-Backed Orchestration** (derives from §1 Tool-Choice Tier). An
  orchestration step that involves a computed value or mode-aware
  branching MUST live in a companion script under `scripts/`; the SKILL.md
  invokes the script and the script owns the logic. SKILL.md bodies MUST
  NOT carry bash blocks with runtime placeholders (e.g. `<feature-name>`,
  `<branch-name>`) that the model assembles at invocation time — that is
  prompt-tier, not script-tier. Exception: read-only informational
  commands (e.g. `git log --oneline -5`) are acceptable inline.
<!-- END VERBATIM spec-rules.md §4 Script-Backed Orchestration -->

## Scope: the consuming project, not rabbit's self-repo

This skill is user-facing (invocable as `/rabbit-housekeep`). A wave operates on
the CONSUMING PROJECT's declared features — the project the user is building
with rabbit — NEVER on rabbit-workflow's own framework features. In a vendored
install that is `rabbit-project/features/*`; rabbit's own `.claude/features/*`
are EXCLUDED. In a standalone install the project IS the repo
(`.claude/features/*`).

Resolve the in-scope feature set deterministically with the mode-aware
companion script — do not enumerate features by hand:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/resolve-housekeep-scope.py \
  list
```

## Inputs

Args format: `<target>`

The target is one of:
- a single project feature name (`user-auth`) — a one-wave tidy;
- a set of project feature names (`user-auth billing`) — one wave per feature;
- a project-wide directive (`--repo` or `all`) — many waves, decomposed;
- omitted — every consuming-project feature
  (`resolve-housekeep-scope.py list`).

When the target is unclear, ask the user one focused question. Do not guess
the scope.

## Protocol

### Step 1 — Size the work into waves

Assess the target's complexity and choose a wave plan:

- **Single-feature tidy** → ONE wave. Run Steps 3-7 inline against that
  feature.
- **Multi-feature or repo-wide** → MANY waves. Go to Step 2 (decompose),
  then each per-feature unit is its own wave executed through the governed
  TDD path.

A wave is the unit of measured reduction: one feature, measured before and
after.

### Step 2 — Decompose cross-feature / repo-wide scope

Reuse the existing decomposition machinery — do NOT reinvent it. For a
repo-wide mandate:

1. Enumerate the in-scope features (the decomposition shape rabbit-decompose
   uses: one bounded per-feature unit each).
2. File one `housekeeping`-tagged per-feature sub-issue per feature via the
   rabbit-issue filing script (contract INVOKE — do not edit rabbit-issue
   files):
   ```bash
   python3 .claude/features/rabbit-issue/scripts/file-item.py \
     --type enhancement --feature <name> --priority medium \
     --title "housekeep <name>: measured reduction wave" \
     --description "<scope>" --filed-by rabbit
   ```
3. Recording the parent→children linkage so the parent closes itself when
   every child closes is LOOP-ONLY machinery owned by rabbit-auto-evolve, NOT
   a user step. When the auto-evolve loop drives this it records the linkage via
   its own `record-decomposition.py`, and the per-tick drain runs
   `close-decomposed-parents.py` to close the parent — the machine's job. That
   loop feature is deliberately ABSENT from the vendored install, so this SKILL
   surface carries NO live invocation of the loop-only script; the reuse is
   declared in `docs/contract.md` (`invokes.scripts`). In a plain
   `/rabbit-housekeep` run there is no loop — the user closes the issues.

### Step 3 — Measure BEFORE

Snapshot the per-artifact line counts of the target with the measurement
script (measurement is script-tier — deterministic, not judgment). The
`<name>` slot is the target feature name:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  count .claude/features/<name> > /tmp/housekeep-<name>-before.json
```

### Step 4 — Verify-or-flag every claim, then REMOVE

Walk the target's doc surfaces (`docs/spec.md`, `docs/contract.md`, each
`skills/*/SKILL.md`). For EACH claim, run the matching deterministic check
from the embedded §6 protocol above:

- proven dead → DELETE;
- proven live → KEEP (preserve schemas, decision tables, exit codes, script
  names, and cross-references verbatim);
- unverifiable → FLAG a `housekeeping`-tagged sub-issue (Step 2 filing
  shape) and CONTINUE — one uncertain sentence never stalls the wave.

Slim under coding-rules §2 (Simplicity First) and §7 (Parenthetical Clarity):
drop redundant sentences, restated rationale, and decorative parentheticals;
fold load-bearing parentheticals into clauses. History belongs in
`docs/CHANGELOG.md`, never in the doc surfaces.

### Step 5 — Scan for non-script-backed orchestration, then verify-or-flag

This is a SECOND verify-or-flag DIMENSION on the same target, enforcing the
spec-rules §4 Script-Backed Orchestration standard embedded verbatim above.
Detection is SCRIPT-tier (the check enforces the same tier it embodies). The
`<name>` slot is the target feature name:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/check-script-backed.py \
  scan .claude/features/<name>
```

The script scans the feature's `skills/*/SKILL.md`, `agents/*.md`, and
`commands/*.md` bodies and emits JSON `{"findings": [...], "count": N}`. Each
finding names the `file`, `line`, `reason` (`runtime-placeholder`,
`computed-value`, or `mode-aware-branching`), and `snippet`. Read-only
informational commands inline (e.g. `git log --oneline -5`) and trivial
one-liners are NOT flagged (the §4 read-only-informational exception).

For EACH finding, apply the prove-it-dead-or-flag disposition:

- FLAG it as a `housekeeping`-tagged sub-issue (the Step 2 filing shape)
  naming the file, the offending step, and the conversion target — move the
  logic into `scripts/`; the SKILL/command invokes it.
- A straightforward conversion MAY be done inline within this governed touch
  (move the computed/branching logic into a companion script and invoke it).
- Do NOT silently rewrite complex orchestration — FLAG it and CONTINUE.

### Step 6 — Execute the per-feature unit through the governed TDD path

Each per-feature reduction is a real edit through the governed TDD cycle, not
an ad-hoc edit. Invoke the feature-touch path so the change is test-driven:

```
Skill("rabbit-feature-touch", args: "<name> housekeep: measured reduction wave")
```

The housekeeping test the TDD subagent authors MUST assert BOTH:
- **measured reduction** — `measure-reduction.py diff before.json after.json`
  reports `reduced: true` (negative total delta); a reword FAILS here; and
- **load-bearing survival** — the named load-bearing tokens (script names,
  schema fields, key cross-references) are still present; deleting one FAILS
  here.

### Step 7 — Measure AFTER and report

Snapshot again and diff. The `<name>` slot is the target feature name:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  count .claude/features/<name> > /tmp/housekeep-<name>-after.json
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  diff /tmp/housekeep-<name>-before.json /tmp/housekeep-<name>-after.json
```

Report to the user: total lines removed (the `total_delta`), per-artifact
breakdown, any `housekeeping`-tagged sub-issues filed for unverifiable items,
and confirmation that load-bearing tokens survived (zero behavior loss).

## Nesting constraint — do NOT invoke this skill inside an Agent() call

rabbit-housekeep is a SUBAGENT-DISPATCHING skill: Step 6 dispatches the TDD
subagent and Step 2 files sub-issues. Per the SKILL.md authoring standard's
"No Subagent-Dispatching Skill Inside Agent()" rule, it MUST NOT itself be
invoked inside an `Agent(...)` call — that creates illegal two-level subagent
nesting (main → Agent level-1 → TDD subagent level-2), which Claude Code does
not support. To parallelize per-feature waves, dispatch the underlying TDD
subagent directly at level-1 (main → N parallel subagents), reusing this
skill's measurement script and decomposition shape. The skill wrapper exists
for a single, main-session invocation.

## What you do NOT do

- Reword to manufacture a diff. A reduction wave REMOVES; the
  `measure-reduction.py diff` verdict (`reduced: true`) is the gate.
- Delete a load-bearing token to inflate the line delta. Schemas, decision
  tables, exit codes, script names, and cross-references are KEPT verbatim.
- Silently keep an unverifiable claim. FLAG it as a `housekeeping`-tagged
  sub-issue and continue.
- Edit files outside the target feature's directory. Cross-feature scope is
  DECOMPOSED into per-feature units, each scoped to its own feature.
- Wrap this skill in an `Agent(...)` call (see the nesting constraint above).
