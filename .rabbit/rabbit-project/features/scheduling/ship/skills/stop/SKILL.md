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

1. Set the loop disposition to `STOPPED` via lifecycle-dispositions
   (`write_disposition(runtime_dir, Disposition.STOPPED)`). This is the durable
   latch: the next tick's GUARD reads `STOPPED`, halts before doing any work,
   and the counter does not advance.

2. Cancel the recurring ~1-minute heartbeat that `/auto-maintainer:start`
   scheduled, so no further wake fires.

3. Confirm to the user that the loop is stopped and will not tick until they
   run `/auto-maintainer:start` again.

The `STOPPED` latch and the heartbeat cancellation are independent safeguards:
the latch freezes the loop even if a stray wake fires, and the cancellation
removes the wake. Both are applied so a stop is durable across a restart.
