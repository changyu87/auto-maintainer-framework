---
feature: safety-governance
version: 0.4.0
owner: changyu87
deprecation_criterion: Superseded when the governance config schema reaches a breaking major version, or when trust-ladder / budget enforcement moves into a different layer than a project-local governance config consulted at tick entry.
---

# safety-governance

## Purpose

The cross-cutting governance layer (DESIGN §3.8). **Slice 1** ships the pieces
that either have present value or are the safety harness the model-backed
IMPLEMENT doer must be born into:

- **Trust-ladder mode** (§3.8.2, §2.3) — `dry-run` / `propose` / `gated-merge`.
- **Budget ceiling** (§3.8.4) — a per-tick / per-day **token** ceiling, enforced
  as an auto-resuming **readiness gate**, not a latched halt.
- **No-`AskUserQuestion`-in-autonomous-mode → ABORTED + escalation** (§3.8.3).

Governance is consulted at tick entry and by acting adapters; it provides
deterministic decision functions over a machine-first, versioned config.

## Governance config schema (this feature owns it)

Machine-first, versioned; project-local at
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/governance.json` (mirrors `route.json`,
§3.10.2). Absent file ⇒ documented defaults.

```json
{
  "schema_version": "1.1.0",
  "mode": "propose",
  "budget": {
    "per_tick_tokens": null,
    "per_day_tokens": null,
    "window_tz": "local"
  },
  "maintainer_repo": null
}
```

- `mode` — `dry-run` | `propose` | `gated-merge`. **Default `propose`** (§2.3).
- `maintainer_repo` — the destination repo (`owner/repo`) for REPORT discoveries
  whose `target` is `maintainer-self` (bugs in the loop's OWN tooling — the
  dogfood case §3.10.5 / §3.11.6). **Default `null`**: with no maintainer repo
  set, `maintainer-self` discoveries fall back to the project tracker.
  `load_governance` PRESERVES an explicit value (a known top-level key, backfilled
  like the others); `run_tick._repo_for_target` routes `maintainer-self` → this
  repo. Schema bumped to **1.1.0** (additive — optional, default null).
- `budget.per_tick_tokens` / `budget.per_day_tokens` — integer ceilings, or
  **`null`/omitted = NO LIMIT** (unbounded; the gate is a no-op for that
  dimension). Both default to **`null` (NO LIMIT)** per an explicit user
  decision; a finite ceiling is **opt-in** (set in `governance.json`, later
  prompted by `userConfig` §3.10.1). §3.8.4's "a real ceiling, not judgment"
  intent is therefore satisfied by configuration, not by the default — a
  ceiling SHOULD be set when enabling the model-backed implement doer (the
  real token spender).
- `budget.window_tz` — the day-boundary basis for the per-day window;
  **`local` (the host's local timezone) by default**, the deterministic
  alternative being a fixed tz string.

## Trust-ladder gate (§3.8.2)

A deterministic `permits(effect_kind, mode) -> bool` over the closed effect set:

- `dry-run` — no outward effect; intent is logged, not performed (incl. filing,
  §3.11.7).
- `propose` — implement + open PR permitted; **merge denied** (§2.3).
- `gated-merge` — merge permitted (gated).

Slice-1 note: the only acting adapter today is the reference dry-run IMPLEMENT,
which is inherently dry-run; the gate is harness-ready for the model-backed doer,
not yet load-bearing.

## Merge guardrails (§3.8.1)

Declarative red-flags the host enforces before a merge — a hard backstop BELOW
the trust ladder. Even at `gated-merge` (where `permits("merge", …)` is True), a
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

**Backoff (§3.8.5) — DEFERRED (minimal/none in this slice).** Re-verifying a
perpetually-red PR each tick is cheap and never merges (VERIFY reports red,
INTEGRATE skips), so there is no thrash to break yet; a consecutive-failure
defer/escalate counter is a later refinement.

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
  3. **per-tick ceiling** (finite) → curtail/skip this tick's work; the next
     tick retries (per-tick resets every tick).
  4. **ceiling `null`** → no gate for that dimension.

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

safety_governance.py READS + decides over `governance.json`; this feature also
ships its **writer half** — the deterministic `src/configure.py` script and the
`/auto-maintainer:configure` skill (`ship/skills/configure/`) — so a user can set
the trust `mode` and budget ceilings without hand-editing JSON.

- **`src/configure.py`** (deterministic, script-tier — spec-rules §1). A
  load-modify-save of `governance.json`: it loads the current config via
  `load_governance` (absent keys backfilled from the defaults), applies only the
  mentioned fields, validates, writes back (pretty, `sort_keys`), and prints the
  resulting config. It owns NO schema — the schema is this feature's
  (`GOVERNANCE_SCHEMA_VERSION`).
  - `mode` is validated through `permits` (the closed mode set
    {dry-run, propose, gated-merge}); an unknown mode raises `ValueError`
    (CLI exit 2 with an error, never a silent write).
  - `per_day_tokens` / `per_tick_tokens` accept a non-negative int, or
    `none`/`null`/`unlimited`/`""` meaning NO LIMIT (stored as JSON `null`). A
    dimension not mentioned is preserved unchanged (other durable keys preserved
    too).
  - `--maintainer-repo <owner/repo>` sets `maintainer_repo` (the
    `maintainer-self` REPORT destination); `none`/`null`/`""` clears it to
    `null` (fall back to the project tracker).
  - Project dir resolves from `--project-dir`, else `$CLAUDE_PROJECT_DIR`, else
    cwd. `--show` (or no mutating flag) prints the current config and writes
    nothing.
- **`/auto-maintainer:configure` skill** (`ship/skills/configure/SKILL.md`) — a
  thin relay: it invokes `configure.py` with only the flags matching the user's
  request (the values are the user's data, passed verbatim) and reports the
  resulting config. It never edits `governance.json` directly. This is the
  arming surface for the model-backed doer: `--mode dry-run` keeps it inert,
  `--mode propose` arms it to open PRs.

## Invariants

- Deterministic given injected `now` + injected spend: no model, no network, no
  wall-clock except through the injectable `now`, no filesystem beyond the
  durable budget state.
- `configure.py` is a deterministic load-modify-save: it validates `mode` against
  the closed set and budget ceilings as non-negative-int-or-null, preserves
  unmentioned keys, and never writes an invalid config (an invalid value is a
  non-zero exit, not a partial write).
- Budget NEVER latches a halt disposition — it gates work and auto-resumes via
  `IDLE` at the next window. `null` ceiling ⇒ unbounded (no gate).
- ABORTED (§3.8.3 faults) IS a true latch; only budget auto-resumes.
- Trust default is `propose` (§2.3).
- Bounded scope: owns the governance config + the gate/decision functions;
  consumes `lifecycle-dispositions` unchanged.

## Deferred (NOT in this slice)

- **Declarative guardrails** (§3.8.1) — IMPLEMENTED this slice as
  `merge_guardrails` (see "Merge guardrails"); consumed by `verify-integrate`'s
  INTEGRATE/CLEANUP.
- **Backoff / circuit-breaker** (§3.8.5) → still deferred (re-verifying a red PR
  is cheap and never merges, so there is no thrash to break yet).
- **Loopback / provenance guard** (§3.11.5, `filed_by` stamp recognized by the
  TRIAGE gates) → with `outbound-report` (nothing files until REPORT exists).
- **Blast-radius / learned scope** (§3.8.6) → v2.
- **`userConfig` interactive prompting** of mode/budget values (§3.10.1) — the
  writer (`configure.py` + the `/auto-maintainer:configure` skill) now ships; a
  guided interactive prompt flow is a later refinement.
- **Per-day window basis other than local-tz**, and a real escalation sink
  (§3.9.3, observability) → later refinements.
