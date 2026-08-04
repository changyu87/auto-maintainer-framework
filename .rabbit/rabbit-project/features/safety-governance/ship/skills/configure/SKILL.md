---
name: configure
description: Set the auto-maintainer's trust mode, token budget, heartbeat cadence, backoff threshold, GATE regression command, GATE doc-check features root, VERIFY features root, the work-own-filings loopback toggle, and the PULL issue filter (labels + title pattern) in the project-local central config. Use this whenever the user runs /auto-maintainer:configure, or asks to set/change the maintainer's mode (dry-run, propose, auto-merge), arm or disarm the implementer doer, set or clear a daily token budget, change the heartbeat/tick interval, change the backoff threshold, set or clear the GATE regression command or doc-check features root, set the VERIFY features root, toggle whether the loop works its own filings, filter which issues the loop pulls by label or title, or view the current settings. Also use this for the guided --setup onboarding whenever the user runs /auto-maintainer:configure --setup or asks to be "walked through" / "set up" / "onboarded" / "configured step by step" — it runs a read-only preflight (gh auth + resolved repo), confirms the repo, then walks every config knob stage-by-stage in loop order and writes the choices in one shot. It relays the requested values to the deterministic configure script, which validates them and writes .auto-maintainer/config.json.
version: 0.10.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when the central-config schema reaches a breaking major version, or when governance configuration moves out of a project-local JSON file consulted at tick entry.
---

# auto-maintainer configure

Set the maintainer's **trust mode**, **token budget**, **heartbeat cadence**,
**backoff threshold**, **GATE regression command**, and **GATE doc-check
features root**, which live in the project-local central config
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json`. This config is what the
tick loop consults to decide whether an acting state (e.g. the IMPLEMENT doer)
may act, how much it may spend, how often the loop ticks, when to defer a
stuck work order, what regression command the GATE state runs before merge, and
where the GATE looks for feature doc surfaces when checking load-bearing-token
survival.

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

## GATE doc-check features root

- `--doc-check-features-root` is the **repo-relative** features root the GATE
  state (verify-integrate) uses for its doc-surface load-bearing-token survival
  check — it maps a PR's diff paths to features and locates their doc surfaces.
  A repo-relative path (e.g. `features` or `<subtree>/features`) turns the check
  ON; an **absolute** path is rejected (exits non-zero). `none` (also
  `null`/empty) clears it back to `null`, which turns the check OFF. With no
  config.json it defaults to off. This is kept distinct from the loop's on-disk
  feature locator so the doc check can be enabled independently.

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

- Set the GATE doc-check features root (repo-relative path, or clear it with
  `none`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --doc-check-features-root "<repo-relative path>|none"
  ```

