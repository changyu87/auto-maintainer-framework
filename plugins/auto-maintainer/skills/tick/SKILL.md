---
name: tick
description: Run EXACTLY ONE auto-maintainer tick, including any subagent (agent-state) dispatches. Use this whenever the user runs /auto-maintainer:tick, asks to run/execute a single tick or step the maintainer loop once, or when the recurring heartbeat prompt asks for a tick. It drives the deterministic tick-runner and, whenever the runner pauses at an agent-state, dispatches the requested subagent(s) — pointing each one at the invocation-envelope file the runner names (prompt_path) and passing its description and (when present) isolation — and resumes, reporting the dispatched subagents' token usage, until the tick reaches its terminal. It runs one tick and STOPS: a `refire` final signal (actionable work remains) is REPORTED but does NOT auto-loop into another tick. Continuous back-to-back draining is /auto-maintainer:start's job, not this skill's.
version: 0.8.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when Claude Code can dispatch subagents from within a script (removing the need for a session-mediated executor), or when the tick CLI's --step/--resume protocol reaches a breaking major version.
---

# auto-maintainer tick (executor)

Run **exactly one** tick of the maintainer loop, then STOP. "A tick" is one FSM
iteration — one route pass from GUARD to a terminal — so this skill runs that
single pass (through any agent-state pause/dispatch/resume) and stops at the
terminal, even when actionable work remains in the pool. A `refire` final signal
is REPORTED so the caller knows work remains, but this skill does NOT start
another tick on its own; continuous draining belongs to `/auto-maintainer:start`
(which fires this skill again on `refire` until the backlog drains). Running one
tick per invocation lets a user step the loop deliberately.

The tick is **script-driven**: the
deterministic tick-runner at `${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py` walks the
route and does all control flow. Most states are pure script and need nothing
from you. An **agent-state** is the exception: a script cannot call the `Agent`
tool, so the runner **pauses** and hands you a `prompt_path` — the path to a file
holding the subagent's full invocation envelope; your only job is to **dispatch**
the subagent for it (telling it to read that file) and then resume. You decide
nothing about the route — the runner does.

Two things stay out of your context by design — both inputs and outputs flow
through files, never through you:

- **The invocation envelope is a FILE you don't read.** The runner writes each
  dispatch's rendered envelope to `prompt_path` and hands you only the path. You
  pass that path into a short reference prompt and let the SUBAGENT read the file
  — you never open it yourself. A large envelope (e.g. an IMPLEMENT dispatch with
  the full work-order body) therefore never lands in your context or truncates
  your runner output.
- **The output is a FILE the subagent writes.** The envelope's `## Handoff`
  section tells the subagent the exact schema, the output file path, and to reply
  with only a short ack. You never handle the output content — the runner reads
  the file on resume.

## ⚠️ The one rule that keeps a tick correct: `--step` once, then only `--resume`

The runner is advanced by exactly two commands, and **mixing them up corrupts
the tick**:

- **`--step`** — call it **exactly ONCE for this tick, as its very first runner
  command**. This skill runs one tick, so there is exactly one `--step`. The only
  other time you may call `--step` is to **re-emit a pause after an
  `invalid_output`** (see step 5). NEVER for anything else.
- **`--resume`** — the ONLY way to advance after you have dispatched the
  subagent(s) for a pause. After **every** `paused` you handle, the next runner
  command is `--resume` — never `--step`.

**Why this matters:** the `--resume` is what applies the subagent's output,
advances the route through the remaining states, and fires the terminal work
(disposition selection + the out-of-band REPORT flush). If you call `--step`
again after a dispatch instead of `--resume`, you skip that resume — the
subagent's work is not applied, discoveries are not reported, and the tick
double-runs. So: **one `--step` to begin; after each dispatch, `--resume`; repeat
until `done`.**

## The runner's JSON protocol

`run_tick.py --step` (and `--resume`) print a single JSON object to stdout:

