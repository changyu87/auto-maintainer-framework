---
name: status
description: Report the current state of the auto-maintainer plugin and its maintainer loop. Use this whenever the user runs /auto-maintainer:status, or asks what the auto-maintainer is doing, whether a maintainer loop is configured or running, or what the plugin's current state is.
version: 0.1.0
owner: rabbit-workflow team
deprecation_criterion: Superseded when the maintainer loop ships real adapters and this reporter is replaced by a status command that reads live loop state.
---

# auto-maintainer status

Report the plugin's current operating state to the user.

This is packaging slice 1 — a walking skeleton. The maintainer loop's real
adapters do not exist yet, so there is no live loop state to read. The plugin
is installed and loaded, but nothing is being maintained.

When invoked, report exactly:

```
auto-maintainer: no loop configured yet (packaging slice 1).
The plugin is installed and loaded; no maintainer loop adapters are wired.
```

Do not fabricate loop activity, tracked repositories, schedules, or budgets —
none exist at this slice. If the user asks to configure or start a loop,
explain that adapter wiring and configuration arrive in a later slice and are
not available yet.