- Set the VERIFY features root (VERIFY's complement locator; it MAY be
  absolute, unlike the doc-check root; clear it with `none`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --features-root "<path>|none"
  ```

- Toggle whether the loop works its OWN filings (the loopback provision; a
  bool — `true`/`false`, also `1`/`0`, `yes`/`no`):

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --work-own-filings <true|false>
  ```

- Filter which open issues PULL pulls, by label — compact DNF syntax where a
  comma is AND within a group and a semicolon is OR between groups (e.g.
  `"bug,triaged;urgent"` means *(bug AND triaged) OR urgent*); clear with
  `none`:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --issue-labels "<a,b;c>|none"
  ```

- Filter pulled issues by a title regex the title must match; clear with
  `none`:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --issue-title-pattern "<regex>|none"
  ```

- Combine in one call when the user asked for several at once, e.g. mode +
  daily budget:

  ```
  python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --mode propose --per-day-tokens 200000
  ```

Then report the resulting config (the script prints it as JSON) back to the user
in a short sentence — e.g. "mode is now propose, daily budget 200000 tokens".

## Guided `--setup` onboarding

Use this when the user runs `/auto-maintainer:configure --setup` or asks to be
"walked through" / "set up" / "onboarded" / "configured step by step". It is a
**re-runnable** guided onboarding — usable any time, not only right after
install — that walks the user through everything needed to run the loop,
**ordered by the loop's own stages** (the route). Do all of it yourself in this
conversation — dispatch **no subagent**; this is a short, interactive,
single-session flow, and a subagent could neither ask the user questions nor read
their answers.

The `configure.py --describe` field catalog **and** the `configure.py --preflight`
probe are the **single source of truth** — do **not** hardcode field names,
labels, prose, the `stage` grouping, or repo/auth values in this skill; both
commands already carry them (SKILL.md authoring §4: derive from source, do not
paraphrase). Both are read-only and write nothing.

### Step 1 — Preflight (confirm the repo)

Run the read-only environment probe:

```
python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --preflight
```

It prints JSON with these fields:

- `gh_authenticated` — whether `gh` is logged in.
- `gh_account` — the active `gh` account login, or null.
- `resolved_repo` — the `owner/repo` the loop would maintain (gh-default /
  git-remote resolved; there is no `repo` config key), or null.
- `config_exists` — whether a project-local `config.json` already exists.

Surface the result and **confirm the repo** with the user ("I'll maintain
`owner/repo` — correct?"). If `gh_authenticated` is false, tell them to run
`gh auth login` first and stop until they have. If `resolved_repo` is wrong or
null, tell them to run `gh repo set-default <owner/repo>` (the repo is
`gh`-resolved, not a config knob). This step writes nothing.

### Step 2 — Stage-by-stage walk

Read the catalog:

```
python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --describe
```

It prints a JSON list; each entry is one knob with these fields:

- `key` — the config key the value lands under (e.g. `budget.per_day_tokens`).
- `label` — the human name to show the user.
- `controls` — a one-line explanation of what the knob does.
- `default` — the value used when nothing is set.
- `current` — the value in effect right now (from `config.json` or defaults).
- `type` — the value's shape (e.g. `enum`, `int`, `int_or_null`, `str_or_null`,
  `bool`, `dnf_labels`).
- `validator` — the accepted values, to quote when prompting.
- `stage` — the loop state that consumes the knob.

**Group the knobs by their `stage`** and present each stage in the catalog's
order (the catalog is already ordered by loop stage, PULL → IMPLEMENT → VERIFY →
GATE → SCHEDULING → SAFETY, so following catalog order follows the route). For
**each** knob show its `label`, what it `controls`, its `default`, and its
`current` value, quote its `validator`, then ask for a new value or to keep the
current one. Take the user's answers verbatim — they are the user's data; do not
substitute a default or guess a value the user did not give.

### Step 3 — Advanced (opt-in) routing

Offer to keep the recommended default `route` / `adapter-map` (the common case).
Only dive into `/auto-maintainer:route` or `/auto-maintainer:adapter-map` if the
user wants to change which stages run or swap a port's adapter.

### Step 4 — Review + apply

Apply every chosen change in **one** `configure.py` invocation, mapping each
catalog `key` to its flag (`mode` → `--mode`, `budget.per_day_tokens` →
`--per-day-tokens`, `heartbeat.interval_minutes` → `--interval-minutes`,
`backoff.threshold` → `--backoff-threshold`, `regression_command` →
`--regression-command`, `doc_check_features_root` → `--doc-check-features-root`,
`features_root` → `--features-root`, `work_own_filings` → `--work-own-filings`,
`issue_filter.labels` → `--issue-labels`, `issue_filter.title_pattern` →
`--issue-title-pattern`). The deterministic writer validates and writes
`config.json`. Then `--show` the result and read it back to the user:

```
python3 ${CLAUDE_PLUGIN_ROOT}/lib/configure.py --show
```

If the user kept every value unchanged, skip the write and just `--show` the
current config. A power user may still hand-edit `config.json` directly.

### Step 5 — Offer to start

Point the user at `/auto-maintainer:start` to launch the loop.

## Rules

- Relay only the flags the user asked to change; omitted dimensions are left
  exactly as they were (the script load-modify-saves, preserving other keys).
- Never write or edit `config.json` directly — the script owns the schema,
  validation, and the write. If the script exits non-zero, surface its error
  message to the user and do not retry with a guessed value.
- The mode and budget values are the user's data — pass them verbatim; do not
  substitute a default or "helpfully" pick a value the user didn't ask for.
