#!/usr/bin/env python3
"""auto-maintainer configure — the deterministic WRITER for the project-local
governance config (``${project_dir}/.auto-maintainer/governance.json``).

safety_governance.py is the READER/decider over governance.json (load, the
trust-ladder gate, the budget gate); this module is its writer half. It performs
a load-modify-save of governance.json: it loads the current config via
``safety_governance.load_governance`` (so absent keys are backfilled from the
documented defaults), applies the requested ``mode`` / budget-ceiling changes,
validates them against safety_governance's CLOSED vocabularies, and writes the
result back. It owns NO schema of its own — the schema is safety_governance's.

It is script-tier (spec-rules §1): all validation and the file write are
deterministic here, so the ``/auto-maintainer:configure`` skill only relays the
user's requested values to this CLI and never hand-rolls JSON.

Mode is validated through ``safety_governance.permits`` (the closed mode set
{dry-run, propose, gated-merge}); an unknown mode raises ValueError. A budget
ceiling is a non-negative int, or one of {none, null, unlimited, ""} meaning NO
LIMIT (stored as JSON null) for that dimension.

Version: 0.1.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when the governance config schema
  (safety_governance.GOVERNANCE_SCHEMA_VERSION) reaches a breaking major
  version, or when governance configuration moves out of a project-local JSON
  file consulted at tick entry.
"""

import argparse
import json
import os
import sys

import safety_governance as sg

# governance.json lives at the same project-local path safety_governance reads.
_GOVERNANCE_RELPATH = os.path.join(".auto-maintainer", "governance.json")

# Sentinel: "this budget dimension was not mentioned, leave it as-is". Distinct
# from None, which is an explicit "no limit" the caller CAN request.
_UNSET = object()


def _config_path(project_dir):
    return os.path.join(project_dir, _GOVERNANCE_RELPATH)


def _parse_ceiling(raw):
    """Parse a budget-ceiling CLI value -> None (no limit) or a non-negative int.

    Raises ValueError on a negative or non-integer value.
    """
    s = str(raw).strip().lower()
    if s in ("none", "null", "unlimited", ""):
        return None
    value = int(s)  # ValueError on non-int
    if value < 0:
        raise ValueError("budget ceiling must be >= 0 or one of none/null/unlimited")
    return value


def configure(project_dir, *, mode=None, per_day_tokens=_UNSET, per_tick_tokens=_UNSET):
    """Apply the requested changes to governance.json and return the new config.

    Loads the current (backfilled) config, applies only the mentioned fields,
    validates, writes governance.json, and returns the written dict. A field
    left at its default sentinel is preserved unchanged.
    """
    # Start from the backfilled current config (defaults when the file is absent),
    # and deep-copy so we never mutate the lib's DEFAULT_GOVERNANCE.
    gov = json.loads(json.dumps(sg.load_governance(project_dir)))

    if mode is not None:
        # Validate against the closed mode set; permits() raises ValueError on an
        # unknown mode. (The effect choice is immaterial to mode validation.)
        sg.permits("implement", mode)
        gov["mode"] = mode

    budget = gov.setdefault("budget", {})
    if per_day_tokens is not _UNSET:
        budget["per_day_tokens"] = per_day_tokens
    if per_tick_tokens is not _UNSET:
        budget["per_tick_tokens"] = per_tick_tokens

    path = _config_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(gov, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return gov


def _resolve_project_dir(explicit):
    return explicit or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = argparse.ArgumentParser(
        prog="configure.py",
        description="Set the trust mode and budget ceilings in the project-local "
        "governance config (.auto-maintainer/governance.json).",
    )
    parser.add_argument("--project-dir", default=None)
    parser.add_argument(
        "--mode",
        default=None,
        help="trust mode: one of dry-run, propose, gated-merge",
    )
    parser.add_argument(
        "--per-day-tokens",
        default=None,
        help="per-day token ceiling: a non-negative int, or none/null/unlimited",
    )
    parser.add_argument(
        "--per-tick-tokens",
        default=None,
        help="per-tick token ceiling: a non-negative int, or none/null/unlimited",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current config without changing it",
    )
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    mutating = args.mode is not None or args.per_day_tokens is not None or args.per_tick_tokens is not None

    # --show, or no mutating flags at all: print the current config and stop.
    if args.show or not mutating:
        print(json.dumps(sg.load_governance(project_dir), indent=2, sort_keys=True))
        return 0

    try:
        per_day = _parse_ceiling(args.per_day_tokens) if args.per_day_tokens is not None else _UNSET
        per_tick = _parse_ceiling(args.per_tick_tokens) if args.per_tick_tokens is not None else _UNSET
        gov = configure(
            project_dir,
            mode=args.mode,
            per_day_tokens=per_day,
            per_tick_tokens=per_tick,
        )
    except ValueError as exc:
        print(f"configure: error: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(gov, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
