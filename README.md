# auto-maintainer-framework

**Autonomous and automatic maintainer loop for AI-backed projects, packaged as a
Claude Code plugin.**

*"auto" stands for both **automatic** and **autonomous**.*

A project drops this in, points it at a work tracker, and gets a self-driving
maintainer: on a schedule it pulls actionable work, triages it, dispatches
isolated coding agents to implement it, verifies the result, integrates it
(PR / merge / release), cleans up, and reschedules — with safety guards,
crash-safe resumable state, and human-delegated authority — until a human
stops it.

The framework is **ports-and-adapters**: the loop core is fixed and
project-agnostic; every project-specific concern (where work comes from, how it
gets implemented, what "safe to merge" means, how integration happens) is a
swappable adapter. Default adapters ship for GitHub Issues + git.

## Status

**Actively developed and functional.** The full close-the-loop chain is
implemented and self-driving: PULL → TRIAGE → PRIORITIZE → IMPLEMENT → VERIFY →
REVIEW → GATE → INTEGRATE → CLEANUP, on a scheduled heartbeat, with a durable
crash-safe checkpoint, a trust ladder, a per-day token budget, and convergence
guards (it parks repeatedly-failing issues and idles instead of looping
forever). See [`docs/DESIGN.md`](docs/DESIGN.md) for the design and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for per-feature status.

## Requirements

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — this is a
  Claude Code plugin, installed and run from inside Claude Code.
- **The [`gh`](https://cli.github.com/) CLI, installed and authenticated** — the
  default GitHub adapters shell out to `gh` for issues and pull requests.

## Installation

Distributed as a **Claude Code plugin** served from a self-hosted marketplace.
Inside Claude Code, run these three steps:

```
/plugin marketplace add changyu87/auto-maintainer-framework
/plugin install auto-maintainer@auto-maintainer
/reload-plugins
```

> **The third step is required.** `/plugin install` stages the plugin and prints
> *"Run /reload-plugins to apply"* — `/reload-plugins` activates it in the
> current session. (Restarting Claude Code instead also works.)

Verify it loaded:

```
/auto-maintainer:status
```

## Quick start — configure and run

Three steps get you from a fresh install to a running maintainer.

### 1. Point `gh` at the repo to maintain

The maintainer works on whatever repo `gh` resolves for the project you run
Claude Code in — there is no repo to type into a config file. Authenticate once,
and (if needed) pin the default repo:

```
gh auth login
gh repo set-default <owner>/<repo>      # only if gh can't infer it from your git remote
```

### 2. Configure — the guided walk-through

Run the interactive setup any time (not just the first run):

```
/auto-maintainer:configure --setup
```

It checks your `gh` auth, confirms the repo it will maintain, then walks you
through every setting **in the order the loop uses them** (which issues to pull →
how it implements → how it verifies and gates → how often it ticks → safety
limits), and writes your choices to
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json`.

The most important knob is the **trust mode** — start conservative and move up:

| Mode | What the loop may do |
| --- | --- |
| `dry-run` | Nothing outward — plans only. The safe rung to watch a tick end-to-end. |
| `propose` | Implements work and **opens PRs**, but never merges. |
| `auto-merge` | As `propose`, and merges green PRs automatically (guardrails still apply). |

You can also set any knob directly without the walk-through, e.g.:

```
/auto-maintainer:configure --mode propose --per-day-tokens 500000
```

### 3. Start the loop

```
/auto-maintainer:start
```

This runs the first tick immediately and schedules a recurring heartbeat that
keeps ticking until you stop it. Check in or stop any time:

```
/auto-maintainer:status     # current disposition + last pull's work-item count
/auto-maintainer:stop       # latch STOPPED and cancel the heartbeat
```

## Choosing which issues it works on

By default the loop pulls **every open issue**. To narrow it, set an
`issue_filter` — by **label**, by **title**, or both. During `--setup` you'll be
prompted for these; to set them directly:

```
# Labels — comma = AND (same issue), semicolon = OR (either group):
/auto-maintainer:configure --issue-labels "bug,triaged;urgent"
#   => pull issues that are (labeled `bug` AND `triaged`) OR (labeled `urgent`)

# Title — a regular expression the issue title must match:
/auto-maintainer:configure --issue-title-pattern "^\[auto\] "

# Clear a filter (back to "pull everything"):
/auto-maintainer:configure --issue-labels none --issue-title-pattern none
```

Label and title filters compose (an issue must clear both). Equivalent
`config.json` form:

```json
{
  "issue_filter": {
    "labels": [["bug", "triaged"], ["urgent"]],
    "title_pattern": "^\\[auto\\] "
  }
}
```

## Configuration reference

All settings live in `${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json` and are
written by `/auto-maintainer:configure` (never hand-edit unless you want to).

| Setting | Flag | Default | Controls |
| --- | --- | --- | --- |
| Trust mode | `--mode` | `propose` | `dry-run` / `propose` / `auto-merge` (see table above). |
| Issue labels | `--issue-labels` | none | Which labels an issue must carry (DNF: comma = AND, semicolon = OR). |
| Issue title | `--issue-title-pattern` | none | Regex the issue title must match. |
| Work own filings | `--work-own-filings` | `true` | Whether the loop reworks issues it filed itself. |
| Per-day token budget | `--per-day-tokens` | none (unbounded) | Daily token ceiling; the loop idles until the next day when hit, then auto-resumes. |
| Heartbeat interval | `--interval-minutes` | `3` | Minutes between ticks. |
| Backoff threshold | `--backoff-threshold` | `5` | Consecutive-blocked count before a stuck work order is deferred. |
| GATE regression command | `--regression-command` | none | Shell command run against each PR before merge (exit 0 = pass). |
| GATE doc-check root | `--doc-check-features-root` | none | Repo-relative features root for the doc-surface survival check. |
| VERIFY features root | `--features-root` | none | Features dir for the cross-feature verification complement. |

## Commands

| Command | Description |
| --- | --- |
| `/auto-maintainer:configure` | Set any config knob, or run `--setup` for the guided walk-through. |
| `/auto-maintainer:start` | Start (or resume) the tick loop: runs one tick now and schedules the heartbeat. |
| `/auto-maintainer:stop` | Stop the loop — latches it STOPPED and cancels the heartbeat. |
| `/auto-maintainer:status` | Report the loop's on-disk status: current disposition and last pull's work-item count. |
| `/auto-maintainer:tick` | Run exactly one tick (including any subagent dispatches), then report the result. |
| `/auto-maintainer:route` | Advanced: view or edit which loop stages run (the route). |
| `/auto-maintainer:adapter-map` | Advanced: view or edit which adapter backs each loop stage. |

**Update or remove later:**

```
/plugin marketplace update                          # pull catalog changes, then re-install
/plugin uninstall auto-maintainer@auto-maintainer   # remove the plugin
```

## Relationship to rabbit-workflow

This framework generalizes the `rabbit-auto-evolve` feature of the
rabbit-workflow project into a portable, project-agnostic Claude Code plugin.
rabbit-workflow is the framework's first adapter consumer (dogfood).
