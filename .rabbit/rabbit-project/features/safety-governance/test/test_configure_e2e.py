#!/usr/bin/env python3
"""End-to-end tests for the central-config WRITER (src/configure.py) and the
shipped /auto-maintainer:configure skill (schema 2.5.0).

safety_governance.py is the READER/decider over config.json; configure.py is its
writer half (spec: "Config writer + the configure skill"). It performs a
deterministic load-modify-save of project-local
${project_dir}/.auto-maintainer/config.json:

  - sets `mode`, validated through the closed mode set via permits; an unknown
    mode is a non-zero CLI exit / ValueError and NO file is written;
  - sets `per_day_tokens` to a non-negative int, or to none/null/unlimited/""
    meaning NO LIMIT (stored as JSON null);
  - sets `heartbeat.interval_minutes` / `backoff.threshold` to a POSITIVE int;
  - preserves unmentioned keys (load-modify-save: an earlier mode survives a
    later budget-only write);
  - `--show` (or no mutating flag) prints the current config and writes nothing;
  - `--describe` emits the machine-first field catalog (read-only).

The removed flags --per-tick-tokens / --maintainer-repo are no longer accepted.

Determinism: all writes go to a temp project-dir; the real .auto-maintainer is
never touched.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

import yaml

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# configure.py imports safety_governance, which imports lifecycle_dispositions
# (which in turn imports fsm_contracts). Put those sibling srcs on the path too.
_LD_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import configure  # noqa: E402
import safety_governance as sg  # noqa: E402

_SKILL_PATH = os.path.join(
    _FEATURE_DIR, "ship", "skills", "configure", "SKILL.md")


def _cfg_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "config.json")


def _read_cfg(project_dir):
    with open(_cfg_path(project_dir), "r") as f:
        return json.load(f)


# ==========================================================================
# E2E Behaviour: --mode sets the trust mode and writes config.json with it,
# at schema 2.5.0.
# ==========================================================================

def test_cli_sets_mode_and_writes_file():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir, "--mode", "dry-run"])
        assert rc == 0
        cfg = _read_cfg(project_dir)
        assert cfg["mode"] == "dry-run"
        assert cfg["schema_version"] == "2.9.0"


# ==========================================================================
# E2E Behaviour: the legacy mode name `gated-merge` is TOLERATED on the CLI and
# stored canonically as `auto-merge` (the rename coexistence migration).
# ==========================================================================

def test_cli_legacy_gated_merge_stored_as_auto_merge():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--mode", "gated-merge"])
        assert rc == 0
        cfg = _read_cfg(project_dir)
        assert cfg["mode"] == "auto-merge"


# ==========================================================================
# E2E Behaviour: an unknown mode is rejected with a non-zero CLI exit AND no
# file is written (never a partial/invalid write).
# ==========================================================================

def test_cli_unknown_mode_exits_nonzero_and_writes_nothing():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--mode", "bogus-mode"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# Behaviour: the configure() API raises ValueError on an unknown mode.
# ==========================================================================

def test_configure_api_unknown_mode_raises():
    with tempfile.TemporaryDirectory() as project_dir:
        raised = False
        try:
            configure.configure(project_dir, mode="bogus-mode")
        except ValueError:
            raised = True
        assert raised, "configure() must raise ValueError on unknown mode"


# ==========================================================================
# E2E Behaviour: --per-day-tokens accepts an int, and `none` -> JSON null.
# ==========================================================================

def test_cli_per_day_tokens_int_then_none():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--per-day-tokens", "200000"])
        assert rc == 0
        assert _read_cfg(project_dir)["budget"]["per_day_tokens"] == 200000

        rc = configure.main(
            ["--project-dir", project_dir, "--per-day-tokens", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["budget"]["per_day_tokens"] is None


# ==========================================================================
# E2E Behaviour: a negative ceiling is rejected (non-zero exit, no write).
# ==========================================================================

def test_cli_negative_ceiling_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--per-day-tokens", "-5"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: --interval-minutes sets heartbeat.interval_minutes (positive
# int); zero/negative is rejected (non-zero exit, no write).
# ==========================================================================

def test_cli_interval_minutes_positive():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--interval-minutes", "10"])
        assert rc == 0
        assert _read_cfg(project_dir)["heartbeat"]["interval_minutes"] == 10


def test_cli_interval_minutes_nonpositive_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--interval-minutes", "0"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: --backoff-threshold sets backoff.threshold (positive int);
# zero/negative is rejected.
# ==========================================================================

def test_cli_backoff_threshold_positive():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--backoff-threshold", "8"])
        assert rc == 0
        assert _read_cfg(project_dir)["backoff"]["threshold"] == 8


def test_cli_backoff_threshold_nonpositive_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--backoff-threshold", "-1"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: --regression-command sets the top-level GATE regression_command
# to the given shell command, and a clear sentinel (none/null/"") resets it to
# JSON null (no gate), mirroring --per-day-tokens.
# ==========================================================================

def test_cli_regression_command_set_then_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--regression-command", "bash scripts/gate-regression.sh"])
        assert rc == 0
        assert (_read_cfg(project_dir)["regression_command"]
                == "bash scripts/gate-regression.sh")

        rc = configure.main(
            ["--project-dir", project_dir, "--regression-command", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["regression_command"] is None


def test_cli_regression_command_empty_clears():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--regression-command", "make check"]) == 0
        assert _read_cfg(project_dir)["regression_command"] == "make check"
        assert configure.main(
            ["--project-dir", project_dir, "--regression-command", ""]) == 0
        assert _read_cfg(project_dir)["regression_command"] is None


# ==========================================================================
# E2E Behaviour: --implement-test-command sets the top-level
# implement_test_command to the given shell command; '' (empty) clears it to
# JSON null (the default test/run.py behavior); the literal 'none'/'skip' is
# PRESERVED VERBATIM as the skip sentinel (NOT mapped to null — null and 'none'
# mean different things: null = run test/run.py, 'none' = skip the IMPLEMENT
# gate). The writer stores the RAW value; implement's test_gate.py interprets it.
# ==========================================================================

def test_cli_implement_test_command_set_command():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--implement-test-command", "pytest -q"])
        assert rc == 0
        assert (_read_cfg(project_dir)["implement_test_command"]
                == "pytest -q")


def test_cli_implement_test_command_empty_clears_to_null():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--implement-test-command", "make test"]) == 0
        assert (_read_cfg(project_dir)["implement_test_command"]
                == "make test")
        # '' clears back to the default null (run test/run.py).
        assert configure.main(
            ["--project-dir", project_dir,
             "--implement-test-command", ""]) == 0
        assert _read_cfg(project_dir)["implement_test_command"] is None


def test_cli_implement_test_command_none_preserved_verbatim():
    """The literal 'none' is the SKIP sentinel — preserved verbatim, NOT mapped
    to null (unlike --regression-command's none -> null clear)."""
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--implement-test-command", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["implement_test_command"] == "none"


def test_cli_implement_test_command_skip_preserved_verbatim():
    """The literal 'skip' is an alias for the skip sentinel — preserved verbatim
    (lowercased), NOT mapped to null."""
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--implement-test-command", "SKIP"])
        assert rc == 0
        assert _read_cfg(project_dir)["implement_test_command"] == "skip"


# ==========================================================================
# E2E Behaviour: --doc-check-features-root sets the top-level
# doc_check_features_root to a repo-relative path (turning the GATE doc-survival
# check ON), and a clear sentinel (none/null/"") resets it to JSON null (check
# off), mirroring --regression-command.
# ==========================================================================

def test_cli_doc_check_features_root_set_then_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--doc-check-features-root", "features"])
        assert rc == 0
        assert (_read_cfg(project_dir)["doc_check_features_root"]
                == "features")

        rc = configure.main(
            ["--project-dir", project_dir,
             "--doc-check-features-root", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["doc_check_features_root"] is None


def test_cli_doc_check_features_root_nested_repo_relative():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--doc-check-features-root", ".rabbit/rabbit-project/features"])
        assert rc == 0
        assert (_read_cfg(project_dir)["doc_check_features_root"]
                == ".rabbit/rabbit-project/features")


# ==========================================================================
# E2E Behaviour: an ABSOLUTE doc-check-features-root is rejected (non-zero exit,
# no write) — the GATE doc root must stay repo-relative, kept distinct from
# features_root (which may be absolute).
# ==========================================================================

def test_cli_doc_check_features_root_absolute_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--doc-check-features-root", "/abs/features"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: load-modify-save preserves unmentioned keys. Set mode first,
# then a budget-only write later; the earlier mode must survive.
# ==========================================================================

def test_load_modify_save_preserves_unmentioned_keys():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir, "--mode", "auto-merge"]) == 0
        assert configure.main(
            ["--project-dir", project_dir,
             "--per-day-tokens", "200000"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["mode"] == "auto-merge"
        assert cfg["budget"]["per_day_tokens"] == 200000
        assert cfg["budget"]["window_tz"] == "local"
        # heartbeat/backoff defaults preserved across the modify-save.
        assert cfg["heartbeat"]["interval_minutes"] == 10
        assert cfg["backoff"]["threshold"] == 5


# ==========================================================================
# E2E Behaviour: --show prints the current config and writes nothing.
# ==========================================================================

def test_cli_show_writes_nothing():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir, "--show"])
        assert rc == 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: no mutating flag at all behaves like --show (prints, no write).
# ==========================================================================

def test_cli_no_flags_writes_nothing():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir])
        assert rc == 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: the removed flags are no longer accepted (argparse error =>
# non-zero exit, no write).
# ==========================================================================

def test_cli_removed_per_tick_flag_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        raised_or_nonzero = False
        try:
            rc = configure.main(
                ["--project-dir", project_dir, "--per-tick-tokens", "5000"])
            raised_or_nonzero = rc != 0
        except SystemExit as exc:
            raised_or_nonzero = exc.code != 0
        assert raised_or_nonzero
        assert not os.path.exists(_cfg_path(project_dir))


def test_cli_removed_maintainer_repo_flag_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        raised_or_nonzero = False
        try:
            rc = configure.main(
                ["--project-dir", project_dir,
                 "--maintainer-repo", "octo/tooling"])
            raised_or_nonzero = rc != 0
        except SystemExit as exc:
            raised_or_nonzero = exc.code != 0
        assert raised_or_nonzero
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: --describe emits the machine-first field catalog as JSON — a
# list of {key, label, controls, default, current, type, validator} — read-only
# (writes nothing).
# ==========================================================================

def test_cli_describe_emits_field_catalog():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(["--project-dir", project_dir, "--describe"])
        assert rc == 0
        catalog = json.loads(buf.getvalue())
        assert isinstance(catalog, list) and catalog
        keys = {entry["key"] for entry in catalog}
        # The user-facing knobs are described.
        assert "mode" in keys
        assert "budget.per_day_tokens" in keys
        assert "heartbeat.interval_minutes" in keys
        assert "backoff.threshold" in keys
        assert "regression_command" in keys
        assert "doc_check_features_root" in keys
        for entry in catalog:
            for field in ("key", "label", "controls", "default",
                          "current", "type", "validator", "stage"):
                assert field in entry, f"catalog entry missing {field}: {entry}"
        # read-only: no config.json written.
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: the --describe catalog is COMPLETE — exactly one entry per
# user-facing knob, each carrying every required field (key/label/controls/
# default/current/type/validator), and no extras. This is the single source of
# truth the guided --setup walk-through reads, so it must enumerate every knob.
# ==========================================================================

def test_describe_catalog_is_complete_one_entry_per_knob():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(["--project-dir", project_dir, "--describe"])
        assert rc == 0
        catalog = json.loads(buf.getvalue())
        keys = [entry["key"] for entry in catalog]
        # Exactly one entry per writable knob — no duplicates, no omissions.
        expected_keys = {
            "mode",
            "implement_test_command",
            "budget.per_day_tokens",
            "heartbeat.interval_minutes",
            "backoff.threshold",
            "regression_command",
            "doc_check_features_root",
            "features_root",
            "work_own_filings",
            "issue_filter.include_labels",
            "issue_filter.with_title_regex",
        }
        assert set(keys) == expected_keys, (
            f"catalog knobs {set(keys)} != expected {expected_keys}")
        assert len(keys) == len(set(keys)), "duplicate keys in catalog"
        # Every entry carries exactly the eight required fields (no more).
        required = {"key", "label", "controls", "default",
                    "current", "type", "validator", "stage"}
        for entry in catalog:
            assert set(entry.keys()) == required, (
                f"catalog entry fields {set(entry.keys())} != {required}: "
                f"{entry}")


# ==========================================================================
# Behaviour: the shipped /auto-maintainer:configure skill exists and carries
# frontmatter with the lifecycle/identity keys.
# ==========================================================================

def _skill_frontmatter():
    with open(_SKILL_PATH, "r") as f:
        text = f.read()
    assert text.startswith("---"), "SKILL.md must open with YAML frontmatter"
    _, fm, _body = text.split("---", 2)
    return yaml.safe_load(fm)


def test_shipped_configure_skill_exists():
    assert os.path.isfile(_SKILL_PATH), (
        "shipped configure skill must exist at "
        "ship/skills/configure/SKILL.md")


def test_shipped_configure_skill_frontmatter_keys():
    fm = _skill_frontmatter()
    for key in ("name", "description", "version", "owner",
                "deprecation_criterion"):
        assert key in fm, f"frontmatter missing required key: {key}"


def _skill_body():
    with open(_SKILL_PATH, "r") as f:
        text = f.read()
    _, _fm, body = text.split("---", 2)
    return body


# ==========================================================================
# E2E Behaviour (guided --setup walk-through, spec "Guided --setup
# walk-through"): the configure skill advertises the guided --setup /
# walk-me-through entrypoint so the skill triggers when the user asks to be
# walked through the config. The skill version tracks the latest config change
# (bumped to 0.7.0 for the gated-merge -> auto-merge mode rename).
# ==========================================================================

def test_skill_version_bumped():
    fm = _skill_frontmatter()
    assert fm["version"] == "0.10.0", (
        f"configure skill must be bumped to 0.10.0, got {fm['version']}")


def test_skill_description_advertises_setup_walkthrough():
    fm = _skill_frontmatter()
    desc = fm["description"].lower()
    assert "--setup" in desc or "setup" in desc, (
        "description must advertise the guided --setup entrypoint")
    assert "walk" in desc, (
        "description must mention walking the user through the config")


# ==========================================================================
# E2E Behaviour: the skill body documents the guided --setup walk-through that
# orchestrates over the machine-first --describe catalog, field-by-field, and
# applies the chosen values in ONE configure.py invocation then --show.
# ==========================================================================

def test_skill_body_documents_describe_driven_setup():
    body = _skill_body()
    assert "--setup" in body, "skill body must document the --setup mode"
    assert "--describe" in body, (
        "skill body must read the --describe field catalog")
    # The catalog field names are the contract surface the walk-through reads.
    for token in ("key", "label", "controls", "default", "current"):
        assert token in body, (
            f"skill body must reference the catalog field '{token}'")
    assert "--show" in body, (
        "skill body must --show the result after applying")


# ==========================================================================
# E2E Behaviour: the guided walk-through dispatches NO subagent (spec: "The
# skill orchestrates over the machine-first catalog, dispatching NO subagent").
# ==========================================================================

def test_skill_body_dispatches_no_subagent():
    body = _skill_body()
    lowered = body.lower()
    assert "subagent" not in lowered or "no subagent" in lowered, (
        "guided --setup must dispatch no subagent")
    assert "agent(" not in lowered, (
        "skill body must not dispatch an Agent()")


# ==========================================================================
# E2E Behaviour: the walk-through does not hardcode field names/prose; it
# derives them from the catalog (SKILL.md authoring §4: derive from source, do
# not paraphrase). The body must state the catalog is the single source of
# truth and that it does not hardcode field names.
# ==========================================================================

def test_skill_body_states_catalog_is_source_of_truth():
    body = _skill_body().lower()
    assert "source of truth" in body, (
        "skill body must state the catalog is the single source of truth")
    assert "hardcode" in body or "hard-code" in body or "hard code" in body, (
        "skill body must state it does not hardcode field names")


# ==========================================================================
# E2E Behaviour: the SKILL.md documents the --regression-command flag and its
# catalog-key -> flag mapping (regression_command -> --regression-command), so
# the guided walk-through can drive the new GATE knob.
# ==========================================================================

def test_skill_body_documents_regression_command():
    body = _skill_body()
    assert "--regression-command" in body, (
        "skill body must document the --regression-command flag")
    assert "regression_command" in body, (
        "skill body must map the regression_command catalog key to its flag")


# ==========================================================================
# E2E Behaviour: the SKILL.md documents the --doc-check-features-root flag and
# its catalog-key -> flag mapping (doc_check_features_root ->
# --doc-check-features-root), so the guided walk-through can drive the GATE
# doc-survival knob.
# ==========================================================================

def test_skill_body_documents_doc_check_features_root():
    body = _skill_body()
    assert "--doc-check-features-root" in body, (
        "skill body must document the --doc-check-features-root flag")
    assert "doc_check_features_root" in body, (
        "skill body must map the doc_check_features_root catalog key to its "
        "flag")


# ==========================================================================
# Phase 2 E2E Behaviour: --features-root sets the top-level features_root
# (VERIFY's complement locator). Unlike doc_check_features_root it MAY be
# absolute; a clear sentinel (none/null/"") resets it to JSON null.
# ==========================================================================

def test_cli_features_root_set_then_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--features-root", "rabbit-project/features"])
        assert rc == 0
        assert (_read_cfg(project_dir)["features_root"]
                == "rabbit-project/features")

        rc = configure.main(
            ["--project-dir", project_dir, "--features-root", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["features_root"] is None


def test_cli_features_root_accepts_absolute():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--features-root", "/abs/project/features"])
        assert rc == 0
        assert (_read_cfg(project_dir)["features_root"]
                == "/abs/project/features")


# ==========================================================================
# Phase 2 E2E Behaviour: --work-own-filings parses a bool (true/false, also
# 1/0, yes/no case-insensitive) and writes work_own_filings; an unparseable
# value is a non-zero exit and writes nothing.
# ==========================================================================

def test_cli_work_own_filings_true_then_false():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--work-own-filings", "false"])
        assert rc == 0
        assert _read_cfg(project_dir)["work_own_filings"] is False

        rc = configure.main(
            ["--project-dir", project_dir, "--work-own-filings", "true"])
        assert rc == 0
        assert _read_cfg(project_dir)["work_own_filings"] is True


def test_cli_work_own_filings_tolerant_synonyms():
    for raw, expected in (("1", True), ("0", False),
                          ("yes", True), ("NO", False), ("True", True)):
        with tempfile.TemporaryDirectory() as project_dir:
            rc = configure.main(
                ["--project-dir", project_dir, "--work-own-filings", raw])
            assert rc == 0, f"{raw!r} should parse"
            assert _read_cfg(project_dir)["work_own_filings"] is expected


def test_cli_work_own_filings_unparseable_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--work-own-filings", "maybe"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# Phase 2 E2E Behaviour: --issue-labels parses the compact DNF syntax
# (comma = AND within a group, semicolon = OR between groups) into the
# canonical List[List[str]] and writes issue_filter.include_labels (the flag
# name is retained for back-compat; the field was RENAMED in schema 2.9.0); a
# clear sentinel (none/null/"") resets it to [].
# ==========================================================================

def test_cli_issue_labels_dnf_parse():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--issue-labels", "bug,triaged;urgent"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["include_labels"]
                == [["bug", "triaged"], ["urgent"]])


def test_cli_issue_labels_single_group():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--issue-labels", "bug"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["include_labels"]
                == [["bug"]])


def test_cli_issue_labels_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir, "--issue-labels", "bug"]) == 0
        assert configure.main(
            ["--project-dir", project_dir, "--issue-labels", "none"]) == 0
        assert _read_cfg(project_dir)["issue_filter"]["include_labels"] == []


