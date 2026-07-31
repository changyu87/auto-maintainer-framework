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
      .rabbit/rabbit-project/features/observability/src/observability.py
      .rabbit/rabbit-project/features/scheduling/src/run_tick.py
      .rabbit/rabbit-project/features/scheduling/src/status.py
      .rabbit/rabbit-project/features/scheduling/src/stop.py
      .rabbit/rabbit-project/features/scheduling/src/start.py
      .rabbit/rabbit-project/features/work-intake/src/work_intake.py
      .rabbit/rabbit-project/features/adapter-wiring/src/adapter_wiring.py
      .rabbit/rabbit-project/features/prioritize/src/prioritize.py
      .rabbit/rabbit-project/features/implement/src/implement.py
      .rabbit/rabbit-project/features/safety-governance/src/safety_governance.py
      .rabbit/rabbit-project/features/safety-governance/src/configure.py
      .rabbit/rabbit-project/features/verify-integrate/src/verify_integrate.py
    The six pure libs are copied byte-for-byte; run_tick.py, status.py,
    stop.py, start.py, work_intake.py, adapter_wiring.py, prioritize.py,
    implement.py, safety_governance.py, configure.py, and verify_integrate.py
    are normalized so their
    sibling-lib imports resolve from the co-located lib/ dir alone (the shipped plugin
    carries only its own dir, so it cannot reach the feature src/ trees the dev
    copy resolves through). agent_dispatch.py and observability.py are PURE
    stdlib libs (agent_dispatch imports only json; observability imports only
    json + os; neither imports a sibling lib), so each is copied byte-for-byte
    alongside the other pure libs — neither is normalized. run_tick imports
    observability (`import observability as ob`) and emits a structured tick
    event log to ${runtime_dir}/events.jsonl via observability.EventLog;
    run_tick's existing self-path bootstrap resolves observability from lib/ once
    shipped, so observability needs no normalization entry. status.py and stop.py
    import run_tick + the lifecycle/durable libs, start.py imports run_tick +
    lifecycle_dispositions, run_tick imports work_intake + adapter_wiring +
    prioritize + implement + safety_governance + agent_dispatch + observability
    (and uses
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
    same with-imports variant (re-importing os is harmless); start.py and
    configure.py already import os/sys at top so they use the plain variant.
    configure.py is safety_governance's governance-config writer; it imports its
    sibling safety_governance (`import safety_governance as sg`), so its plain
    bootstrap resolves safety_governance from lib/ once shipped.
    verify_integrate.py is the VERIFY/INTEGRATE/CLEANUP lib; it imports its
    siblings fsm_contracts (its first import, the bootstrap anchor) and
    safety_governance, and does not import os/sys at module top, so it takes the
    with-imports bootstrap (the same as prioritize/implement/work_intake),
    resolving fsm_contracts + safety_governance from lib/ once shipped.

  - shipped components (collected automatically from feature ship/ dirs): the
    tick executor skill (scheduling's ship/skills/tick/SKILL.md) lands at
    skills/tick/SKILL.md and the auto-maintainer-echo subagent (scheduling's
    ship/agents/auto-maintainer-echo.md) lands at
    agents/auto-maintainer-echo.md — both via the ship/ collection convention,
    no build change needed.

  - a plugin-internal README.md generated at the plugin root
    (plugins/auto-maintainer/README.md), per the Claude Code plugin docs best
    practice ("Add a README.md with installation and usage instructions"). Its
    Commands table is DERIVED from the shipped skills/ dir so it lists every
    shipped slash command (a skill with no curated description fails the build).

The build is deterministic and idempotent: it rebuilds the plugin tree from
scratch each run (removing any prior tree first) and emits byte-stable JSON,
so re-running on unchanged sources yields a byte-identical tree.

Version: 0.7.3
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
_PLUGIN_VERSION = "0.29.0"
_DESCRIPTION = (
    "Auto-maintainer: an autonomous repository maintenance loop, "
    "shipped as a Claude Code plugin."
)
_AUTHOR_NAME = "changyu87"

# Curated one-line descriptions for each shipped slash command, keyed by the
# skill dir name (= the command suffix, /auto-maintainer:<name>). The README's
# Commands table is GENERATED from the shipped skills/ dir (so it stays complete
# — every shipped skill MUST appear), and each row's prose is looked up here.
# Adding a new ship/skills/<name> without a matching entry fails the build, which
# forces this map (and thus the shipped README) to stay in sync with the skills.
_COMMAND_DESCRIPTIONS = {
    "start": (
        "Start (or resume) the maintainer's in-session tick loop: runs the "
        "first tick now and schedules a recurring heartbeat that keeps ticking "
        "until stopped."
    ),
    "stop": (
        "Stop the tick loop — latches it STOPPED and cancels the scheduled "
        "heartbeat so no further ticks run."
    ),
    "status": (
        "Report the loop's real on-disk status: current disposition and the "
        "last pull's persisted work-items count."
    ),
    "tick": (
        "Run exactly one tick, including any subagent (agent-state) "
        "dispatches, then report the result."
    ),
    "configure": (
        "Set the maintainer's trust mode (dry-run / propose / auto-merge), "
        "per-day token budget, heartbeat interval, and backoff threshold in the "
        "central config (.auto-maintainer/config.json)."
    ),
    "route": (
        "View and edit the loop's route — the ordered state graph "
        "(GUARD -> DRAIN -> PULL -> ... -> PERSIST -> EXIT) the tick runner "
        "walks each tick. Every edit is validated before it is saved."
    ),
    "adapter-map": (
        "View and edit the loop's adapter map — which adapter implements each "
        "route port (GUARD, DRAIN, PULL, TRIAGE, PRIORITIZE, IMPLEMENT, VERIFY, "
        "REVIEW, INTEGRATE, CLEANUP, PERSIST, EXIT). Every edit is validated "
        "before it is saved."
    ),
    "clobber": (
        "Reset the loop for a clean start: clears runtime state (durable state, "
        "disposition, events log, dispatch outputs) while preserving your "
        "config; confirms before it deletes anything."
    ),
}

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
    # observability is pure stdlib (imports only json + os, no sibling libs), so
    # it ships byte-for-byte alongside the other pure libs. run_tick imports it
    # (`import observability as ob`) and emits structured tick events; run_tick's
    # own bootstrap resolves it from lib/ once shipped, so it is NOT normalized.
    "observability.py": os.path.join(
        _FEATURES_REL, "observability", "src", "observability.py",
    ),
    # test_gate is the IMPLEMENT doer's deterministic test gate. It imports only
    # stdlib (argparse/json/os/subprocess/sys) and NO sibling lib, so it ships
    # byte-for-byte alongside the other pure libs — NOT normalized.
    "test_gate.py": os.path.join(
        _FEATURES_REL, "implement", "src", "test_gate.py",
    ),
}

# build_plugin (the clean-ship assembler itself) is deliberately NOT shipped into
# the plugin lib/: it references the .rabbit/ dev tree it reads from, which the
# clean-ship leak guard correctly forbids in the installed plugin (an install has
# no .rabbit/ tree). Self-deploy (#309) therefore fires ONLY inside the framework's
# OWN checkout — the only place that carries the .rabbit/ build tree build() needs
# AND the dev feature src run_tick resolves build_plugin from. In any other install
# run_tick's optional build_plugin import is absent and the PACKAGE flush is dormant.

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
    # clobber is scheduling's loop-reset control lib (the /auto-maintainer:clobber
    # skill's companion). Its FIRST sibling import is run_tick (the bootstrap
    # anchor; it also imports lifecycle_dispositions + heartbeat), and it already
    # imports os/sys at module top, so it takes the PLAIN self-path bootstrap —
    # exactly like status.py — resolving its siblings from the co-located lib/.
    "clobber.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "clobber.py"),
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
    "configure.py": (
        os.path.join(_FEATURES_REL, "safety-governance", "src", "configure.py"),
        "import safety_governance as sg",
        _SELF_PATH_BOOTSTRAP,
    ),
    "verify_integrate.py": (
        os.path.join(
            _FEATURES_REL, "verify-integrate", "src", "verify_integrate.py"
        ),
        "import fsm_contracts as fc",
        _SELF_PATH_BOOTSTRAP_WITH_IMPORTS,
    ),
    # route_config + adapter_map_config are scheduling's wiring-config editor
    # CLIs (the v0.3.0 configurables overhaul). Each imports its sibling
    # adapter_wiring (its FIRST sibling import, the bootstrap anchor) plus
    # run_tick (adapter_map_config also imports agent_dispatch/work_intake/
    # implement), and each already imports os/sys at module top, so each takes
    # the PLAIN self-path bootstrap (the same as run_tick/status/start/configure)
    # inserted before the adapter_wiring import — resolving every sibling from
    # the co-located lib/ alone.
    # heartbeat is scheduling's durable loop-intent + cross-session auto-resume
    # decision lib (the #31 durable heartbeat). Its FIRST (and only) sibling
    # import is lifecycle_dispositions (the bootstrap anchor), and it already
    # imports os/sys at module top before that anchor, so it takes the PLAIN
    # self-path bootstrap (the same as run_tick/start/configure) — resolving
    # lifecycle_dispositions from the co-located lib/ alone. The shipped
    # session-start-resume.py hook resolves heartbeat from ../lib via its own
    # path insert.
    "heartbeat.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "heartbeat.py"),
        "import lifecycle_dispositions as ld  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "route_config.py": (
        os.path.join(_FEATURES_REL, "scheduling", "src", "route_config.py"),
        "import adapter_wiring as aw  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
    ),
    "adapter_map_config.py": (
        os.path.join(
            _FEATURES_REL, "scheduling", "src", "adapter_map_config.py"
        ),
        "import adapter_wiring as aw  # noqa: E402",
        _SELF_PATH_BOOTSTRAP,
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


def _render_readme(plugin_root):
    """Render the plugin-internal README.md from the assembled plugin tree.

    The Commands table is DERIVED from the shipped skills/ dir (each
    skills/<name>/ is a /auto-maintainer:<name> command), so the README can
    never omit a shipped command — a skill with no curated description in
    _COMMAND_DESCRIPTIONS fails the build, keeping the docs complete. The Status
    section states the accurate, current reality: a working autonomous
    maintainer with the full pull->triage->implement->verify->integrate->report
    loop live-proven (NOT a "packaging skeleton" — that wording is stale).
    """
    skills_dir = os.path.join(plugin_root, "skills")
    skill_names = sorted(
        name for name in os.listdir(skills_dir)
        if os.path.isdir(os.path.join(skills_dir, name))
    ) if os.path.isdir(skills_dir) else []

    missing = [n for n in skill_names if n not in _COMMAND_DESCRIPTIONS]
    if missing:
        raise RuntimeError(
            "shipped skills with no README command description (add them to "
            f"_COMMAND_DESCRIPTIONS so the README stays complete): {missing}"
        )

    command_rows = "\n".join(
        f"| `/auto-maintainer:{name}` | {_COMMAND_DESCRIPTIONS[name]} |"
        for name in skill_names
    )

    return f"""# {_PLUGIN_NAME}

**An autonomous repository maintenance loop, shipped as a Claude Code plugin.**

This is the installed plugin tree. It bundles the maintainer's tick-FSM core, its
default GitHub-Issues + git adapters, and the slash commands that drive the loop.
For the framework's source, design, and roadmap, see the project repository:
<https://github.com/{_AUTHOR_NAME}/auto-maintainer-framework>.

## Status

**v1 complete — a working autonomous maintainer.** The full
pull -> triage -> implement -> verify -> integrate -> report loop is live-proven:
the loop pulls open issues, triages them, opens labelled pull requests for the
work it accepts, verifies and integrates its own PRs, and reports back — driven
by a session-mediated heartbeat. See the repository's `docs/ROADMAP.md` for
per-feature status and what is still being hardened.

## Requirements

- **[Claude Code](https://docs.anthropic.com/en/docs/claude-code)** — this is a
  Claude Code plugin and is installed and run from inside Claude Code.
- **The [`gh`](https://cli.github.com/) CLI, installed and authenticated** — the
  default GitHub adapters shell out to `gh` for issues and pull requests. Run
  `gh auth login` once so it can talk to your repository.

## Installation

Distributed as a **Claude Code plugin** served from a self-hosted marketplace.
Inside Claude Code, run these three steps:

```
/plugin marketplace add {_AUTHOR_NAME}/auto-maintainer-framework
/plugin install {_PLUGIN_NAME}@{_PLUGIN_NAME}
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
/plugin uninstall {_PLUGIN_NAME}@{_PLUGIN_NAME}   # remove the plugin
```

## Usage

1. **Configure** the loop's trust mode and budget once with
   `/auto-maintainer:configure` (start in `dry-run` or `propose` to watch what
   it would do before letting it act).
2. **Start** the loop with `/auto-maintainer:start` — it runs the first tick now
   and schedules a recurring heartbeat that keeps ticking.
3. **Check** progress any time with `/auto-maintainer:status`, run a single tick
   on demand with `/auto-maintainer:tick`, and **stop** the loop with
   `/auto-maintainer:stop`.
4. **Customize** the loop's route and adapter map with `/auto-maintainer:route`
   and `/auto-maintainer:adapter-map` (the shipped defaults work out of the box).

## Commands

Once installed, the plugin provides these slash commands:

| Command | Description |
| --- | --- |
{command_rows}

## Layout

This installed plugin carries:

- `.claude-plugin/plugin.json` — the plugin manifest.
- `skills/` — the slash-command skills listed above.
- `agents/` — the subagents the tick loop dispatches (triager, implementer, …).
- `hooks/` — the SessionStart persona/banner hook.
- `lib/` — the self-contained Python control libraries the skills invoke.
"""


def _write_readme(plugin_root):
    """Write the rendered plugin-internal README.md to the plugin root."""
    with open(
        os.path.join(plugin_root, "README.md"), "w", encoding="utf-8"
    ) as fh:
        fh.write(_render_readme(plugin_root))


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

    # The version is the in-memory _PLUGIN_VERSION constant — the single source
    # of truth, bumped by an operator edit to this file (releases are
    # operator-cut; the self-deploy build rewrite was removed with the
    # self-deploy action #324 + knob #325).
    plugin_version = _PLUGIN_VERSION

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
            "version": plugin_version,
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

    # 3b. Plugin-internal README.md at plugins/auto-maintainer/README.md, per
    #     the Claude Code plugin docs best practice ("Add a README.md with
    #     installation and usage instructions"). Its Commands table is DERIVED
    #     from the shipped skills/ dir (assembled in step 1b above), so it can
    #     never omit a shipped slash command.
    _write_readme(plugin_root)

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
                    "version": plugin_version,
                }
            ],
        },
    )

    return plugin_root


