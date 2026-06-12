# scheduling — Changelog

## contract 0.1.1 — 2026-06-10

- Fixed auto-maintainer-framework#69: `status.py` now ALWAYS reports
  `work_orders=N`, including `work_orders=0`, matching the tick trace's
  unconditional `work_orders=N` field. The previous conditional (append
  `work_orders` only when the count was truthy) made a default (no-TRIAGE)
  tick's status drop the field, so a reader could not distinguish "no TRIAGE
  routed" from "TRIAGE ran, found nothing", and status diverged from the tick
  trace. Field order is unchanged (disposition, work_items, work_orders, route,
  runtime_dir). Informational stdout only; no typed schema field changed.
