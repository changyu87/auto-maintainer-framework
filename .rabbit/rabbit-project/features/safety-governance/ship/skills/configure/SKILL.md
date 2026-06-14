---
name: configure
description: Set the auto-maintainer's trust mode and token budget in the project-local governance config. Use this whenever the user runs /auto-maintainer:configure, or asks to set/change the maintainer's mode (dry-run, propose, gated-merge), arm or disarm the implementer doer, set or clear a daily/per-tick token budget, or view the current governance settings. It relays the requested values to the deterministic configure script, which validates them and writes .auto-maintainer/governance.json.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when the governance config schema reaches a breaking major version, or when governance configuration moves out of a project-local JSON file consulted at tick entry.
---

# auto-maintainer configure

Set the maintainer's **trust mode** and **token budget**, which live in the
project-local governance config `${CLAUDE_PROJECT_DIR}/.auto-maintainer/governance.json`.
This config is what the tick loop consults to decide whether an acting state
(e.g. the IMPLEMENT doer) may act, and how much it may spend.

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

- `--per-day-tokens` and `--per-tick-tokens` are token ceilings. A non-negative
  integer sets a limit; `none` (also `null`/`unlimited`) means NO LIMIT for that
  dimension. With no governance.json, both default to no limit.

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
- Never write or edit `governance.json` directly — the script owns the schema,
  validation, and the write. If the script exits non-zero, surface its error
  message to the user and do not retry with a guessed value.
- The mode and budget values are the user's data — pass them verbatim; do not
  substitute a default or "helpfully" pick a value the user didn't ask for.