# ==========================================================================
# Phase 2 E2E Behaviour: an --issue-labels value that normalizes to an empty
# label / empty group (e.g. all-delimiters) is rejected THROUGH this feature's
# issue_filter normalizer (non-zero exit, no partial write).
# ==========================================================================

def test_cli_issue_labels_empty_group_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--issue-labels", "bug;;urgent"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# Phase 2 E2E Behaviour: --issue-title-pattern sets
# issue_filter.with_title_regex to a regex string (the flag name is retained for
# back-compat; the field was RENAMED in schema 2.9.0); a clear sentinel
# (none/null/"") resets it to null; a non-compilable regex is a non-zero exit
# (validated via the normalizer) with no write.
# ==========================================================================

def test_cli_issue_title_pattern_set_then_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--issue-title-pattern", r"^\[bug\]"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["with_title_regex"]
                == r"^\[bug\]")

        rc = configure.main(
            ["--project-dir", project_dir, "--issue-title-pattern", "none"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["with_title_regex"]
                is None)


def test_cli_issue_title_pattern_compile_fail_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--issue-title-pattern", "(unclosed"])
        assert rc != 0
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# Phase 2 E2E Behaviour: setting ONLY --issue-labels preserves an existing
# title_pattern (and vice versa) — the writer preserves unmentioned issue_filter
# sub-keys.
# ==========================================================================

def test_cli_issue_labels_preserves_existing_title_pattern():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-title-pattern", r"^\[bug\]"]) == 0
        assert configure.main(
            ["--project-dir", project_dir, "--issue-labels", "urgent"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["issue_filter"]["include_labels"] == [["urgent"]]
        assert cfg["issue_filter"]["with_title_regex"] == r"^\[bug\]"


def test_cli_issue_title_pattern_preserves_existing_labels():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir, "--issue-labels", "urgent"]) == 0
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-title-pattern", r"^\[bug\]"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["issue_filter"]["include_labels"] == [["urgent"]]
        assert cfg["issue_filter"]["with_title_regex"] == r"^\[bug\]"


# ==========================================================================
# E2E Behaviour: --issue-exclude-labels sets the issue_filter.exclude_labels
# flat OR of forbidden labels (comma-separated); a clear sentinel
# (none/null/"") resets it to []. Validated + canonicalized through this
# feature's issue_filter normalizer (the writer owns no validation the reader
# does not).
# ==========================================================================

def test_cli_issue_exclude_labels_set_then_clear():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--issue-exclude-labels", "auto-maintainer-rejected,wontfix"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["exclude_labels"]
                == ["auto-maintainer-rejected", "wontfix"])

        rc = configure.main(
            ["--project-dir", project_dir, "--issue-exclude-labels", "none"])
        assert rc == 0
        assert _read_cfg(project_dir)["issue_filter"]["exclude_labels"] == []


def test_cli_issue_exclude_labels_single():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir,
             "--issue-exclude-labels", "auto-maintainer-rejected"])
        assert rc == 0
        assert (_read_cfg(project_dir)["issue_filter"]["exclude_labels"]
                == ["auto-maintainer-rejected"])


