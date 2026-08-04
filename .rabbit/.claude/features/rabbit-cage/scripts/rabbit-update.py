#!/usr/bin/env python3
"""rabbit-update.py — backs the /rabbit-update slash command (spec Inv 35).

Two deterministic subcommands:

  check    Non-mutating current-vs-latest release probe. Prints a single JSON
           line: {"current", "latest", "newer", "self_update_available"}.
           Reuses contract's check-release-update.py fetch/compare helpers
           (read_version / fetch_upstream_version / resolve_repo_root /
           probe_self_update) rather than duplicating the urllib fetch or the
           version compare. Unlike the SessionStart check_release_update path,
           this probe is NOT throttled and writes NO throttle file — an
           explicit user request always forces a fresh probe. For deterministic
           testing the latest tag may be injected via RABBIT_UPDATE_TEST_LATEST,
           bypassing the network fetch.

  install  Applies the self-update by invoking the existing install.py
           self-update path: <rabbit_root>/install.py --update (the SOLE update
           mechanism per spec Inv 22). No fetch/copy logic is re-implemented
           here; install.py owns the in-place refresh. After a SUCCESSFUL
           update, it diffs restart-sensitive surfaces (the .claude/hooks,
           .claude/skills, and .claude/agents trees, any .claude/settings*.json,
           and CLAUDE.md) across the install via a
           content-hash signature; if any changed it WRITES the restart-needed
           marker `<rabbit_root>/.rabbit-update-restart-needed` that contract's
           SessionStart update banner reads + consumes (contract Inv 39), so the
           next session alerts the user to restart. A no-change update or a
           FAILED install (non-zero exit) writes no marker — no false alert.

repo_root resolves from RABBIT_ROOT (plugin mode) when set, otherwise from
`git rev-parse --show-toplevel`, mirroring the other rabbit-cage scripts.

Usage:
  rabbit-update.py check
  rabbit-update.py install

Exit: 0 success, 1 error, 2 bad invocation.

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: when the rabbit CLI exposes a native self-update
    command that subsumes /rabbit-update.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_REPO = "changyu87/rabbit-workflow"

# Inv 35 (#1099) — the WRITE side of contract's #1096 restart alert. The marker
# name MUST match contract's reader byte-for-byte (contract/lib/runtime.py
# `_UPDATE_RESTART_MARKER`); the SessionStart update banner reads + consumes it.
RESTART_MARKER = ".rabbit-update-restart-needed"

# Surfaces whose change requires a Claude Code restart to take effect: the
# hooks/skills/agents trees, any settings*.json under .claude/, and CLAUDE.md at
# the root. The skills/agents trees match the Inv 54 mid-session monitored set
# so the install path and the mid-session snapshot agree on what is
# restart-sensitive.
RESTART_SENSITIVE_DIRS = (".claude/hooks", ".claude/skills", ".claude/agents")
RESTART_SENSITIVE_FILES = ("CLAUDE.md",)


def usage() -> None:
    sys.stderr.write(
        "usage:\n"
        "  rabbit-update.py check\n"
        "  rabbit-update.py install\n"
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
        # scripts/rabbit-update.py -> parents: [0]=scripts [1]=rabbit-cage
        # [2]=features [3]=.claude [4]=repo_root
        return Path(__file__).resolve().parents[4]


def _load_check_release(rroot: Path):
    """Import the contract-owned release-check helpers (Inv 35 reuse)."""
    path = rroot / ".claude/features/contract/scripts/check-release-update.py"
    spec = importlib.util.spec_from_file_location(
        "rabbit_check_release_update", str(path))
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load check-release-update.py from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cmd_check(rroot: Path) -> int:
    try:
        cru = _load_check_release(rroot)
    except (ImportError, OSError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 1

    version_path = os.path.join(str(rroot), ".version")
    current = cru.read_version(version_path)
    if current is None or current == "":
        sys.stderr.write(
            f"ERROR: cannot read installed version at {version_path}\n")
        return 1

    # Non-throttled, explicit probe: a test injection bypasses the network.
    injected = os.environ.get("RABBIT_UPDATE_TEST_LATEST")
    if injected is not None:
        latest = injected.strip()
    else:
        repo = os.environ.get("RABBIT_REPO", DEFAULT_REPO)
        latest = cru.fetch_upstream_version(repo, current)

    if latest is None or latest == "":
        sys.stderr.write(
            "ERROR: could not determine latest release (network failure?)\n")
        return 1

    self_update = cru.probe_self_update(str(rroot))
    sys.stdout.write(json.dumps({
        "current": current,
        "latest": latest,
        "newer": latest != current,
        "self_update_available": self_update,
    }) + "\n")
    return 0


def _restart_sensitive_paths(rroot: Path) -> list[Path]:
    """Enumerate restart-sensitive surfaces under `rroot`: every file in the
    hooks tree, every settings*.json under .claude/, and CLAUDE.md. Returns
    only paths that exist (a path absent before AND after the update simply
    never appears, which is correct — its absence is no change)."""
    paths: list[Path] = []
    for rel_dir in RESTART_SENSITIVE_DIRS:
        base = rroot / rel_dir
        if base.is_dir():
            paths.extend(p for p in base.rglob("*") if p.is_file())
    claude_dir = rroot / ".claude"
    if claude_dir.is_dir():
        paths.extend(p for p in claude_dir.glob("settings*.json") if p.is_file())
    for rel in RESTART_SENSITIVE_FILES:
        p = rroot / rel
        if p.is_file():
            paths.append(p)
    return paths


def _restart_sensitive_signature(rroot: Path) -> dict[str, str]:
    """Map each existing restart-sensitive path (relative to rroot) to a
    content hash. A before/after diff of this map is the deterministic
    restart-required signal."""
    sig: dict[str, str] = {}
    for p in _restart_sensitive_paths(rroot):
        try:
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        except OSError:
            continue
        sig[str(p.relative_to(rroot))] = digest
    return sig


def _mark_restart_needed(rroot: Path) -> None:
    """Write the marker contract's SessionStart banner reads + consumes. The
    content names why the restart is required (surfaced for diagnostics)."""
    try:
        (rroot / RESTART_MARKER).write_text(
            "rabbit-update changed a restart-sensitive surface "
            "(hooks/skills/agents/settings/CLAUDE.md)\n")
    except OSError as e:  # best-effort; never fail the install over the marker
        sys.stderr.write(f"WARNING: could not write restart marker: {e}\n")


def cmd_install(rroot: Path) -> int:
    install_py = rroot / "install.py"
    if not install_py.is_file():
        sys.stderr.write(f"ERROR: install.py not found at {install_py}\n")
        return 1

    before = _restart_sensitive_signature(rroot)
    rc = subprocess.call([sys.executable, str(install_py), "--update"])
    # Only a SUCCESSFUL update should flag a restart — a failed install must
    # not raise a false restart alert even if it left files half-changed.
    if rc == 0:
        after = _restart_sensitive_signature(rroot)
        if after != before:
            _mark_restart_needed(rroot)
    return rc


def main() -> int:
    args = sys.argv[1:]
    if not args:
        usage()
        return 2
    cmd = args[0]
    rroot = repo_root()

    if cmd == "check":
        return cmd_check(rroot)
    if cmd == "install":
        return cmd_install(rroot)
    if cmd in ("-h", "--help", "help"):
        usage()
        return 0

    sys.stderr.write(f"ERROR: unknown subcommand '{cmd}'\n")
    usage()
    return 2


if __name__ == "__main__":
    sys.exit(main())
