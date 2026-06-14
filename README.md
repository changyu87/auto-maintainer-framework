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

**Early development.** The FSM core is taking shape and the plugin is already
installable as a packaging skeleton — but the autonomous maintainer loop itself
is not built yet. Implemented so far: the tick-FSM contract layer
(`fsm-contracts`), the external route orchestrator (`tick-orchestrator`), and
clean plugin packaging + a marketplace (`packaging-config`). See
[`docs/DESIGN.md`](docs/DESIGN.md) for the design and
[`docs/ROADMAP.md`](docs/ROADMAP.md) for per-feature status.

Installing today gives you a working `/plugin install` and an
`/auto-maintainer:status` skill that reports *"no loop configured yet"* — the
maintainer adapters (PULL / TRIAGE / IMPLEMENT / scheduling) are still on the
roadmap.

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

## Relationship to rabbit-workflow

This framework generalizes the `rabbit-auto-evolve` feature of the
rabbit-workflow project into a portable, project-agnostic Claude Code plugin.
rabbit-workflow is intended to be the framework's first adapter consumer
(dogfood).
