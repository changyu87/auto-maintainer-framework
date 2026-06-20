---
name: stop
description: Stop the auto-maintainer's tick loop. Use this whenever the user runs /auto-maintainer:stop, or asks to stop, halt, pause, cancel, or shut down the maintainer loop / heartbeat / background ticking. This latches the loop STOPPED and cancels the scheduled heartbeat so no further ticks run.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the tick interval/route become config-driven and this slice's hardcoded 1-min heartbeat is removed.
---

# auto-maintainer stop

Stop the maintainer tick loop and cancel its in-session heartbeat.

Stopping is a human action that LATCHES: the loop stays stopped until a human
starts it again. It does not auto-resume on the next heartbeat the way an idle
loop does.

## Steps

1. Latch the loop STOPPED by running the stop script:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/stop.py
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the script resolves regardless of the session's working directory. The
   script writes the durable `STOPPED` disposition marker via the
   lifecycle-dispositions API AND clears the durable **loop-intent** marker: the
   next tick's GUARD reads `STOPPED`, halts before doing any work, and no issues
   are pulled, and the next session's `SessionStart` auto-resume hook finds no
   loop-intent so it does NOT re-arm the heartbeat. Do not hand-roll any
   Python or write the markers yourself — `stop.py` owns the state writes; this
   skill only runs it. Print the confirmation line it emits.

2. Cancel the recurring heartbeat that `/auto-maintainer:start` scheduled
   (CronDelete), so no further wake fires. This is the one inherently
   agent-mediated step: Claude Code exposes no plugin-level cron API, so the
   session scheduler cancels the wake.

3. Confirm to the user that the loop is stopped and will not tick until they
   run `/auto-maintainer:start` again.

Three safeguards make a stop durable: the `STOPPED` latch freezes the loop even
if a stray wake fires, the heartbeat cancellation removes the current session's
wake, and clearing the durable loop-intent stops the next session's
`SessionStart` hook from auto-resuming. All three are applied so a stop holds
both within the session and across a restart.
