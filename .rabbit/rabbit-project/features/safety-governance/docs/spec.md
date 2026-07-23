---
feature: safety-governance
version: 0.12.0
owner: changyu87
deprecation_criterion: Superseded when trust-ladder / budget enforcement moves into a different layer than a project-local central config (config.json) consulted at tick entry, or when the config schema reaches its next breaking major (3.0.0).
---

# safety-governance

## Purpose

The cross-cutting governance layer (DESIGN §3.8). **Slice 1** ships the pieces
that either have present value or are the safety harness the model-backed
IMPLEMENT doer must be born into:

- **Trust-ladder mode** (§3.8.2, §2.3) — `dry-run` / `propose` / `auto-merge`.
- **Budget ceiling** (§3.8.4) — a **per-day token** ceiling, enforced as an
  auto-resuming **readiness gate**, not a latched halt (the per-tick ceiling is
  removed).
- **No-`AskUserQuestion`-in-autonomous-mode → ABORTED + escalation** (§3.8.3).

Governance is consulted at tick entry and by acting adapters; it provides
deterministic decision functions over a machine-first, versioned config.

## Central config schema (this feature owns it)

Machine-first, versioned; project-local at
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json` (mirrors `route.json` /
`adapter-map.json`, §3.10.2). Absent file ⇒ documented defaults. This file is the
single central `userConfig` (§3.10.1); it **replaces** the former
`governance.json` (rename-and-migrated — see "Migration" below).

```json
{
  "schema_version": "2.8.0",
  "mode": "propose",
  "work_own_filings": true,
  "regression_command": null,
  "doc_check_features_root": null,
  "implement_test_command": null,
  "issue_filter": {
    "labels": [],
    "title_pattern": null
  },
  "budget": {
    "per_day_tokens": null,
    "window_tz": "local"
  },
  "heartbeat": {
    "interval_minutes": 3
  },
  "backoff": {
    "threshold": 5
  }
}
```

- `mode` — `dry-run` | `propose` | `auto-merge`. **Default `propose`** (§2.3).
  The legacy value `gated-merge` is tolerated on load and mapped to `auto-merge`.
- `budget.per_day_tokens` — an integer ceiling, or **`null`/omitted = NO LIMIT**
  (unbounded; the gate is a no-op). **Default `null`** per an explicit user
  decision; a finite ceiling is **opt-in**. §3.8.4's "a real ceiling, not
  judgment" intent is satisfied by configuration, not by the default — a ceiling
  SHOULD be set when enabling the model-backed implement doer. The **per-tick
  ceiling is REMOVED** (per-day suffices); a config still carrying
  `per_tick_tokens` is tolerated (the key is ignored).
- `budget.window_tz` — the day-boundary basis for the per-day window; **`local`
  (the host's local timezone) by default**.
- `heartbeat.interval_minutes` — the tick cadence the `/start` heartbeat
  schedules (§3.3.2). **Default `3`.** Owned here, **read by `scheduling`** via
  the cross-feature contract.
- `backoff.threshold` — the per-item consecutive-`blocked` count K at which the
  loop escalates + defers a work order (§3.8.5). **Default `5`.** Owned here,
  **read by `scheduling`** (`run_tick`).
- `work_own_filings` — whether the loop works its OWN filings (the loopback
  provision, §3.11.5). **Default `true`** per an explicit owner decision: the
  loop works its own discoveries by default, with a manual **opt-OUT**
  (`work_own_filings: false`). §3.11.5 originally deferred this to "explicitly
  opted in" to prevent self-amplification; the owner has flipped it to
  default-on opt-out. Read through the pure accessor `work_own_filings(config)`
  (default `True` when absent — an existing config without the key opts IN).
  Owned here; **`work-intake` PULL** consumes it to conditionally apply the
  loopback exclusion and **`scheduling`** threads it from the loaded config into
  PULL (both separate cycles).
- `regression_command` — the project's full-regression **shell command** the GATE
  state (§3.7, verify-integrate) runs against each REVIEW-passed PR in a
  disposable integration worktree; **exit 0 = pass, nonzero = fail** (its output
  is captured, bounded, for the failure comment). **Default `null` = NO gate** —
  GATE is a no-op PASS, so an unconfigured project merges exactly as before
  (non-breaking, opt-in). A maintained project sets its own command (e.g.
  `pytest`, `npm test`); the shipped self-repo default (in
  `default-config/config.json`) runs every `rabbit-project/features/*/test/run.py`.
  Surfaced through `_overlay` like any known key (absent ⇒ `null`). Owned here;
  **read by `verify-integrate`** GATE via the cross-feature contract (through
  `load_config`).
- `doc_check_features_root` — the **repo-relative** features root the GATE's
  doc-surface load-bearing-token survival check (§3.7, verify-integrate) uses to
  map a PR's diff paths to features. **Default `null` = the doc check is OFF.**
  Kept **separate** from `features_root` (VERIFY's complement locator, which may
  be absolute) so the doc gate is opt-in **independently** and is LIVE on the
  auto-merge path when set. Read through `doc_check_features_root(config)`.
- `implement_test_command` — configures the **IMPLEMENT-side per-work-order
  test-gate** (`implement`'s `test_gate.py`), which runs BEFORE a PR is opened.
  This is DISTINCT from `regression_command` (the whole-repo GATE-state gate at
  PR-merge time): this one is per-touched-feature at implement time. Three modes:
  - **`null` (default)** — the historical behavior: the gate runs the touched
    feature's `<feature-dir>/test/run.py` (exit 0 = pass; a **missing** `run.py`
    is a FAILED verdict, so no PR is opened). Unchanged for existing installs.
  - **a shell command string** — the gate runs THAT command instead (exit 0 =
    pass), for repos whose tests are not the rabbit `test/run.py` convention
    (e.g. `pytest`). Run with the touched feature dir as cwd.
  - **the literal `"none"`** (also `"skip"`) — the IMPLEMENT test-gate is
    **SKIPPED** (a no-op PASS): the PR is opened without an implement-time test
    verdict, deferring verification to the GATE-state `regression_command`. This
    is the explicit opt-out for repos with no per-feature harness (the case that
    otherwise blocks every work order with "no test/run.py found"). Read through
    the accessor `implement_test_command(config)` (returns the raw value, `None`
    default); the three-way interpretation lives in `implement`'s `test_gate.py`
    (the consumer), which owns the gate. Owned here (the schema + accessor +
    configure flag); **read by `implement`** via the cross-feature contract.
- `issue_filter` — an optional filter narrowing WHICH open GitHub issues the
  PULL stage (work-intake) pulls. An object
  `{"labels": <DNF>, "title_pattern": <regex-string-or-null>}`:
  - `labels` — a **disjunctive-normal-form (OR-of-ANDs)** label matcher in
    canonical form `List[List[str]]`: the outer list is OR, each inner list is
    AND. `[["A","B"],["C"]]` means *(label A AND label B) OR (label C)*. The
    canonical empty form `[]` = **no label filter**.
  - `title_pattern` — a regular-expression string an issue's title must match
    (applied post-fetch, since `gh` has no title query), or **`null` = no title
    filter**.
  **Default `{"labels": [], "title_pattern": null}` = NO filter** — PULL pulls
  every open issue, exactly as before (non-breaking, opt-in). Read + normalized
  through the pure accessor `issue_filter(config)`, which returns the canonical
  object. The **normalizer** accepts and canonicalizes user input:
  absent / `null` / `[]` ⇒ no-filter; a **flat** list of non-empty strings
  `["A","B"]` is sugar for a single AND-group ⇒ `[["A","B"]]`; a `List[List[str]]`
  is validated as-is. It **rejects** (raises `ValueError`, never a silent write)
  non-string label entries, empty-string labels, and empty inner groups; and
  `title_pattern` must be a string that **compiles** as a regex, or `null`.
  Owned here; **`work-intake` PULL** consumes it to build the `gh` query (one
  `gh issue list --label …` query per AND-group, unioned + deduped, since `gh`
  cannot OR labels) plus the post-fetch title match, and
  **`scheduling`/`tick-orchestrator`** threads it from the loaded config into
  PULL (separate cycles).

The runtime file stays **lean**: schema-definition metadata (`owner`,
`deprecation_criterion`) lives in this spec, `safety_governance.py`'s module
docstring, and `contract.md` — NOT in the user's `config.json`.

### Maintainer-self REPORT destination — a FIXED constant (§3.11.6)

`maintainer-self` discoveries (bugs in the loop's OWN tooling — the dogfood case
§3.10.5) route to a **fixed upstream repo**, NOT a config field:
`MAINTAINER_REPO = "changyu87/auto-maintainer-framework"` (a module constant in
`safety_governance.py`). They go there **always** — **never** the project
tracker, **no fallback**: the auto-maintainer's own defects belong to the
auto-maintainer, not whatever project it currently maintains.
`run_tick._repo_for_target` imports this constant. The former configurable
`maintainer_repo` field is **removed**. (Deprecation: revisit only if the
upstream home repo moves or per-install self-tracking is reintroduced.)

### Migration (governance.json → config.json)

`load_config(project_dir)` prefers `config.json`. If it is absent but a legacy
`governance.json` is present, it **migrates once**: map the surviving fields
(`mode`, `budget.per_day_tokens`, `budget.window_tz`), DROP `per_tick_tokens` +
`maintainer_repo`, backfill `heartbeat`/`backoff` defaults, WRITE `config.json`,
and rename the legacy file to `governance.json.migrated` (non-destructive). A
thin `load_governance` alias delegates to `load_config` during the coexistence
window (consumers migrate to `load_config`).

**Default resolution (read shipped default FRESH, #337).** When no project-local
`config.json` (and no legacy `governance.json`) exists, the default is read FRESH
from the shipped `default-config/config.json` at `<plugin_root>/default-config/`
(sibling of `lib/`) when present — the aggressive operational default
(`mode: auto-merge`, …) built by packaging-config and refreshed every release —
backfilled onto a fresh defaults copy and validated like any config. When that
shipped file is absent (the source tree / no plugin), the conservative embedded
`DEFAULT_GOVERNANCE` constant is the fallback. There is NO seed-once copy: a
release that changes the shipped config reaches an existing install
automatically, while a project-local `.auto-maintainer/config.json` override
still wins.

**Field-level (3-way) merge of an override (#357, deferred #336).** A present
`config.json` is 3-way merged via `merge_config(base, theirs, mine)` — `base`
the **current** embedded `DEFAULT_GOVERNANCE` (no historical base is recorded),
`theirs` the override, `mine` the current shipped default: a new default key the
override lacks is **adopted** (the unfreeze), a user value that **differs from
the base** is **preserved**, and a key both sides changed differently is surfaced
as a **conflict** (warned on stderr) keeping the user value. With no shipped
default `mine` equals `base`, so the merge is a no-op (the prior whole-file
overlay). CAVEAT (#396): since `base` is today's embedded default (not the
historical default the override was taken from), a user value that **equals the
base** is indistinguishable from unset — treated as unset and, if a later release
changes that key's default, **re-adopted** with **no conflict recorded**. So
preservation holds only for a user value that differs from the base; recording
the base each override was taken from (out of scope) would remove this.

**`schema_version` is loader-owned metadata, EXCLUDED from the field-merge.**
`schema_version` is the config-schema stamp (`GOVERNANCE_SCHEMA_VERSION`), NOT a
user knob, and it gates nothing at runtime (purely informational). It is already
normalized to the current constant by `_overlay` (which never re-surfaces it from
the override), so the loaded config's `schema_version` is ALWAYS the current
version. But `merge_config` used to run over it too — with a user `2.7.0`, a
frozen shipped `2.2.0`, and a base `2.8.0` all differing, it recorded a spurious
**conflict** and emitted a misleading "keeping user value 2.7.0" stderr warning
(even though `_overlay` then discarded that value). Therefore `load_config`
**excludes `schema_version` from the 3-way merge** (it is stripped from `theirs`
/ `mine` before `merge_config`, or filtered out of the surfaced conflicts), so no
spurious conflict is ever recorded for it — a pure noise fix with NO behavior
change (the loaded `schema_version` was, and remains, the current constant). A
stale `schema_version` in a user's `config.json` is thus harmless (overridden on
every read); no migration of the on-disk value is required.

## Trust-ladder gate (§3.8.2)

A deterministic `permits(effect_kind, mode) -> bool` over the closed effect set:

- `dry-run` — no outward effect; intent is logged, not performed (incl. filing,
  §3.11.7).
- `propose` — implement + open PR permitted; **merge denied** (§2.3).
- `auto-merge` — merge permitted (the loop merges automatically; the §3.8.1
  merge guardrails are the hard backstop that actually gates it). The legacy
  name `gated-merge` is tolerated on load and mapped forward to `auto-merge`
  (a non-breaking coexistence migration; schema 2.1.0).

Slice-1 note: the only acting adapter today is the reference dry-run IMPLEMENT,
which is inherently dry-run; the gate is harness-ready for the model-backed doer,
not yet load-bearing.

## Merge guardrails (§3.8.1)

Declarative red-flags the host enforces before a merge — a hard backstop BELOW
the trust ladder. Even at `auto-merge` (where `permits("merge", …)` is True), a
PR must clear these or it is NOT merged. Owned here (the safety layer);
`verify-integrate`'s INTEGRATE consumes it.

`merge_guardrails(pr_meta, default_branch) -> {ok, violations}` — a pure,
deterministic check over a PR's metadata (`{base, mergeable, head}` and similar):

- **never-merge-wrong-base** — `pr_meta.base != default_branch` ⇒ violation.
  The loop only merges PRs targeting the repo's default branch.
- **never-merge-dirty** — `pr_meta.mergeable` is not cleanly mergeable
  (CONFLICTING / UNKNOWN / missing) ⇒ violation. Never merge a conflicted or
  not-yet-computed tree.
- **never-delete-non-matching-branch** — a branch-deletion target that is not the
  PR's own `head` ⇒ violation. (Consumed by CLEANUP to bound branch deletion to
  the PR's head only.)

Returns `ok=True` with an empty `violations` list only when every check passes;
otherwise `ok=False` and `violations` names each failed check (machine-first, so
INTEGRATE can record the reason in its `skipped` list). Deterministic: pure
function of the passed metadata, no I/O.

## Budget readiness gate (§3.8.4) — auto-resuming, NOT a latch

Budget exhaustion is a **readiness gate evaluated at tick entry**, mirroring
GUARD's existing mutex / STOPPED checks — it never latches a halt disposition.

- Durable budget state: `{ window_key, spent_tokens }`. `window_key` is the
  current **local-tz calendar day** derived from an injectable `now`.
- Token spend is read from an **injectable spend seam** (real model-token counts
  arrive with the doer; tests inject spend; dry-run spends ~0).
- At tick entry:
  1. **window rolled over** (`window_key` changed) → reset `spent_tokens = 0`,
     proceed. *This is the auto-resume — no human `/start`.*
  2. **per-day ceiling spent** (same window, finite ceiling) → the tick performs
     no act work and **idles** (disposition `IDLE`); the heartbeat keeps firing
     and re-checks each tick; work resumes automatically at the next local-day
     window.
  3. **ceiling `null`** → no gate (unbounded).

Because the disposition stays `IDLE` (§1.2: IDLE auto-resumes), the loop resumes
on its own at the next window — no human intervention. `lifecycle-dispositions`
is NOT modified for budget (no new latch).

## No-`AskUserQuestion` → ABORTED (§3.8.3)

A deterministic helper that, instead of blocking on an interactive prompt in
autonomous mode, **latches `ABORTED`** (via `lifecycle-dispositions`) and emits
an escalation through a **seam** (the issue-comment sink is §3.9.3, owned by
observability — stubbed here). ABORTED is a TRUE latch (§1.2: fault, alarm, holds
until a human investigates) — unlike budget, faults do NOT auto-resume.

## Observability

Active `mode` and budget state (`spent/ceiling`, `window_key`, and a
budget-paused reason when idling over-ceiling) are surfaced in the tick trace and
status — same spirit as route-source (#59) — so "idle: budget exhausted, resumes
next window" is distinguishable from "idle: no work".

## Config writer + the configure skill (userConfig §3.10.1)

safety_governance.py READS + decides over `config.json`; this feature also
ships its **writer half** — the deterministic `src/configure.py` script and the
`/auto-maintainer:configure` skill (`ship/skills/configure/`) — so a user can set
**every** central-config knob (trust `mode`, budget, heartbeat, backoff, GATE
regression command + doc-check root, VERIFY `features_root`, the §3.11.5
`work_own_filings` loopback toggle, and the PULL `issue_filter`) without
hand-editing JSON, and be walked through them via the guided `--setup` onboarding.

- **`src/configure.py`** (deterministic, script-tier — spec-rules §1). A
  load-modify-save of `config.json`: it loads the current config via
  `load_config` (absent keys backfilled from the defaults), applies only the
  mentioned fields, validates, writes back (pretty, `sort_keys`), and prints the
  resulting config. It owns NO schema — the schema is this feature's
  (`GOVERNANCE_SCHEMA_VERSION` = 2.1.0).
  - `--mode` is validated through `permits` (the closed mode set
    {dry-run, propose, auto-merge}); an unknown mode raises `ValueError`
    (CLI exit 2 with an error, never a silent write). The legacy `gated-merge`
    is accepted and stored as `auto-merge`.
  - `--per-day-tokens` accepts a non-negative int, or `none`/`null`/`unlimited`/`""`
    meaning NO LIMIT (stored as JSON `null`). `--per-tick-tokens` and
    `--maintainer-repo` are **removed**.
  - `--interval-minutes` (heartbeat cadence) and `--backoff-threshold` accept a
    **positive int**.
  - `--regression-command` / `--doc-check-features-root` — as documented above
    (GATE section); `none`/`null`/`""` clear to `null`.
  - `--implement-test-command` — the IMPLEMENT-side test-gate command
    (documented above). An arbitrary shell command string sets it; `""` (empty)
    clears to `null` (the default `test/run.py` behavior); the literal `none`/
    `skip` is preserved VERBATIM as the skip sentinel (NOT cleared to null — the
    reader distinguishes `null` = run `test/run.py` from `"none"` = skip the
    gate). Stage: IMPLEMENT.
  - `--features-root` — VERIFY's cross-feature complement locator. An
    arbitrary path string (it MAY be absolute, UNLIKE `doc_check_features_root`);
    `none`/`null`/`""` clears to `null` (unconfigured, the conservative gate).
  - `--work-own-filings` — the §3.11.5 loopback toggle. Accepts a boolean
    `true`/`false` (case-insensitive; `1`/`0`, `yes`/`no` tolerated); an
    unparseable value raises `ValueError`.
  - `--issue-labels` — the `issue_filter.labels` DNF (public surface added in
    schema 2.7.0). Compact syntax: **comma = AND within a group, semicolon = OR
    between groups**, e.g. `"bug,triaged;urgent"` → `[["bug","triaged"],["urgent"]]`
    = *(bug AND triaged) OR urgent*. A single group `"bug"` → `[["bug"]]`;
    `none`/`null`/`""` clears to `[]` (no label filter). The parsed DNF is
    validated + canonicalized through this feature's `issue_filter` normalizer
    (rejecting empty labels/groups) before the write — the writer owns NO
    validation the reader does not.
  - `--issue-title-pattern` — the `issue_filter.title_pattern` regex string an
    issue's title must match; `none`/`null`/`""` clears to `null`. It must
    **compile** as a regex (validated via the same `issue_filter` normalizer),
    else `ValueError`.
  - `--preflight` — a **read-only** environment check emitting machine-first JSON
    `{gh_authenticated: bool, gh_account: str|null, resolved_repo: str|null,
    config_exists: bool}` for the guided `--setup` onboarding: it shells
    `gh auth status` (authenticated? which account?) and resolves the target repo
    the loop would maintain (the `gh`-default / git-remote repo, the same
    resolution PULL uses — since there is no `repo` config key), and reports
    whether a project-local `config.json` already exists. It writes NOTHING.
  - `--describe` emits the machine-first **field catalog** as JSON — a list of
    `{key, label, controls, default, current, type, validator, stage}` — the
    single source of truth the guided `--setup` walk-through reads. The **`stage`**
    field (added for the onboarding walk-through) groups each knob by the loop
    state that consumes it, and the catalog is **ordered by loop stage** so the
    walk-through follows the route: `PULL` (`issue_filter.labels`,
    `issue_filter.title_pattern`, `work_own_filings`) →
    `IMPLEMENT` (`mode`, `implement_test_command`) →
    `VERIFY` (`features_root`) → `GATE` (`regression_command`,
    `doc_check_features_root`) → `SCHEDULING` (`heartbeat.interval_minutes`) →
    `SAFETY` (`budget.per_day_tokens`, `backoff.threshold`).
  - An unmentioned field is preserved unchanged.
  - Project dir resolves from `--project-dir`, else `$CLAUDE_PROJECT_DIR`, else
    cwd. `--show` (or no mutating flag) prints the current config and writes
    nothing.
- **`/auto-maintainer:configure` skill** (`ship/skills/configure/SKILL.md`) — a
  thin relay: it invokes `configure.py` with only the flags matching the user's
  request (the values are the user's data, passed verbatim) and reports the
  resulting config. It never edits `config.json` directly. This is the
  arming surface for the model-backed doer: `--mode dry-run` keeps it inert,
  `--mode propose` arms it to open PRs.
- **Guided `--setup` onboarding** (§3.10.1 userConfig). The same skill supports
  a `--setup` mode (the user runs `/auto-maintainer:configure --setup`, or asks
  to be "walked through" / "set up" the maintainer). It is a **re-runnable**
  guided onboarding — usable any time, not only right after install — that walks
  the user through everything needed to run the loop, **ordered by the loop's own
  stages** (the route). The skill orchestrates **over the machine-first catalog +
  the `--preflight` probe**, dispatching NO subagent (it must ask the user
  questions and read their answers inline):
  1. **Preflight.** Run `configure.py --preflight`. Surface the result: whether
     `gh` is authenticated (and as which account) and the **resolved repo** the
     loop would maintain, and confirm it with the user ("I'll maintain
     `owner/repo` — correct?"). If `gh` is NOT authenticated, tell the user to run
     `gh auth login` first; if the resolved repo is wrong, tell them to
     `gh repo set-default <owner/repo>` (there is no `repo` config key — the repo
     is `gh`-resolved). This step writes nothing.
  2. **Stage-by-stage walk.** Run `configure.py --describe` and group the knobs by
     their `stage`, presenting each stage in loop order (PULL → IMPLEMENT →
     VERIFY → GATE → SCHEDULING → SAFETY). For each knob show its `label`,
     `controls`, `default`, and `current`, quote its `validator`, and ask for a
     new value or to keep current. Take answers verbatim (the user's data).
  3. **Advanced (opt-in) routing.** Offer to keep the recommended default
     `route`/`adapter-map` (the common case); only dive into `/auto-maintainer:route`
     / `/auto-maintainer:adapter-map` if the user wants to change which stages run
     or swap a port's adapter.
  4. **Review + apply.** Apply all chosen values in ONE `configure.py`
     invocation (the deterministic writer validates + writes `config.json`), then
     `--show` the result and read it back. If nothing changed, skip the write.
  5. **Offer to start.** Point the user at `/auto-maintainer:start`.
  The catalog + preflight JSON are the **single source of truth** — the skill
  never hardcodes field names, prose, `stage` grouping, or repo/auth values
  (SKILL.md authoring §4: derive from source, do not paraphrase). A power user
  may still hand-edit `config.json` directly.

## Invariants

- Deterministic given injected `now` + injected spend: no model, no network, no
  wall-clock except through the injectable `now`, no filesystem beyond the
  durable budget state.
- `configure.py` is a deterministic load-modify-save: it validates `mode` against
  the closed set, `per_day_tokens` as non-negative-int-or-null,
  `interval_minutes` / `backoff.threshold` as positive ints, `work_own_filings`
  as a bool, and the `issue_filter` (`--issue-labels` DNF + `--issue-title-pattern`)
  through THIS feature's `issue_filter` normalizer (the writer owns no validation
  the reader does not); preserves unmentioned keys; and never writes an invalid
  config (an invalid value is a non-zero exit, not a partial write). `--describe`
  (field catalog, now carrying a loop-`stage` per knob) and `--preflight` (the
  read-only `gh` auth + resolved-repo probe for onboarding) are BOTH read-only:
  they emit JSON and write nothing.
- The `config.json` runtime file carries only `schema_version` + the knobs; it is
  the single central config (no scattered per-concern files). `maintainer-self`
  routing is a fixed `MAINTAINER_REPO` constant, not a config field.
- `issue_filter(config)` is a pure normalizer: it canonicalizes to
  `{"labels": List[List[str]], "title_pattern": str|None}`, treats
  absent/`null`/`[]` as the no-filter default, expands a flat string list into a
  single AND-group, and raises `ValueError` (never a silent/partial write) on a
  non-string label, an empty-string label, an empty inner group, or a
  `title_pattern` that is neither `null` nor a compilable regex. The default
  (empty labels + null pattern) preserves the pull-all behavior.
- Budget NEVER latches a halt disposition — it gates work and auto-resumes via
  `IDLE` at the next window. `null` ceiling ⇒ unbounded (no gate).
- ABORTED (§3.8.3 faults) IS a true latch; only budget auto-resumes.
- Trust default is `propose` (§2.3).
- Bounded scope: owns the governance config + the gate/decision functions;
  consumes `lifecycle-dispositions` unchanged.

## Deferred (NOT in this slice)

- **Backoff / circuit-breaker** (§3.8.5) → still deferred (re-verifying a red PR
  is cheap and never merges, so there is no thrash to break yet).
- **Loopback / provenance guard** (§3.11.5, `filed_by` stamp recognized by the
  TRIAGE gates) → with `outbound-report` (nothing files until REPORT exists).
  The governance `work_own_filings` knob (default-on opt-out) is shipped here;
  `work-intake` PULL and `scheduling` consume it to apply the loopback exclusion
  (separate cycles).
- **Blast-radius / learned scope** (§3.8.6) → v2.
- **Per-day window basis other than local-tz**, and a real escalation sink
  (§3.9.3, observability) → later refinements.