def test_cli_issue_exclude_labels_preserves_labels_and_pattern():
    """Setting ONLY --issue-exclude-labels preserves an existing include_labels
    DNF and with_title_regex (the writer preserves unmentioned issue_filter
    sub-keys)."""
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-labels", "bug", "--issue-title-pattern", r"^\[x\]"]) == 0
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-exclude-labels", "wontfix"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["issue_filter"]["include_labels"] == [["bug"]]
        assert cfg["issue_filter"]["with_title_regex"] == r"^\[x\]"
        assert cfg["issue_filter"]["exclude_labels"] == ["wontfix"]


def test_cli_issue_labels_preserves_existing_exclude_labels():
    """Setting --issue-labels preserves a previously-set exclude_labels."""
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-exclude-labels", "wontfix"]) == 0
        assert configure.main(
            ["--project-dir", project_dir, "--issue-labels", "bug"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["issue_filter"]["include_labels"] == [["bug"]]
        assert cfg["issue_filter"]["exclude_labels"] == ["wontfix"]


def test_cli_canonicalizes_legacy_keyed_config_on_write():
    """COEXISTENCE: an existing config.json written with the LEGACY issue_filter
    keys (labels/title_pattern) is canonicalized to the NEW schema-2.9.0 names
    (include_labels/with_title_regex) when configure loads-modifies-saves it —
    the legacy names are dropped, the user's values preserved."""
    with tempfile.TemporaryDirectory() as project_dir:
        os.makedirs(os.path.dirname(_cfg_path(project_dir)), exist_ok=True)
        with open(_cfg_path(project_dir), "w") as fh:
            json.dump({"issue_filter": {"labels": [["bug"]],
                                        "title_pattern": r"^\[x\]"}}, fh)
        # Modify an unrelated issue_filter sub-key; the load-modify-save must
        # canonicalize the whole object to the new key names.
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-exclude-labels", "wontfix"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["issue_filter"]["include_labels"] == [["bug"]]
        assert cfg["issue_filter"]["with_title_regex"] == r"^\[x\]"
        assert cfg["issue_filter"]["exclude_labels"] == ["wontfix"]
        assert "labels" not in cfg["issue_filter"]
        assert "title_pattern" not in cfg["issue_filter"]


# ==========================================================================
# Phase 2 E2E Behaviour: --describe entries carry a loop-'stage' field and the
# catalog is ORDERED by loop stage:
# PULL -> IMPLEMENT -> VERIFY -> GATE -> SCHEDULING -> SAFETY, with the new
# knobs (issue_filter.include_labels/with_title_regex, work_own_filings,
# features_root) present under their stages.
# ==========================================================================

def _describe(project_dir):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = configure.main(["--project-dir", project_dir, "--describe"])
    assert rc == 0
    return json.loads(buf.getvalue())


def test_describe_entries_carry_stage_in_loop_order():
    with tempfile.TemporaryDirectory() as project_dir:
        catalog = _describe(project_dir)
        by_key = {e["key"]: e["stage"] for e in catalog}
        assert by_key["issue_filter.include_labels"] == "PULL"
        assert by_key["issue_filter.with_title_regex"] == "PULL"
        assert by_key["work_own_filings"] == "PULL"
        assert by_key["mode"] == "IMPLEMENT"
        assert by_key["implement_test_command"] == "IMPLEMENT"
        assert by_key["features_root"] == "VERIFY"
        assert by_key["regression_command"] == "GATE"
        assert by_key["doc_check_features_root"] == "GATE"
        assert by_key["heartbeat.interval_minutes"] == "SCHEDULING"
        assert by_key["budget.per_day_tokens"] == "SAFETY"
        assert by_key["backoff.threshold"] == "SAFETY"
        # The catalog is ordered by loop stage.
        stage_order = [e["stage"] for e in catalog]
        expected_stage_seq = ["PULL", "IMPLEMENT", "VERIFY", "GATE",
                              "SCHEDULING", "SAFETY"]
        # The stages appear in the documented order (contiguous, non-repeating
        # after first appearance).
        seen = []
        for st in stage_order:
            if not seen or seen[-1] != st:
                seen.append(st)
        assert seen == expected_stage_seq, (
            f"catalog stage order {seen} != {expected_stage_seq}")


# ==========================================================================
# Phase 2 E2E Behaviour: --preflight is a READ-ONLY probe emitting the
# machine-first 4-key dict {gh_authenticated, gh_account, resolved_repo,
# config_exists}. The gh subprocess runner is injectable so tests drive it
# without network; --preflight writes nothing.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_runner(*, auth_rc, auth_out="", repo_rc=0, repo_out=""):
    def run(cmd, *args, **kwargs):
        # Distinguish `gh auth status` from `gh repo view ...`.
        if "auth" in cmd:
            return _FakeCompleted(auth_rc, stdout=auth_out)
        if "repo" in cmd:
            return _FakeCompleted(repo_rc, stdout=repo_out)
        raise AssertionError(f"unexpected command {cmd!r}")
    return run


def test_preflight_authed_repo_resolved():
    with tempfile.TemporaryDirectory() as project_dir:
        runner = _fake_runner(
            auth_rc=0,
            auth_out="  Logged in to github.com account changyu87 (keyring)\n",
            repo_rc=0,
            repo_out="changyu87/auto-maintainer-framework\n",
        )
        result = configure._preflight(project_dir, runner=runner)
        assert result["gh_authenticated"] is True
        assert result["gh_account"] == "changyu87"
        assert result["resolved_repo"] == "changyu87/auto-maintainer-framework"
        assert result["config_exists"] is False
        # read-only: no config.json written.
        assert not os.path.exists(_cfg_path(project_dir))


def test_preflight_unauthed():
    with tempfile.TemporaryDirectory() as project_dir:
        runner = _fake_runner(
            auth_rc=1,
            auth_out="",
            repo_rc=1,
            repo_out="",
        )
        result = configure._preflight(project_dir, runner=runner)
        assert result["gh_authenticated"] is False
        assert result["gh_account"] is None
        assert result["resolved_repo"] is None
        assert result["config_exists"] is False


def test_preflight_authed_repo_unresolved():
    with tempfile.TemporaryDirectory() as project_dir:
        runner = _fake_runner(
            auth_rc=0,
            auth_out="  Logged in to github.com account changyu87 (keyring)\n",
            repo_rc=1,
            repo_out="",
        )
        result = configure._preflight(project_dir, runner=runner)
        assert result["gh_authenticated"] is True
        assert result["gh_account"] == "changyu87"
        assert result["resolved_repo"] is None


def test_preflight_reports_config_exists():
    with tempfile.TemporaryDirectory() as project_dir:
        # Create a config first.
        assert configure.main(
            ["--project-dir", project_dir, "--mode", "propose"]) == 0
        runner = _fake_runner(
            auth_rc=0,
            auth_out="  Logged in to github.com account changyu87 (keyring)\n",
            repo_rc=0,
            repo_out="changyu87/auto-maintainer-framework\n",
        )
        result = configure._preflight(project_dir, runner=runner)
        assert result["config_exists"] is True


def test_preflight_cli_emits_json_and_writes_nothing():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        # The CLI --preflight path uses the real subprocess default runner, but
        # since we only assert the JSON shape + no-write, monkeypatch the module
        # default runner to a fake to avoid network.
        original = configure._DEFAULT_RUNNER
        configure._DEFAULT_RUNNER = _fake_runner(
            auth_rc=1, auth_out="", repo_rc=1, repo_out="")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = configure.main(
                    ["--project-dir", project_dir, "--preflight"])
            assert rc == 0
            result = json.loads(buf.getvalue())
            assert set(result.keys()) == {
                "gh_authenticated", "gh_account",
                "resolved_repo", "config_exists"}
        finally:
            configure._DEFAULT_RUNNER = original
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# Phase 2 E2E Behaviour: the SKILL.md documents the new Phase 2 flags and the
# preflight-driven onboarding (--preflight, --features-root, --work-own-filings,
# --issue-labels, --issue-title-pattern) and groups the walk by loop 'stage'.
# ==========================================================================

def test_skill_body_documents_phase2_flags():
    body = _skill_body()
    for flag in ("--preflight", "--features-root", "--work-own-filings",
                 "--issue-labels", "--issue-title-pattern"):
        assert flag in body, f"skill body must document {flag}"
    assert "stage" in body, (
        "skill body must group the walk-through by the catalog 'stage' field")


# ==========================================================================
# E2E Behaviour: render_config(config) is the DERIVED HUMAN VIEW of a loaded
# config — a pure, deterministic formatter. Same config in => byte-identical
# text out, and the passed config dict is NOT mutated.
# ==========================================================================

def test_render_config_is_pure_and_deterministic():
    with tempfile.TemporaryDirectory() as project_dir:
        cfg = sg.load_config(project_dir)
        before = json.loads(json.dumps(cfg))
        out1 = configure.render_config(cfg, project_dir)
        out2 = configure.render_config(cfg, project_dir)
        assert isinstance(out1, str)
        assert out1 == out2, "same config must render byte-identical text"
        # Pure: the input config is not mutated.
        assert cfg == before, "render_config must not mutate its input config"


# ==========================================================================
# E2E Behaviour: render_config produces a grouped, labeled, loop-stage-ordered
# plain-text view with a schema-version header and the documented friendly value
# formatting (interval '<n> min', null budget 'unlimited' + window, on/off
# booleans, DNF readable, em dash for null/empty fields).
# ==========================================================================

def test_render_config_friendly_renderings():
    with tempfile.TemporaryDirectory() as project_dir:
        # Configure a representative config: a single-AND-group include_labels
        # DNF, default interval 10, default null budget, default work_own_filings
        # true, default null regression_command.
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-labels", "dci-team marketplace"]) == 0
        cfg = sg.load_config(project_dir)
        text = configure.render_config(cfg, project_dir)
        # Schema-version header.
        assert "schema 2.9.0" in text
        assert "auto-maintainer config" in text
        # Loop-stage groups appear in order.
        assert "PULL" in text
        assert text.index("PULL") < text.index("IMPLEMENT") < text.index(
            "VERIFY") < text.index("GATE") < text.index(
            "SCHEDULING") < text.index("SAFETY")
        # Labels from the catalog are used (no drift).
        assert "Heartbeat interval" in text
        # Friendly value formatting.
        assert "10 min" in text
        assert "unlimited" in text  # null budget ceiling
        assert "on" in text  # work_own_filings True
        assert "(dci-team marketplace)" in text  # include_labels DNF
        assert "—" in text  # em dash for a null field (regression_command)


# ==========================================================================
# E2E Behaviour: render_config renders a multi-group include_labels DNF as
# '(A AND B) OR (C)', and the empty DNF as '— (pull all)'.
# ==========================================================================

def test_render_config_dnf_multigroup_and_empty():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir,
             "--issue-labels", "bug,triaged;urgent"]) == 0
        text = configure.render_config(sg.load_config(project_dir), project_dir)
        assert "(bug AND triaged) OR (urgent)" in text

    with tempfile.TemporaryDirectory() as project_dir:
        text = configure.render_config(sg.load_config(project_dir), project_dir)
        assert "— (pull all)" in text


