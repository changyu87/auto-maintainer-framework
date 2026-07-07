#!/usr/bin/env python3
"""start — deterministic fresh-start + tick #1 for the maintainer (script-tier).

This is the script-backed control for ``/auto-maintainer:start`` (spec-rules §1).
It exists to fix auto-maintainer-framework#44, where the start SKILL did not
clear a latched ``STOPPED`` disposition (from a prior ``/stop``): GUARD then
halted every tick and the loop would not run, so the skill resorted to
hand-rolling Python to clear the latch (a #30-class prompt-tier regression).
Disposition handling is NEVER prompt-tier: this script owns the clear-or-refuse
decision so the skill only invokes it.

It resolves the runtime dir the SAME way ``run_tick`` does — by reusing
``run_tick.resolve_runtime_paths`` (no duplicated path logic) — reads the current
disposition (lifecycle-dispositions API), and prepares a fresh start:

  - ``STOPPED`` — a prior human stop latched the loop. Start IS the §1.2 human
    resume, so clear the latch to a runnable state (IDLE) and announce it, then
    run tick #1.
  - ``ABORTED`` — a fault latched the loop. REFUSE: print a clear "investigate;
    not auto-cleared" message and exit non-zero WITHOUT ticking. A fault is
    never silently cleared.
  - otherwise (RUNNING / IDLE / absent) — proceed straight to tick #1.

The shipped aggressive default config is NO LONGER seeded (copied) into the
runtime dir. Config is resolved at RUNTIME (override-else-default, #337): the
config readers (``sg.load_config`` / ``aw.load_route`` / ``aw.load_adapter_map``)
read a project-local override from ``${project_dir}/.auto-maintainer/`` if
present, else the shipped read-only default at
``${CLAUDE_PLUGIN_ROOT}/config/default/``. This means a release that changes a
default reaches an unoverridden install with no manual re-seed (the staleness the
old seed-once copy caused — #337 supersedes #336). Instead of seeding, start
MIGRATES any install that was seeded by the OLD copy-once path
(``migrate_seeded_config``): for each of ``config.json`` / ``route.json`` /
``adapter-map.json`` present in the runtime dir, if the file is byte-identical to
the shipped default it is a leftover seed (not a real override) and is REMOVED so
the shipped default flows through on every release; if it differs it is a genuine
user override and is kept untouched. The conservative code
``DEFAULT_ROUTE`` / ``DEFAULT_GOVERNANCE`` fallback is unchanged and still applies
when neither an override nor a reachable shipped default exists.

Tick #1 itself is NOT re-implemented here: start calls ``run_tick.run_tick`` so
the route lives in exactly one place. The recurring heartbeat keeps using
``run_tick`` directly (no reset per tick); only the FRESH start goes through this
script.

It also durably records the **loop-intent** (heartbeat.py) when the latch-clear
succeeds, so a future session's SessionStart auto-resume hook re-arms the
in-session heartbeat (the durable heartbeat, §3.3.2). It does NOT clear the
cross-session resume-dedup (heartbeat.py owns that on the SessionStart path), so
a duplicate heartbeat can never be re-armed within one session.

scheduling CONSUMES run_tick + lifecycle-dispositions UNCHANGED; it never edits
or forks them.

Version: 0.5.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the control surface is replaced.
"""

import os
import sys

# Resolve sibling modules via sys.path exactly as run_tick does. In the worktree
# the consumed features live under ../<dep>/src; in the installed plugin lib/
# they are flat siblings of this file. Importing run_tick first reuses its path
# setup and its resolve_runtime_paths, so start never duplicates that logic.
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_tick as rt  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import safety_governance as sg  # noqa: E402
import heartbeat as hb  # noqa: E402


class StartRefused(Exception):
    """Raised when start refuses to run because a fault (ABORTED) is latched.

    A fault is never silently cleared: the human must investigate and resolve it
    before the loop may resume. The CLI entrypoint maps this to a non-zero exit.
    """


# The runtime config files subject to the override-else-default model (#337).
# Order is cosmetic.
_CONFIG_FILES = ("config.json", "route.json", "adapter-map.json")


