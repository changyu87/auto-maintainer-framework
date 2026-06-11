---
name: start
description: Start the auto-maintainer's in-session tick loop. Use this whenever the user runs /auto-maintainer:start, or asks to start, launch, run, kick off, or resume the maintainer loop / heartbeat / background ticking. This runs the first tick now and schedules a recurring ~1-minute heartbeat that keeps ticking until stopped.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoded 1-min heartbeat is removed.
---

# auto-maintainer start

Start the in-session durable heartbeat that drives the maintainer tick loop.

A Claude Code plugin cannot install its own clock, so the heartbeat is
session-mediated: this skill runs one tick immediately and then asks the
session to wake roughly every minute to run the next tick. One tick =
one invocation of the deterministic tick-runner `src/run_tick.py`.

## What a tick does

`run_tick.py` runs the real lifecycle route
`GUARD -> DRAIN -> DEMO_WORK -> PERSIST -> EXIT` through tick-orchestrator:
GUARD takes the single-writer mutex, DRAIN finishes any owed work from a
truncated prior tick, DEMO_WORK does one unit of (stubbed) work and advances
the persisted counter, PERSIST flushes durable state, and EXIT selects the next
disposition and releases the mutex. The counter is persisted across ticks.

## Steps

1. Run the first tick now:

   ```
   python3 src/run_tick.py
   ```

   Print the tick trace it emits (tick path, counter, resulting disposition).

2. Schedule a recurring heartbeat that re-runs `python3 src/run_tick.py` every
   ~1 minute. The interval is hardcoded to 1 minute for this slice
   (configurability is deferred — see auto-maintainer-framework#17). Record the
   scheduled heartbeat so `/auto-maintainer:stop` can cancel it
   deterministically.

3. Honor the EXIT disposition signal each tick:
   - `refire` — the loop has more work; an immediate next tick may run, with
     at-most-one-refire dedup so a single refire never fans out into multiple
     concurrent ticks.
   - `idle` — the queue is empty; wait for the next heartbeat.
   - `halt` — a latched STOPPED/ABORTED disposition; stop ticking.

Do not advance the loop by any path other than `run_tick.py`: the tick logic is
deterministic and lives entirely in that script. This skill only runs it and
schedules the wake.
