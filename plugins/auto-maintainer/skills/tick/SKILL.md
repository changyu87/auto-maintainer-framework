---
name: tick
description: Run exactly one auto-maintainer tick, including any subagent (agent-state) dispatches. Use this whenever the user runs /auto-maintainer:tick, asks to run/execute one tick or step the maintainer loop once, or when the recurring heartbeat prompt asks for a tick. It drives the deterministic tick-runner and, whenever the runner pauses at an agent-state, dispatches the requested subagent(s) and feeds their output back until the tick completes.
version: 0.1.1
owner: rabbit-workflow team
deprecation_criterion: Superseded when Claude Code can dispatch subagents from within a script (removing the need for a session-mediated executor), or when the tick CLI's --step/--resume protocol reaches a breaking major version.
---

# auto-maintainer tick (executor)

Run one tick of the maintainer loop. The tick is **script-driven**: the
deterministic tick-runner shipped at `${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py`
walks the route and does all control flow. Most states are pure script and need
nothing from you. An **agent-state** is the exception: a script cannot call the
`Agent` tool, so the runner **pauses** and hands you a rendered prompt; your only
job is to **press the `Agent` button** for it and feed the result back. You
decide nothing about the route — the runner does.

This skill therefore runs a small loop: step the runner, and each time it pauses,
dispatch the named subagent(s) and resume it, until the tick is done.

## The runner's JSON protocol

`run_tick.py --step` (and `--resume`) print a single JSON object to stdout:

- `{"status":"done","signal":"<idle|halt|...>","trace":"<one-line trace>"}` —
  the tick finished. Print the `trace` and stop.
- `{"status":"paused","state":"<name>","dispatches":[ {"subagent_type","prompt",
  "writes","schema_ref","signal_rule","cardinality","item"?}, ... ]}` — the
  runner is waiting at an agent-state. Dispatch each entry, then resume.
- `{"status":"invalid_output","state":"<name>","reason":"<why>"}` — the last
  dispatch's output did not match the slot schema. Re-dispatch that state (see
  Steps); after 2 failed attempts, stop and report the reason.

## Steps

1. Step the runner:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --step
   ```

   Parse the single JSON object it prints to stdout.

2. If `status` is `"done"`: print the `trace` and stop — the tick is complete.

3. If `status` is `"paused"`: for **each** entry in `dispatches` (in order),
   dispatch the subagent with that entry's exact rendered prompt:

   `Agent(subagent_type=<entry.subagent_type>, prompt=<entry.prompt>)`

   Collect each subagent's final message as a string, in dispatch order.

4. Write the collected outputs to the resume file, then resume the runner
   pointing at it.

   Use the **`Write` tool** (not a hand-rolled `python -c`) to write a JSON
   array of strings — one entry per dispatch, in order, each the subagent's
   **full final message verbatim** — to the **absolute** path
   `${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json`. This matters:
   subagent outputs can be large and contain code fences, quotes, and newlines;
   the `Write` tool with the absolute path serializes them faithfully, whereas an
   improvised `python -c` tends to truncate or mis-escape, and a relative path
   resolves against the wrong directory. Do **not** truncate, summarize, or
   re-format any subagent output — the runner validates it against the slot
   schema and will reject a mangled payload.

   Then resume:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --resume ${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json
   ```

   Parse the JSON it prints and go back to step 2 with the new result.

5. If `status` is `"invalid_output"`: re-dispatch the same state — go back to
   step 1 (the runner re-emits the same pause from its checkpoint, since the
   result was rejected and the checkpoint is intact). Allow at most **2**
   re-dispatch attempts for a given state; if it still fails, print the `reason`
   and stop (do not force-advance past a state whose output won't validate).

## Rules

- Dispatch a subagent ONLY for the entries the runner hands you, with the prompt
  the runner rendered — do not author or alter the prompt, and do not invoke any
  other subagent. The prompt is the machine-first invocation envelope; pass it
  through verbatim.
- A dispatched subagent runs one level below this session and must not itself
  dispatch another agent (the 2-level nesting cap).
- Never advance the tick by hand-rolling Python or by any path other than
  `run_tick.py --step` / `--resume`. All tick logic — route, validation, slot
  writes, signal selection, crash-safe checkpointing — lives in `run_tick.py`.
  This skill only presses the `Agent` button where the runner asks and relays
  the result.