- `{"status":"done","signal":"<idle|halt|...>","trace":"<one-line trace>"}` —
  the tick finished. Print the `trace` and stop.
- `{"status":"paused","state":"<name>","dispatches":[ {"subagent_type",
  "prompt_path","description", ... }, ... ]}` — the runner is waiting at an
  agent-state. Dispatch each entry, then **`--resume`**. Every entry carries a
  `subagent_type`, a `prompt_path` (the file holding that subagent's full
  invocation envelope — you pass the path, you do NOT read the file), and a
  `description`; an **acting** entry (one that performs outward effects)
  additionally carries an `isolation` value (e.g. `"worktree"`).
- `{"status":"invalid_output","state":"<name>","reason":"<why>"}` — a dispatched
  subagent's output file was missing or didn't match the schema. Re-dispatch
  that state (see step 5); after 2 failed attempts, print the reason and stop.

## Limits are operator-owned: config is the bound

A tick's limits are **operator-owned** — they come from the loop's
**configuration** (`budget.per_day_tokens`, `backoff.threshold`, `issue_filter`,
`mode`, and the route/adapter-map), and the deterministic runner already ENFORCES
every configured bound: the budget pre-gate, backoff, and park deferral all live
inside `run_tick.py`. So your job as the executor is to run the tick the runner
describes, not to add limits of your own on top:

- **Run the tick to its terminal and dispatch the subagents the runner
  requests.** The only limits are the config-defined ones the runner enforces
  (budget pre-gate, backoff, park). Do not invent an ad-hoc token / issue-count
  cap of your own, and do not silently narrow the operator's configured scope —
  if the runner keeps handing you dispatches, work them.
- **`subagent_tokens` is spend-metering only.** The summed `subagent_tokens` you
  carry back via `--resume --spent` feeds spend metering into the operator's
  configured **budget window** — it is the INPUT to that budget so the runner can
  enforce it, NOT itself a stop trigger you act on. The runner, not this executor,
  decides against the configured budget; you never cap the tick because a spend
  number looked large.
- **Surface anomalies to the operator.** If a run looks genuinely unusual — e.g.
  a self-feeding loop, or high cost with NO budget configured — SURFACE that
  observation to the operator, who bounds the loop via `/auto-maintainer:configure`
  or halts it via `/auto-maintainer:stop`. That keeps them in control rather than
  the executor silently self-limiting or silently pressing on.

## Steps

