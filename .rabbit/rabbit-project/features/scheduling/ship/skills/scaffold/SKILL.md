---
name: scaffold
description: Scaffold a bring-your-own (BYO) adapter for a NEW auto-maintainer route port. Use this whenever the user runs /auto-maintainer:scaffold, or asks to author/create/scaffold a new adapter, add a custom step/stage/state to the tick loop, generate an adapter skeleton/template, or wire their own script into the loop as a new port. It emits a skeleton Python adapter conforming to the factory convention, wires it into the adapter-map + route, and validates the whole wiring before saving. To swap an EXISTING port's implementation (e.g. wire an agent to TRIAGE/IMPLEMENT) use the adapter-map skill instead; this skill authors NEW ports.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when scheduling moves to a different clock source (e.g. a native plugin cron API), or when a native rabbit/plugin config system subsumes the wiring-config CLIs.
---

# auto-maintainer scaffold

Author a **bring-your-own adapter** for a NEW route port. The maintainer loop is
ports-and-adapters: each route state (port) resolves to an adapter — a Python
module exposing `factory(runtime) -> (StateManifest, run_callable)` where
`run_callable` has the signature `run(TickContext) -> StateResult`. This skill
generates a conforming skeleton, wires it into the project-local route +
adapter-map, and **validates the whole wiring before saving** — so adding a BYO
adapter is a CHECKED operation, not hand-rolled JSON + Python.

## New port vs existing port

- **This skill** authors a **NEW** port — a custom step inserted into the loop
  (e.g. an `ENRICH` step between PULL and PERSIST). It emits the adapter file AND
  inserts the new state into the route.
- To **swap an existing** port's implementation (point GUARD/PULL/TRIAGE/… at a
  different script, or wire an AI agent into TRIAGE/IMPLEMENT), use the
  **adapter-map** skill (`/auto-maintainer:adapter-map`) instead.

## All authoring goes through the script

`adapter_scaffold.py` owns emit-wire-VALIDATE-save. It writes the skeleton to
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/adapters/<port>.py`, sets the adapter-map
entry `port -> "<module>:factory"`, inserts the state into the route, then
resolves + validates the resulting wiring (the SAME load-time validator the loop
runs — signal-validity + data-readiness + anchor invariants). If anything fails,
the operation is **rejected and fully rolled back** — no partial config is left.
Do not hand-write the adapter or the config JSON; the script is the single source
of truth.

`${CLAUDE_PLUGIN_ROOT}` is set by Claude Code to the installed plugin's root, so
the script resolves regardless of the session's working directory.

## Steps

1. Preview the skeleton for a port WITHOUT writing anything, so the user can see
   the shape they will fill in:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/adapter_scaffold.py --emit --port ENRICH
   ```

2. To author the new port — take the port name and where it goes in the route
   (which existing state it follows with `--after` and which it precedes with
   `--before`) — run `new`. The script emits the adapter, wires the map + route,
   validates, and saves:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/adapter_scaffold.py new --port ENRICH --after PULL --before PERSIST
   ```

3. If the script rejects the request, report the rejection reason verbatim and
   help the user correct it — do NOT retry blindly. A rejection means the port is
   already a route state, the insertion point is unknown, or the resulting wiring
   would not validate.

4. On success, point the user at the emitted adapter file
   (`.auto-maintainer/adapters/<port>.py`) and tell them to edit its `# TODO`:
   the skeleton is a deterministic no-op that writes its product slot and emits
   `OK`. As they add slots they read, those must be written by a predecessor on
   every path (the validator enforces this); re-run nothing — the file is theirs
   to edit, and the loop re-validates the wiring at load.

This skill never dispatches a subagent — it is a guided, deterministic authoring
tool.
