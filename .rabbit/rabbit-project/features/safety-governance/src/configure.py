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
  - ``--implement-test-command`` is the IMPLEMENT-side per-work-order test-gate
    command (implement's test_gate.py). An arbitrary shell command string sets
    it; ``''`` (empty) clears it to JSON null (the default test/run.py
    behavior); the literal ``none``/``skip`` is PRESERVED VERBATIM as the skip
    sentinel (NOT mapped to null — null and 'none' mean different things:
    null = run test/run.py, 'none' = skip the gate). Stored RAW; implement
    interprets it.
  - ``--doc-check-features-root`` is the REPO-RELATIVE features root the
    verify-integrate GATE uses for the doc-surface load-bearing-token survival
    check; it must be non-absolute (repo-relative), or one of {none, null, ""}
    meaning the check is OFF (stored as JSON null).
  - ``--features-root`` is VERIFY's cross-feature complement locator. An
    arbitrary path string that MAY be absolute (UNLIKE doc-check-features-root);
    one of {none, null, ""} clears it to JSON null (unconfigured).
  - ``--work-own-filings`` is the §3.11.5 loopback toggle: a bool
    (true/false, also 1/0, yes/no, case-insensitive); an unparseable value
    raises ValueError.
  - ``--issue-labels`` writes the ``issue_filter.include_labels`` DNF in compact
    syntax (comma = AND within a group, semicolon = OR between groups); one of
    {none, null, ""} clears it to ``[]``. The FLAG name is retained for
    back-compat; the config field was RENAMED in schema 2.9.0. Validated +
    canonicalized through safety_governance's ``issue_filter`` normalizer before
    the write.
  - ``--issue-title-pattern`` writes the ``issue_filter.with_title_regex`` regex
    (flag name retained for back-compat; field renamed in schema 2.9.0); one
    of {none, null, ""} clears it to null. It must compile as a regex
    (validated via the same ``issue_filter`` normalizer).
  - ``--issue-exclude-labels`` is the ``issue_filter.exclude_labels`` flat OR of
    forbidden labels (comma-separated; an open issue carrying ANY is dropped by
    PULL); one of {none, null, ""} clears it to ``[]``. Validated +
    canonicalized through the same ``issue_filter`` normalizer.
  - ``--describe`` emits the machine-first field catalog as JSON (read-only);
    each entry carries a loop-``stage`` and the catalog is ORDERED by loop
    stage (PULL -> IMPLEMENT -> VERIFY -> GATE -> SCHEDULING -> SAFETY).
  - ``--preflight`` is a READ-ONLY environment probe emitting JSON
    ``{gh_authenticated, gh_account, resolved_repo, config_exists}`` for the
    guided ``--setup`` onboarding; it shells ``gh`` and writes nothing.
  - ``--show`` (or no mutating flag) prints the current config and writes
    nothing — BY DEFAULT as the human-readable ``render_config`` view (grouped,
    labeled, loop-stage-ordered). The post-write echo uses the same render.
  - ``--json`` is the machine-first escape hatch: ``--show`` and the post-write
    echo emit the raw ``json.dumps`` instead of the human render.
    ``--describe`` / ``--preflight`` ALWAYS emit their JSON catalogs and are
    UNAFFECTED by ``--json``.

Version: 0.6.0
Owner: rabbit-workflow team
Deprecation criterion: Superseded when the central-config schema
  (safety_governance.GOVERNANCE_SCHEMA_VERSION) reaches a breaking major
  version, or when governance configuration moves out of a project-local JSON
  file consulted at tick entry.
"""

import argparse
import json
import os
import subprocess
import sys

import safety_governance as sg

# config.json lives at the same project-local path safety_governance reads.
_CONFIG_RELPATH = os.path.join(".auto-maintainer", "config.json")

# The default subprocess runner used by --preflight to shell `gh`. Kept as a
# module-level attribute (not a hard-coded call) so tests can inject a fake that
# drives the probe without network.
_DEFAULT_RUNNER = subprocess.run

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


def _parse_implement_test_command(raw):
    """Parse a --implement-test-command CLI value -> None (reset to the default
    test/run.py behavior) or the raw stored value.

    UNLIKE --regression-command, the sentinels here are NOT all mapped to None,
    because null and 'none' mean DIFFERENT things: null runs the touched
    feature's test/run.py, while 'none' SKIPS the IMPLEMENT gate. So:
      - '' (empty) -> None (clear to the default test/run.py behavior);
      - 'none'/'skip' (case-insensitive) -> PRESERVED VERBATIM (returned as the
        lowercased sentinel string) so the reader can distinguish skip from null;
      - any other value -> kept verbatim (whitespace-trimmed) as the command."""
    s = str(raw).strip()
    if s == "":
        return None
    if s.lower() in ("none", "skip"):
        return s.lower()
    return s


def _parse_doc_check_features_root(raw):
    """Parse a --doc-check-features-root CLI value -> None (check OFF) or a
    repo-relative features-root string. The clear sentinels none/null/""
    (case-insensitive) map to None, mirroring the per-day-ceiling clear. Any
    other value is kept verbatim (whitespace-trimmed) but MUST be repo-relative:
    an absolute path raises ValueError (it is kept DISTINCT from `features_root`,
    which may be absolute, so the doc gate stays repo-relative)."""
    s = str(raw).strip()
    if s.lower() in ("none", "null", ""):
        return None
    if os.path.isabs(s):
        raise ValueError(
            "doc-check-features-root must be repo-relative, not absolute")
    return s


def _parse_positive_int(raw, label):
    """Parse a CLI value -> a positive int. Raises ValueError otherwise."""
    value = int(str(raw).strip())  # ValueError on non-int
    if value <= 0:
        raise ValueError(f"{label} must be a positive int")
    return value


def _parse_bool(raw):
    """Parse a --work-own-filings CLI value -> a Python bool.

    Accepts true/false, 1/0, yes/no (case-insensitive). An unparseable value
    raises ValueError (never a silent write)."""
    s = str(raw).strip().lower()
    if s in ("true", "1", "yes"):
        return True
    if s in ("false", "0", "no"):
        return False
    raise ValueError(
        f"work-own-filings must be a bool (true/false/1/0/yes/no), got {raw!r}")


def _parse_features_root(raw):
    """Parse a --features-root CLI value -> None (unconfigured) or the path.

    The clear sentinels none/null/"" (case-insensitive) map to None. Any other
    value is kept verbatim (whitespace-trimmed); it MAY be absolute (UNLIKE
    doc-check-features-root), so no absolute-path check."""
    s = str(raw).strip()
    if s.lower() in ("none", "null", ""):
        return None
    return s


def _parse_issue_labels(raw):
    """Parse a --issue-labels CLI value -> a DNF List[List[str]] candidate.

    Compact syntax: comma = AND within a group, semicolon = OR between groups
    (e.g. "bug,triaged;urgent" -> [["bug","triaged"],["urgent"]]). The clear
    sentinels none/null/"" (case-insensitive) map to [] (no label filter). Each
    label is whitespace-trimmed; stray-delimiter empties WITHIN a non-empty
    group are dropped, but a genuinely-empty group (e.g. between ';;') is kept as
    [] so the issue_filter normalizer rejects it (the writer owns no validation
    the reader does not)."""
    s = str(raw).strip()
    if s.lower() in ("none", "null", ""):
        return []
    groups = []
    for group_str in s.split(";"):
        labels = [lbl.strip() for lbl in group_str.split(",")]
        nonempty = [lbl for lbl in labels if lbl]
        groups.append(nonempty if nonempty else [])
    return groups


def _parse_issue_title_pattern(raw):
    """Parse a --issue-title-pattern CLI value -> None (no filter) or the regex
    string. The clear sentinels none/null/"" (case-insensitive) map to None; any
    other value is kept verbatim (whitespace-trimmed). The regex is COMPILED by
    the issue_filter normalizer at write time, not here."""
    s = str(raw).strip()
    if s.lower() in ("none", "null", ""):
        return None
    return s


def _parse_issue_exclude_labels(raw):
    """Parse a --issue-exclude-labels CLI value -> a flat List[str] candidate.

    Comma-separated (e.g. "auto-maintainer-rejected,wontfix" ->
    ["auto-maintainer-rejected","wontfix"]). The clear sentinels none/null/""
    (case-insensitive) map to [] (no exclusion). Each label is whitespace-
    trimmed and stray-delimiter empties are dropped. exclude_labels is a FLAT OR
    of forbidden labels, never a DNF; the value is validated + canonicalized by
    the issue_filter normalizer at write time (the writer owns no validation the
    reader does not)."""
    s = str(raw).strip()
    if s.lower() in ("none", "null", ""):
        return []
    return [lbl.strip() for lbl in s.split(",") if lbl.strip()]


def _preflight(project_dir, runner=None):
    """READ-ONLY environment probe for the guided --setup onboarding.

    Emits {gh_authenticated, gh_account, resolved_repo, config_exists}. Shells
    `gh auth status` (authenticated = exit 0; the active account is parsed from
    the output when present) and resolves the repo the loop would maintain
    (`gh repo view --json nameWithOwner -q .nameWithOwner`, tolerating failure ->
    None). The subprocess runner is injectable (default `_DEFAULT_RUNNER`) so
    tests drive it without network. Writes NOTHING."""
    if runner is None:
        runner = _DEFAULT_RUNNER

    gh_authenticated = False
    gh_account = None
    try:
        result = runner(
            ["gh", "auth", "status"], capture_output=True, text=True)
        gh_authenticated = result.returncode == 0
        if gh_authenticated:
            gh_account = _parse_gh_account(result.stdout)
    except Exception:
        gh_authenticated = False
        gh_account = None

    resolved_repo = None
    try:
        result = runner(
            ["gh", "repo", "view", "--json", "nameWithOwner",
             "-q", ".nameWithOwner"],
            capture_output=True, text=True)
        if result.returncode == 0:
            out = (result.stdout or "").strip()
            resolved_repo = out or None
    except Exception:
        resolved_repo = None

    return {
        "gh_authenticated": gh_authenticated,
        "gh_account": gh_account,
        "resolved_repo": resolved_repo,
        "config_exists": os.path.exists(_config_path(project_dir)),
    }


def _parse_gh_account(text):
    """Parse the active account login from `gh auth status` output, or None.

    The output carries a line like 'Logged in to github.com account <login>
    (keyring)'; the token after 'account' is the login."""
    for line in (text or "").splitlines():
        parts = line.split()
        for i, token in enumerate(parts):
            if token == "account" and i + 1 < len(parts):
                return parts[i + 1]
    return None


def configure(project_dir, *, mode=None, per_day_tokens=_UNSET,
              interval_minutes=_UNSET, backoff_threshold=_UNSET,
              regression_command=_UNSET, implement_test_command=_UNSET,
              doc_check_features_root=_UNSET,
              features_root=_UNSET, work_own_filings=_UNSET,
              issue_labels=_UNSET, issue_title_pattern=_UNSET,
              issue_exclude_labels=_UNSET):
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

    if implement_test_command is not _UNSET:
        cfg["implement_test_command"] = implement_test_command

    if doc_check_features_root is not _UNSET:
        cfg["doc_check_features_root"] = doc_check_features_root

    if features_root is not _UNSET:
        cfg["features_root"] = features_root

    if work_own_filings is not _UNSET:
        cfg["work_own_filings"] = work_own_filings

    if (issue_labels is not _UNSET or issue_title_pattern is not _UNSET
            or issue_exclude_labels is not _UNSET):
        # Preserve unmentioned issue_filter sub-keys: start from the current
        # (backfilled, already canonicalized to the schema-2.9.0 keys by
        # load_config) issue_filter and override only what was mentioned.
        existing = cfg.get("issue_filter") or {}
        candidate_labels = (issue_labels if issue_labels is not _UNSET
                            else existing.get("include_labels", []))
        candidate_pattern = (issue_title_pattern
                            if issue_title_pattern is not _UNSET
                            else existing.get("with_title_regex"))
        candidate_exclude = (issue_exclude_labels
                            if issue_exclude_labels is not _UNSET
                            else existing.get("exclude_labels", []))
        # Validate + canonicalize THROUGH this feature's reader normalizer (the
        # writer owns no validation the reader does not); a bad label/group/
        # regex/exclude label raises ValueError -> non-zero exit, no partial
        # write. The normalizer returns the consumer DTO (labels/title_pattern/
        # exclude_labels); write config.json with the schema-2.9.0 field names.
        matcher = sg.issue_filter(
            {"issue_filter": {"include_labels": candidate_labels,
                              "with_title_regex": candidate_pattern,
                              "exclude_labels": candidate_exclude}})
        cfg["issue_filter"] = {
            "include_labels": matcher["labels"],
            "with_title_regex": matcher["title_pattern"],
            "exclude_labels": matcher["exclude_labels"],
        }

    path = _config_path(project_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(cfg, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return cfg


def _field_catalog(project_dir):
    """The machine-first field catalog: a list of
    {key, label, controls, default, current, type, validator, stage} entries —
    the single source of truth the guided --setup walk-through reads. Each entry
    carries the loop `stage` that consumes it, and the catalog is ORDERED by loop
    stage (PULL -> IMPLEMENT -> VERIFY -> GATE -> SCHEDULING -> SAFETY) so the
    walk-through follows the route. Read-only."""
    current = sg.load_config(project_dir)
    defaults = sg.DEFAULT_GOVERNANCE
    return [
        # ---- PULL (work-intake): which issues, and the loopback toggle. ----
        {
            "key": "issue_filter.include_labels",
            "label": "Issue label filter",
            "controls": "Which open issues PULL pulls, by label (DNF: "
                        "comma = AND within a group, semicolon = OR between "
                        "groups); empty = no label filter.",
            "default": defaults["issue_filter"]["include_labels"],
            "current": current["issue_filter"]["include_labels"],
            "type": "dnf_labels",
            "validator": "compact DNF 'a,b;c' (comma=AND, semicolon=OR), "
                         "or none/null to clear",
            "stage": "PULL",
        },
        {
            "key": "issue_filter.with_title_regex",
            "label": "Issue title pattern",
            "controls": "A regex an issue's title must match (post-fetch); "
                        "null = no title filter.",
            "default": defaults["issue_filter"]["with_title_regex"],
            "current": current["issue_filter"]["with_title_regex"],
            "type": "str_or_null",
            "validator": "a compilable regex string, or none/null to clear",
            "stage": "PULL",
        },
        {
            "key": "issue_filter.exclude_labels",
            "label": "Issue exclude labels",
            "controls": "A flat list of labels; an open issue carrying ANY of "
                        "them is DROPPED by PULL (a negative filter); "
                        "empty = no exclusion.",
            "default": defaults["issue_filter"]["exclude_labels"],
            "current": current["issue_filter"]["exclude_labels"],
            "type": "flat_labels",
            "validator": "a comma-separated flat list of labels, "
                         "or none/null to clear",
            "stage": "PULL",
        },
        {
            "key": "work_own_filings",
            "label": "Work own filings",
            "controls": "Whether the loop works its OWN filings (the loopback "
                        "provision).",
            "default": defaults["work_own_filings"],
            "current": current["work_own_filings"],
            "type": "bool",
            "validator": "a bool: true/false (also 1/0, yes/no)",
            "stage": "PULL",
        },
        # ---- IMPLEMENT: the trust mode + the per-work-order test-gate. ----
        {
            "key": "mode",
            "label": "Trust mode",
            "controls": "Whether acting states implement, open PRs, or merge.",
            "default": defaults["mode"],
            "current": current["mode"],
            "type": "enum",
            "validator": "one of dry-run | propose | auto-merge",
            "stage": "IMPLEMENT",
        },
        {
            "key": "implement_test_command",
            "label": "IMPLEMENT test-gate command",
            "controls": "The per-work-order test-gate implement runs before "
                        "opening a PR; null = run the touched feature's "
                        "test/run.py, none/skip = skip the gate.",
            "default": defaults["implement_test_command"],
            "current": current["implement_test_command"],
            "type": "str_or_null",
            "validator": "a shell command, none/skip to skip the gate, "
                         "or empty to reset to test/run.py",
            "stage": "IMPLEMENT",
        },
        # ---- VERIFY: the complement locator. ----
        {
            "key": "features_root",
            "label": "VERIFY features root",
            "controls": "VERIFY's cross-feature complement locator (the "
                        "maintained project's features directory); "
                        "null = unconfigured.",
            "default": defaults["features_root"],
            "current": current["features_root"],
            "type": "str_or_null",
            "validator": "a path (MAY be absolute), or none/null to clear",
            "stage": "VERIFY",
        },
        # ---- GATE: regression + doc-check. ----
        {
            "key": "regression_command",
            "label": "GATE regression command",
            "controls": "The full-regression shell command the GATE state runs "
                        "against each REVIEW-passed PR; null = no gate.",
            "default": defaults["regression_command"],
            "current": current["regression_command"],
            "type": "str_or_null",
            "validator": "a shell command string, or none/null to clear",
            "stage": "GATE",
        },
        {
            "key": "doc_check_features_root",
            "label": "GATE doc-check features root",
            "controls": "The repo-relative features root the GATE uses for the "
                        "doc-surface load-bearing-token survival check; "
                        "null = check off.",
            "default": defaults["doc_check_features_root"],
            "current": current["doc_check_features_root"],
            "type": "str_or_null",
            "validator": "a repo-relative (non-absolute) path, "
                         "or none/null to clear",
            "stage": "GATE",
        },
        # ---- SCHEDULING: the tick cadence. ----
        {
            "key": "heartbeat.interval_minutes",
            "label": "Heartbeat interval (minutes)",
            "controls": "The tick cadence the /start heartbeat schedules.",
            "default": defaults["heartbeat"]["interval_minutes"],
            "current": current["heartbeat"]["interval_minutes"],
            "type": "int",
            "validator": "a positive int",
            "stage": "SCHEDULING",
        },
        # ---- SAFETY: budget + backoff. ----
        {
            "key": "budget.per_day_tokens",
            "label": "Per-day token budget",
            "controls": "The per-day token ceiling; null = no limit.",
            "default": defaults["budget"]["per_day_tokens"],
            "current": current["budget"]["per_day_tokens"],
            "type": "int_or_null",
            "validator": "a non-negative int, or none/null/unlimited",
            "stage": "SAFETY",
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
            "stage": "SAFETY",
        },
    ]


_EM_DASH = "—"


def _config_get(config, dotted_key):
    """Read a (possibly dotted) key off the config dict, returning None if any
    segment is missing."""
    node = config
    for seg in dotted_key.split("."):
        if not isinstance(node, dict) or seg not in node:
            return None
        node = node[seg]
    return node


def _render_dnf(dnf):
    """Render an issue_filter include_labels DNF (List[List[str]]) as a readable
    string: [] -> '— (pull all)', a single AND-group ['a','b'] -> '(a AND b)',
    multiple groups -> '(a AND b) OR (c)'."""
    if not dnf:
        return f"{_EM_DASH} (pull all)"
    groups = ["(" + " AND ".join(group) + ")" for group in dnf]
    return " OR ".join(groups)


def _friendly_value(key, config):
    """The friendly, human-readable rendering of a single catalog knob's value,
    read straight off the passed config dict (a pure function of config)."""
    value = _config_get(config, key)
    if key == "heartbeat.interval_minutes":
        return f"{value} min"
    if key == "budget.per_day_tokens":
        tz = _config_get(config, "budget.window_tz")
        if value is None:
            return f"unlimited (window: {tz})"
        return f"{value} tokens/day (window: {tz})"
    if key == "work_own_filings":
        return "on" if value else "off"
    if key == "issue_filter.include_labels":
        return _render_dnf(value or [])
    if key == "issue_filter.exclude_labels":
        return ", ".join(value) if value else _EM_DASH
    if key in ("regression_command", "doc_check_features_root",
               "features_root", "implement_test_command",
               "issue_filter.with_title_regex"):
        return value if value else _EM_DASH
    # mode, backoff.threshold and any other plain scalar render as-is.
    return str(value)


def render_config(config, project_dir=None):
    """The DERIVED HUMAN VIEW of a loaded config (machine-first §1: config.json is
    the machine artifact; this render is its derived human view — mirrors
    scheduling/status.py's status_data()/render_status() split).

    A PURE, DETERMINISTIC formatter: same config in => byte-identical text out; no
    I/O beyond reading the _field_catalog labels/order, no wall-clock, no model,
    and it does not mutate config. It groups the catalog knobs by their loop
    `stage`, PRESERVING the catalog's loop-stage order (PULL -> IMPLEMENT ->
    VERIFY -> GATE -> SCHEDULING -> SAFETY), so the human view can never drift
    from the knob catalog (the same catalog --describe emits)."""
    catalog = _field_catalog(project_dir)
    lines = []
    header = f"auto-maintainer config {_EM_DASH} schema {config.get('schema_version')}"
    lines.append(header)
    lines.append("=" * len(header))
    # Group knobs by stage, preserving the catalog's first-seen stage order.
    stage_order = []
    by_stage = {}
    for entry in catalog:
        stage = entry["stage"]
        if stage not in by_stage:
            by_stage[stage] = []
            stage_order.append(stage)
        by_stage[stage].append(entry)
    for stage in stage_order:
        lines.append("")
        lines.append(f"{stage}")
        lines.append("-" * len(stage))
        for entry in by_stage[stage]:
            friendly = _friendly_value(entry["key"], config)
            lines.append(f"  {entry['label']}: {friendly}")
    return "\n".join(lines) + "\n"


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
        "--implement-test-command",
        default=None,
        help="IMPLEMENT-side per-work-order test-gate command; empty ('') "
             "resets to the default test/run.py, none/skip skips the gate "
             "(preserved verbatim, NOT cleared to null)",
    )
    parser.add_argument(
        "--doc-check-features-root",
        default=None,
        help="repo-relative features root for the GATE doc-surface "
             "load-bearing-token survival check, or none/null to clear "
             "(check off)",
    )
    parser.add_argument(
        "--features-root",
        default=None,
        help="VERIFY's complement locator path (MAY be absolute), or "
             "none/null to clear (unconfigured)",
    )
    parser.add_argument(
        "--work-own-filings",
        default=None,
        help="whether the loop works its OWN filings: a bool "
             "(true/false, also 1/0, yes/no)",
    )
    parser.add_argument(
        "--issue-labels",
        default=None,
        help="issue_filter.include_labels DNF: compact 'a,b;c' (comma=AND "
             "within a group, semicolon=OR between groups), or none/null to "
             "clear",
    )
    parser.add_argument(
        "--issue-title-pattern",
        default=None,
        help="issue_filter.with_title_regex: a compilable regex an issue title "
             "must match, or none/null to clear",
    )
    parser.add_argument(
        "--issue-exclude-labels",
        default=None,
        help="issue_filter.exclude_labels: a flat comma-separated list of "
             "forbidden labels (an issue carrying ANY is dropped by PULL), or "
             "none/null to clear",
    )
    parser.add_argument(
        "--describe",
        action="store_true",
        help="emit the machine-first field catalog as JSON (read-only)",
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="emit the read-only environment probe as JSON "
             "(gh auth + resolved repo + config_exists); writes nothing",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="print the current config without changing it",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="machine-first escape hatch: --show and the post-write echo emit "
             "the raw JSON instead of the human render (--describe/--preflight "
             "always emit JSON, unaffected by this flag)",
    )
    args = parser.parse_args(argv)
    project_dir = _resolve_project_dir(args.project_dir)

    if args.describe:
        print(json.dumps(_field_catalog(project_dir), indent=2))
        return 0

    if args.preflight:
        print(json.dumps(_preflight(project_dir), indent=2))
        return 0

    mutating = (
        args.mode is not None
        or args.per_day_tokens is not None
        or args.interval_minutes is not None
        or args.backoff_threshold is not None
        or args.regression_command is not None
        or args.implement_test_command is not None
        or args.doc_check_features_root is not None
        or args.features_root is not None
        or args.work_own_filings is not None
        or args.issue_labels is not None
        or args.issue_title_pattern is not None
        or args.issue_exclude_labels is not None
    )

    # --show, or no mutating flags at all: print the current config and stop.
    # By DEFAULT this is the human render; --json re-selects the raw JSON.
    if args.show or not mutating:
        current = sg.load_config(project_dir)
        if args.json:
            print(json.dumps(current, indent=2, sort_keys=True))
        else:
            print(render_config(current, project_dir))
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
        implement_test = (
            _parse_implement_test_command(args.implement_test_command)
            if args.implement_test_command is not None else _UNSET)
        doc_check_root = (
            _parse_doc_check_features_root(args.doc_check_features_root)
            if args.doc_check_features_root is not None else _UNSET)
        feats_root = (_parse_features_root(args.features_root)
                      if args.features_root is not None else _UNSET)
        work_own = (_parse_bool(args.work_own_filings)
                    if args.work_own_filings is not None else _UNSET)
        labels = (_parse_issue_labels(args.issue_labels)
                  if args.issue_labels is not None else _UNSET)
        title_pattern = (_parse_issue_title_pattern(args.issue_title_pattern)
                         if args.issue_title_pattern is not None else _UNSET)
        exclude_labels = (
            _parse_issue_exclude_labels(args.issue_exclude_labels)
            if args.issue_exclude_labels is not None else _UNSET)
        cfg = configure(
            project_dir,
            mode=args.mode,
            per_day_tokens=per_day,
            interval_minutes=interval,
            backoff_threshold=threshold,
            regression_command=regression,
            implement_test_command=implement_test,
            doc_check_features_root=doc_check_root,
            features_root=feats_root,
            work_own_filings=work_own,
            issue_labels=labels,
            issue_title_pattern=title_pattern,
            issue_exclude_labels=exclude_labels,
        )
    except ValueError as exc:
        print(f"configure: error: {exc}", file=sys.stderr)
        return 2

    # Post-write echo: the human render by default, raw JSON under --json.
    if args.json:
        print(json.dumps(cfg, indent=2, sort_keys=True))
    else:
        print(render_config(cfg, project_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
