---
name: rabbit-housekeep
description: Run measured verify-or-flag housekeeping against a target — a single feature, a set of features, or the whole project — in complexity-sized waves. The wave targets the CONSUMING PROJECT's declared features, not rabbit's own framework. Each wave proves-it-dead-or-flags every claim and reports an HONEST measured outcome: it removes dead/redundant/simplifiable content when present, else honestly reports a no-op / already-clean verdict (an already-lean target is a SUCCESS, never forced into a reword). The ONE mandatory gate is behavior preserved (the feature's existing test suite stays green). The default DOC dimension slims doc surfaces; an OPT-IN CODE dimension (--code) simplifies the feature's src/ via the code-simplifier agent (simplify-first) and removes dead src/ symbols via the coding-rules §6 grep-for-callers protocol, routed through the governed TDD path. Also enforces the spec-rules §4 Script-Backed Orchestration standard as a script-tier verify-or-flag dimension: it scans SKILL/agent/command bodies for non-script-backed orchestration steps and flags each. Cross-feature or project-wide scope is decomposed into per-feature sub-issues, each worked through the governed TDD path. Use when the user wants to slim/clean/reduce a feature's docs or their project, simplify a feature's code, remove dead prose or dead code, scrub historical burden, check that orchestration is script-backed, or run a housekeeping pass. Phrases like "housekeep this feature", "slim the specs", "simplify this feature's code", "run a reduction wave", "clean up dead prose", "remove dead code", "check script-backed orchestration", "/rabbit-housekeep". Do NOT use to author new behavior (that's rabbit-feature-touch) or to propose a feature decomposition for a greenfield project (that's rabbit-decompose).
version: 0.9.0
owner: rabbit-workflow team
deprecation_criterion: when housekeeping is provided natively by the rabbit CLI as a first-class measured-reduction subcommand
---

# rabbit-housekeep — Measured Verify-or-Flag Housekeeping in Waves

Your job: turn a housekeeping intent — "slim this feature", "scrub dead prose
repo-wide", "reduce the specs", "simplify this feature's code" — into an HONEST
cleanup with zero behavior loss. The cleanup runs in two dimensions: the DOC
dimension (default) and an OPT-IN CODE dimension (the `--code` selector,
below). In both, reduction is REPORTED, never MANDATED.

The ONE MANDATORY gate of a wave is BEHAVIOR PRESERVED: the feature's existing
test suite stays green — zero behavior loss. Reduction is the honest measured
OUTCOME, not the gate: when there is dead, redundant, or simplifiable content
the wave removes it (`reduced: true`); when the target is already lean and
nothing is dead, the wave honestly reports a `no-op` / already-clean verdict
and that is a SUCCESS, not a failure. Never reword to manufacture a diff and
never delete a load-bearing token to inflate the line delta — both fail the
behavior-preserved gate. The deterministic `measure-reduction.py diff` reports
the verdict (`reduced` or `no-op`); the test asserts behavior is preserved and
named load-bearing tokens survive.

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

Args format: `<target> [--code]`

The target is one of:
- a single project feature name (`user-auth`) — a one-wave tidy;
- a set of project feature names (`user-auth billing`) — one wave per feature;
- a project-wide directive (`--repo` or `all`) — many waves, decomposed;
- omitted — every consuming-project feature
  (`resolve-housekeep-scope.py list`).

The DIMENSION selector is OPT-IN and defaults to the DOC dimension:
- default (no `--code`) → the DOC dimension: Steps 3-7 operate on the doc
  surfaces (`docs/spec.md`, `docs/contract.md`, `skills/*/SKILL.md`) measured
  with `count --docs-only`;
- `--code` → the OPT-IN CODE dimension (below): the wave operates on the
  feature's `src/` source, measured with `count --code`.

When the target or dimension is unclear, ask the user one focused question. Do
not guess the scope.

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

Snapshot the per-artifact line counts of the target's DOC SURFACES with the
measurement script (measurement is script-tier — deterministic, not judgment).
`--docs-only` scopes the count to the surfaces a wave slims (`docs/spec.md`,
`docs/contract.md`, `skills/*/SKILL.md`) and excludes `test/` and
`docs/CHANGELOG.md`, so the mandated housekeeping test the wave adds (Step 6)
does not flip the Step-7 `reduced` verdict. The `<name>` slot is the target
feature name:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  count --docs-only .claude/features/<name> > /tmp/housekeep-<name>-before.json
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
`docs/CHANGELOG.md`, never in the doc surfaces. Remove IF there is dead /
redundant / simplifiable content; if the surfaces are already lean and nothing
is dead, that is an honest `no-op` outcome — do NOT reword to force a diff.

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

The `housekeep: measured reduction wave` request is the signal rabbit-feature-
touch's `is-reduction-wave` detection keys on. On that path the whole reduction
rides ONE governed RED->GREEN cycle: feature-touch runs rabbit-spec-update in
its `--intent-only` no-commit mode, threads the spec-reduction intent into the
dispatch, and the TDD subagent authors BOTH the spec reduction AND its gating
test inside that single cycle. The dispatcher does NOT pre-edit or pre-commit
`docs/spec.md` outside the cycle, so there is no forced no-spec-change escape
hatch — the real spec edit is the subagent's working-tree diff that satisfies
the `spec-update -> test-red` gate.

The housekeeping test the TDD subagent authors MUST assert:
- **behavior preserved (the ONE MANDATORY gate)** — the feature's existing
  test suite stays green; zero behavior loss. This is the only gate that can
  FAIL a wave. For the code dimension it is the direct analogue of the doc
  dimension's load-bearing-survival gate.
- **load-bearing survival** — the named load-bearing tokens (script names,
  schema fields, key cross-references) are still present; deleting one FAILS
  here.

The test REPORTS the measured reduction; it does NOT mandate it. Run
`measure-reduction.py diff before.json after.json` over the `--docs-only`
(doc dimension) or `--code` (code dimension) snapshots and record the
`verdict`: `reduced` when content was removed, `no-op` when the target was
already clean. A `no-op` is a valid honest outcome and MUST pass — do not fail
an already-clean feature or force it into a reword. Scope the test's own
baseline to the same surfaces as Step 7 so the test and the Step-7 operator
command agree; the test the subagent adds under `test/` is wave overhead, never
measured as bloat.

### Step 7 — Measure AFTER and report

Snapshot the surfaces again with the SAME scope as Step 3 (`--docs-only` for
the doc dimension, `--code` for the code dimension), then diff. Matching scopes
is what makes the `verdict` agree with the in-test gate: the mandated test
added in Step 6 is wave overhead under `test/`, excluded from both snapshots.
The `<name>` slot is the target feature name:

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  count --docs-only .claude/features/<name> > /tmp/housekeep-<name>-after.json
python3 .claude/features/rabbit-housekeep/scripts/measure-reduction.py \
  diff /tmp/housekeep-<name>-before.json /tmp/housekeep-<name>-after.json
```

Report to the user the honest `verdict`: when `reduced`, the total lines
removed (the `total_delta`) and per-artifact breakdown; when `no-op`, that the
target was already clean and nothing was dead — a SUCCESS, not a failure. In
both cases report any `housekeeping`-tagged sub-issues filed for unverifiable
items and confirm behavior was preserved (the existing test suite stayed green;
load-bearing tokens survived).

### Step 8 — Auto-merge the wave's PR on green gates

Step 6 delegates to `rabbit-feature-touch`, which CREATES the wave's PR (its
Step 7) for the audit trail. For a user-installed `/rabbit-housekeep` run the
PR should not be left pending for the user to merge by hand: when the wave's
gates are all green, this step MERGES that PR to `main` and cleans up its
branch/worktree. The PR is still always created — auto-merge only lands it when
the gates hold. Auto-merge is DEFAULT-ON for housekeep waves (they are
mechanical and fully gated). A user opts out with the phrase `--no-automerge`
in the housekeep request; when opted out, the PR is created and left open for
the user to merge, exactly as a plain feature-touch PR.

The MERGE-OR-LEAVE-OPEN decision is a computed, gate-aware branch, so it is
SCRIPT-tier (spec-rules §4): `scripts/wave-automerge.py` owns the gating logic
and the SKILL invokes it. Build the gating payload from the wave's signals —
the HANDOFF gates from `.rabbit/tdd-report-<name>.json` (`tdd_state`,
`test_result`, `spec_compliance`), the honest-reduction `verdict` from Step 7,
and the PR-side `mergeable` / `merge_state_status` / `ci_status` collected by
the script's `gather` subcommand — then ask the script to `decide`. It emits
`decision: merge` ONLY when ALL hold: the HANDOFF gates are green
(`tdd_state: test-green`, `test_result: pass`, `spec_compliance: pass`), the PR
is mergeable/clean with green CI, AND the honest-reduction outcome held (a
measured `reduced` OR an honest already-clean `no-op` — a `no-op` is a PASSING
outcome, never a failure). Any failed gate yields `decision: leave-open` with
the failing gate NAMED in `reasons`, leaving the PR OPEN for human attention,
exactly as today.

<!-- example: illustrative invocation, not a live step -->
```bash
python3 .claude/features/rabbit-housekeep/scripts/wave-automerge.py \
  gather --pr <pr-number> > /tmp/housekeep-<name>-prstate.json
python3 .claude/features/rabbit-housekeep/scripts/wave-automerge.py decide \
  < /tmp/housekeep-<name>-gates.json
```

On `decision: merge`, land the PR to `main` with a direct squash merge that also
deletes the wave's branch:

<!-- example: illustrative invocation, not a live step -->
```bash
gh pr merge <pr-number> --squash --delete-branch
```

On `decision: leave-open`, report to the user that the PR was created and left
open with the failing gate from `reasons`, and do NOT merge.

## The OPT-IN code dimension (`--code`)

The DOC dimension above is the DEFAULT. With `--code` the wave runs a parallel,
OPT-IN dimension on the target feature's `src/` source. It honors the same
philosophies: machine-first measurement, BOUNDED SCOPE (edit only the TARGET
feature's `src/`, never cross-feature), designed deprecation, and coding-rules
§2 (Simplicity First) + §3 (Surgical Changes). The ONE MANDATORY gate is
unchanged: behavior preserved (the feature's existing test suite stays green).

The code dimension reuses the wave skeleton (measure BEFORE with `count
--code`, do the work, measure AFTER, report the honest `verdict`) and runs the
edit through the governed TDD path (Step 6). Inside the code dimension, apply
this PRIORITY ORDER:

1. **SIMPLIFY (the main focus).** Refine `src/` for clarity, consistency, and
   maintainability while PRESERVING all functionality. Route the
   simplification through the in-environment `code-simplifier` agent, which
   "simplifies and refines code for clarity, consistency, and maintainability
   while preserving all functionality". The simplification runs through the
   governed TDD path with the feature's EXISTING test suite as the
   zero-behavior-loss gate — the code analogue of the doc dimension's
   load-bearing-survival gate.
2. **DEAD CODE.** Apply the embedded coding-rules §6 prove-it-dead-or-flag
   protocol to `src/` SYMBOLS: for each function / flag / script / symbol,
   `grep` for callers/usages across the repo; none = dead → REMOVE; proven
   live → KEEP; unverifiable → FLAG a `housekeeping`-tagged sub-issue (the
   Step 2 filing shape) and CONTINUE (annotate-and-continue — one uncertain
   symbol never stalls the wave).
3. **REDUCTION.** Measured `src/` reduction is the honest OUTCOME of steps 1-2,
   reported by `measure-reduction.py diff` over `--code` snapshots — `reduced`
   when simplification or removal actually happened, `no-op` when the source
   was already clean. Reduction is REPORTED, never MANDATED; never reword code
   to manufacture a diff.

Any cross-scope reach or risky rewrite is FLAGGED as a `housekeeping`-tagged
sub-issue, not silently applied (§6 annotate-and-continue).

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

- Reword to manufacture a diff. A wave removes only dead/redundant content;
  an already-clean target is an honest `no-op` SUCCESS, never forced into a
  reword. Behavior preserved (tests green) is the only MANDATORY gate.
- Delete a load-bearing token to inflate the line delta. Schemas, decision
  tables, exit codes, script names, and cross-references are KEPT verbatim.
- Silently keep an unverifiable claim. FLAG it as a `housekeeping`-tagged
  sub-issue and continue.
- Edit files outside the target feature's directory. Cross-feature scope is
  DECOMPOSED into per-feature units, each scoped to its own feature.
- Wrap this skill in an `Agent(...)` call (see the nesting constraint above).
