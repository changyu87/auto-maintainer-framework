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
one invocation of the deterministic tick-runner shipped at
`${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py`.

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

1. Run the first tick now:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the tick-runner resolves regardless of the session's working directory
   (skills run with cwd = the user's project, where a bare `src/` path would not
   exist). Print the tick trace it emits (tick path, work_items count, resulting
   disposition).

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

Do not advance the loop by any path other than
`${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py`: the tick logic is deterministic and
lives entirely in that script. This skill only runs it and schedules the wake.