# ==========================================================================
# E2E Behaviour: `--show` (default, no --json) prints the HUMAN RENDER — NOT
# raw JSON — and writes nothing.
# ==========================================================================

def test_cli_show_prints_human_render_by_default():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(["--project-dir", project_dir, "--show"])
        assert rc == 0
        out = buf.getvalue()
        # The human render carries the header and is NOT valid JSON.
        assert "auto-maintainer config" in out
        assert "schema 2.9.0" in out
        raised = False
        try:
            json.loads(out)
        except ValueError:
            raised = True
        assert raised, "default --show must print the human render, not JSON"
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: `--show --json` is the machine-first escape hatch — it prints
# valid JSON equal to load_config(project_dir) and writes nothing.
# ==========================================================================

def test_cli_show_json_prints_raw_json():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(
                ["--project-dir", project_dir, "--show", "--json"])
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        assert parsed == sg.load_config(project_dir)
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: no mutating flag + --json also emits raw JSON (the escape
# hatch works without --show).
# ==========================================================================

def test_cli_no_flags_json_prints_raw_json():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(["--project-dir", project_dir, "--json"])
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        assert parsed == sg.load_config(project_dir)
        assert not os.path.exists(_cfg_path(project_dir))


# ==========================================================================
# E2E Behaviour: the post-write config echo is the human render by DEFAULT and
# raw JSON under --json.
# ==========================================================================

def test_cli_post_write_echo_human_by_default():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(
                ["--project-dir", project_dir, "--mode", "propose"])
        assert rc == 0
        out = buf.getvalue()
        assert "auto-maintainer config" in out
        raised = False
        try:
            json.loads(out)
        except ValueError:
            raised = True
        assert raised, "post-write echo must be the human render by default"


def test_cli_post_write_echo_json_under_flag():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(
                ["--project-dir", project_dir, "--mode", "propose", "--json"])
        assert rc == 0
        parsed = json.loads(buf.getvalue())
        assert parsed["mode"] == "propose"


# ==========================================================================
# E2E Behaviour: --describe and --preflight ALWAYS emit their JSON catalogs and
# are UNAFFECTED by --json (machine-first by design, never a human render).
# ==========================================================================

def test_describe_unaffected_by_json_flag():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = configure.main(
                ["--project-dir", project_dir, "--describe", "--json"])
        assert rc == 0
        catalog = json.loads(buf.getvalue())
        assert isinstance(catalog, list) and catalog


def test_preflight_unaffected_by_json_flag():
    import io
    import contextlib
    with tempfile.TemporaryDirectory() as project_dir:
        original = configure._DEFAULT_RUNNER
        configure._DEFAULT_RUNNER = _fake_runner(
            auth_rc=1, auth_out="", repo_rc=1, repo_out="")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = configure.main(
                    ["--project-dir", project_dir, "--preflight", "--json"])
            assert rc == 0
            result = json.loads(buf.getvalue())
            assert set(result.keys()) == {
                "gh_authenticated", "gh_account",
                "resolved_repo", "config_exists"}
        finally:
            configure._DEFAULT_RUNNER = original
