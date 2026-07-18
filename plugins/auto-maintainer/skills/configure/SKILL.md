---
name: configure
description: Set the auto-maintainer's trust mode, token budget, heartbeat cadence, backoff threshold, and GATE regression command in the project-local central config. Use this whenever the user runs /auto-maintainer:configure, or asks to set/change the maintainer's mode (dry-run, propose, auto-merge), arm or disarm the implementer doer, set or clear a daily token budget, change the heartbeat/tick interval, change the backoff threshold, set or clear the GATE regression command, or view the current settings. Also use this for the guided --setup walk-through whenever the user runs /auto-maintainer:configure --setup or asks to be "walked through" / "set up" / "configured step by step" — it walks every config knob field-by-field and writes the choices. It relays the requested values to the deterministic configure script, which validates them and writes .auto-maintainer/config.json.
version: 0.8.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when the central-config schema reaches a breaking major version, or when governance configuration moves out of a project-local JSON file consulted at tick entry.
---

# auto-maintainer configure

Set the maintainer's **trust mode**, **token budget**, **heartbeat cadence**,
**backoff threshold**, and **GATE regression command**, which live in the
project-local central config
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json`. This config is what the
tick loop consults to decide whether an acting state (e.g. the IMPLEMENT doer)
may act, how much it may spend, how often the loop ticks, when to defer a
stuck work order, and what regression command the GATE state runs before merge.

All validation and the file write are owned by the deterministic script
`${CLAUDE_PLUGIN_ROOT}/lib/configure.py` — this skill only relays the values the
user asked for. It never edits the JSON by hand.

## Trust modes

- `dry-run` — inert. Acting states perform nothing; the IMPLEMENT doer emits
  planned handoffs only (no PR, no issue close). The safe rung to verify a route.
- `propose` — the doer implements and **opens PRs** (and closes rejected issues),
  but never merges.
- `auto-merge` — as propose, and merging is permitted (the loop merges
  automatically; the §3.8.1 merge guardrails are the hard backstop). The legacy
  name `gated-merge` is still accepted and stored as `auto-merge`.

## Budget

- `--per-day-tokens` is the per-day token ceiling. A non-negative integer sets a
  limit; `none` (also `null`/`unlimited`) means NO LIMIT. With no config.json it
  defaults to no limit (unbounded). When a limit is reached the loop idles until
  the next local-day window, then auto-resumes.

## Heartbeat and backoff

- `--interval-minutes` is the heartbeat (tick) cadence in minutes (a positive
  integer; default 3).
- `--backoff-threshold` is the consecutive-blocked count at which the loop
  escalates and defers a stuck work order (a positive integer; default 5).

## GATE regression command

- `--regression-command` is the full-regression shell command the GATE state
  (verify-integrate) runs against each REVIEW-passed PR before merge (exit 0 =
  pass). An arbitrary command string sets it; `none` (also `null`/empty) clears
  it back to `null`, which makes GATE a no-op PASS (no gate). With no config.json
  it defaults to no gate.

## How to run

Invoke the script, passing ONLY the flags for what the user asked to change. The
flag values come straight from the user's request — pass them through; do not
compute or invent values. The script validates them (an unknown mode or a
negative ceiling exits non-zero with an error) and prints the resulting config.

- View current settings (no change):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --show
  ```

- Set the mode (use the exact mode the user named):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --mode <dry-run|propose|auto-merge>
  ```

- Set a daily token budget (or clear it with `none`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --per-day-tokens <int|none>
  ```

- Set the heartbeat cadence or the backoff threshold (positive ints):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --interval-minutes <int>
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --backoff-threshold <int>
  ```

- Set the GATE regression command (or clear it with `none`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --regression-command "<shell command>|none"
  ```

- Combine in one call when the user asked for several at once, e.g. mode +
  daily budget:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --mode propose --per-day-tokens 200000
  ```

Then report the resulting config (the script prints it as JSON) back to the user
in a short sentence — e.g. "mode is now propose, daily budget 200000 tokens".

## Guided `--setup` walk-through

Use this when the user runs `/auto-maintainer:configure --setup` or asks to be
walked through the configuration step by step. The walk-through covers every knob
in turn so a new user does not have to know the flag names. Do all of it yourself
in this conversation — dispatch **no subagent**; this is a short, interactive,
single-session flow, and a subagent could neither ask the user questions nor read
their answers.

The `configure.py --describe` field catalog is the **single source of truth** for
which knobs exist and what they mean. Read it at the start and drive the whole
walk-through from it, so this skill stays correct as knobs are added or renamed.
Do **not** hardcode field names, labels, or prose in this skill — the catalog
already carries them (SKILL.md authoring §4: derive from source, do not
paraphrase).

1. Read the catalog:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --describe
   ```

   It prints a JSON list; each entry is one knob with these fields:

   - `key` — the config key the value lands under (e.g. `budget.per_day_tokens`).
   - `label` — the human name to show the user.
   - `controls` — a one-line explanation of what the knob does.
   - `default` — the value used when nothing is set.
   - `current` — the value in effect right now (from `config.json` or defaults).
   - `type` — the value's shape (`enum`, `int`, `int_or_null`, `str_or_null`).
   - `validator` — the accepted values, to quote when prompting.

2. For **each** entry, in catalog order, show the user its `label`, what it
   `controls`, its `default`, and its `current` value, then ask for a new value
   or to keep the current one. Quote the `validator` so they know what is
   accepted. Take the user's answers verbatim — they are the user's data; do not
   substitute a default or guess a value the user did not give.

3. Apply every chosen change in **one** `configure.py` invocation, mapping each
   catalog `key` to its flag (`mode` → `--mode`, `budget.per_day_tokens` →
   `--per-day-tokens`, `heartbeat.interval_minutes` → `--interval-minutes`,
   `backoff.threshold` → `--backoff-threshold`, `regression_command` →
   `--regression-command`). The deterministic writer
   validates and writes `config.json`. Then `--show` the result and read it back
   to the user:

   ```
   python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --show
   ```

   If the user kept every value unchanged, skip the write and just `--show` the
   current config. A power user may still hand-edit `config.json` directly.

## Rules

- Relay only the flags the user asked to change; omitted dimensions are left
  exactly as they were (the script load-modify-saves, preserving other keys).
- Never write or edit `config.json` directly — the script owns the schema,
  validation, and the write. If the script exits non-zero, surface its error
  message to the user and do not retry with a guessed value.
- The mode and budget values are the user's data — pass them verbatim; do not
  substitute a default or "helpfully" pick a value the user didn't ask for.
