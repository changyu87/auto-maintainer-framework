#!/usr/bin/env python3
"""End-to-end tests for the governance config WRITER (src/configure.py) and the
shipped /auto-maintainer:configure skill.

safety_governance.py is the READER/decider over governance.json; configure.py is
its writer half (spec: "Config writer + the configure skill"). It performs a
deterministic load-modify-save of project-local
${project_dir}/.auto-maintainer/governance.json:

  - sets `mode`, validated through the closed mode set via permits; an unknown
    mode is a non-zero CLI exit / ValueError and NO file is written;
  - sets `per_day_tokens` / `per_tick_tokens` to a non-negative int, or to
    none/null/unlimited/"" meaning NO LIMIT (stored as JSON null);
  - preserves unmentioned keys (load-modify-save: an earlier mode survives a
    later budget-only write);
  - `--show` (or no mutating flag) prints the current config and writes nothing.

Also guards the shipped ship/skills/configure/SKILL.md presence + frontmatter
(version 0.1.0).

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
# (which in turn imports fsm_contracts). Put those sibling srcs on the path too,
# the same way the safety_governance e2e test does, so this module's imports
# resolve regardless of test-file load order.
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


def _gov_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "governance.json")


def _read_gov(project_dir):
    with open(_gov_path(project_dir), "r") as f:
        return json.load(f)


# ==========================================================================
# E2E Behaviour: --mode sets the trust mode and writes governance.json with it.
# ==========================================================================

def test_cli_sets_mode_and_writes_file():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir, "--mode", "dry-run"])
        assert rc == 0
        gov = _read_gov(project_dir)
        assert gov["mode"] == "dry-run"
        assert gov["schema_version"] == "1.0.0"


# ==========================================================================
# E2E Behaviour: an unknown mode is rejected with a non-zero CLI exit AND no
# file is written (never a partial/invalid write).
# ==========================================================================

def test_cli_unknown_mode_exits_nonzero_and_writes_nothing():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--mode", "bogus-mode"])
        assert rc != 0
        assert not os.path.exists(_gov_path(project_dir)), (
            "invalid mode must NOT write governance.json")


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
        assert _read_gov(project_dir)["budget"]["per_day_tokens"] == 200000

        rc = configure.main(
            ["--project-dir", project_dir, "--per-day-tokens", "none"])
        assert rc == 0
        assert _read_gov(project_dir)["budget"]["per_day_tokens"] is None


# ==========================================================================
# E2E Behaviour: a negative ceiling is rejected (non-zero exit, no write).
# ==========================================================================

def test_cli_negative_ceiling_rejected():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(
            ["--project-dir", project_dir, "--per-day-tokens", "-5"])
        assert rc != 0
        assert not os.path.exists(_gov_path(project_dir))


# ==========================================================================
# E2E Behaviour: load-modify-save preserves unmentioned keys. Set mode first,
# then a budget-only write later; the earlier mode must survive.
# ==========================================================================

def test_load_modify_save_preserves_unmentioned_keys():
    with tempfile.TemporaryDirectory() as project_dir:
        assert configure.main(
            ["--project-dir", project_dir, "--mode", "gated-merge"]) == 0
        # A later, budget-only write must not clobber the earlier mode.
        assert configure.main(
            ["--project-dir", project_dir,
             "--per-tick-tokens", "5000"]) == 0
        gov = _read_gov(project_dir)
        assert gov["mode"] == "gated-merge"
        assert gov["budget"]["per_tick_tokens"] == 5000
        # window_tz default is preserved across the modify-save too.
        assert gov["budget"]["window_tz"] == "local"


# ==========================================================================
# E2E Behaviour: --show prints the current config and writes nothing.
# ==========================================================================

def test_cli_show_writes_nothing(capsys=None):
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir, "--show"])
        assert rc == 0
        assert not os.path.exists(_gov_path(project_dir)), (
            "--show must not write governance.json")


# ==========================================================================
# E2E Behaviour: no mutating flag at all behaves like --show (prints, no write).
# ==========================================================================

def test_cli_no_flags_writes_nothing():
    with tempfile.TemporaryDirectory() as project_dir:
        rc = configure.main(["--project-dir", project_dir])
        assert rc == 0
        assert not os.path.exists(_gov_path(project_dir))


# ==========================================================================
# Behaviour: the shipped /auto-maintainer:configure skill exists and carries
# frontmatter at version 0.1.0 with the lifecycle/identity keys.
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


def test_shipped_configure_skill_frontmatter_version_and_keys():
    fm = _skill_frontmatter()
    for key in ("name", "description", "version", "owner",
                "deprecation_criterion"):
        assert key in fm, f"frontmatter missing required key: {key}"
    assert str(fm["version"]) == "0.1.0"
