#!/usr/bin/env python3
"""auto-maintainer configure — the deterministic WRITER for the project-local
central config (``${project_dir}/.auto-maintainer/config.json``).

safety_governance.py is the READER/decider over config.json (load_config, the
trust-ladder gate, the budget gate); this module is its writer half. It performs
a load-modify-save of config.json: it loads the current config via
``safety_governance.load_config`` (so absent keys are backfilled from the
documented defaults, and a legacy governance.json is migrated), applies only the
mentioned fields, validates them against safety_governance's CLOSED vocabularies,
and writes the result back (pretty, sort_keys). It owns NO schema of its own —
the schema is safety_governance's (GOVERNANCE_SCHEMA_VERSION = 2.1.0).

It is script-tier (spec-rules §1): all validation and the file write are
deterministic here, so the ``/auto-maintainer:configure`` skill only relays the
user's requested values to this CLI and never hand-rolls JSON.

Knobs:
  - ``--mode`` validated through ``safety_governance.permits`` (the closed mode
    set {dry-run, propose, auto-merge}); an unknown mode raises ValueError. The
    legacy name ``gated-merge`` is tolerated and stored as ``auto-merge``.
  - ``--per-day-tokens`` is a non-negative int, or one of {none, null, unlimited,
    ""} meaning NO LIMIT (stored as JSON null).
  - ``--interval-minutes`` (heartbeat cadence) and ``--backoff-threshold`` are
    POSITIVE ints.
  - ``--regression-command`` is the GATE full-regression shell command (an
    arbitrary string), or one of {none, null, ""} meaning NO gate (stored as
    JSON null), mirroring the ``--per-day-tokens`` clear sentinel.
  - ``--describe`` emits the machine-first field catalog as JSON (read-only).
  - ``--show`` (or no mutating flag) prints the current config and writes nothing.

Version: 0.3.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when the central-config schema
  (safety_governance.GOVERNANCE_SCHEMA_VERSION) reaches a breaking major
  version, or when governance configuration moves out of a project-local JSON
  file consulted at tick entry.
"""

import argparse
import json
import os
import sys

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import safety_governance as sg

# config.json lives at the same project-local path safety_governance reads.
_CONFIG_RELPATH = os.path.join(".auto-maintainer", "config.json")

# Sentinel: "this field was not mentioned, leave it as-is". Distinct from None,
# which is an explicit "no limit" the caller CAN request for per_day_tokens.
_UNSET = object()


def _config_path(project_dir):
    return os.path.join(project_dir, _CONFIG_RELPATH)


def _parse_ceiling(raw):
    """Parse a per-day-ceiling CLI value -> None (no limit) or a non-negative int.

    Raises ValueError on a negative or non-integer value.
    """
    s = str(raw).strip().lower()
    if s in ("none", "null", "unlimited", ""):
        return None
    value = int(s)  # ValueError on non-int
    if value < 0:
        raise ValueError(
            "budget ceiling must be >= 0 or one of none/null/unlimited")
    return value


def _parse_regression_command(raw):
    """Parse a --regression-command CLI value -> None (no gate) or the command
    string. The clear sentinels none/null/"" (case-insensitive) map to None,
    mirroring the per-day-ceiling clear; any other value is kept verbatim
    (whitespace-trimmed)."""
    if str(raw).strip().lower() in ("none", "null", ""):
        return None
    return str(raw).strip()


def _parse_positive_int(raw, label):
    """Parse a CLI value -> a positive int. Raises ValueError otherwise."""
    value = int(str(raw).strip())  # ValueError on non-int
    if value <= 0:
        raise ValueError(f"{label} must be a positive int")
    return value


def configure(project_dir, *, mode=None, per_day_tokens=_UNSET,
              interval_minutes=_UNSET, backoff_threshold=_UNSET,
              regression_command=_UNSET):
    """Apply the requested changes to config.json and return the new config.

    Loads the current (backfilled, migrated) config, applies only the mentioned
    fields, validates, writes config.json, and returns the written dict. A field
    left at its default sentinel is preserved unchanged.
    """
    # Start from the backfilled current config (defaults when absent), deep-copied
    # so we never mutate the lib's DEFAULT_GOVERNANCE.
    cfg = json.loads(json.dumps(sg.load_config(project_dir)))

    if mode is not None:
        # Validate against the closed mode set; permits() raises ValueError on an
        # unknown mode. (The effect choice is immaterial to mode validation.)
        sg.permits("implement", mode)
        # Store the canonical (post-rename) name so a legacy `gated-merge`
        # request is persisted as `auto-merge`.
        cfg["mode"] = sg._normalize_mode(mode)

    budget = cfg.setdefault("budget", {})
    if per_day_tokens is not _UNSET:
        budget["per_day_tokens"] = per_day_tokens

    if interval_minutes is not _UNSET:
        cfg.setdefault("heartbeat", {})["interval_minutes"] = interval_minutes

    if backoff_threshold is not _UNSET:
        cfg.setdefault("backoff", {})["threshold"] = backoff_threshold

    if regression_command is not _UNSET:
        cfg["regression_command"] = regression_command

    path = _config_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return cfg