def _default_config_dir():
    """The shipped read-only default config dir (#337).

    In the installed plugin this file is ``<plugin_root>/lib/start.py`` and the
    shipped defaults live at ``<plugin_root>/config/default/`` (``config/`` is a
    sibling of ``lib/``), so the dir is
    ``dirname(dirname(__file__))/config/default``. In the source tree that dir
    does not exist (the assets are a packaging-config plugin asset), so
    ``migrate_seeded_config`` no-ops there unless a source dir is injected by a
    test.
    """
    return os.path.join(os.path.dirname(_SRC), "config", "default")


def _same_bytes(path_a, path_b):
    """True when both files exist and have byte-identical contents."""
    try:
        with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
            return fa.read() == fb.read()
    except OSError:
        return False


def migrate_seeded_config(runtime_dir, source_dir=None):
    """Retire leftover seed files so the runtime override-else-default resolves (#337).

    The old seed-once path (#211) copied the shipped default into the runtime dir
    on a fresh install; those copies then never refreshed (the staleness #337
    fixes). Now config is resolved at runtime against the shipped default, so a
    runtime file is only meaningful as a genuine USER OVERRIDE. For each of
    ``config.json`` / ``route.json`` / ``adapter-map.json`` present in
    ``runtime_dir``: if it is byte-identical to the shipped default it is a
    leftover seed (not a real override) and is REMOVED so the shipped default
    flows through on every release; if it differs it is a genuine override and is
    KEPT untouched. Returns the list of files removed. A no-op (returns ``[]``)
    when the shipped default dir is absent — e.g. the source tree, or a test that
    injects no ``source_dir`` — so it is safe to call unconditionally at the top
    of every start.

    `source_dir` overrides the shipped ``config/default/`` location (tests inject
    a temp dir).
    """
    src = source_dir or _default_config_dir()
    if not os.path.isdir(src):
        return []
    removed = []
    for name in _CONFIG_FILES:
        s = os.path.join(src, name)
        d = os.path.join(runtime_dir, name)
        if os.path.isfile(s) and os.path.isfile(d) and _same_bytes(s, d):
            os.remove(d)
            removed.append(name)
    return removed


def _clear_or_refuse(runtime_dir, clear_only=False):
    """The shared FRESH-start disposition decision (no tick).

    Reads the current disposition and either clears a latched STOPPED, refuses
    on a latched ABORTED, or no-ops on a clean state — in EXACTLY one place, so
    both --clear-only and the default clear+tick mode share it (never forked):

      - STOPPED -> clear the latch to IDLE (start IS the §1.2 human resume) and
        announce it.
      - ABORTED -> raise StartRefused (the CLI exits non-zero); the fault latch
        stays in place. A fault is never silently cleared.
      - otherwise (RUNNING / IDLE / absent) -> no-op.

    `clear_only` only changes the announce wording (it makes the tick-deferral
    explicit); the decision itself is identical in both modes.
    """
    disposition = ld.read_disposition(runtime_dir)

    if disposition == ld.Disposition.ABORTED:
        raise StartRefused(
            "[start] disposition=ABORTED — a fault is latched; investigate and "
            "resolve it before resuming. NOT auto-cleared; no tick run.")

    if disposition == ld.Disposition.STOPPED:
        # Start is the human resume: clear the STOPPED latch to a runnable state.
        ld.write_disposition(runtime_dir, ld.Disposition.IDLE)
        if clear_only:
            sys.stdout.write(
                "[start] latch cleared -> IDLE (clear-only; tick deferred to "
                "executor)\n")
        else:
            sys.stdout.write(
                "[start] cleared latched STOPPED disposition -> IDLE "
                "(human resume)\n")


def heartbeat_interval_minutes(project_dir=None):
    """The configured heartbeat (tick) cadence in minutes (§3.3.2).

    Read from the central config's heartbeat.interval_minutes via sg.load_config
    (project-local config.json, else the documented default 3). The /start skill
    schedules the recurring heartbeat at THIS cadence, so the interval is
    config-driven (#17 resolved) rather than hardcoded. Resolves project_dir the
    same way run_tick does when not injected (the installed case).
    """
    if project_dir is None:
        project_dir = rt._resolve_project_dir()
    return sg.load_config(project_dir)["heartbeat"]["interval_minutes"]


