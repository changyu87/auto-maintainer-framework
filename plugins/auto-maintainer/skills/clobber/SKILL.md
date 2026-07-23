---
name: clobber
description: Reset the auto-maintainer tick loop to a clean start by clearing its runtime state while preserving user config. Use this whenever the user runs /auto-maintainer:clobber, or asks to reset, wipe, clear, or start-fresh the maintainer loop — e.g. the loop is stuck, its durable state is corrupt, or a plugin upgrade left an incompatible checkpoint. This deletes durable-state, the disposition/lock markers, the event log, the tick journal, the dispatch-out dir, and the heartbeat markers, but NEVER touches config.json / route.json / adapter-map.json.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API) or when the control surface / runtime layout is replaced.
---

# auto-maintainer clobber

Reset the maintainer loop to a clean start. This clears the loop's RUNTIME STATE
(durable ledgers, budget window, read products, the tick checkpoint, the
disposition/lock markers, the event log, the tick journal, the paused-dispatch
`dispatch-out/` dir, and the heartbeat markers) while PRESERVING the user's
config (`config.json`, `route.json`, `adapter-map.json`, and any `*.bak` /
`*.migrated` backups). Use it when the loop is stuck, its durable state is
corrupt, or a plugin upgrade left an incompatible checkpoint that a normal tick
cannot recover from.

This is a DESTRUCTIVE reset: the cleared state cannot be recovered. The config is
kept, so the loop's wiring survives — but every ledger, counter, and in-flight
checkpoint is gone.

## Steps

1. CONFIRM with the user before doing anything. Because the reset is destructive
   and irreversible, do not proceed on a bare command — restate what will be
   cleared vs preserved and ask the user to confirm. If the loop may be running,
   remind them to run `/auto-maintainer:stop` FIRST: clobbering a live loop
   removes the disposition/lock markers and heartbeat markers out from under a
   tick in flight, which can leave a half-run tick's effects unreconciled. Once
   the loop is stopped and the user has confirmed, continue.

2. Preview what would be cleared with a dry-run (deletes nothing):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/clobber.py
   ```

   `${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root,
   so the script resolves regardless of the session's working directory (skills
   run with cwd = the user's project). Without `--yes` the script is a DRY-RUN:
   it prints the machine-first `{"removed": [...], "preserved": [...]}` summary of
   what it WOULD remove and what it WOULD keep, and deletes nothing. Relay this
   preview so the user sees exactly what the reset will touch.

3. On the user's confirmation, perform the reset:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/clobber.py --yes
   ```

   `--yes` actually deletes the runtime-state artifacts. The script resolves the
   runtime dir the same way the tick runner does, removes only runtime state,
   never touches config, never creates the runtime dir (an absent dir is a
   nothing-to-do no-op), and is idempotent (a missing artifact is skipped, not an
   error). Do not hand-roll any Python or delete files yourself — `clobber.py`
   owns all deletions; this skill only runs it. Relay the `{removed, preserved}`
   summary it prints.

4. Confirm to the user that the loop was reset: the runtime state is cleared, the
   config is intact, and the loop will start fresh on the next
   `/auto-maintainer:start`.
