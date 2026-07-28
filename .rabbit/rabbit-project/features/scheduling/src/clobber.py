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
names it can import. It is idempotent (a missing artifact is a no-op / action
``absent``) and NEVER creates the runtime dir (an absent dir is nothing-to-do).

UX (clobber-preview slice): the user-facing ``--yes`` flag is REPLACED by an
internal ``--apply`` flag. The DEFAULT invocation (no flag) is a PREVIEW that
deletes NOTHING; ``--apply`` actually deletes. Both modes emit the SAME
machine-first structured payload::

    {"mode": "preview"|"applied",
     "artifacts": [{"name", "path", "exists", "action"}],
     "preserved": [names],
     "loop_intent_present": <bool>}

``action`` is ``would-remove`` (preview + present), ``removed`` (apply +
present), or ``absent`` (not present). The SKILL renders it as a WOULD-DELETE /
DELETED / PRESERVED table. The verbatim-``yes`` confirmation gate is a
SKILL-owned conversational confirmation, NOT a CLI flag; the SKILL maps a user
``--no-dry-run`` request onto ``clobber.py --apply`` (the no-gate immediate
delete). clobber writes NOTHING except the deletions (no model, no network).

scheduling CONSUMES run_tick + lifecycle-dispositions + heartbeat UNCHANGED; it
never edits or forks them.

Version: 0.3.0
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
    """Reset the loop: preview (``apply=False``) or delete (``apply=True``) the
    runtime-state artifacts under ``runtime_dir``, preserving config. Returns the
    machine-first structured payload::

        {"mode": "preview"|"applied",
         "artifacts": [{"name", "path", "exists", "action"}],
         "preserved": [names],
         "loop_intent_present": <bool>}

    One ``artifacts`` record per known runtime-state artifact (each state marker
    + the ``dispatch-out/`` dir). ``action`` is ``would-remove`` on the preview
    path when the artifact ``exists``, ``removed`` on the apply path when it
    exists, else ``absent``. ``preserved`` names the config files PRESENT in the
    dir (surfacing only — config is NEVER an artifact / NEVER removed).

    ``loop_intent_present`` is heartbeat's durable loop-intent marker, read
    BEFORE any deletion (clobber removes the marker, so the flag must be captured
    first). It is True ONLY when the human ``/start``-ed a live scheduled loop; a
    ``/tick``-only or paused-tick session (``disposition==RUNNING`` + a
    checkpoint but no loop-intent) reports False. The ``/clobber`` SKILL keys its
    ``/stop``-first recommendation on this flag, NOT on ``disposition``.

    NEVER creates ``runtime_dir``: an absent dir yields all-``absent`` artifacts
    + empty ``preserved`` (nothing to do). Idempotent: a missing artifact is
    silently skipped. Config (``config.json`` / ``route.json`` /
    ``adapter-map.json`` / ``*.bak`` / ``*.migrated``) is NEVER removed.
    """
    # Read the durable loop-intent BEFORE deleting anything (clobber removes the
    # loop-intent marker, so the flag has to be captured up front). Works for an
    # absent dir too: no marker -> False.
    loop_intent_present = heartbeat.loop_intent_is_running(runtime_dir)
    mode = "applied" if apply else "preview"
    present_action = "removed" if apply else "would-remove"

    dir_exists = os.path.isdir(runtime_dir)
    state_names = _state_artifact_names()

    artifacts = []
    # Each runtime-state file: build its record, then delete when applying.
    for name in state_names:
        full = os.path.join(runtime_dir, name)
        exists = os.path.exists(full)
        artifacts.append({
            "name": name,
            "path": os.path.abspath(full),
            "exists": exists,
            "action": present_action if exists else "absent",
        })
        if exists and apply:
            os.remove(full)

    # The dispatch-out dir (idempotent via ignore_errors on removal).
    dispatch_out = os.path.join(runtime_dir, _DISPATCH_OUT_DIRNAME)
    do_exists = os.path.isdir(dispatch_out)
    artifacts.append({
        "name": _DISPATCH_OUT_DIRNAME,
        "path": os.path.abspath(dispatch_out),
        "exists": do_exists,
        "action": present_action if do_exists else "absent",
    })
    if do_exists and apply:
        shutil.rmtree(dispatch_out, ignore_errors=True)

    # Record what config is preserved (surfacing only — never touched).
    preserved = []
    if dir_exists:
        state_set = set(state_names)
        for entry in sorted(os.listdir(runtime_dir)):
            if entry in state_set or entry == _DISPATCH_OUT_DIRNAME:
                continue
            preserved.append(entry)

    return {
        "mode": mode,
        "artifacts": artifacts,
        "preserved": preserved,
        "loop_intent_present": loop_intent_present,
    }


def main(argv=None):
    """CLI: PREVIEW by default (deletes nothing); ``--apply`` actually deletes.
    Prints the machine-first structured payload as JSON + a short human line.
    Exit code 0."""
    parser = argparse.ArgumentParser(
        description="Reset the maintainer loop: clear runtime state, keep config.")
    parser.add_argument(
        "--apply", action="store_true",
        help="actually delete (without it, a PREVIEW that deletes nothing)")
    parser.add_argument("--runtime-dir", dest="runtime_dir")
    parser.add_argument("--project-dir", dest="project_dir")
    args = parser.parse_args(argv)

    runtime_dir = args.runtime_dir or _resolve_runtime_dir(args.project_dir)

    summary = clobber(runtime_dir, apply=args.apply)
    sys.stdout.write(json.dumps(summary) + "\n")
    removed_count = sum(1 for a in summary["artifacts"] if a["exists"])
    verb = ("removed" if args.apply
            else "would remove (preview; pass --apply)")
    intent = ("loop-intent PRESENT (a /start-ed loop is live; /stop first)"
              if summary["loop_intent_present"]
              else "no loop-intent (no live scheduled loop)")
    sys.stdout.write(
        f"[clobber] {verb} {removed_count} runtime-state artifact(s); "
        f"preserved {len(summary['preserved'])} config file(s) under "
        f"{runtime_dir}; {intent}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
