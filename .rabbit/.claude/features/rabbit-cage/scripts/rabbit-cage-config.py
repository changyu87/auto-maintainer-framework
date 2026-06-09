#!/usr/bin/env python3
"""rabbit-cage-config.py — backs the /rabbit-cage-config slash command (spec Inv 40).

THIN wrapper over contract.lib.config_dispatch.dispatch_config for rabbit-cage's
five genuinely-owned configurables (scope-guard, bypass-permissions,
allowed-tools, bash-allow, prompt-threshold). Phase 3 of #733: each per-feature
config command delegates validation + mutation + restart-prompt rendering to the
shared helper so the generic interpreter logic lives ONCE in contract.lib
(script > prompt; no N drifting copies).

This script owns ONLY argv parsing and IO. It:
  1. resolves repo_root (RABBIT_ROOT env in plugin mode, else
     `git rev-parse --show-toplevel`),
  2. reads rabbit-cage's OWN feature.json configuration[],
  3. finds the entry whose `subcommand` matches argv[1],
  4. calls dispatch_config(cfg, value, repo_root=..., feature_dir=...,
     template_value=...),
  5. prints result["messages"] then result["restart_prompt"] (when present),
     and exits non-zero with result["error"] to stderr on failure.

It MUST NOT re-implement the values/actions interpreter, the template
substitution, the validation rules, or the restart-prompt framing.

Usage:
  rabbit-cage-config.py <subcommand> <value-or-action> [<template-value>]

    values-style:  rabbit-cage-config <subcommand> <value>
                   (e.g. scope-guard on, bypass-permissions true)
    actions-style: rabbit-cage-config <subcommand> <action> [<template-value>]
                   (e.g. bash-allow add npm, prompt-threshold set 30)

Exit: 0 success, 1 dispatch/mutation error, 2 bad invocation.

Version: 1.1.0
Owner: rabbit-workflow team
Deprecation criterion: when the rabbit CLI exposes a native per-feature
    configuration mechanism that subsumes /rabbit-cage-config.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

# This script lives at .claude/features/rabbit-cage/scripts/; the rabbit-cage
# feature dir is its parent's parent.
FEATURE_DIR = Path(__file__).resolve().parents[1]


# Permission-bypass guidance — surfaced ON-DEMAND via the help/query path only
# (issue #914). It used to print on EVERY SessionStart as a welcome subline
# (issue #889); the always-on startup advertisement was removed so this useful
# but non-urgent information shows only when explicitly queried. The message
# content/branding is preserved verbatim: the ephemeral live toggle AND the
# persisted rabbit-native path. This is NOT the scope-override SAFETY notice
# (which still fires on startup) — only the permission-bypass info message moved.
PERMISSION_BYPASS_HELP = (
    "permission bypass — live toggle: Shift+Tab (ephemeral, this session); "
    "persisted: /rabbit-cage-config bypass-permissions true|false "
    "(writes defaultMode, takes effect after a Claude relaunch)"
)


def usage() -> None:
    sys.stderr.write(
        "usage:\n"
        "  rabbit-cage-config.py <subcommand> <value-or-action> [<template-value>]\n"
    )


def repo_root() -> Path:
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return Path(env)
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        # scripts/rabbit-cage-config.py -> parents: [0]=scripts [1]=rabbit-cage
        # [2]=features [3]=.claude [4]=repo_root
        return Path(__file__).resolve().parents[4]


def _load_configuration(rroot: Path):
    """Read rabbit-cage's OWN configuration[] from its feature.json."""
    fj = rroot / ".claude/features/rabbit-cage/feature.json"
    with open(fj) as f:
        data = json.load(f)
    return data.get("configuration") or []


def main() -> int:
    args = sys.argv[1:]
    if not args:
        usage()
        return 2
    if args[0] in ("-h", "--help", "help"):
        usage()
        # On-demand surface for the permission-bypass guidance (issue #914):
        # print to stdout so the query path emits it on explicit request.
        print(PERMISSION_BYPASS_HELP)
        return 0

    subcommand = args[0]
    rroot = repo_root()

    # Make contract.lib importable the same way the other rabbit-cage scripts do.
    contract_dir = rroot / ".claude/features/contract"
    if str(contract_dir) not in sys.path:
        sys.path.insert(0, str(contract_dir))
    try:
        from lib.config_dispatch import dispatch_config  # noqa: PLC0415
    except ImportError as e:
        sys.stderr.write(
            f"rabbit-cage-config: cannot import contract.lib.config_dispatch: {e}\n")
        return 1

    configuration = _load_configuration(rroot)
    matches = [c for c in configuration if c.get("subcommand") == subcommand]
    if not matches:
        known = sorted({c.get("subcommand", "") for c in configuration
                        if c.get("command") == "rabbit-cage-config"})
        sys.stderr.write(
            f"rabbit-cage-config: unknown subcommand '{subcommand}'\n")
        sys.stderr.write(f"Known subcommands: {', '.join(known)}\n")
        return 2
    cfg = matches[0]

    # values-style takes <value>; actions-style takes <action> [<template-value>].
    if len(args) < 2:
        sys.stderr.write(
            f"rabbit-cage-config {subcommand}: requires a value or action\n")
        return 2
    value = args[1]
    template_value = args[2] if len(args) > 2 else None

    result = dispatch_config(
        cfg, value,
        repo_root=str(rroot),
        feature_dir=str(FEATURE_DIR),
        template_value=template_value,
    )

    for msg in result.get("messages") or []:
        print(msg)

    if not result.get("ok"):
        err = result.get("error")
        if err:
            sys.stderr.write(f"{err}\n")
        return 1

    restart = result.get("restart_prompt")
    if restart:
        print(restart)
    return 0


if __name__ == "__main__":
    sys.exit(main())
