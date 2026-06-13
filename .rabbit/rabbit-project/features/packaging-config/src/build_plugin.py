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
      .rabbit/rabbit-project/features/agent-dispatch/src/agent_dispatch.py
      .rabbit/rabbit-project/features/scheduling/src/run_tick.py
      .rabbit/rabbit-project/features/scheduling/src/status.py
      .rabbit/rabbit-project/features/scheduling/src/stop.py
      .rabbit/rabbit-project/features/scheduling/src/start.py
      .rabbit/rabbit-project/features/work-intake/src/work_intake.py
      .rabbit/rabbit-project/features/adapter-wiring/src/adapter_wiring.py
      .rabbit/rabbit-project/features/prioritize/src/prioritize.py
      .rabbit/rabbit-project/features/implement/src/implement.py
      .rabbit/rabbit-project/features/safety-governance/src/safety_governance.py
    The five pure libs are copied byte-for-byte; run_tick.py, status.py,
    stop.py, start.py, work_intake.py, adapter_wiring.py, prioritize.py,
    implement.py, and safety_governance.py are normalized so their sibling-lib
    imports resolve from the co-located lib/ dir alone (the shipped plugin
    carries only its own dir, so it cannot reach the feature src/ trees the dev
    copy resolves through). agent_dispatch.py is a PURE stdlib lib (imports only
    json, no sibling libs), so it is copied byte-for-byte alongside the other
    four pure libs — it is NOT normalized. status.py and stop.py import run_tick
    + the lifecycle/durable libs, start.py imports run_tick +
    lifecycle_dispositions, run_tick imports work_intake + adapter_wiring +
    prioritize + implement + safety_governance + agent_dispatch (and uses
    adapter_wiring.AgentState for its yield/resume --step/--resume CLI),
    work_intake imports fsm_contracts, adapter_wiring imports fsm_contracts +
    tick_orchestrator + agent_dispatch, prioritize + implement each import
    fsm_contracts, and safety_governance imports lifecycle_dispositions — so each
    gets the SAME self-path bootstrap, generalized here so they all resolve their
    siblings from lib/. agent_dispatch ships unmodified because it imports no
    siblings, so run_tick + adapter_wiring resolve it from lib/ once present.
    work_intake, prioritize, and implement do not import os/sys at module top,
    so their bootstrap variant imports them before the sys.path insert;
    adapter_wiring and safety_governance import os but not sys, so they use the
    same with-imports variant (re-importing os is harmless); start.py already
    imports os/sys at top so it uses the plain variant.

  - shipped components (collected automatically from feature ship/ dirs): the
    tick executor skill (scheduling's ship/skills/tick/SKILL.md) lands at
    skills/tick/SKILL.md and the auto-maintainer-echo subagent (scheduling's
    ship/agents/auto-maintainer-echo.md) lands at
    agents/auto-maintainer-echo.md — both via the ship/ collection convention,
    no build change needed.

The build is deterministic and idempotent: it rebuilds the plugin tree from
scratch each run (removing any prior tree first) and emits byte-stable JSON,
so re-running on unchanged sources yields a byte-identical tree.

Version: 0.2.12
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
_PLUGIN_VERSION = "0.2.12"
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
    # agent_dispatch is pure stdlib (imports only json, no sibling libs), so it
    # ships byte-for-byte alongside the other pure libs. run_tick +
    # adapter_wiring import it and resolve it from lib/ via their own bootstrap.
    "agent_dispatch.py": os.path.join(
        _FEATURES_REL, "agent-dispatch", "src", "agent_dispatch.py",
    ),
}

# The self-path bootstrap inserted before the anchor: it puts the file's own
# (co-located) dir on sys.path so sibling imports resolve from lib/ alone.
_SELF_PATH_BOOTSTRAP = (
    "# packaging-config: ship-time normalization — resolve sibling libs from\n"
    "# this file's own (co-located) dir so the shipped plugin is self-contained.\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
)

