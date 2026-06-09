---
name: rabbit-refresh
description: Re-inject the rabbit-workflow policy files into context and reset the auto-refresh counter.
version: 1.0.0
owner: rabbit-workflow team
deprecation_criterion: when Claude Code natively re-injects governing policy files on demand, subsuming /rabbit-refresh
template_version: 1.0.0
allowed-tools: Bash
---

Refreshing rabbit-workflow policy files.

!`echo 0 > .rabbit-prompt-counter`

!`for p in $(grep -oE '^@[^[:space:]]+' CLAUDE.md | sed 's/^@//'); do echo "=== $p ==="; cat "$p"; echo; done`

In one sentence, confirm which files were refreshed.
