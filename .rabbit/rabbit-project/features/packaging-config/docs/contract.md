---
feature: packaging-config
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the framework adopts a different distribution channel, or when this folds into a full configure/run UX feature (see feature.json / spec.md).
---

# packaging-config — Contract

```json
{
  "provides": {
    "files": [
      ".claude-plugin/marketplace.json (repo-root marketplace catalog)",
      "plugins/auto-maintainer/ (clean, committed plugin tree; no .rabbit)",
      "plugins/auto-maintainer/.claude-plugin/plugin.json",
      "plugins/auto-maintainer/hooks/hooks.json (SessionStart persona/banner)",
      "plugins/auto-maintainer/skills/status/SKILL.md"
    ],
    "scripts": ["src/build_plugin.py (deterministic plugin-tree assembly)"],
    "skills": ["auto-maintainer:status (shipped inside the plugin)"]
  },
  "reads": {
    "files": [
      "rabbit-project/features/fsm-contracts/src/* (copied into plugin lib/)",
      "rabbit-project/features/tick-orchestrator/src/* (copied into plugin lib/)"
    ],
    "external": ["Claude Code plugin/marketplace schema (code.claude.com/docs)"]
  },
  "invokes": {
    "scripts": [],
    "agents": []
  },
  "never": [
    "includes any .rabbit/ dev infrastructure in the shipped plugin tree",
    "places component dirs (hooks/skills/lib) inside .claude-plugin/",
    "references files outside the plugin directory from within the plugin",
    "implements maintainer-loop adapters or userConfig/adapter-wiring (deferred)"
  ]
}
```
