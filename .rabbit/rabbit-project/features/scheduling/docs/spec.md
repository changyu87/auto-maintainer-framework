---
feature: scheduling
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoding is removed.
---

# scheduling

## Purpose

The **in-session durable heartbeat** that drives the tick loop, the user control
skills **`/auto-maintainer:start`** and **`/auto-maintainer:stop`**, and (slice 1)
the **demo route + tick-runner** that composes the real lifecycle-core anchors
into a self-restarting ~1-minute loop you can watch in an installed session.

> Design references: DESIGN.md §3.3.1 (scheduler detection: system cron else
> in-session durable heartbeat — a plugin can't install its own clock), §3.3.2
> (heartbeat bootstrap, user-authorized), §3.3.3 (immediate-refire + at-most-one
> dedup), §3.3.4 (restart-and-resume), §3.10.4 (run UX).

## The real loop (slice 1)

Each tick runs this route through the existing `tick-orchestrator`:

```
GUARD → DRAIN → DEMO_WORK → PERSIST → EXIT
```

- `GUARD`, `EXIT` from `lifecycle-dispositions`; `DRAIN`, `PERSIST` from
  `durable-state`; `DEMO_WORK` owned here.
- The loop mechanics are REAL (mutex, journal, durable persisted state,
  disposition transitions, DRAIN crash-recovery). Only the *work* is stubbed:
  `DEMO_WORK` increments a persisted `counter` slot.

## Paths governed

Greenfield. Code under `.../features/scheduling/src/`. Shippable plugin
components (the two skills + the tick-runner entrypoint) live under the feature's
**`ship/`** dir (the convention `packaging-config`'s assembly collects).

## Public surface

1. **Tick-runner script** (`src/run_tick.py`, deterministic, script-tier) —
   assembles `route` = `GUARD→DRAIN→DEMO_WORK→PERSIST→EXIT` and the `states` map
   (importing GUARD/EXIT from lifecycle-dispositions, DRAIN/PERSIST from
   durable-state, DEMO_WORK local), runs `tick_orchestrator.run(...)` over a
   `TickContext` seeded from durable state, prints a tick trace (tick number,
   state path, counter, resulting disposition), and returns/persists the EXIT
   disposition signal. One invocation = one tick.
2. **`DEMO_WORK` state** — `run(TickContext) -> StateResult`: reads `counter`,
   writes `counter+1`, journals the intent (record-before-act, via durable-state),
   emits `OK` while `counter < THRESHOLD` (hardcoded, e.g. 5) else `EMPTY`
   (signals queue-empty → idle).
3. **Control scripts** (deterministic, script-tier — spec-rules §1; this is the
   fix for #29/#30 where prompt-tier skills drifted/broke):
   - `src/status.py` — reads the disposition marker + durable-state counter (via
     `run_tick`'s `resolve_runtime_paths`) and prints the real loop status.
   - `src/stop.py` — writes disposition `STOPPED` (via the lifecycle-dispositions
     API) using the same runtime-path resolution. Owns the state write.
   These own ALL state operations so the skills never hand-roll Python.
4. **Shipped control skills** (`ship/skills/{start,stop,status}`):
   - `/auto-maintainer:start` — invokes `run_tick.py` for tick #1, then schedules
     a recurring ~1-min heartbeat (CronCreate) that re-runs `run_tick.py`; honors
     EXIT's signal — `refire` → one immediate extra tick with **at-most-one-refire
     dedup** (§3.3.3); `idle` → wait; `halt` → stop. **Interval hardcoded to 1 min**
     (#17). Heartbeat is **session-only** for slice 1 (durable + restart-resume
     deferred, #31).
   - `/auto-maintainer:stop` — invokes `stop.py` (latch STOPPED) then cancels the
     heartbeat (CronDelete).
   - `/auto-maintainer:status` — invokes `status.py` and reports the real
     disposition + counter (replaces packaging-config's slice-1 stub).
   Only the heartbeat scheduling (CronCreate/CronDelete) is agent-mediated (no
   plugin-level cron API); every state operation is a script.
5. **Scheduler detection (§3.3.1)** — slice 1 uses the in-session durable
   heartbeat; system-cron detection is stubbed/deferred.
6. **Restart-resume wiring (§3.3.4)** — on restart, the next tick's DRAIN +
   durable state resume the loop; full RESTART_NEEDED→SessionStart auto-resume is
   a follow-up.

> Tool-tier note (spec-rules §1): the deterministic tick logic is a **script**
> (`run_tick.py`); the act of *scheduling the wake* is necessarily
> **agent-mediated** (Claude Code exposes no plugin-level cron API), so the
> `/start` skill body instructs the session scheduler. This seam is inherent to
> the platform constraint in §3.3.1.

## What you'll see (installed plugin)

`/auto-maintainer:start` → tick #1 trace (counter 0→1, disposition RUNNING→refire)
→ every ~1 min the counter advances (**persisted across ticks**) until THRESHOLD →
EXIT idle. `/auto-maintainer:status` shows disposition + counter. `/stop` latches
STOPPED + cancels. Kill mid-tick + restart → DRAIN finishes the owed increment
exactly once (no double-count).

## Current behaviour

Implemented and merged (`tdd_state: test-green`). The tick-runner + `DEMO_WORK`,
the script-backed `status.py`/`stop.py`, and the
`/auto-maintainer:start`/`:stop`/`:status` ship skills are in `src/`/`ship/` with
32 passing tests. Live-validated in an installed plugin session. See
`feature.json` / `docs/ROADMAP.md`.

## Known gaps / deferred

- Configurable interval + route (config feature) — interval hardcoded to 1 min
  (auto-maintainer-framework#17).
- System-cron scheduler backend (§3.3.1) — slice 1 is in-session heartbeat only.
- Real adapter work (PULL/TRIAGE/IMPLEMENT) replacing DEMO_WORK — later features.
- Full RESTART_NEEDED→SessionStart auto-resume (§3.3.4) — follow-up.

## Interfaces (composition)

- Depends on `tick-orchestrator` (run loop + resolve_next), `durable-state`
  (DRAIN/PERSIST/journal/state), `lifecycle-dispositions` (GUARD/EXIT/disposition).
- Declares shippable components under `ship/` for `packaging-config` to assemble
  into `plugins/auto-maintainer/`.
- Owns the `/auto-maintainer:status` skill (script-backed via `status.py`);
  `packaging-config` no longer ships a status stub. Host projects should gitignore
  the runtime dir `.auto-maintainer/`.

## Open questions

- Exact recurring-vs-one-shot scheduling shape and how `/start` records the
  active heartbeat so `/stop` can cancel it deterministically — settle in
  implementation.
