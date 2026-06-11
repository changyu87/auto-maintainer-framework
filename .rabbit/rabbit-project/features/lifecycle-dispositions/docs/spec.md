---
feature: lifecycle-dispositions
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the lifecycle disposition model changes incompatibly (e.g. v2 parallelism introduces per-stream dispositions) or the marker encoding is replaced.
---

# lifecycle-dispositions

## Purpose

The loop's coarse cross-tick operating condition (its **disposition**) plus the
two core anchor states that read/select it: **GUARD** (entry gate + single-writer
mutex) and **EXIT** (terminal; emits the disposition-selecting signal). Plus
**host-agnostic resumption** (identical behavior fresh/headless or warm/in-session).

> Design references: DESIGN.md §1.2 (dispositions table), §3.1.2 (dispositions +
> markers), §3.1.3 (single-writer mutex + stale-marker detection), §3.1.4
> (host-agnostic resumption), §1.1 (GUARD/EXIT core states).

## Paths governed

Greenfield. Code under `.../features/lifecycle-dispositions/src/`. The disposition
marker + lock marker live at a runtime path (passed in; defaults under the project
runtime dir).

## Public surface

1. **`Disposition`** — the closed set `RUNNING | IDLE | STOPPED | ABORTED |
   RESTART_NEEDED`, persisted via a durable marker. Read/write helpers. Key
   distinctions (§1.2): IDLE auto-resumes on the next heartbeat; STOPPED (human
   stop) and ABORTED (fault) both **latch** until a human acts; RESTART_NEEDED
   auto-resumes after a restart.
2. **Single-writer mutex** — a lock marker with **stale-marker detection**
   (owner/timestamp; a dead holder's stale lock is reclaimable). Guarantees
   exactly one loop instance runs (§3.1.3).
3. **`GUARD` state** — `run(TickContext) -> StateResult` (fsm-contracts contract).
   Entry gate: read disposition — if `STOPPED`/`ABORTED` emit `HALT_REQUESTED`; if
   `RESTART_NEEDED` emit `RESTART_REQUIRED`; otherwise acquire the single-writer
   mutex (reclaim if stale), set disposition `RUNNING`, emit `OK`.
4. **`EXIT` state** — `run(TickContext) -> StateResult`. Terminal anchor: read the
   tick outcome (a slot signaling work-remains / empty / fault), **select the next
   disposition and emit** the matching signal — `refire` (work remains), `idle`
   (queue empty → rely on heartbeat), `break` (restart owed), or `halt`
   (stop/abort). Release the mutex.
5. **Host-agnostic resumption** — behavior is identical on a fresh headless
   context or a warm in-session one; the on-disk disposition + lock markers are
   the only source of truth (§3.1.4).

Per-state manifests for GUARD and EXIT.

## Anchor invariants this feature can now enforce

With GUARD (entry) and EXIT (terminal) concrete, the spine-specific anchor
invariants deferred from `tick-orchestrator` (entry is GUARD; PERSIST precedes
EXIT; EXIT is sole terminal) become checkable here. Slice 1 wires the anchors;
formal anchor-invariant validation may be a follow-up.

## Current behaviour

None yet — `tdd_state: spec`.

## Known gaps / deferred

- The durable state document + journal + DRAIN/PERSIST — owned by `durable-state`
  (orthogonal). This feature does NOT import durable-state; it manages disposition
  + lock markers only.
- The heartbeat, route assembly, demo work — owned by `scheduling`.
- Self-evolution restart hook (§3.10.6) — v2.

## Interfaces (composition)

- GUARD/EXIT implement the fsm-contracts state contract so `tick-orchestrator`
  runs them as the route's entry and terminal anchors.
- `scheduling` reads EXIT's emitted signal / the disposition to decide
  refire-now vs idle vs halt for the next heartbeat.

## Open questions

- Marker file format/location and how mutex ownership identity is stamped
  (PID + start-time vs a session token) — settle in implementation; tests inject
  a temp runtime path.
