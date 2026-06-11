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

## The real loop (slice 2: real PULL work)

Each tick runs this route through the existing `tick-orchestrator`:

```
GUARD → DRAIN → PULL → PERSIST → EXIT
```

- `GUARD`, `EXIT` from `lifecycle-dispositions`; `DRAIN`, `PERSIST` from
  `durable-state`; `PULL` from `work-intake` (the GitHub-Issues adapter).
- The slice-1 `DEMO_WORK` stub is **retired** — `PULL` is real work: each tick
  fetches the repo's open issues into the `work_items` slot (persisted).
- **Read-and-idle:** with only a read stage (no `IMPLEMENT` yet), `EXIT` goes
  **IDLE** after the pull rather than `refire` — otherwise the loop would
  busy-loop re-pulling the same issues forever. EXIT's refire/idle becomes
  work-driven again once an act stage lands.

## Paths governed

Greenfield. Code under `.../features/scheduling/src/`. Shippable plugin
components (the two skills + the tick-runner entrypoint) live under the feature's
**`ship/`** dir (the convention `packaging-config`'s assembly collects).

## Public surface

1. **Tick-runner script** (`src/run_tick.py`, deterministic, script-tier) —
   assembles `route` = `GUARD→DRAIN→PULL→PERSIST→EXIT` and the `states` map
   (GUARD/EXIT from lifecycle-dispositions, DRAIN/PERSIST from durable-state,
   `PULL` from work-intake), runs `tick_orchestrator.run(...)` over a
   `TickContext` seeded from durable state, prints a tick trace (tick number,
   state path, `work_items` count, resulting disposition), and returns/persists
   the outcome. One invocation = one tick. `PULL`'s issue source is the live `gh`
   CLI in production but **injectable** so tests pass a stub (no network).
2. **PULL integration (read-and-idle)** — the route uses work-intake's `PULL`
   state (writes `work_items`). After PULL + PERSIST, `EXIT` selects **IDLE** (no
   act stage yet), so the heartbeat re-pulls next interval rather than the loop
   busy-firing. The slice-1 `DEMO_WORK` stub is removed.
3. **Control scripts** (deterministic, script-tier — spec-rules §1; the fix for
   #29/#30/#44 where prompt-tier skills drifted/broke):
   - `src/start.py` — prepares a fresh start, then runs tick #1: if the
     disposition is `STOPPED` it clears the latch to a runnable state (start IS
     the §1.2 human resume); if `ABORTED` it REFUSES and tells the user to
     investigate (never silently clears a fault); otherwise proceeds. Reuses
     `run_tick`'s path resolution + the lifecycle API.
   - `src/status.py` — reads the disposition marker + the persisted `work_items`
     count (via `run_tick`'s `resolve_runtime_paths`) and prints the real loop
     status.
   - `src/stop.py` — writes disposition `STOPPED` (via the lifecycle-dispositions
     API) using the same runtime-path resolution. Owns the state write.
   These own ALL state operations so the skills never hand-roll Python.
4. **Shipped control skills** (`ship/skills/{start,stop,status}`):
   - `/auto-maintainer:start` — invokes `start.py` for tick #1 (which first clears
     a latched `STOPPED`, or refuses on `ABORTED`), then schedules a recurring
     ~1-min heartbeat (CronCreate) that re-runs `run_tick.py` (not `start.py` — no
     reset per tick); honors EXIT's signal — `refire` → one immediate extra tick
     with **at-most-one-refire dedup** (§3.3.3); `idle` → wait; `halt` → stop.
     **Interval hardcoded to 1 min** (#17). Heartbeat is **session-only** for
     slice 1 (durable + restart-resume deferred, #31).
   - `/auto-maintainer:stop` — invokes `stop.py` (latch STOPPED) then cancels the
     heartbeat (CronDelete).
   - `/auto-maintainer:status` — invokes `status.py` and reports the real
     disposition + last-pull `work_items` count (replaces packaging-config's
     slice-1 stub).
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

`/auto-maintainer:start` → tick #1 pulls the repo's open issues into `work_items`
(trace shows the count), PERSISTs them, and EXITs **IDLE**. Every ~1 min the
heartbeat re-pulls the current open issues. `/auto-maintainer:status` shows the
disposition + last-pull count. `/stop` latches STOPPED + cancels the heartbeat.

## Current behaviour

Implemented and merged (`tdd_state: test-green`). The tick-runner runs the real
route `GUARD→DRAIN→PULL→PERSIST→EXIT` (read-and-idle); the script-backed
`status.py`/`stop.py` and the `/auto-maintainer:start`/`:stop`/`:status` ship
skills compose work-intake's PULL with the loop core. Live-validated in an
installed plugin session. See `feature.json` / `docs/ROADMAP.md`.

## Known gaps / deferred

- Configurable interval + route (config feature) — interval hardcoded to 1 min
  (auto-maintainer-framework#17).
- System-cron scheduler backend (§3.3.1) — slice 1 is in-session heartbeat only.
- TRIAGE/IMPLEMENT/VERIFY/INTEGRATE — the loop now PULLs (read-and-idle); acting
  on `work_items` lands with later features, at which point EXIT becomes
  work-driven (refire while actionable work remains).
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
