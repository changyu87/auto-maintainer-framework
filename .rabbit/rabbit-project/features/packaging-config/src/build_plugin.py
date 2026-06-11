#!/usr/bin/env python3
"""build_plugin — deterministic clean-ship assembly for the auto-maintainer plugin.

Assembles the installable Claude Code plugin tree at
`<out_root>/plugins/auto-maintainer/` plus the marketplace catalog at
`<out_root>/.claude-plugin/marketplace.json`, copying ONLY the shippable
pieces out of the framework source and leaving ALL `.rabbit/` development
infrastructure behind (the clean-ship invariant).

Sources:
  - static plugin assets (hooks/, skills/) from this feature's
    `src/plugin_assets/`
  - core libs copied byte-for-byte from their feature `src/` dirs:
      .rabbit/rabbit-project/features/fsm-contracts/src/fsm_contracts.py
      .rabbit/rabbit-project/features/tick-orchestrator/src/tick_orchestrator.py

The build is deterministic and idempotent: it rebuilds the plugin tree from
scratch each run (removing any prior tree first) and emits byte-stable JSON,
so re-running on unchanged sources yields a byte-identical tree.

Version: 0.1.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when the framework adopts a different
  distribution channel than a self-hosted Claude Code plugin marketplace, or
  when a later slice folds packaging into a full configure/run UX feature.
"""

import json
import os
import shutil

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ASSETS = os.path.join(_FEATURE_DIR, "src", "plugin_assets")

_PLUGIN_NAME = "auto-maintainer"
_PLUGIN_VERSION = "0.1.0"
_DESCRIPTION = (
    "Auto-maintainer: an autonomous repository maintenance loop, "
    "shipped as a Claude Code plugin (packaging slice 1)."
)
_AUTHOR_NAME = "changyu87"

# Core libs to copy in: dest filename -> source path relative to repo_root
# (the worktree/repo root that contains the .rabbit/ dev tree).
_LIBS = {
    "fsm_contracts.py": os.path.join(
        ".rabbit", "rabbit-project", "features", "fsm-contracts", "src",
        "fsm_contracts.py",
    ),
    "tick_orchestrator.py": os.path.join(
        ".rabbit", "rabbit-project", "features", "tick-orchestrator", "src",
        "tick_orchestrator.py",
    ),
}


def _write_json(path, data):
    """Write byte-stable JSON: sorted keys, fixed separators, trailing NL."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")


def _copy_tree(src, dst):
    """Copy a directory tree deterministically (file bytes preserved)."""
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames.sort()
        rel = os.path.relpath(dirpath, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        for name in sorted(filenames):
            shutil.copyfile(
                os.path.join(dirpath, name),
                os.path.join(target_dir, name),
            )


def build(repo_root, out_root=None):
    """Assemble the clean plugin tree and marketplace catalog.

    Args:
      repo_root: the worktree/repo root that contains the .rabbit/ dev tree.
        The two core libs are read from
        repo_root/.rabbit/rabbit-project/features/*/src/.
      out_root: where to write outputs (default: repo_root). Produces:
          <out_root>/.claude-plugin/marketplace.json
          <out_root>/plugins/auto-maintainer/...
    """
    repo_root = os.path.abspath(repo_root)
    if out_root is None:
        out_root = repo_root
    out_root = os.path.abspath(out_root)

    plugin_root = os.path.join(out_root, "plugins", _PLUGIN_NAME)

    # Rebuild from scratch so stale files never linger (idempotency).
    if os.path.isdir(plugin_root):
        shutil.rmtree(plugin_root)

    # 1. Static assets: hooks/, skills/ (copied verbatim into the plugin root).
    _copy_tree(_ASSETS, plugin_root)

    # 2. plugin.json manifest at plugins/auto-maintainer/.claude-plugin/.
    _write_json(
        os.path.join(plugin_root, ".claude-plugin", "plugin.json"),
        {
            "name": _PLUGIN_NAME,
            "version": _PLUGIN_VERSION,
            "description": _DESCRIPTION,
            "author": {"name": _AUTHOR_NAME},
        },
    )

    # 3. Core libs copied byte-identical into lib/.
    lib_dir = os.path.join(plugin_root, "lib")
    os.makedirs(lib_dir, exist_ok=True)
    for dst_name, src_rel in sorted(_LIBS.items()):
        shutil.copyfile(
            os.path.join(repo_root, src_rel),
            os.path.join(lib_dir, dst_name),
        )

    # 4. Marketplace catalog at <out_root>/.claude-plugin/marketplace.json.
    _write_json(
        os.path.join(out_root, ".claude-plugin", "marketplace.json"),
        {
            "name": _PLUGIN_NAME,
            "owner": {"name": _AUTHOR_NAME},
            "plugins": [
                {
                    "name": _PLUGIN_NAME,
                    "source": "./plugins/auto-maintainer",
                    "description": _DESCRIPTION,
                    "version": _PLUGIN_VERSION,
                }
            ],
        },
    )

    return plugin_root


def main():
    # Default: build into the worktree/repo root (parent of .rabbit/).
    here = os.path.dirname(os.path.abspath(__file__))
    # src/ -> feature -> features -> rabbit-project -> .rabbit -> worktree root
    repo_root = os.path.abspath(
        os.path.join(here, "..", "..", "..", "..", "..")
    )
    build(repo_root=repo_root)


if __name__ == "__main__":
    main()
