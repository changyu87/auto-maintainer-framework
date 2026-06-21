# auto-maintainer

**An autonomous repository maintenance loop, shipped as a Claude Code plugin.**

This is the installed plugin tree. It bundles the maintainer's tick-FSM core, its
default GitHub-Issues + git adapters, and the slash commands that drive the loop.
For the framework's source, design, and roadmap, see the project repository:
<https://github.com/changyu87/auto-maintainer-framework>.

## Status

**v1 complete — a working autonomous maintainer.** The full
pull -> triage -> implement -> verify -> integrate -> report loop is live-proven:
the loop pulls open issues, triages them, opens labelled pull requests for the
work it accepts, verifies and integrates its own PRs, and reports back — driven
by a session-mediated heartbeat. See the repository's `docs/ROADMAP.md` for
per-feature status and what is still being hardened.

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

## Usage

1. **Configure** the loop's trust mode and budget once with
   `/auto-maintainer:configure` (start in `dry-run` or `propose` to watch what
   it would do before letting it act).
2. **Start** the loop with `/auto-maintainer:start` — it runs the first tick now
   and schedules a recurring heartbeat that keeps ticking.
3. **Check** progress any time with `/auto-maintainer:status`, run a single tick
   on demand with `/auto-maintainer:tick`, and **stop** the loop with
   `/auto-maintainer:stop`.
4. **Customize** the loop's route and adapter map with `/auto-maintainer:route`
   and `/auto-maintainer:adapter-map` (the shipped defaults work out of the box).

## Commands

Once installed, the plugin provides these slash commands:

| Command | Description |
| --- | --- |
| `/auto-maintainer:adapter-map` | View and edit the loop's adapter map — which adapter implements each route port (GUARD, DRAIN, PULL, TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, REVIEW, INTEGRATE, CLEANUP, PERSIST, EXIT). Every edit is validated before it is saved. |
| `/auto-maintainer:configure` | Set the maintainer's trust mode (dry-run / propose / auto-merge), per-day token budget, heartbeat interval, and backoff threshold in the central config (.auto-maintainer/config.json). |
| `/auto-maintainer:route` | View and edit the loop's route — the ordered state graph (GUARD -> DRAIN -> PULL -> ... -> PERSIST -> EXIT) the tick runner walks each tick. Every edit is validated before it is saved. |
| `/auto-maintainer:start` | Start (or resume) the maintainer's in-session tick loop: runs the first tick now and schedules a recurring heartbeat that keeps ticking until stopped. |
| `/auto-maintainer:status` | Report the loop's real on-disk status: current disposition and the last pull's persisted work-items count. |
| `/auto-maintainer:stop` | Stop the tick loop — latches it STOPPED and cancels the scheduled heartbeat so no further ticks run. |
| `/auto-maintainer:tick` | Run exactly one tick, including any subagent (agent-state) dispatches, then report the result. |

## Layout

This installed plugin carries:

- `.claude-plugin/plugin.json` — the plugin manifest.
- `skills/` — the slash-command skills listed above.
- `agents/` — the subagents the tick loop dispatches (triager, implementer, …).
- `hooks/` — the SessionStart persona/banner hook.
- `lib/` — the self-contained Python control libraries the skills invoke.
