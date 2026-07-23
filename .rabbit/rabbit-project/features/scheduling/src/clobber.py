#!/usr/bin/env python3
"""clobber — deterministic loop-reset control for the maintainer (script-tier).

This is the script-backed control for ``/auto-maintainer:clobber`` (spec-rules
§1). It RESETS the loop to a clean start by DELETING only the RUNTIME-STATE
artifacts under the runtime dir while PRESERVING user config, so a stuck /
corrupt / upgrade-in-flight loop can be cleared without hand-deleting files or
losing the project's route / adapter-map / governance config.

Cleared (runtime state):
  - ``durable-state.json`` — ledgers, budget window, read products, tick
    checkpoint, counters.
  - the ``disposition`` + ``lock.json`` markers (lifecycle-dispositions).
  - ``events.jsonl`` (the structured event log) + ``tick-journal.jsonl``.
  - the ``dispatch-out/`` directory (paused agent dispatch prompts/outputs).
  - the heartbeat markers ``loop-intent`` + ``last-resume-session``.

Preserved (NEVER touched):
  - ``config.json``, ``route.json``, ``adapter-map.json``, and any ``*.bak`` /
    ``*.migrated`` config backups.

It resolves the runtime dir the SAME way ``run_tick`` does — reusing
``run_tick.resolve_runtime_paths`` (no duplicated path logic) — and reuses the
canonical marker-name constants (``run_tick.EVENTS_FILENAME``,
``lifecycle_dispositions._DISPOSITION_MARKER`` / ``_LOCK_MARKER``,
``heartbeat._INTENT_MARKER`` / ``_RESUME_DEDUP_MARKER``) rather than hardcoding
names it can import. It is idempotent (a missing artifact is a no-op), NEVER
creates the runtime dir (an absent dir is nothing-to-do), and requires ``--yes``
to actually delete — without it a DRY-RUN prints what WOULD be removed and
deletes nothing. It prints a machine-first ``{removed, preserved}`` JSON summary
plus a short human line. clobber writes NOTHING except the deletions (no model,
no network).

scheduling CONSUMES run_tick + lifecycle-dispositions + heartbeat UNCHANGED; it
never edits or forks them.

Version: 0.1.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the control surface / runtime
  layout is replaced.
"""

import argparse
import json
import os
import shutil
import sys

# Resolve sibling modules via run_tick's path setup (worktree ../<dep>/src, or
# the flat installed lib/), so clobber never duplicates that logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import run_tick as rt  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import heartbeat  # noqa: E402


# The dispatch-out dir name run_tick writes paused-dispatch files under
# (``${runtime_dir}/dispatch-out/``). run_tick derives it inline (no exported
# constant), so the name is carried here as a plain literal.
_DISPATCH_OUT_DIRNAME = "dispatch-out"


def _state_artifact_names():
    """The RUNTIME-STATE artifact names clobber removes from the runtime dir,
    reusing the canonical marker-name constants each owner exports (never a
    hardcoded name it can import)."""
    return [
        os.path.basename(rt.resolve_runtime_paths()[1]),  # durable-state.json
        os.path.basename(rt.resolve_runtime_paths()[2]),  # tick-journal.jsonl
        rt.EVENTS_FILENAME,
        ld._DISPOSITION_MARKER,
        ld._LOCK_MARKER,
        heartbeat._INTENT_MARKER,
        heartbeat._RESUME_DEDUP_MARKER,
    ]


def _resolve_runtime_dir(project_dir=None):
    """The runtime dir run_tick resolves for ``project_dir`` (default: the
    CLAUDE_PROJECT_DIR env / cwd), reusing ``run_tick.resolve_runtime_paths`` so
    the path logic is never duplicated. ``project_dir`` is threaded in via the
    same env anchor resolve_runtime_paths reads, restored after the call so this
    invocation leaves no global side effect (it writes NOTHING but the
    deletions)."""
    if not project_dir:
        return rt.resolve_runtime_paths()[0]
    saved = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        return rt.resolve_runtime_paths()[0]
    finally:
        if saved is None:
            del os.environ["CLAUDE_PROJECT_DIR"]
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = saved


def clobber(runtime_dir, apply=False):
    """Reset the loop: remove the runtime-state artifacts under ``runtime_dir``,
    preserving config. Returns ``{"removed": [...], "preserved": [...]}`` of
    absolute paths (the removed list is what WAS removed when ``apply`` is True,
    else what WOULD be removed on a dry-run).

    NEVER creates ``runtime_dir``: an absent dir yields empty lists (nothing to
    do). Idempotent: a missing artifact is silently skipped. Config
    (``config.json`` / ``route.json`` / ``adapter-map.json`` / ``*.bak`` /
    ``*.migrated``) is NEVER removed.
    """
    removed = []
    preserved = []
    if not os.path.isdir(runtime_dir):
        return {"removed": removed, "preserved": preserved}

    state_names = set(_state_artifact_names())

    # Record what config is preserved (surfacing only — never touched).
    for entry in sorted(os.listdir(runtime_dir)):
        full = os.path.join(runtime_dir, entry)
        if entry in state_names or entry == _DISPATCH_OUT_DIRNAME:
            continue
        preserved.append(os.path.abspath(full))

    # Remove each present runtime-state file (idempotent: skip the absent).
    for name in _state_artifact_names():
        full = os.path.join(runtime_dir, name)
        if os.path.exists(full):
            removed.append(os.path.abspath(full))
            if apply:
                os.remove(full)

    # Remove the dispatch-out dir (idempotent via ignore_errors).
    dispatch_out = os.path.join(runtime_dir, _DISPATCH_OUT_DIRNAME)
    if os.path.isdir(dispatch_out):
        removed.append(os.path.abspath(dispatch_out))
        if apply:
            shutil.rmtree(dispatch_out, ignore_errors=True)

    return {"removed": sorted(removed), "preserved": sorted(preserved)}


def main(argv=None):
    """CLI: DRY-RUN by default; ``--yes`` actually deletes. Prints a machine-first
    ``{removed, preserved}`` JSON summary + a short human line. Exit code 0."""
    parser = argparse.ArgumentParser(
        description="Reset the maintainer loop: clear runtime state, keep config.")
    parser.add_argument(
        "--yes", action="store_true",
        help="actually delete (without it, a DRY-RUN that deletes nothing)")
    parser.add_argument("--runtime-dir", dest="runtime_dir")
    parser.add_argument("--project-dir", dest="project_dir")
    args = parser.parse_args(argv)

    runtime_dir = args.runtime_dir or _resolve_runtime_dir(args.project_dir)

    summary = clobber(runtime_dir, apply=args.yes)
    sys.stdout.write(json.dumps(summary) + "\n")
    verb = "removed" if args.yes else "would remove (dry-run; pass --yes)"
    sys.stdout.write(
        f"[clobber] {verb} {len(summary['removed'])} runtime-state artifact(s); "
        f"preserved {len(summary['preserved'])} config file(s) under "
        f"{runtime_dir}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
