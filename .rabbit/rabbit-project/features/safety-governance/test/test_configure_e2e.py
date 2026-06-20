#!/usr/bin/env python3
"""End-to-end tests for the central-config WRITER (src/configure.py) and the
shipped /auto-maintainer:configure skill (schema 2.0.0).

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

_SKILL_PATH = os.path.join(
    _FEATURE_DIR, "ship", "skills", "configure", "SKILL.md")


def _cfg_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "config.json")


def _read_cfg(project_dir):
    with open(_cfg_path(project_dir), "r") as f:
        return json.load(f)


# ==========================================================================
# E2E Behaviour: --mode sets the trust mode and writes config.json with it,
# at schema 2.0.0.
# ==========================================================================

def test_cli_sets_mode_and_writes_file():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir, "--mode", "dry-run"])
        assert rc == 0
        cfg = _read_cfg(project_dir)
        assert cfg["mode"] == "dry-run"
        assert cfg["schema_version"] == "2.0.0"


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
# E2E Behaviour: load-modify-save preserves unmentioned keys. Set mode first,
# then a budget-only write later; the earlier mode must survive.
# ==========================================================================

def test_load_modify_save_preserves_unmentioned_keys():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir, "--mode", "gated-merge"]) == 0
        assert configure.main(
            ["--project-dir", project_dir,
             "--per-day-tokens", "200000"]) == 0
        cfg = _read_cfg(project_dir)
        assert cfg["mode"] == "gated-merge"
        assert cfg["budget"]["per_day_tokens"] == 200000
        assert cfg["budget"]["window_tz"] == "local"
        # heartbeat/backoff defaults preserved across the modify-save.
        assert cfg["heartbeat"]["interval_minutes"] == 3
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
        for entry in catalog:
            for field in ("key", "label", "controls", "default",
                          "current", "type", "validator"):
                assert field in entry, f"catalog entry missing {field}: {entry}"
        # read-only: no config.json written.
        assert not os.path.exists(_cfg_path(project_dir))


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
