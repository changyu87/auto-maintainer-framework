# scheduling — Changelog

## contract 0.1.3 — 2026-06-10

- Wired `safety-governance` into the tick loop (slice 1: load + surface +
  persist; consumed UNCHANGED). `run_tick` now loads governance via
  `sg.load_governance(project_dir)` (project-local
  `.auto-maintainer/governance.json`, else the documented defaults) and threads
  the config into the factory `runtime` dict under a new `governance` key, so
  future acting adapters can consult `permits`/budget. The existing runtime keys
  (`project_dir`/`runtime_dir`/`source`/`now`) are preserved.
- Added a durable, cross-tick **budget window** under the new durable-state key
  `BUDGET_KEY = "budget"` (`{window_key, spent_tokens}`). Each tick resolves a
  tz-aware `now` (injected when tz-aware, else `datetime.now().astimezone()`),
  calls `sg.evaluate_budget(gov, prior_budget_state, now, tick_spend)`, and
  PERSISTS the returned `budget_state` (the lib performs window rollover /
  auto-resume). The budget window is a durable cross-tick fact like the counter,
  NOT a per-tick ephemeral read product (#64) — it is not reset under the #64
  logic; only a window rollover resets `spent_tokens`. Added the
  `persisted_budget_state(state_path)` helper.
- Surfaced governance state in BOTH the tick trace and `status.py` (#69 style,
  always shown): `mode=<mode>` and a compact
  `budget=<spent>/<ceiling-or-"none"> win=<window_key>` field, plus a
  `budget_paused=<reason>` indicator when `evaluate_budget` returns
  `allowed=False`. Placed after the existing fields; all current fields/order
  preserved.
- `run_tick(...)` gained injectable `now` (the tz-aware budget clock; defaults to
  the host local-aware now) and `tick_spend` (default 0 — no model spender yet;
  tests inject it). Act-skip enforcement on a budget-blocked tick is DEFERRED to
  the acting doer (next milestone); this slice only loads + surfaces + persists.
  New public surface (status/trace fields, durable `budget` key); informational
  stdout only — no typed schema field changed.

## contract 0.1.2 — 2026-06-10

- Wired the two new deterministic adapters into the route-as-data loop:
  `PRIORITIZE` (prioritize) and `IMPLEMENT` (implement, dry-run) are now in
  `DEFAULT_ADAPTER_MAP` (mapped to `run_tick:make_prioritize` /
  `run_tick:make_implement`), so an override route
  `TRIAGE → PRIORITIZE → IMPLEMENT` wires with NO code change. The default route
  is unchanged (read-and-idle spine; the two new ports are wireable but omitted).
- Surfaced two new per-tick ephemeral read products, `execution_plan` and
  `handoffs`, alongside `work_items`/`work_orders`. They are persisted per #64
  discipline (overwritten each tick, empty when the active route did not route the
  producing stage — no stale carry-forward) and shown unconditionally in BOTH the
  tick trace and `status.py` (#69), in the order
  `work_items work_orders execution_plan handoffs`.
- Added persisted-read-product helpers `persisted_execution_plan/_count` and
  `persisted_handoffs/_count` (mirroring the work_orders helpers). Added `BLOCKED`
  (IMPLEMENT's signal) to the closed signal vocabulary. Consumes prioritize +
  implement UNCHANGED; edits only in scheduling. Informational stdout only; no
  typed schema field changed.

## contract 0.1.1 — 2026-06-10

- Fixed auto-maintainer-framework#69: `status.py` now ALWAYS reports
  `work_orders=N`, including `work_orders=0`, matching the tick trace's
  unconditional `work_orders=N` field. The previous conditional (append
  `work_orders` only when the count was truthy) made a default (no-TRIAGE)
  tick's status drop the field, so a reader could not distinguish "no TRIAGE
  routed" from "TRIAGE ran, found nothing", and status diverged from the tick
  trace. Field order is unchanged (disposition, work_items, work_orders, route,
  runtime_dir). Informational stdout only; no typed schema field changed.
