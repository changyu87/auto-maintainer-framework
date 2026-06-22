---
name: status
description: Report the auto-maintainer tick loop's real status — current disposition and the last pull's persisted work_items count. Use this whenever the user runs /auto-maintainer:status, or asks how the maintainer loop is doing, whether it's running/idle/stopped, how many work items the last tick pulled, or to check on the heartbeat / background ticking. This reads the real on-disk loop state, not a guess.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the control surface is replaced.
---

# auto-maintainer status

Report the real status of the in-session maintainer tick loop: its current
disposition (RUNNING / IDLE / STOPPED / ABORTED / RESTART_NEEDED) and the last
pull's persisted work_items count.

The status is read from durable on-disk state by a deterministic script, so the
answer is the real loop state rather than something inferred from the
conversation. If the loop was never started, the script reports a sane "not
started" view (disposition IDLE, work_items 0) without creating any runtime files.

## Steps

1. Run the status script:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/status.py
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the script resolves regardless of the session's working directory (skills
   run with cwd = the user's project). The script reads the disposition marker
   and the persisted work_items count and prints one status line.

2. Report the line it prints to the user verbatim (disposition, work_items, the
   runtime dir it read from, and `awaiting`). Do not hand-roll any Python or
   invent a status: `status.py` owns all state reads, and this skill only runs it
   and relays the output.

The `awaiting` field distinguishes "actively working" from "paused mid-tick":
while a tick is paused at an agent-state awaiting a subagent's output, the
disposition still reads RUNNING, so `awaiting=<state>` names that paused state
(e.g. `awaiting=REVIEW`); when no tick is paused it reads `awaiting=none`.
