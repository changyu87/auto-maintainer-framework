---
name: configure
description: Set the auto-maintainer's trust mode, token budget, heartbeat cadence, and backoff threshold in the project-local central config. Use this whenever the user runs /auto-maintainer:configure, or asks to set/change the maintainer's mode (dry-run, propose, gated-merge), arm or disarm the implementer doer, set or clear a daily token budget, change the heartbeat/tick interval, change the backoff threshold, or view the current settings. It relays the requested values to the deterministic configure script, which validates them and writes .auto-maintainer/config.json.
version: 0.2.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when the central-config schema reaches a breaking major version, or when governance configuration moves out of a project-local JSON file consulted at tick entry.
---

# auto-maintainer configure

Set the maintainer's **trust mode**, **token budget**, **heartbeat cadence**, and
**backoff threshold**, which live in the project-local central config
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json`. This config is what the
tick loop consults to decide whether an acting state (e.g. the IMPLEMENT doer)
may act, how much it may spend, how often the loop ticks, and when to defer a
stuck work order.

All validation and the file write are owned by the deterministic script
`${CLAUDE_PLUGIN_ROOT}/lib/configure.py` — this skill only relays the values the
user asked for. It never edits the JSON by hand.

## Trust modes

- `dry-run` — inert. Acting states perform nothing; the IMPLEMENT doer emits
  planned handoffs only (no PR, no issue close). The safe rung to verify a route.
- `propose` — the doer implements and **opens PRs** (and closes rejected issues),
  but never merges.
- `gated-merge` — as propose, and merging is permitted.

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
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --mode <dry-run|propose|gated-merge>
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

- Combine in one call when the user asked for several at once, e.g. mode +
  daily budget:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --mode propose --per-day-tokens 200000
  ```

Then report the resulting config (the script prints it as JSON) back to the user
in a short sentence — e.g. "mode is now propose, daily budget 200000 tokens".

## Rules

- Relay only the flags the user asked to change; omitted dimensions are left
  exactly as they were (the script load-modify-saves, preserving other keys).
- Never write or edit `config.json` directly — the script owns the schema,
  validation, and the write. If the script exits non-zero, surface its error
  message to the user and do not retry with a guessed value.
- The mode and budget values are the user's data — pass them verbatim; do not
  substitute a default or "helpfully" pick a value the user didn't ask for.
