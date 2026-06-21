---
name: start
description: Start (or resume) the auto-maintainer's in-session tick loop. Use this whenever the user runs /auto-maintainer:start, or asks to start, launch, run, kick off, restart, or resume the maintainer loop / heartbeat / background ticking — including resuming after a prior /auto-maintainer:stop. It clears a latched STOPPED disposition, runs the first tick now through the executor (dispatching any agent-states), and schedules a recurring prompt-heartbeat at the configured interval (heartbeat.interval_minutes, default 3) that keeps ticking until stopped; a latched ABORTED fault is refused, not cleared.
version: 0.3.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the route becomes config-driven via the /auto-maintainer:route CLI (Phase 4).
---

# auto-maintainer start

Start the in-session loop that drives the maintainer tick. A Claude Code plugin
cannot install its own clock, so the heartbeat is session-mediated: this skill
clears any stop latch, runs one tick now, and then asks the session to wake at
the configured cadence to run the next tick. The cadence is config-driven — the
deterministic starter reports it, so the user can change the interval without
editing this skill.

Crucially, a tick can contain **agent-states** (states that need a model, e.g. a
TRIAGE/IMPLEMENT subagent). A bare script can't dispatch the `Agent` tool, so
ticks run through the **executor skill `/auto-maintainer:tick`**, which steps the
deterministic runner and presses the `Agent` button wherever the runner pauses.
This skill therefore drives the first tick *through the executor*, and schedules
a heartbeat that fires the executor each interval — not a bare script.

## Why the first tick clears the latch first

A prior `/auto-maintainer:stop` LATCHES the loop `STOPPED`: every tick's GUARD
then reads `STOPPED` and halts, so a tick would do nothing. Starting the loop IS
the §1.2 human resume, so the latch must be cleared before the first tick. The
deterministic `${CLAUDE_PLUGIN_ROOT}/lib/start.py` owns that decision
(spec-rules §1) so this skill never hand-rolls Python to clear it:

- `STOPPED` — start clears the latch to a runnable state (IDLE).
- `ABORTED` — a fault is latched. `start.py --clear-only` REFUSES, exits
  non-zero, clears nothing. Relay its message and STOP — do not run a tick, do
  not schedule the heartbeat. A fault must be investigated, never silently
  cleared.
- otherwise (RUNNING / IDLE / absent) — nothing to clear; proceed.

The latch is cleared ONCE at start; the recurring heartbeat does NOT re-clear it
(re-clearing each interval would defeat a `/stop` that lands between heartbeats).

## Why the heartbeat is durable across sessions

The recurring heartbeat is session-mediated, so it ends when the Claude session
ends. To make it DURABLE (DESIGN §3.3.2 heartbeat bootstrap), `start.py` records
a durable **loop-intent** marker when it clears the latch — "the human wants the
loop ticking" — which survives the session ending. On the NEXT session the
plugin's `SessionStart` auto-resume hook reads that marker and, unless the loop
is latched (`STOPPED`/`ABORTED`) or owes a restart, asks the session to re-run
this `/start` skill to re-arm the heartbeat — at most once per session
(cross-session dedup). `/auto-maintainer:stop` clears the loop-intent, so a
stopped loop does NOT auto-resume. You do not need to do anything for this:
`start.py` records the intent for you in step 1. (Note: `start.py` does NOT clear
the resume-dedup, so a second `SessionStart` in the SAME session — which fires on
startup / resume / `/clear` / compact — can never re-arm a duplicate heartbeat.)

## Steps

1. Clear the latch (no tick yet) with the deterministic starter:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/start.py --clear-only
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the path resolves regardless of the session's working directory. Print the
   status line it emits. If it exits non-zero on a latched `ABORTED`, relay its
   message and STOP — do not proceed to steps 2, 3, or 4.

2. Run the first tick **now, through the executor**: invoke the
   `/auto-maintainer:tick` skill. That skill steps the runner and, whenever the
   runner pauses at an agent-state, dispatches the requested subagent(s) and
   resumes — so the first tick completes whether the route is pure-script or
   contains agent-states. Report the tick's final trace.

3. Read the configured heartbeat interval (in minutes) from the deterministic
   starter — the cadence lives in the central config (`heartbeat.interval_minutes`,
   default 3), and `start.py` owns reading it so this skill never hard-codes a
   value:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/start.py --print-interval
   ```

   It prints a single integer (the interval in minutes) on stdout. Use that
   value as the heartbeat cadence in the next step.

4. Schedule the recurring heartbeat as a **prompt** (so the session is present to
   fulfill any agent dispatches), firing the executor every interval-minutes
   (the value from step 3):

   schedule a recurring job at that interval whose prompt is:
   `"Run one auto-maintainer tick now by invoking the /auto-maintainer:tick skill."`

   The interval is config-driven (`heartbeat.interval_minutes`, #17 resolved), so
   honor whatever `--print-interval` reported rather than assuming a fixed value.
   Record the scheduled job so `/auto-maintainer:stop` can cancel it
   deterministically. Use a recurring schedule; a bare script command will NOT do
   here, because a script-launched tick can't fulfill agent-state dispatches — the
   heartbeat must wake the session.

## Rules

- Drive every tick through `/auto-maintainer:tick` (the executor) — both the
  first tick and each heartbeat tick. Never advance the loop by hand-rolling
  Python or by calling `run_tick.py` yourself; the executor owns the
  step/dispatch/resume loop, and `start.py --clear-only` owns the one-time
  latch-clear. This skill clears the latch, runs the first tick via the
  executor, and schedules the wake — nothing more.
- If `start.py --clear-only` refused (ABORTED), do not schedule a heartbeat.
