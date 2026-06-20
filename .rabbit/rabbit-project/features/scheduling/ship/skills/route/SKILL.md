---
name: route
description: View and edit the auto-maintainer tick loop's ROUTE (the ordered state graph GUARD->DRAIN->PULL->...->PERSIST->EXIT). Use this whenever the user runs /auto-maintainer:route, or asks to change the loop's route, add/insert/remove a state (e.g. enable TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, INTEGRATE, CLEANUP, or the close-the-loop chain), reorder states, change which state runs next on a signal, add/remove an edge, or just inspect the current route and where it loads from. The default route is recommended for most projects; only change it when the user wants to enable acting stages. Every edit is validated before it is saved.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API), or when a native rabbit/plugin config system subsumes the wiring-config CLIs.
---

# auto-maintainer route

View and safely edit the maintainer loop's **route** — the ordered state graph the
tick runner walks each tick. The shipped default route is the read-and-idle spine:

```
GUARD -> DRAIN -> PULL -> PERSIST -> EXIT
```

The route is **data**: a project-local
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/route.json` overrides the default. Editing
it lets a project enable acting stages (TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY,
INTEGRATE, CLEANUP) with no code change — every known port is already wired in the
adapter map, so a route edit alone activates it.

## Recommend the default first

The **default route is the right choice for most projects** — it pulls open issues
and idles, which is the safe read-only baseline. Recommend keeping it unless the
user has a concrete reason to change it (e.g. they want the loop to start triaging
or implementing). When they do want a change, make the smallest edit that achieves
it and let the validator confirm it is sound.

## All editing goes through the script

`route_config.py` owns the load-modify-VALIDATE-save. It applies the edit to the
route, then **validates it by building the loop** (resolve + signals +
data-readiness + anchor invariants) BEFORE writing. A failing edit is rejected and
the file is left untouched, so an invalid route can never be saved. Do not
hand-write `route.json` or hand-roll any Python — the script is the single source
of truth, and its validation is what keeps a broken route from reaching the loop.

`${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root, so
the script resolves regardless of the session's working directory.

## Steps

1. Show the user the active route and where it loads from (default vs an override
   file):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py --show
   ```

2. If the user only wants to understand the editable structure, emit the
   machine-first catalog of states, edges, and available operations:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py --describe
   ```

3. Apply the user's requested change with the matching subcommand. Each subcommand
   validates before writing; relay the script's `OK:` or `REJECTED:` line verbatim.

   - Insert a state between two existing states (the most common edit — e.g.
     enable TRIAGE between PULL and PERSIST):

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py insert-state --state TRIAGE --after PULL --before PERSIST
     ```

   - Remove a state:

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py remove-state --state TRIAGE
     ```

   - Add or replace an edge (which state runs next on a given signal):

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py add-edge --state TRIAGE --signal OK --next PRIORITIZE
     ```

   - Remove an edge:

     ```
     python3 ${CLAUDE_PLUGIN_ROOT}/lib/route_config.py remove-edge --state TRIAGE --signal OK
     ```

4. If the script rejects the edit, report the rejection reason to the user and do
   NOT retry blindly — the rejection names what made the route invalid (e.g. a
   state reads a slot no predecessor wrote). Help the user pick a valid placement,
   then re-run the command.

5. On success, confirm the new route and offer to `--show` it so the user can see
   the result.

This skill never dispatches a subagent — it is a guided, deterministic editor.
