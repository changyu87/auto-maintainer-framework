# auto-maintainer

**An autonomous repository maintenance loop, shipped as a Claude Code plugin.**

This is the installed plugin tree. It bundles the maintainer's tick-FSM core, its
default GitHub-Issues + git adapters, and the slash commands that drive the loop.
For the framework's source, design, and roadmap, see the project repository:
<https://github.com/changyu87/auto-maintainer-framework>.

## Status

**Early development (packaging skeleton).** The FSM core, the route
orchestrator, and clean plugin packaging are in place; the full autonomous loop
is still being built out. Installing today gives you a working `/plugin install`
and the slash commands below — see the repository's `docs/ROADMAP.md` for
per-feature status.

## Requirements

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — this is a
  Claude Code plugin and is installed and run from inside Claude Code.
- **The [`gh`](https://cli.github.com/) CLI, installed and authenticated** — the
  default GitHub adapters shell out to `gh` for issues and pull requests. Run
  `gh auth login` once so it can talk to your repository.

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

A startup banner also appears the next time you open Claude Code.

**Update or remove later:**

```
/plugin marketplace update                          # pull catalog changes, then re-install
/plugin uninstall auto-maintainer@auto-maintainer   # remove the plugin
```

## Commands

Once installed, the plugin provides these slash commands:

| Command | Description |
| --- | --- |
| `/auto-maintainer:start` | Start (or resume) the maintainer's in-session tick loop: runs the first tick now and schedules a recurring heartbeat that keeps ticking until stopped. |
| `/auto-maintainer:stop` | Stop the tick loop — latches it STOPPED and cancels the scheduled heartbeat so no further ticks run. |
| `/auto-maintainer:status` | Report the loop's real on-disk status: current disposition and the last pull's persisted work-items count. |
| `/auto-maintainer:tick` | Run exactly one tick, including any subagent dispatches, then report the result. |
| `/auto-maintainer:configure` | Set the maintainer's trust mode (dry-run / propose / gated-merge) and token budget in the project-local governance config. |

## Layout

This installed plugin carries:

- `.claude-plugin/plugin.json` — the plugin manifest.
- `skills/` — the slash-command skills listed above.
- `agents/` — the subagents the tick loop dispatches (triager, implementer, …).
- `hooks/` — the SessionStart persona/banner hook.
- `lib/` — the self-contained Python control libraries the skills invoke.
