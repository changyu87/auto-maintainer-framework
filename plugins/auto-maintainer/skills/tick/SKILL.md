---
name: tick
description: Run exactly one auto-maintainer tick, including any subagent (agent-state) dispatches. Use this whenever the user runs /auto-maintainer:tick, asks to run/execute one tick or step the maintainer loop once, or when the recurring heartbeat prompt asks for a tick. It drives the deterministic tick-runner and, whenever the runner pauses at an agent-state, dispatches the requested subagent(s) — passing each one's description and (when present) isolation — and resumes, reporting the dispatched subagents' token usage, until the tick completes.
version: 0.3.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when Claude Code can dispatch subagents from within a script (removing the need for a session-mediated executor), or when the tick CLI's --step/--resume protocol reaches a breaking major version.
---

# auto-maintainer tick (executor)

Run one tick of the maintainer loop. The tick is **script-driven**: the
deterministic tick-runner at `${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py` walks the
route and does all control flow. Most states are pure script and need nothing
from you. An **agent-state** is the exception: a script cannot call the `Agent`
tool, so the runner **pauses** and hands you a fully-rendered prompt; your only
job is to **dispatch** the subagent for it and then resume. You decide nothing
about the route — the runner does.

Crucially, the **dispatched subagent writes its own output to a file** (the
prompt's `## Handoff` section tells it the exact schema, the file path, and to
reply with only a short ack). So you never handle the subagent's output content
— the runner reads the file on resume. This keeps your context clean no matter
how large the output is.

## The runner's JSON protocol

`run_tick.py --step` (and `--resume`) print a single JSON object to stdout:

- `{"status":"done","signal":"<idle|halt|...>","trace":"<one-line trace>"}` —
  the tick finished. Print the `trace` and stop.
- `{"status":"paused","state":"<name>","dispatches":[ {"subagent_type","prompt",
  "description", ... }, ... ]}` — the runner is waiting at an agent-state.
  Dispatch each entry, then resume. Every entry carries a `subagent_type`, a
  `prompt`, and a `description`; an **acting** entry (one that performs outward
  effects) additionally carries an `isolation` value (e.g. `"worktree"`).
- `{"status":"invalid_output","state":"<name>","reason":"<why>"}` — a dispatched
  subagent's output file was missing or didn't match the schema. Re-dispatch
  that state (see Steps); after 2 failed attempts, print the reason and stop.

## Steps

1. Step the runner:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --step
   ```

   Parse the single JSON object it prints to stdout.

2. If `status` is `"done"`: print the `trace` and stop — the tick is complete.

3. If `status` is `"paused"`: for **each** entry in `dispatches` (in order),
   dispatch the subagent with that entry's exact rendered prompt, passing the
   entry's `description`, and its `isolation` **only when the entry includes
   one**:

   - entry has no `isolation`:
     `Agent(subagent_type=<entry.subagent_type>, description=<entry.description>, prompt=<entry.prompt>)`
   - entry has an `isolation`:
     `Agent(subagent_type=<entry.subagent_type>, description=<entry.description>, prompt=<entry.prompt>, isolation=<entry.isolation>)`

   The `prompt` is the complete, self-contained handoff contract — pass it
   through **verbatim**, do not author or alter it. The `description` and
   `isolation` come straight from the runner; pass them through unchanged too —
   do not invent or omit them. Each subagent writes its own output file (as the
   prompt instructs) and replies with a short ack; you do **not** write any file
   yourself and you do **not** need to capture the subagent's output — just let
   each dispatch finish.

   As each dispatch finishes, note the `subagent_tokens` it reports in its
   result. Sum these across all entries in this pause — call it the spend. This
   value is observable only from the dispatch results (no script can compute
   it), so you carry it to the resume in the next step.

4. Resume the runner — it reads the subagent-written output files itself, and
   meters the reported spend against the budget window:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --resume --spent <spend>
   ```

   `<spend>` is the summed `subagent_tokens` from step 3 (use `0` if no entry
   reported usage). `--resume` needs no other arguments; the runner knows the
   output files from its checkpoint. Parse the JSON it prints and go back to
   step 2 with the result.

5. If `status` is `"invalid_output"`: re-dispatch the same state — go back to
   step 1 (the runner re-emits the same pause from its checkpoint; a fresh
   dispatch lets the subagent re-write its output file). Allow at most **2**
   re-dispatch attempts for a given state; if it still fails, print the `reason`
   and stop (do not force-advance past a state whose output won't validate).

## Rules

- Dispatch a subagent ONLY for the entries the runner hands you, with the
  prompt, `description`, and (when present) `isolation` the runner provides,
  verbatim — do not alter them or invoke any other subagent. The prompt is the
  machine-first invocation envelope; it alone tells the subagent the schema, the
  output file, and the ack — the subagent needs no other knowledge.
- Never marshal, copy, or write the subagent's output yourself — the subagent
  writes its file and the runner reads it. This is deliberate: it keeps the
  output out of your context.
- The only value you carry back to the runner is the summed `subagent_tokens`
  spend via `--spent`; pass it as observed, do not fabricate it.
- A dispatched subagent runs one level below this session and must not itself
  dispatch another agent (the 2-level nesting cap).
- Never advance the tick by hand-rolling Python or by any path other than
  `run_tick.py --step` / `--resume`. All tick logic — route, validation, slot
  writes, signal selection, spend metering, crash-safe checkpointing — lives in
  `run_tick.py`.