# --------------------------------------------------------------------------
# Release-detection support: the dev-tree marker + the shipped-source change
# detector that scheduling's `release_needed` operator signal uses to surface
# when a merged PR's diff changed shipped bytes (so an operator knows a release
# is due). The plugin is NOT self-deployable: the self-deploy ACTION was removed
# (scheduling #324) and the self_deploy knob was removed (safety-governance
# #325), so the dead build helpers (bump_version, package_commit_paths, the
# same-process disk version-read) that only served that action are gone too —
# releases are operator-cut by editing _PLUGIN_VERSION above.
# --------------------------------------------------------------------------

# The current shipped plugin version (the single source of truth, mirrored into
# plugin.json + marketplace.json by build()).
PLUGIN_VERSION = _PLUGIN_VERSION

# The repo-root-relative path of THIS build source: the dev-tree marker
# scheduling walks up from project_dir to find — its presence identifies the
# framework's OWN checkout (the only tree carrying the dev .rabbit/ build tree).
# Defined HERE (not in scheduling) so the shipped run_tick carries NO .rabbit
# literal — the clean-ship leak guard forbids it; build_plugin is NOT shipped, so
# it may reference .rabbit freely.
SELF_DEPLOY_MARKER = os.path.join(
    _FEATURES_REL, "packaging-config", "src", "build_plugin.py")