def _field_catalog(project_dir):
    """The machine-first field catalog: a list of
    {key, label, controls, default, current, type, validator} entries — the
    single source of truth the guided --setup walk-through reads. Read-only."""
    current = sg.load_config(project_dir)
    defaults = sg.DEFAULT_GOVERNANCE
    return [
        {
            "key": "mode",
            "label": "Trust mode",
            "controls": "Whether acting states implement, open PRs, or merge.",
            "default": defaults["mode"],
            "current": current["mode"],
            "type": "enum",
            "validator": "one of dry-run | propose | auto-merge",
        },
        {
            "key": "budget.per_day_tokens",
            "label": "Per-day token budget",
            "controls": "The per-day token ceiling; null = no limit.",
            "default": defaults["budget"]["per_day_tokens"],
            "current": current["budget"]["per_day_tokens"],
            "type": "int_or_null",
            "validator": "a non-negative int, or none/null/unlimited",
        },
        {
            "key": "heartbeat.interval_minutes",
            "label": "Heartbeat interval (minutes)",
            "controls": "The tick cadence the /start heartbeat schedules.",
            "default": defaults["heartbeat"]["interval_minutes"],
            "current": current["heartbeat"]["interval_minutes"],
            "type": "int",
            "validator": "a positive int",
        },
        {
            "key": "backoff.threshold",
            "label": "Backoff threshold",
            "controls": "Consecutive-blocked count K at which the loop "
                        "escalates + defers a work order.",
            "default": defaults["backoff"]["threshold"],
            "current": current["backoff"]["threshold"],
            "type": "int",
            "validator": "a positive int",
        },
        {
            "key": "regression_command",
            "label": "GATE regression command",
            "controls": "The full-regression shell command the GATE state runs "
                        "against each REVIEW-passed PR; null = no gate.",
            "default": defaults["regression_command"],
            "current": current["regression_command"],
            "type": "str_or_null",
            "validator": "a shell command string, or none/null to clear",
        },
    ]


def _resolve_project_dir(explicit):
    return explicit or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="configure.py",
        description="Set the trust mode and budget/heartbeat/backoff/"
        "regression-command knobs in the project-local central config "
        "(.auto-maintainer/config.json).",
    )
    parser.add_argument("--project-dir", default=None)
    parser.add_argument(
        "--mode",
        default=None,
        help="trust mode: one of dry-run, propose, auto-merge "
             "(legacy 'gated-merge' is accepted and stored as 'auto-merge')",
    )
    parser.add_argument(
        "--per-day-tokens",
        default=None,
        help="per-day token ceiling: a non-negative int, or none/null/unlimited",
    )
    parser.add_argument(
        "--interval-minutes",
        default=None,
        help="heartbeat cadence in minutes: a positive int",
    )
    parser.add_argument(
        "--backoff-threshold",
        default=None,
        help="consecutive-blocked count K before escalate+defer: a positive int",
    )
    parser.add_argument(
        "--regression-command",
        default=None,
        help="GATE full-regression shell command, or none/null to clear "
             "(no gate)",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="emit the machine-first field catalog as JSON (read-only)",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current config without changing it",
    )
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    if args.describe:
        print(json.dumps(_field_catalog(project_dir), indent=2))
        return 0

    mutating = (
        args.mode is not None
        or args.per_day_tokens is not None
        or args.interval_minutes is not None
        or args.backoff_threshold is not None
        or args.regression_command is not None
    )

    # --show, or no mutating flags at all: print the current config and stop.
    if args.show or not mutating:
        print(json.dumps(sg.load_config(project_dir), indent=2, sort_keys=True))
        return 0

    try:
        per_day = (_parse_ceiling(args.per_day_tokens)
                   if args.per_day_tokens is not None else _UNSET)
        interval = (_parse_positive_int(args.interval_minutes, "interval-minutes")
                    if args.interval_minutes is not None else _UNSET)
        threshold = (_parse_positive_int(args.backoff_threshold, "backoff-threshold")
                     if args.backoff_threshold is not None else _UNSET)
        regression = (_parse_regression_command(args.regression_command)
                      if args.regression_command is not None else _UNSET)
        cfg = configure(
            project_dir,
            mode=args.mode,
            per_day_tokens=per_day,
            interval_minutes=interval,
            backoff_threshold=threshold,
            regression_command=regression,
        )
    except ValueError as exc:
        print(f"configure: error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(cfg, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