def start(runtime_dir=None, state_path=None, journal_path=None, source=None,
          clear_only=False):
    """Prepare a fresh start, then run tick #1; return the EXIT disposition signal.

    Resolves the runtime dir the same way run_tick does when paths are not
    injected (the installed case). It first MIGRATES any leftover seed files so
    the runtime override-else-default resolves (`migrate_seeded_config`, #337),
    then performs the FRESH-start disposition decision via `_clear_or_refuse`
    (clear STOPPED / refuse ABORTED / no-op), then runs tick #1 via run_tick.

    With `clear_only=True` it performs the migration + the disposition decision
    and does NOT run tick #1 — returning None. This separates the latch-clear from
    tick #1, which the in-session executor model (DESIGN §2.8) needs: tick #1 of
    an AGENT route must go through the executor skill (which presses the Agent
    button), not start.py's in-process run_tick (which would just pause).

    `source` is the injectable PULL issue source forwarded to run_tick (tests
    inject a stub; production defaults to work-intake's live gh source).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = rt.resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal

    # Runtime override-else-default migration (#337): retire any leftover seed
    # files (byte-identical to the shipped default) so the shipped default now
    # flows through at runtime on every release; a genuinely edited config is a
    # real override and is kept untouched. A no-op once nothing byte-identical
    # remains, and a no-op in the source tree (no shipped config/default sibling).
    removed = migrate_seeded_config(runtime_dir)
    if removed:
        sys.stdout.write(
            "[start] retired leftover seed config (now resolved at runtime): "
            + ", ".join(removed) + "\n")

    _clear_or_refuse(runtime_dir, clear_only=clear_only)

    # The latch-clear/refuse succeeded (ABORTED would have raised above), so the
    # human wants the loop ticking: durably record the loop-intent so a future
    # session's SessionStart hook auto-resumes the heartbeat (§3.3.2). Recorded
    # in BOTH modes — --clear-only is the executor-model start, which still arms
    # the durable heartbeat even though tick #1 is deferred. NOTE: recording the
    # intent does NOT clear the cross-session resume-dedup (heartbeat.py owns
    # that on the SessionStart path), so a 2nd SessionStart that asks the same
    # session to re-run /start cannot re-arm a duplicate heartbeat.
    hb.record_loop_intent(runtime_dir)

    if clear_only:
        return None

    return rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                       journal_path=journal_path, source=source)


if __name__ == "__main__":
    # Production entrypoint: the /auto-maintainer:start skill invokes this once
    # for tick #1 from the installed plugin with no path wiring and no injected
    # source. It retires any leftover seed config (now resolved at runtime),
    # clears a latched STOPPED (or refuses on ABORTED) and then runs tick #1 via
    # run_tick (which prints the tick trace). A latched fault exits non-zero so
    # the skill surfaces it instead of silently clearing it.
    #
    # With --clear-only it performs the migration + the latch-clear/refuse
    # decision and defers tick #1 to the executor skill (DESIGN §2.8). Exit 0 on
    # cleared/no-op, non-zero on the ABORTED refusal.
    import argparse

    parser = argparse.ArgumentParser(
        description="Fresh-start control for /auto-maintainer:start.")
    parser.add_argument(
        "--clear-only", action="store_true",
        help="Only migrate leftover seed config + clear a latched STOPPED (or "
             "refuse on ABORTED); do NOT run tick #1 (deferred to the executor "
             "skill).")
    parser.add_argument(
        "--print-interval", action="store_true",
        help="Print the configured heartbeat interval in minutes "
             "(heartbeat.interval_minutes, default 3) and exit. The /start skill "
             "schedules the recurring heartbeat at this cadence; runs no tick.")
    args = parser.parse_args()

    # --print-interval is a pure read: emit the configured cadence and exit,
    # without touching the disposition or running a tick.
    if args.print_interval:
        sys.stdout.write(f"{heartbeat_interval_minutes()}\n")
        sys.exit(0)

    try:
        start(clear_only=args.clear_only)
    except StartRefused as exc:
        sys.stderr.write(str(exc) + "\n")
        sys.exit(1)
