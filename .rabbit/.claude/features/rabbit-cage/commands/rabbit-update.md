---
name: rabbit-update
description: Check for or install a newer rabbit-workflow release.
version: 1.0.0
owner: rabbit-workflow team
deprecation_criterion: when the rabbit CLI exposes a native self-update command that subsumes /rabbit-update
template_version: 1.0.0
allowed-tools: Bash
---

# /rabbit-update

Check for, or install, a newer rabbit-workflow release.

## Usage

/rabbit-update check
/rabbit-update install

## Implementation

Delegates to the deterministic companion script
`.claude/features/rabbit-cage/scripts/rabbit-update.py`:

- `check`   — non-mutating; reports current vs latest release as JSON
  (`{current, latest, newer, self_update_available}`). Reuses contract's
  release-check probe; forces a fresh, non-throttled check.
- `install` — applies the self-update via `install.py --update`.

The script owns all logic (release probe, version compare, install dispatch);
this command file only routes the subcommand to it.
