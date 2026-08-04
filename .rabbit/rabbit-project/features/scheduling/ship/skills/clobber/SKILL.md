---
name: clobber
description: Reset the auto-maintainer tick loop to a clean start by clearing its runtime state while preserving user config. Use this whenever the user runs /auto-maintainer:clobber, or asks to reset, wipe, clear, or start-fresh the maintainer loop — e.g. the loop is stuck, its durable state is corrupt, or a plugin upgrade left an incompatible checkpoint. By default it shows a WOULD-DELETE preview and asks the user to type the verbatim word `yes` before deleting anything; pass --no-dry-run to delete immediately. It clears durable-state, the disposition/lock markers, the event log, the tick journal, the dispatch-out dir, and the heartbeat markers, but NEVER touches config.json / route.json / adapter-map.json.
version: 0.3.0
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
checkpoint is gone. Because it is irreversible, the default flow shows the user
exactly what will be deleted and waits for an explicit `yes` before touching
anything.

`clobber.py` owns ALL the logic: it computes the delete list, performs the
deletion, and emits a machine-first JSON payload. This skill never deletes files
itself and never hand-rolls Python — it runs the script and renders its payload.
`${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root, so
the script resolves regardless of the session's working directory (skills run
with cwd = the user's project).

## The machine-first payload

Both modes print one JSON object:

```
{"mode": "preview"|"applied",
 "artifacts": [{"name": "...", "path": "...", "exists": true, "action": "..."}],
 "preserved": ["config.json", "route.json", ...],
 "loop_intent_present": false}
```

- `mode` — `preview` (nothing was deleted) or `applied` (the deletion ran).
- `artifacts` — one record per runtime-state artifact. `action` is
  `would-remove` (preview + present), `removed` (apply + present), or `absent`
  (not present). Render the present ones as a table.
- `preserved` — the config files kept untouched.
- `loop_intent_present` — see step 2; it drives the `/stop`-first advice.

## Default flow (preview, then a verbatim `yes`)

Use this whenever the user runs `/auto-maintainer:clobber` without asking for the
immediate path.

1. Run the PREVIEW (no flag — deletes NOTHING):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/clobber.py
   ```

   Render the payload for the user as two tables: a WOULD-DELETE table of the
   `artifacts` whose `action` is `would-remove` (name + path), and a PRESERVED
   table of `preserved`. This is what the reset will and will not touch.

2. Decide the `/stop`-first recommendation from `loop_intent_present` in that
   payload — NOT from the disposition. `loop_intent_present` is `heartbeat`'s
   durable loop-intent marker, set ONLY by `/auto-maintainer:start` and never by
   `/auto-maintainer:tick`. It is the real signal that a live *scheduled* loop is
   running:
   - **`loop_intent_present` is `true`** → a `/start`-ed loop is live. Recommend
     running `/auto-maintainer:stop` FIRST: clobbering a live loop removes the
     disposition/lock and heartbeat markers out from under a scheduled tick,
     which can leave a half-run tick's effects unreconciled. Wait for the stop
     before proceeding.
   - **`loop_intent_present` is `false`** → no live scheduled loop (e.g. a
     `/tick`-only or paused-tick session, even if a `disposition` of `RUNNING`
     and a checkpoint are present). Do NOT tell the user to `/stop` first —
     `/stop` would only latch a `STOPPED` that clobber wipes anyway, and clobber
     already deletes the checkpoint/disposition/lock/heartbeat markers.

3. Ask the user to confirm by typing the verbatim word `yes`. This conversational
   gate is owned by the skill (there is no confirmation flag): because the reset
   is irreversible, only an explicit `yes` should trigger it. Any other reply —
   "y", "sure", a question, silence — ABORTS: nothing is deleted, and you tell
   the user the loop was left untouched.

4. ONLY on a verbatim `yes` (and, if `loop_intent_present` was true, after the
   loop is stopped), run the APPLY:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/clobber.py --apply
   ```

   `--apply` is the internal flag that actually deletes the runtime-state
   artifacts. Render the resulting payload as a DELETED table (`artifacts` with
   `action` `removed`) plus the PRESERVED table, then confirm to the user that
   the runtime state is cleared, the config is intact, and the loop will start
   fresh on the next `/auto-maintainer:start`.

## Immediate flow (`--no-dry-run`)

If the user passes `--no-dry-run` (they already know what clobber does and want
no confirmation), skip the preview and the `yes` gate entirely and delete right
away by invoking the script's apply flag directly:

```
python3 ${CLAUDE_PLUGIN_ROOT}/lib/clobber.py --apply
```

Render the resulting DELETED / PRESERVED tables and confirm the reset, exactly as
in step 4 above. (`--no-dry-run` is the user-facing name for this no-gate path;
the script itself only knows the `--apply` flag.)

## Notes

- The script resolves the runtime dir the same way the tick runner does, removes
  only runtime state, never touches config, never creates the runtime dir (an
  absent dir is a nothing-to-do no-op), and is idempotent (a missing artifact
  reads `absent`, not an error). Re-running after a reset is safe.
- Do not hand-roll any Python or delete files yourself — `clobber.py` owns all
  deletions; this skill only runs it and renders its payload.
