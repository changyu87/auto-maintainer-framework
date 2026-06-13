---
feature: safety-governance
version: 0.1.0
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
  "schema_version": "1.0.0",
  "mode": "propose",
  "budget": {
    "per_tick_tokens": null,
    "per_day_tokens": 200000,
    "window_tz": "local"
  }
}
```

- `mode` — `dry-run` | `propose` | `gated-merge`. **Default `propose`** (§2.3).
- `budget.per_tick_tokens` / `budget.per_day_tokens` — integer ceilings, or
  **`null`/omitted = NO LIMIT** (unbounded; the gate is a no-op for that
  dimension). A finite `per_day_tokens` default is shipped per §3.8.4's "a real
  ceiling, not judgment"; it is overridable to `null`. The concrete values are
  later prompted by `userConfig` (§3.10.1, deferred).
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

## Invariants

- Deterministic given injected `now` + injected spend: no model, no network, no
  wall-clock except through the injectable `now`, no filesystem beyond the
  durable budget state.
- Budget NEVER latches a halt disposition — it gates work and auto-resumes via
  `IDLE` at the next window. `null` ceiling ⇒ unbounded (no gate).
- ABORTED (§3.8.3 faults) IS a true latch; only budget auto-resumes.
- Trust default is `propose` (§2.3).
- Bounded scope: owns the governance config + the gate/decision functions;
  consumes `lifecycle-dispositions` unchanged.

## Deferred (NOT in this slice)

- **Declarative guardrails** (§3.8.1, never-merge-wrong-base / delete-non-matching
  / merge-dirty) → with `verify-integrate` (nothing to guard until INTEGRATE /
  CLEANUP exist).
- **Backoff / circuit-breaker** (§3.8.5) → with `verify-integrate` (needs act /
  verify failures to count).
- **Loopback / provenance guard** (§3.11.5, `filed_by` stamp recognized by the
  TRIAGE gates) → with `outbound-report` (nothing files until REPORT exists).
- **Blast-radius / learned scope** (§3.8.6) → v2.
- **`userConfig` prompting** of mode/budget values (§3.10.1) → later.
- **Per-day window basis other than local-tz**, and a real escalation sink
  (§3.9.3, observability) → later refinements.