def touches_shipped_src(changed_paths):
    """True when any path in `changed_paths` (repo-root-relative, POSIX-style) is
    a shipped source path (the self-deploy rebuild trigger, #309).

    A path triggers a rebuild when it is one of the exact copied/normalized lib
    source files OR it lives under a shipped DIR — a feature `ship/` tree or
    packaging-config's `plugin_assets/`. A path matching none of these (docs/,
    test/, feature.json, etc.) does NOT trigger a rebuild, so a docs-only or
    test-only merge never churns the plugin version.
    """
    features_rel = _FEATURES_REL.replace(os.sep, "/")
    exact = set()
    for src_rel in _LIBS.values():
        exact.add(src_rel.replace(os.sep, "/"))
    for src_rel, _anchor, _bootstrap in _NORMALIZED_LIBS.values():
        exact.add(src_rel.replace(os.sep, "/"))
    assets_prefix = f"{features_rel}/packaging-config/src/plugin_assets/"
    for raw in changed_paths or []:
        path = raw.replace(os.sep, "/")
        if path.startswith("./"):
            path = path[2:]
        if path in exact:
            return True
        if path.startswith(assets_prefix):
            return True
        # A feature ship/ tree: <features_rel>/<feature>/ship/...
        if path.startswith(features_rel + "/") and "/ship/" in path:
            return True
    return False


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
