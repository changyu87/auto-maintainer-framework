---
name: rabbit-cage-config
description: Configure rabbit-cage's owned settings (scope-guard, bypass-permissions, allowed-tools, bash-allow, prompt-threshold).
version: 1.1.0
owner: rabbit-workflow team
deprecation_criterion: when the rabbit CLI exposes a native per-feature configuration mechanism that subsumes /rabbit-cage-config
template_version: 1.0.0
allowed-tools: Bash
---

# /rabbit-cage-config

Configure the five settings rabbit-cage genuinely owns. This is the per-feature
config command for rabbit-cage (phase 3 of #733); it coexists with the central
`/rabbit-config <sub>` surface — both work.

## Usage

/rabbit-cage-config scope-guard on
/rabbit-cage-config bypass-permissions true|false
/rabbit-cage-config allowed-tools add|remove <Tool>
/rabbit-cage-config bash-allow add|remove <command>
/rabbit-cage-config prompt-threshold set <N>
/rabbit-cage-config help

`/rabbit-cage-config help` prints usage plus the on-demand permission-bypass
guidance (the ephemeral `Shift+Tab` live toggle and the persisted
`bypass-permissions true|false` path). That guidance is shown ONLY here on
request — it no longer prints on every session start (issue #914).

## Implementation

Delegates to the deterministic companion script
`.claude/features/rabbit-cage/scripts/rabbit-cage-config.py`.

The script is a THIN wrapper: it reads rabbit-cage's own
`feature.json configuration[]` entry for the named subcommand and delegates all
validation, mutation, and restart-prompt rendering to
`contract.lib.config_dispatch.dispatch_config`. The interpreter logic lives ONCE
in `contract.lib`; this command owns only argv parsing and printing the returned
messages (and the restart prompt, when a setting takes effect only after a
Claude relaunch — e.g. `bypass-permissions`).