1. **Begin the tick — the one and only `--step`:**

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --step
   ```

   Parse the single JSON object it prints to stdout, then follow steps 2–5 on the
   result. From here on you advance ONLY with `--resume` (except an
   `invalid_output` re-emit, step 5).

2. If `status` is `"done"`: print the `trace` and **STOP** — this tick is
   complete. This skill runs exactly one tick, so it does NOT begin another tick
   regardless of the `signal`. Just note what the `signal` means for the caller:
   - `signal` is `"refire"` — the tick finished but **actionable work remains**
     (e.g. PRIORITIZE deferred a same-feature order to a later tick). REPORT this
     — say the pool still has workable items and that the caller can run
     `/auto-maintainer:tick` again to do the next one, or `/auto-maintainer:start`
     for continuous back-to-back draining. Do **NOT** begin another tick yourself;
     one `/tick` invocation is one tick, even when the pool is not drained. (When
     `/auto-maintainer:start` drove this tick, IT owns the drain-loop and will
     fire the next tick on this `refire`.)
   - any other `signal` (`idle` / `halt` / `break` / …) — the loop has no
     immediate follow-on work. Stop here; the next heartbeat will tick again.

3. If `status` is `"paused"`: for **each** entry in `dispatches` (in order),
   dispatch the subagent and point it at its `prompt_path`, passing the entry's
   `description`, and its `isolation` **only when the entry includes one**. The
   `prompt` you pass is a SHORT fixed reference — it tells the subagent that its
   full invocation envelope is the file at `prompt_path`, to read it in full, and
   to follow it literally:

   - entry has no `isolation`:
     `Agent(subagent_type=<entry.subagent_type>, description=<entry.description>, prompt="Your invocation envelope is the file at <entry.prompt_path>. Read it IN FULL and follow it literally.")`
   - entry has an `isolation`:
     `Agent(subagent_type=<entry.subagent_type>, description=<entry.description>, prompt="Your invocation envelope is the file at <entry.prompt_path>. Read it IN FULL and follow it literally.", isolation=<entry.isolation>)`

   **Do NOT open or read the `prompt_path` file yourself** — pass only the path
   into the reference prompt and let the subagent read it. This is deliberate: the
   rendered envelope can be large (an IMPLEMENT dispatch carries the whole
   work-order body), and reading it would pull it into your context and risk
   truncating your runner output. Substitute the real `entry.prompt_path` into the
   reference string, but keep the rest of the prompt fixed. The `description` and
   `isolation` come straight from the runner; pass them through unchanged — do not
   invent or omit them. Each subagent reads its envelope file, writes its own
   output file (as the envelope instructs), and replies with a short ack; you do
   **not** read the envelope, **not** write any output file, and **not** capture
   the subagent's output — just let each dispatch finish.

   As each dispatch finishes, note the `subagent_tokens` it reports in its
   result. Sum these across all entries in this pause — call it the spend. This
   value is observable only from the dispatch results (no script can compute
   it), so you carry it to the resume in the next step.

4. **Advance with `--resume` (NOT `--step`)** — the runner reads the
   subagent-written output files itself and meters the reported spend against the
   budget window:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --resume --spent <spend>
   ```

   `<spend>` is the summed `subagent_tokens` from step 3 (use `0` if no entry
   reported usage). `--resume` needs no other arguments; the runner knows the
   output files from its checkpoint. Parse the JSON it prints and go back to
   step 2 with the result. **Do not call `--step` here** — after a dispatch the
   advance is always `--resume`, even if the dispatched subagent took a long time.

5. If `status` is `"invalid_output"`: re-dispatch the same state. This is the ONE
   case where you call `--step` again — to re-emit the same pause from the
   checkpoint so you get the dispatch entries (and their `prompt_path`s) back:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py --step
   ```

   then dispatch the re-emitted entries (step 3) and `--resume` (step 4). Allow
   at most **2** re-dispatch attempts for a given state; if it still fails, print
   the `reason` and stop (do not force-advance past a state whose output won't
   validate).

## Rules

- **`--step` once to begin EACH tick; `--resume` after every dispatch; `--step`
  again ONLY to re-emit after `invalid_output`.** Never `--step` mid-tick after a
  successful dispatch — that skips the resume that applies outputs and fires the
  terminal REPORT flush.
- **Exactly one tick — do NOT loop on `refire`.** A `done` tick whose `signal` is
  `refire` means actionable work remains, but this skill runs one tick and STOPS:
  REPORT the refire (the caller can run `/tick` again or `/start` for continuous
  draining) and do not begin another tick yourself. The runner decides
  refire-vs-idle deterministically; the drain-loop that fires the next tick on
  `refire` is `/auto-maintainer:start`'s job (tick #1 and each heartbeat), never
  this executor's.
- Dispatch a subagent ONLY for the entries the runner hands you, pointing it at
  the `prompt_path` the runner provides and passing the `description` and (when
  present) `isolation` unchanged — do not alter them or invoke any other subagent.
  The file at `prompt_path` is the machine-first invocation envelope; it alone
  tells the subagent the schema, the output file, and the ack — the subagent needs
  no other knowledge, and reads the file itself.
- Never open or read the `prompt_path` file yourself — pass only the path. The
  rendered envelope stays out of your context (it can be large); the subagent
  reads it.
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
