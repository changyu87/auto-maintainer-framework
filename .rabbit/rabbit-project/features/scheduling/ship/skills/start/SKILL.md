---
name: start
description: Start (or resume) the auto-maintainer's in-session tick loop. Use this whenever the user runs /auto-maintainer:start, or asks to start, launch, run, kick off, restart, or resume the maintainer loop / heartbeat / background ticking — including resuming after a prior /auto-maintainer:stop. This clears a latched STOPPED disposition, runs the first tick now, and schedules a recurring ~1-minute heartbeat that keeps ticking until stopped; a latched ABORTED fault is refused, not cleared.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoded 1-min heartbeat is removed.
---

# auto-maintainer start

Start the in-session durable heartbeat that drives the maintainer tick loop.

A Claude Code plugin cannot install its own clock, so the heartbeat is
session-mediated: this skill runs one tick immediately and then asks the
session to wake roughly every minute to run the next tick.

The first tick goes through the deterministic starter shipped at
`${CLAUDE_PLUGIN_ROOT}/lib/start.py`, which prepares a fresh start before
ticking. Each recurring heartbeat tick is one invocation of the deterministic
tick-runner shipped at `${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py`.

## Why the first tick is special

A prior `/auto-maintainer:stop` LATCHES the loop `STOPPED`: every tick's GUARD
then reads `STOPPED` and halts, so a plain re-run of the tick-runner would do
nothing. Starting the loop IS the human resume, so the first tick must clear
that latch before it can run. `start.py` owns that decision deterministically
(spec-rules §1) so this skill never hand-rolls Python to clear it:

- `STOPPED` — start clears the latch to a runnable state and then ticks.
- `ABORTED` — a fault is latched. `start.py` REFUSES, exits non-zero, and runs
  no tick. Surface its message to the user and stop; do not retry or clear the
  fault yourself. A fault must be investigated, never silently cleared.
- otherwise — start ticks straight away.

The recurring heartbeat keeps using `run_tick.py`, not `start.py`: the latch is
cleared once at start, and re-clearing it every minute would defeat a `/stop`
that lands between heartbeats.

## What a tick does

`run_tick.py` runs the real lifecycle route
`GUARD -> DRAIN -> PULL -> PERSIST -> EXIT` through tick-orchestrator:
GUARD takes the single-writer mutex, DRAIN finishes any owed work from a
truncated prior tick, PULL fetches the repo's open issues into the `work_items`
slot, PERSIST flushes durable state (including the pulled count), and EXIT
selects the next disposition and releases the mutex. The pulled `work_items`
count is persisted across ticks.

Because PULL is a read with no act stage yet, EXIT goes **idle** after the pull
rather than refiring (read-and-idle): re-firing would just busy-loop re-pulling
the same issues, so the heartbeat re-pulls on the next interval instead. EXIT's
refire/idle becomes work-driven again once an act stage lands.

## Steps

1. Run the first tick now through the starter:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/start.py
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the script resolves regardless of the session's working directory (skills
   run with cwd = the user's project, where a bare `src/` path would not exist).
   `start.py` first clears a latched `STOPPED` (or refuses on `ABORTED`), then
   runs tick #1 by calling the tick-runner — so the tick logic still lives in
   one place. Print the trace it emits (tick path, work_items count, resulting
   disposition). If it exits non-zero on a latched `ABORTED`, relay its message
   and stop — do not schedule the heartbeat.

2. Schedule a recurring heartbeat that re-runs
   `python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py` every
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

Do not advance the loop by hand-rolling Python or by any path other than these
two scripts: `start.py` for the first tick (which prepares the fresh start) and
`run_tick.py` for every heartbeat tick. The tick logic is deterministic and
lives entirely in `run_tick.py`; `start.py` only adds the one-time latch-clear.
This skill runs them and schedules the wake — nothing more.
