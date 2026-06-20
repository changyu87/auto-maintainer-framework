---
name: adapter-map
description: View and edit the auto-maintainer tick loop's ADAPTER MAP (which adapter implements each route port — GUARD, DRAIN, PULL, TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, INTEGRATE, CLEANUP, PERSIST, EXIT). Use this whenever the user runs /auto-maintainer:adapter-map, or asks to wire a port to a subagent / agent, swap a port's implementation, point a port at a custom script factory, hook an AI agent (like a triager or doer) into the loop, or inspect which adapter each port currently uses. To wire an agent to a known port the user only needs to give the subagent type; the rest is filled in automatically. The default map is recommended for most projects. Every edit is validated before it is saved.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API), or when a native rabbit/plugin config system subsumes the wiring-config CLIs.
---

# auto-maintainer adapter-map

View and safely edit the maintainer loop's **adapter map** — the table that says
which adapter implements each route port. The shipped default map wires every known
port (GUARD, DRAIN, PULL, TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, INTEGRATE,
CLEANUP, PERSIST, EXIT) to its built-in script factory, so the default route runs
with no configuration.

The map is **data**: a project-local
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/adapter-map.json` overrides the default.
Editing it lets a project swap a port's implementation — most importantly, to wire
an **AI agent** (a subagent) into a port like TRIAGE or IMPLEMENT instead of the
deterministic built-in.

## Recommend the default first

The **default map is the right choice for most projects** — every port already
resolves to a working built-in adapter. Recommend keeping it unless the user wants
to hook an agent into the loop or point a port at a custom implementation. Wiring an
agent is what changes the loop from deterministic to AI-acting, so make sure that is
what the user intends.

## All editing goes through the script

`adapter_map_config.py` owns the load-modify-VALIDATE-save. For a **known
agent-capable port** the user supplies ONLY the subagent type; the script fills in
the rest of the agent entry — the slot it writes, its cardinality, its effect (for
acting ports), and a concrete output example — from the per-port templates the
scheduling feature owns. It then **validates** the resulting map (resolving it,
which deep-validates the agent entry) BEFORE writing; an invalid entry is rejected
and the file is left untouched. Do not hand-write `adapter-map.json` or hand-roll
any Python — the script is the single source of truth, and its templates are what
make a bare subagent type enough to produce a valid entry.

`${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root, so
the script resolves regardless of the session's working directory.

## Steps

1. Show the user the active map and where it loads from (default vs an override
   file):

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/adapter_map_config.py --show
   ```

2. To wire an AI agent to a known port, take the port and the subagent type from the
   user and run set-agent. The script fills the entry from the port's template,
   validates, and writes:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/adapter_map_config.py set-agent --port TRIAGE --subagent-type auto-maintainer-echo
   ```

3. To point a port at a custom script factory (a `module:factory` address), use
   set-script:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/adapter_map_config.py set-script --port PULL --address run_tick:make_pull
   ```

4. If the script rejects the edit, report the rejection reason verbatim and help the
   user correct it — do NOT retry blindly. A rejection means the entry would not
   resolve or validate (e.g. an unknown port that needs more than a subagent type,
   or an output example that is a schema rather than a concrete value).

5. On success, confirm the new wiring and offer to `--show` the map so the user can
   see the result. If the user wired an agent to a port that the current route does
   not yet include, remind them to also enable that state with
   `/auto-maintainer:route`.

This skill never dispatches a subagent — it is a guided, deterministic editor.
