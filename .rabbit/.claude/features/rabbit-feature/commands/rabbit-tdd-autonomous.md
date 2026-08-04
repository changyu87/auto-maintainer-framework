---
name: rabbit-tdd-autonomous
description: Toggle TDD-autonomous mode — skip the feature-touch TDD cycle Step 4 (human approval). true bypasses the gate; false (default) keeps it active.
version: 1.0.2
owner: rabbit-workflow team
deprecation_criterion: when the rabbit CLI exposes a native per-feature configuration mechanism that subsumes /rabbit-tdd-autonomous
template_version: 1.0.0
allowed-tools: Bash
---

# /rabbit-tdd-autonomous

Toggle TDD-autonomous mode for the `rabbit-feature-touch` cycle. This is the
per-feature config command rabbit-feature owns; it is the SOLE supported surface
for this configurable.

The Step-4 approval gate lives in the dispatcher's main session. The
bypass authorization is the marker `.rabbit-tdd-autonomous` at the repo root:

- `false` (DEFAULT) — Step 4 gate ACTIVE; the marker is removed.
- `true` — Step 4 gate BYPASSED; the marker is written so the TDD cycle skips
  human approval.

## Usage

/rabbit-tdd-autonomous true
/rabbit-tdd-autonomous false

## Implementation

Delegates to the deterministic companion script
`.claude/features/rabbit-feature/scripts/rabbit-tdd-autonomous-config.py`.

The script is a THIN wrapper: it reads rabbit-feature's own
`feature.json configuration[]` entry for `tdd-autonomous` and delegates all
validation, mutation, and restart-prompt rendering to
`contract.lib.config_dispatch.dispatch_config`. The interpreter logic lives ONCE
in `contract.lib`; this command owns only argv parsing and printing the returned
messages (and the restart prompt — the new value takes effect only after a
Claude relaunch, so the configurable is `restart_required`).
