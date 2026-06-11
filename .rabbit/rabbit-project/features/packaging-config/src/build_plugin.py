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
  - each feature's `ship/` directory, collected into the plugin root (the
    extensible "ship/ collection" convention: a feature owns the components it
    ships, e.g. scheduling's ship/skills/{start,stop})
  - core libs copied from their feature `src/` dirs into lib/:
      .rabbit/rabbit-project/features/fsm-contracts/src/fsm_contracts.py
      .rabbit/rabbit-project/features/tick-orchestrator/src/tick_orchestrator.py
      .rabbit/rabbit-project/features/durable-state/src/durable_state.py
      .rabbit/rabbit-project/features/lifecycle-dispositions/src/lifecycle_dispositions.py
      .rabbit/rabbit-project/features/scheduling/src/run_tick.py
    The four pure libs are copied byte-for-byte; run_tick.py is normalized so
    its sibling-lib imports resolve from the co-located lib/ dir alone (the
    shipped plugin carries only its own dir, so it cannot reach the feature
    src/ trees the dev copy resolves through).

The build is deterministic and idempotent: it rebuilds the plugin tree from
scratch each run (removing any prior tree first) and emits byte-stable JSON,
so re-running on unchanged sources yields a byte-identical tree.

Version: 0.2.1
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
# The features dir holds every feature's src/ and ship/ trees.
_FEATURES_REL = os.path.join(
    ".rabbit", "rabbit-project", "features",
)

_PLUGIN_NAME = "auto-maintainer"
_PLUGIN_VERSION = "0.2.1"
_DESCRIPTION = (
    "Auto-maintainer: an autonomous repository maintenance loop, "
    "shipped as a Claude Code plugin."
)
_AUTHOR_NAME = "changyu87"

# Pure core libs copied byte-for-byte: dest filename -> source path relative to
# repo_root (the worktree/repo root that contains the .rabbit/ dev tree).
_LIBS = {
    "fsm_contracts.py": os.path.join(
        _FEATURES_REL, "fsm-contracts", "src", "fsm_contracts.py",
    ),
    "tick_orchestrator.py": os.path.join(
        _FEATURES_REL, "tick-orchestrator", "src", "tick_orchestrator.py",
    ),
    "durable_state.py": os.path.join(
        _FEATURES_REL, "durable-state", "src", "durable_state.py",
    ),
    "lifecycle_dispositions.py": os.path.join(
        _FEATURES_REL, "lifecycle-dispositions", "src",
        "lifecycle_dispositions.py",
    ),
}

# run_tick.py is normalized rather than copied byte-for-byte: its sibling-lib
# imports must resolve from the co-located lib/ dir alone. Source path relative
# to repo_root.
_RUN_TICK_REL = os.path.join(_FEATURES_REL, "scheduling", "src", "run_tick.py")

# Marker the normalization inserts a self-path bootstrap BEFORE, so the shipped
# run_tick puts its own dir (lib/) on sys.path ahead of importing its siblings.
_RUN_TICK_IMPORT_ANCHOR = "import fsm_contracts as fc  # noqa: E402"
_RUN_TICK_SELF_PATH = (
    "# packaging-config: ship-time normalization — resolve sibling libs from\n"
    "# this file's own (co-located) dir so the shipped plugin is self-contained.\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
)


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


def _normalize_run_tick(src_path):
    """Read scheduling's run_tick.py and return its self-contained variant.

    The dev copy resolves its sibling libs via ../<dep>/src on sys.path; the
    shipped copy lives in lib/ alongside those libs, so we insert a self-path
    bootstrap (lib/ on sys.path) right before the sibling imports. The original
    ../<dep>/src loop stays harmless: those dirs do not exist in the plugin.
    """
    with open(src_path, "r", encoding="utf-8") as fh:
        body = fh.read()
    if _RUN_TICK_IMPORT_ANCHOR not in body:
        raise RuntimeError(
            f"run_tick.py normalization anchor not found in {src_path}"
        )
    return body.replace(
        _RUN_TICK_IMPORT_ANCHOR,
        _RUN_TICK_SELF_PATH + _RUN_TICK_IMPORT_ANCHOR,
        1,
    )


def build(repo_root, out_root=None):
    """Assemble the clean plugin tree and marketplace catalog.

    Args:
      repo_root: the worktree/repo root that contains the .rabbit/ dev tree.
        Libs and ship/ trees are read from
        repo_root/.rabbit/rabbit-project/features/*/.
      out_root: where to write outputs (default: repo_root). Produces:
          <out_root>/.claude-plugin/marketplace.json
          <out_root>/plugins/auto-maintainer/...
    """
    repo_root = os.path.abspath(repo_root)
    if out_root is None:
        out_root = repo_root
    out_root = os.path.abspath(out_root)

    plugin_root = os.path.join(out_root, "plugins", _PLUGIN_NAME)
    features_dir = os.path.join(repo_root, _FEATURES_REL)

    # Rebuild from scratch so stale files never linger (idempotency).
    if os.path.isdir(plugin_root):
        shutil.rmtree(plugin_root)

    # 1. Static assets: hooks/, skills/ (copied verbatim into the plugin root).
    _copy_tree(_ASSETS, plugin_root)

    # 1b. ship/ collection: each feature's ship/ contents land at the plugin
    #     root (skills/, hooks/, …). Features are walked in sorted order so the
    #     build stays deterministic.
    for feature in sorted(os.listdir(features_dir)):
        ship_dir = os.path.join(features_dir, feature, "ship")
        if os.path.isdir(ship_dir):
            _copy_tree(ship_dir, plugin_root)

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

    # 3. Core libs into lib/. The four pure libs are copied byte-identical;
    #    run_tick.py is normalized for self-contained sibling imports.
    lib_dir = os.path.join(plugin_root, "lib")
    os.makedirs(lib_dir, exist_ok=True)
    for dst_name, src_rel in sorted(_LIBS.items()):
        shutil.copyfile(
            os.path.join(repo_root, src_rel),
            os.path.join(lib_dir, dst_name),
        )
    with open(
        os.path.join(lib_dir, "run_tick.py"), "w", encoding="utf-8"
    ) as fh:
        fh.write(_normalize_run_tick(os.path.join(repo_root, _RUN_TICK_REL)))

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