# work_intake's source does not import os/sys at module top (unlike run_tick),
# so its bootstrap variant imports them before the sys.path insert. Same effect:
# the file's own (co-located) dir on sys.path so its sibling fsm_contracts
# import resolves from lib/ alone.
_SELF_PATH_BOOTSTRAP_WITH_IMPORTS = (
    "# packaging-config: ship-time normalization — resolve sibling libs from\n"
    "# this file's own (co-located) dir so the shipped plugin is self-contained.\n"
    "import os  # noqa: E402\n"
    "import sys  # noqa: E402\n"
    "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
)

# These libs are normalized rather than copied byte-for-byte: each imports
# sibling libs that, in the shipped plugin, are flat neighbours in lib/. A
# self-path bootstrap is inserted right before each one's FIRST sibling import
# so the shipped copy resolves its siblings from the co-located lib/ dir alone
# (the shipped plugin carries only its own dir). Maps dest filename -> (source
# path relative to repo_root, the import-line anchor to insert the bootstrap
# before, the bootstrap snippet to insert).
_NORMALIZED_LIBS = {
    "run_tick.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "run_tick.py"),
        "import fsm_contracts as fc  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "status.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "status.py"),
        "import run_tick as rt  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "stop.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "stop.py"),
        "import run_tick as rt  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "start.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "start.py"),
        "import run_tick as rt  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "work_intake.py": (
        os.path.join(_FEATURES_REL, "work-intake", "src", "work_intake.py"),
        "import fsm_contracts as fc",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
    ),
    "adapter_wiring.py": (
        os.path.join(
            _FEATURES_REL, "adapter-wiring", "src", "adapter_wiring.py"
        ),
        "import fsm_contracts as fc",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
    ),
    "prioritize.py": (
        os.path.join(_FEATURES_REL, "prioritize", "src", "prioritize.py"),
        "import fsm_contracts as fc",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
    ),
    "implement.py": (
        os.path.join(_FEATURES_REL, "implement", "src", "implement.py"),
        "import fsm_contracts as fc",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
    ),
    "safety_governance.py": (
        os.path.join(
            _FEATURES_REL, "safety-governance", "src", "safety_governance.py"
        ),
        "import lifecycle_dispositions as ld",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
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


def _normalize_lib(src_path, anchor, bootstrap):
    """Read a control lib and return its self-contained variant.

    The dev copy resolves its sibling libs via ../<dep>/src on sys.path; the
    shipped copy lives in lib/ alongside those libs, so we insert a self-path
    bootstrap (lib/ on sys.path) right before the FIRST sibling import (the
    given anchor line). Any original ../<dep>/src loop stays harmless: those
    dirs do not exist in the plugin. `bootstrap` is the snippet to insert (the
    plain variant for files that already import os/sys, the with-imports variant
    for work_intake which does not).
    """
    with open(src_path, "r", encoding="utf-8") as fh:
        body = fh.read()
    if anchor not in body:
        raise RuntimeError(
            f"normalization anchor {anchor!r} not found in {src_path}"
        )
    return body.replace(
        anchor,
        bootstrap + anchor,
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

    # 3. Core libs into lib/. The five pure libs are copied byte-identical;
    #    run_tick.py, status.py, stop.py, and work_intake.py are normalized for
    #    self-contained sibling imports.
    lib_dir = os.path.join(plugin_root, "lib")
    os.makedirs(lib_dir, exist_ok=True)
    for dst_name, src_rel in sorted(_LIBS.items()):
        shutil.copyfile(
            os.path.join(repo_root, src_rel),
            os.path.join(lib_dir, dst_name),
        )
    for dst_name, (src_rel, anchor, bootstrap) in sorted(
        _NORMALIZED_LIBS.items()
    ):
        with open(
            os.path.join(lib_dir, dst_name), "w", encoding="utf-8"
        ) as fh:
            fh.write(_normalize_lib(
                os.path.join(repo_root, src_rel), anchor, bootstrap))

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
