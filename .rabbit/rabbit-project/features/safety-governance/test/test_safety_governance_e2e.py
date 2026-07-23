#!/usr/bin/env python3
"""End-to-end + unit conformance tests for safety-governance (slice 1, schema 2.5.0).

Every behaviour in docs/spec.md has a test here. The feature provides
deterministic decision surfaces over a machine-first, versioned CENTRAL config
(config.json) — DESIGN §3.8:

  1. Central config + loader — GOVERNANCE_SCHEMA_VERSION (2.5.0),
     DEFAULT_GOVERNANCE, load_config(project_dir): reads project-local
     ${project_dir}/.auto-maintainer/config.json (absent => defaults),
     backfilling missing keys from defaults. null/absent ceiling => NO LIMIT.
     A legacy governance.json is migrated once. load_governance is a thin alias.
  2. Maintainer-self REPORT destination — a FIXED module constant MAINTAINER_REPO
     (not a config field).
  3. Trust-ladder gate — permits(effect_kind, mode) over the closed effect set
     {implement, open_pr, merge, file} and the closed mode set
     {dry-run, propose, auto-merge}. Unknown mode/effect => ValueError.
  4. Budget readiness gate (auto-resuming, NEVER a latch) — window_key(now),
     evaluate_budget(config, budget_state, now) reporting per-day allowance
     (per-tick ceiling REMOVED), record_spend(budget_state, now, tokens)
     advancing the window's spend.
  5. No-AskUserQuestion -> ABORTED helper — abort_on_would_block(runtime_dir,
     reason, escalate) latches ABORTED via lifecycle-dispositions and invokes
     the escalation seam.

Determinism: `now` is always injected (tz-aware). No model, no network, no
wall clock, no filesystem writes except the durable config (migration) and the
ABORTED marker delegated to lifecycle-dispositions.

Owner: changyu87
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Consume the already-implemented lifecycle-dispositions module via sys.path,
# exactly as the sibling adapters consume fsm-contracts. Do NOT edit/fork it.
# lifecycle-dispositions itself imports fsm_contracts, so its src must be on
# the path too (transitive sibling dependency).
_LD_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import lifecycle_dispositions as ld  # noqa: E402
import safety_governance as sg  # noqa: E402


# A couple of fixed tz-aware instants used across the budget tests. A non-UTC
# fixed offset pins window_key to the injected now's LOCAL date (it must NOT
# call the wall clock or convert to UTC).
_TZ = timezone(timedelta(hours=-5))  # a fixed local offset
_DAY1_MORNING = datetime(2026, 5, 1, 9, 0, 0, tzinfo=_TZ)
_DAY1_EVENING = datetime(2026, 5, 1, 23, 0, 0, tzinfo=_TZ)
_DAY2_MORNING = datetime(2026, 5, 2, 1, 0, 0, tzinfo=_TZ)


def _config_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "config.json")


def _gov_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "governance.json")


def _write_json(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


# ==========================================================================
# Behaviour: the central config schema is versioned (2.5.0) and machine-first.
# DEFAULT_GOVERNANCE matches the spec's documented defaults: mode=propose,
# budget.per_day_tokens=null, budget.window_tz=local, heartbeat.interval_minutes=3,
# backoff.threshold=5, work_own_filings=True, regression_command=None. The
# per_tick_tokens, maintainer_repo, and self_deploy fields are REMOVED.
# ==========================================================================

def test_schema_version_and_defaults():
    assert sg.GOVERNANCE_SCHEMA_VERSION == "2.8.0"
    d = sg.DEFAULT_GOVERNANCE
    assert d["schema_version"] == "2.8.0"
    assert d["mode"] == "propose"
    # doc_check_features_root (§3.7, verify-integrate GATE doc check) defaults
    # null (the doc check is OFF); schema 2.5.0 -> 2.6.0.
    assert d["doc_check_features_root"] is None
    # implement_test_command (the IMPLEMENT-side per-work-order test-gate,
    # implement's test_gate.py) defaults null (= run <feature>/test/run.py, the
    # historical behavior); schema 2.7.0 -> 2.8.0.
    assert d["implement_test_command"] is None
    # issue_filter (the PULL-stage open-issue filter) defaults the no-filter
    # object {labels: [], title_pattern: null}; schema 2.6.0 -> 2.7.0.
    assert d["issue_filter"] == {"labels": [], "title_pattern": None}
    # self_deploy (#309) is REMOVED from the schema (the self_deploy ACTION was
    # removed in #324, so the knob is dead; schema 2.3.0 -> 2.4.0).
    assert "self_deploy" not in d
    # Default per_day is NO LIMIT (null) per explicit user decision; a finite
    # ceiling is opt-in via config.json.
    assert d["budget"]["per_day_tokens"] is None
    assert d["budget"]["window_tz"] == "local"
    # heartbeat + backoff knobs, owned here, read by scheduling.
    assert d["heartbeat"]["interval_minutes"] == 3
    assert d["backoff"]["threshold"] == 5
    # per_tick_tokens + maintainer_repo are REMOVED from the schema.
    assert "per_tick_tokens" not in d["budget"]
    assert "maintainer_repo" not in d


# ==========================================================================
# Behaviour: maintainer-self REPORT destination is a FIXED module constant
# (§3.11.6), NOT a config field.
# ==========================================================================

def test_maintainer_repo_is_fixed_constant():
    assert sg.MAINTAINER_REPO == "changyu87/auto-maintainer-framework"


# ==========================================================================
# E2E Behaviour: absent config.json => documented defaults.
# ==========================================================================

def test_load_config_defaults_when_absent():
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["schema_version"] == "2.8.0"
        assert config["mode"] == "propose"
        assert config["issue_filter"] == {"labels": [], "title_pattern": None}
        assert "self_deploy" not in config
        assert config["budget"]["per_day_tokens"] is None
        assert config["budget"]["window_tz"] == "local"
        assert config["heartbeat"]["interval_minutes"] == 3
        assert config["backoff"]["threshold"] == 5
        # Absent file writes nothing.
        assert not os.path.exists(_config_path(project_dir))


# ==========================================================================
# E2E Behaviour: a project-local config.json override is read; missing keys are
# backfilled from defaults; a present null ceiling is preserved.
# ==========================================================================

def test_load_config_override_read_and_backfilled():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"mode": "auto-merge",
                     "budget": {"per_day_tokens": 500000}})
        config = sg.load_config(project_dir)
        assert config["mode"] == "auto-merge"
        assert config["budget"]["per_day_tokens"] == 500000
        # backfilled from defaults:
        assert config["budget"]["window_tz"] == "local"
        assert config["heartbeat"]["interval_minutes"] == 3
        assert config["backoff"]["threshold"] == 5


def test_load_config_preserves_explicit_null_ceiling():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"budget": {"per_day_tokens": None}})
        config = sg.load_config(project_dir)
        assert config["budget"]["per_day_tokens"] is None
        assert config["budget"]["window_tz"] == "local"
        assert config["mode"] == "propose"


# ==========================================================================
# E2E Behaviour: a config.json still carrying the removed per_tick_tokens key is
# TOLERATED — the key is ignored, never surfaced on the loaded config.
# ==========================================================================

def test_load_config_tolerates_and_drops_per_tick_tokens():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"mode": "propose",
                     "budget": {"per_day_tokens": 1000,
                                "per_tick_tokens": 50}})
        config = sg.load_config(project_dir)
        assert config["budget"]["per_day_tokens"] == 1000
        assert "per_tick_tokens" not in config["budget"]


# ==========================================================================
# E2E Behaviour: features_root (§3.7.6) defaults null (UNCONFIGURED) and an
# explicit top-level override is surfaced on the loaded config.
# ==========================================================================

def test_default_features_root_is_null():
    """features_root (the maintained project's features dir, §3.7.6) defaults
    null (UNCONFIGURED) — VERIFY's cross-feature complement then conservatively
    gates a cross-cutting-flagged tick."""
    assert sg.DEFAULT_GOVERNANCE["features_root"] is None
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["features_root"] is None


def test_load_config_reads_features_root_override():
    """An explicit top-level features_root in config.json is surfaced on the
    loaded config so scheduling can bind it into VERIFY's complement run."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"features_root": "/srv/project/features"})
        config = sg.load_config(project_dir)
        assert config["features_root"] == "/srv/project/features"
        # other defaults still present
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


# ==========================================================================
# E2E Behaviour: work_own_filings (§3.11.5) defaults True (the loop works its
# OWN filings by default; a human opts OUT). DEFAULT_GOVERNANCE carries it True;
# load_config backfills True when absent and preserves an explicit false; the
# pure accessor work_own_filings(config) returns True by default, False when set.
# ==========================================================================

def test_default_work_own_filings_is_true():
    """work_own_filings (§3.11.5) defaults True — the loop works its own
    discoveries by default (the previously-deferred opt-in flipped to default-on
    opt-out per owner decision)."""
    assert sg.DEFAULT_GOVERNANCE["work_own_filings"] is True
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["work_own_filings"] is True


def test_load_config_backfills_work_own_filings_true_when_absent():
    """A config.json that omits work_own_filings loads with the default True
    (backward compatible: an existing config without the key opts IN)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "propose"})
        config = sg.load_config(project_dir)
        assert config["work_own_filings"] is True
        # other defaults still present
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


def test_load_config_preserves_explicit_work_own_filings_false():
    """An explicit work_own_filings=false (the human opt-OUT) is surfaced
    unchanged on the loaded config."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"work_own_filings": False})
        config = sg.load_config(project_dir)
        assert config["work_own_filings"] is False
        # other defaults still present
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


def test_work_own_filings_accessor_default_true():
    """The pure accessor returns True by default when the key is absent
    (mirrors how scheduling._backoff_threshold reads backoff.threshold)."""
    assert sg.work_own_filings({}) is True
    assert sg.work_own_filings(sg.DEFAULT_GOVERNANCE) is True


def test_work_own_filings_accessor_false_when_set():
    """The pure accessor returns False when the config sets work_own_filings
    false (the opt-OUT)."""
    assert sg.work_own_filings({"work_own_filings": False}) is False


# ==========================================================================
# E2E Behaviour: regression_command (the GATE full-regression shell command,
# read by verify-integrate) defaults None (NO gate = GATE no-op PASS, non-breaking
# opt-in). DEFAULT_GOVERNANCE carries it None; load_config surfaces an explicit
# value and backfills None when absent (an existing config without the key loads);
# the pure accessor regression_command(config) returns the command or None.
# ==========================================================================

def test_default_regression_command_is_none():
    """regression_command defaults None — an unconfigured project has NO gate
    (GATE is a no-op PASS), so it merges exactly as before (non-breaking)."""
    assert sg.DEFAULT_GOVERNANCE["regression_command"] is None
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["regression_command"] is None


def test_load_config_reads_regression_command_override():
    """An explicit top-level regression_command in config.json is surfaced on the
    loaded config so verify-integrate's GATE can run it against a REVIEW-passed PR."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"regression_command": "pytest -q"})
        config = sg.load_config(project_dir)
        assert config["regression_command"] == "pytest -q"
        # other defaults still present
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


def test_load_config_backfills_regression_command_none_when_absent():
    """A config.json that omits regression_command loads with the default None
    (backward compatible: an existing config without the key has NO gate)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "propose"})
        config = sg.load_config(project_dir)
        assert config["regression_command"] is None
        # other defaults still present
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


def test_load_config_preserves_explicit_null_regression_command():
    """An explicit regression_command=null (NO gate) is surfaced unchanged."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"regression_command": None})
        config = sg.load_config(project_dir)
        assert config["regression_command"] is None
        assert config["mode"] == "propose"


def test_regression_command_accessor_default_none():
    """The pure accessor returns None by default when the key is absent
    (mirrors how work_own_filings reads its key)."""
    assert sg.regression_command({}) is None
    assert sg.regression_command(sg.DEFAULT_GOVERNANCE) is None


def test_regression_command_accessor_value_when_set():
    """The pure accessor returns the configured command when set (the value
    verify-integrate's GATE runs)."""
    assert sg.regression_command(
        {"regression_command": "npm test"}) == "npm test"


# ==========================================================================
# E2E Behaviour: doc_check_features_root (the repo-relative features root the GATE
# doc-surface load-bearing-token check uses, §3.7) defaults null (the check is
# OFF), is surfaced from config.json (including an explicit null), and is read
# through the pure accessor doc_check_features_root(config). Kept SEPARATE from
# features_root (VERIFY's complement locator) so the doc gate turns on
# independently (issue #381).
# ==========================================================================

def test_default_doc_check_features_root_is_null():
    """doc_check_features_root defaults null — the doc check is OFF until a
    repo-relative root is configured."""
    assert sg.DEFAULT_GOVERNANCE["doc_check_features_root"] is None
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["doc_check_features_root"] is None


def test_load_config_reads_doc_check_features_root_override():
    """An explicit top-level doc_check_features_root in config.json is surfaced on
    the loaded config (the repo-relative root the GATE doc check uses)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"doc_check_features_root": ".rabbit/rabbit-project/features"})
        config = sg.load_config(project_dir)
        assert (config["doc_check_features_root"]
                == ".rabbit/rabbit-project/features")


def test_load_config_backfills_doc_check_features_root_none_when_absent():
    """A config.json that omits doc_check_features_root loads with the default
    None (the doc check stays off, non-breaking)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "propose"})
        config = sg.load_config(project_dir)
        assert config["doc_check_features_root"] is None


def test_doc_check_features_root_accessor():
    """The pure accessor returns None by default and the configured value when
    set (kept separate from the features_root complement locator)."""
    assert sg.doc_check_features_root({}) is None
    assert sg.doc_check_features_root(sg.DEFAULT_GOVERNANCE) is None
    assert sg.doc_check_features_root(
        {"doc_check_features_root": "features"}) == "features"


# ==========================================================================
# E2E Behaviour: implement_test_command (the IMPLEMENT-side per-work-order
# test-gate, implement's test_gate.py) defaults null (= run
# <feature>/test/run.py, today's behavior), is surfaced from config.json
# (backfilled when absent, preserved when present incl. an explicit null and the
# 'none'/'skip' sentinel), and is read through the pure accessor
# implement_test_command(config) which returns the RAW value — safety-governance
# stores + provides it but does NOT interpret it (the three-way interpretation
# lives in implement's test_gate.py). schema 2.7.0 -> 2.8.0.
# ==========================================================================

def test_default_implement_test_command_is_null():
    """implement_test_command defaults null — the IMPLEMENT gate runs the touched
    feature's test/run.py (the historical behavior), unchanged for existing
    installs."""
    assert sg.DEFAULT_GOVERNANCE["implement_test_command"] is None
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["implement_test_command"] is None


def test_load_config_reads_implement_test_command_command_override():
    """An explicit top-level implement_test_command command string in config.json
    is surfaced on the loaded config so implement's test_gate.py runs it."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"implement_test_command": "pytest -q"})
        config = sg.load_config(project_dir)
        assert config["implement_test_command"] == "pytest -q"


def test_load_config_reads_implement_test_command_skip_sentinel():
    """The 'none'/'skip' sentinel is surfaced VERBATIM (NOT mapped to null) —
    null (run test/run.py) and 'none' (skip the gate) mean DIFFERENT things."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"implement_test_command": "none"})
        config = sg.load_config(project_dir)
        assert config["implement_test_command"] == "none"


def test_load_config_backfills_implement_test_command_null_when_absent():
    """A config.json that omits implement_test_command loads with the default
    null (run test/run.py), so an existing install is unaffected."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "propose"})
        config = sg.load_config(project_dir)
        assert config["implement_test_command"] is None


def test_load_config_preserves_explicit_null_implement_test_command():
    """An explicit implement_test_command=null (run test/run.py) is surfaced
    unchanged."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"implement_test_command": None})
        config = sg.load_config(project_dir)
        assert config["implement_test_command"] is None


def test_implement_test_command_accessor_default_none():
    """The pure accessor returns None when the key is absent, mirroring
    regression_command(config)."""
    assert sg.implement_test_command({}) is None
    assert sg.implement_test_command(sg.DEFAULT_GOVERNANCE) is None


def test_implement_test_command_accessor_returns_raw_value():
    """The accessor returns the RAW value — a command string or the 'none'/'skip'
    sentinel — WITHOUT interpreting it (interpretation is implement's job)."""
    assert sg.implement_test_command(
        {"implement_test_command": "npm test"}) == "npm test"
    assert sg.implement_test_command(
        {"implement_test_command": "none"}) == "none"
    assert sg.implement_test_command(
        {"implement_test_command": "skip"}) == "skip"


# ==========================================================================
# E2E Behaviour: issue_filter (the PULL-stage open-issue filter, §work-intake)
# defaults the no-filter object {labels: [], title_pattern: null}, is surfaced
# from config.json (backfilled when absent, preserved when present), and is
# read + normalized through the pure accessor issue_filter(config). The
# normalizer canonicalizes DNF labels (a flat list is sugar for one AND-group),
# validates a title_pattern regex, and raises ValueError (never a silent write)
# on a non-string label, an empty-string label, an empty inner group, or a
# non-compilable title_pattern. Default (empty labels + null pattern) preserves
# the pull-all behavior.
# ==========================================================================

def test_default_issue_filter_is_no_filter():
    """issue_filter defaults the no-filter canonical object — PULL pulls every
    open issue, exactly as before (non-breaking opt-in)."""
    assert sg.DEFAULT_GOVERNANCE["issue_filter"] == {
        "labels": [], "title_pattern": None}
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_config(project_dir)
        assert config["issue_filter"] == {"labels": [], "title_pattern": None}


def test_load_config_reads_issue_filter_override():
    """An explicit top-level issue_filter in config.json is surfaced on the
    loaded config (already-canonical DNF list-of-lists passthrough)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"issue_filter": {"labels": [["bug", "P1"], ["security"]],
                                      "title_pattern": "^\\[fix\\]"}})
        config = sg.load_config(project_dir)
        assert config["issue_filter"] == {
            "labels": [["bug", "P1"], ["security"]],
            "title_pattern": "^\\[fix\\]"}


def test_load_config_backfills_issue_filter_when_absent():
    """A config.json that omits issue_filter loads with the default no-filter
    object (non-breaking backfill)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "propose"})
        config = sg.load_config(project_dir)
        assert config["issue_filter"] == {"labels": [], "title_pattern": None}


def test_issue_filter_accessor_default_no_filter():
    """The pure accessor returns the no-filter canonical object by default
    (absent key on a bare config, and on DEFAULT_GOVERNANCE)."""
    assert sg.issue_filter({}) == {"labels": [], "title_pattern": None}
    assert sg.issue_filter(sg.DEFAULT_GOVERNANCE) == {
        "labels": [], "title_pattern": None}


def test_issue_filter_accessor_normalizes_null_and_empty_to_no_filter():
    """absent / null / [] all canonicalize to the no-filter default."""
    assert sg.issue_filter({"issue_filter": None}) == {
        "labels": [], "title_pattern": None}
    assert sg.issue_filter({"issue_filter": {"labels": None,
                                             "title_pattern": None}}) == {
        "labels": [], "title_pattern": None}
    assert sg.issue_filter({"issue_filter": {"labels": [],
                                             "title_pattern": None}}) == {
        "labels": [], "title_pattern": None}


def test_issue_filter_accessor_expands_flat_list_to_single_and_group():
    """A flat list of non-empty strings is sugar for a single AND-group."""
    assert sg.issue_filter(
        {"issue_filter": {"labels": ["bug", "P1"], "title_pattern": None}}) == {
        "labels": [["bug", "P1"]], "title_pattern": None}


def test_issue_filter_accessor_passes_through_dnf_list_of_lists():
    """An already-canonical List[List[str]] is validated + returned as-is."""
    assert sg.issue_filter(
        {"issue_filter": {"labels": [["bug", "P1"], ["security"]],
                          "title_pattern": None}}) == {
        "labels": [["bug", "P1"], ["security"]], "title_pattern": None}


def test_issue_filter_accessor_accepts_compilable_title_pattern():
    """A title_pattern that compiles as a regex is preserved unchanged."""
    assert sg.issue_filter(
        {"issue_filter": {"labels": [], "title_pattern": "^\\[bug\\].*"}}) == {
        "labels": [], "title_pattern": "^\\[bug\\].*"}


def test_issue_filter_accessor_rejects_non_compilable_title_pattern():
    """A title_pattern that is not a compilable regex raises ValueError (never a
    silent write)."""
    try:
        sg.issue_filter(
            {"issue_filter": {"labels": [], "title_pattern": "([unclosed"}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-compilable pattern")


def test_issue_filter_accessor_rejects_non_string_title_pattern():
    """A title_pattern that is neither null nor a string raises ValueError."""
    try:
        sg.issue_filter({"issue_filter": {"labels": [], "title_pattern": 123}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-string pattern")


def test_issue_filter_accessor_rejects_non_string_label():
    """A non-string label entry raises ValueError."""
    try:
        sg.issue_filter(
            {"issue_filter": {"labels": ["bug", 7], "title_pattern": None}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-string label")


def test_issue_filter_accessor_rejects_empty_string_label():
    """An empty-string label raises ValueError."""
    try:
        sg.issue_filter(
            {"issue_filter": {"labels": ["bug", ""], "title_pattern": None}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on empty-string label")


def test_issue_filter_accessor_rejects_empty_inner_group():
    """An empty inner AND-group raises ValueError."""
    try:
        sg.issue_filter(
            {"issue_filter": {"labels": [["bug"], []], "title_pattern": None}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on empty inner group")


def test_issue_filter_accessor_rejects_non_string_label_in_group():
    """A non-string label inside an AND-group raises ValueError."""
    try:
        sg.issue_filter(
            {"issue_filter": {"labels": [["bug", 7]], "title_pattern": None}})
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError on non-string label in group")


# ==========================================================================
# E2E Behaviour: self_deploy is REMOVED (#324 removed the self_deploy ACTION, so
# the knob is dead). DEFAULT_GOVERNANCE carries NO self_deploy; there is NO
# self_deploy accessor; load_config does not surface it; and a config.json still
# carrying a stale self_deploy key is TOLERATED — the key is dropped, never
# surfaced on the loaded config.
# ==========================================================================

def test_self_deploy_removed_from_defaults():
    """The dead self_deploy knob is gone from DEFAULT_GOVERNANCE (#324)."""
    assert "self_deploy" not in sg.DEFAULT_GOVERNANCE


def test_self_deploy_accessor_removed():
    """The self_deploy(config) accessor is removed (the knob is dead, #324)."""
    assert not hasattr(sg, "self_deploy")


def test_load_config_tolerates_and_drops_stale_self_deploy():
    """A config.json still carrying a stale self_deploy key is TOLERATED — the
    key is dropped, never surfaced on the loaded config (back-compat for an
    existing config written before #324)."""
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"mode": "propose", "self_deploy": True})
        config = sg.load_config(project_dir)
        assert "self_deploy" not in config
        # the surviving fields still load.
        assert config["mode"] == "propose"
        assert config["backoff"]["threshold"] == 5


# ==========================================================================
# E2E Behaviour: heartbeat.interval_minutes and backoff.threshold overrides are
# read and surfaced; absent sub-keys backfill from defaults.
# ==========================================================================

def test_load_config_reads_heartbeat_and_backoff_overrides():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"heartbeat": {"interval_minutes": 10},
                     "backoff": {"threshold": 8}})
        config = sg.load_config(project_dir)
        assert config["heartbeat"]["interval_minutes"] == 10
        assert config["backoff"]["threshold"] == 8
        # other defaults still present
        assert config["mode"] == "propose"


# ==========================================================================
# E2E Behaviour: MIGRATION — config.json absent but legacy governance.json
# present => migrate once. Surviving fields (mode, budget.per_day_tokens,
# budget.window_tz) are mapped; per_tick_tokens + maintainer_repo are DROPPED;
# heartbeat/backoff backfilled; config.json WRITTEN; the legacy file renamed to
# governance.json.migrated (non-destructive).
# ==========================================================================

def test_migration_governance_json_to_config_json():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_gov_path(project_dir),
                    {"schema_version": "1.1.0",
                     "mode": "gated-merge",
                     "budget": {"per_day_tokens": 200000,
                                "per_tick_tokens": 5000,
                                "window_tz": "local"},
                     "maintainer_repo": "octo/legacy"})

        config = sg.load_config(project_dir)
        # surviving fields mapped; the legacy mode name maps forward.
        assert config["mode"] == "auto-merge"
        assert config["budget"]["per_day_tokens"] == 200000
        assert config["budget"]["window_tz"] == "local"
        # dropped fields gone:
        assert "per_tick_tokens" not in config["budget"]
        assert "maintainer_repo" not in config
        # backfilled:
        assert config["heartbeat"]["interval_minutes"] == 3
        assert config["backoff"]["threshold"] == 5
        # config.json written; legacy renamed (non-destructive).
        assert os.path.exists(_config_path(project_dir))
        assert not os.path.exists(_gov_path(project_dir))
        assert os.path.exists(_gov_path(project_dir) + ".migrated")
        # the written config.json round-trips to the same values.
        with open(_config_path(project_dir)) as f:
            on_disk = json.load(f)
        assert on_disk["mode"] == "auto-merge"
        assert on_disk["budget"]["per_day_tokens"] == 200000
        assert "per_tick_tokens" not in on_disk.get("budget", {})


def test_migration_prefers_config_json_when_both_present():
    # When config.json already exists, governance.json is NOT consulted and NOT
    # renamed (no migration occurs).
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "dry-run"})
        _write_json(_gov_path(project_dir), {"mode": "gated-merge"})
        config = sg.load_config(project_dir)
        assert config["mode"] == "dry-run"
        # governance.json left untouched (still present, not renamed).
        assert os.path.exists(_gov_path(project_dir))
        assert not os.path.exists(_gov_path(project_dir) + ".migrated")


# ==========================================================================
# E2E Behaviour: DEFAULT RESOLUTION (read shipped default FRESH, #337). When no
# project-local config.json (and no legacy governance.json) exists, load_config
# reads the shipped default-config/config.json (sibling of lib/) FRESH when
# present — backfilled + validated like any config — else the embedded
# DEFAULT_GOVERNANCE constant. A project-local override still WINS. There is NO
# seed-once copy (the shipped file is never copied into the project).
#
# Tests point the module-level shipped-default dir at a temp fixture (mirroring
# scheduling's DEFAULT_CONFIG_DIR seam) so the source tree — which ships no
# default-config/ — does not gate the behaviour under test.
# ==========================================================================

def test_default_config_dir_points_at_plugin_sibling_of_src():
    """The shipped-default dir resolves to <plugin_root>/default-config, the
    sibling of lib/ (i.e. dirname(dirname(safety_governance.__file__))
    /default-config), mirroring scheduling's resolver."""
    expected = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(sg.__file__))),
        "default-config")
    assert sg.DEFAULT_CONFIG_DIR == expected


def test_load_config_reads_shipped_default_fresh_when_no_project_local():
    """No project-local config.json/legacy governance.json + a shipped
    default-config/config.json present => load_config returns the SHIPPED values
    (e.g. the aggressive mode auto-merge), backfilled from defaults."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"mode": "auto-merge",
                     "budget": {"per_day_tokens": 750000}})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        # Shipped values surfaced.
        assert config["mode"] == "auto-merge"
        assert config["budget"]["per_day_tokens"] == 750000
        # Backfilled from defaults (shipped omitted these).
        assert config["budget"]["window_tz"] == "local"
        assert config["heartbeat"]["interval_minutes"] == 3
        assert config["backoff"]["threshold"] == 5
        assert config["work_own_filings"] is True
        # No seed-once copy: the shipped file was NOT copied into the project.
        assert not os.path.exists(_config_path(project_dir))


def test_load_config_falls_back_to_default_governance_when_no_shipped_file():
    """No project-local config AND no shipped default-config/config.json =>
    the conservative embedded DEFAULT_GOVERNANCE (mode propose) is the fallback
    (the source-tree / no-plugin case)."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # shipped_dir exists but carries NO config.json -> absent shipped file.
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        assert config["mode"] == "propose"
        assert config["schema_version"] == "2.8.0"
        assert config["budget"]["per_day_tokens"] is None
        assert config["heartbeat"]["interval_minutes"] == 3
        assert config["backoff"]["threshold"] == 5


def test_load_config_project_local_overrides_shipped_default():
    """A project-local .auto-maintainer/config.json still WINS over the shipped
    default-config/config.json (override-else-default, #337)."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"mode": "auto-merge"})
        _write_json(_config_path(project_dir), {"mode": "dry-run"})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        # project-local wins; shipped auto-merge is NOT consulted.
        assert config["mode"] == "dry-run"


def test_load_config_unparsable_shipped_file_falls_back_to_defaults():
    """A present-but-unparsable shipped default-config/config.json falls back to
    DEFAULT_GOVERNANCE (never raises), mirroring scheduling's safety fallback."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        with open(os.path.join(shipped_dir, "config.json"), "w") as f:
            f.write("{ not valid json")
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        assert config["mode"] == "propose"
        assert config["schema_version"] == "2.8.0"


# ==========================================================================
# E2E Behaviour: load_governance is a thin alias delegating to load_config
# (coexistence window for consumers still calling the old name).
# ==========================================================================

def test_load_governance_alias_delegates_to_load_config():
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir),
                    {"mode": "auto-merge",
                     "budget": {"per_day_tokens": 500000}})
        via_alias = sg.load_governance(project_dir)
        assert via_alias["mode"] == "auto-merge"
        assert via_alias["budget"]["per_day_tokens"] == 500000
        assert via_alias["heartbeat"]["interval_minutes"] == 3


# ==========================================================================
# Behaviour: trust-ladder gate — FULL truth table (3 modes x 4 effects).
# ==========================================================================

def test_permits_full_truth_table():
    expected = {
        "dry-run": {"implement": False, "open_pr": False,
                    "merge": False, "file": False},
        "propose": {"implement": True, "open_pr": True,
                    "merge": False, "file": True},
        "auto-merge": {"implement": True, "open_pr": True,
                       "merge": True, "file": True},
    }
    for mode, effects in expected.items():
        for effect_kind, allowed in effects.items():
            assert sg.permits(effect_kind, mode) is allowed, (
                f"permits({effect_kind!r}, {mode!r}) should be {allowed}")


def test_permits_tolerates_legacy_gated_merge_alias():
    # The pre-2.1.0 mode name `gated-merge` is mapped forward to `auto-merge`,
    # so the trust gate decides identically for either name (coexistence).
    for effect_kind in ("implement", "open_pr", "merge", "file"):
        assert sg.permits(effect_kind, "gated-merge") is (
            sg.permits(effect_kind, "auto-merge"))


def test_load_config_maps_legacy_gated_merge_mode_forward():
    # A config.json still carrying the legacy mode name is TOLERATED and mapped
    # to the current name on load (not an error).
    with tempfile.TemporaryDirectory() as project_dir:
        _write_json(_config_path(project_dir), {"mode": "gated-merge"})
        config = sg.load_config(project_dir)
        assert config["mode"] == "auto-merge"


def test_permits_unknown_mode_raises():
    raised = False
    try:
        sg.permits("implement", "bogus-mode")
    except ValueError:
        raised = True
    assert raised, "unknown mode must raise ValueError (closed vocabulary)"


def test_permits_unknown_effect_raises():
    raised = False
    try:
        sg.permits("deploy", "propose")
    except ValueError:
        raised = True
    assert raised, "unknown effect_kind must raise ValueError (closed set)"


# ==========================================================================
# Behaviour: window_key derives from the injected now's LOCAL date.
# ==========================================================================

def test_window_key_is_injected_now_local_date():
    assert sg.window_key(_DAY1_MORNING) == "2026-05-01"
    assert sg.window_key(_DAY1_EVENING) == "2026-05-01"
    assert sg.window_key(_DAY2_MORNING) == "2026-05-02"


# ==========================================================================
# E2E Behaviour: with finite per_day, spend below ceiling is allowed; once
# spent >= ceiling within the SAME window, the gate blocks with the documented
# reason. No disposition is latched (decision only).
# ==========================================================================

def test_evaluate_budget_per_day_exhaustion_blocks():
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": 200000, "window_tz": "local"},
    }
    # Below ceiling within today's window -> allowed.
    state = {"window_key": "2026-05-01", "spent_tokens": 100000}
    out = sg.evaluate_budget(config, state, _DAY1_MORNING)
    assert out["allowed"] is True
    assert out["reason"] == "ok"
    assert out["budget_state"]["window_key"] == "2026-05-01"
    assert out["budget_state"]["spent_tokens"] == 100000

    # At/over ceiling within the same window -> blocked.
    state2 = {"window_key": "2026-05-01", "spent_tokens": 200000}
    out2 = sg.evaluate_budget(config, state2, _DAY1_MORNING)
    assert out2["allowed"] is False
    assert out2["reason"] == "per_day_exhausted"


# ==========================================================================
# E2E Behaviour: window rollover RESETS spent (auto-resume across a local-day
# boundary) using two different injected `now`s. This is the auto-resume; no
# human /start.
# ==========================================================================

def test_evaluate_budget_window_rollover_resets_and_resumes():
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": 200000, "window_tz": "local"},
    }
    # Exhausted on day 1.
    state = {"window_key": "2026-05-01", "spent_tokens": 200000}
    blocked = sg.evaluate_budget(config, state, _DAY1_EVENING)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "per_day_exhausted"

    # Same durable state, but `now` is day 2: window rolls over, spent resets,
    # work resumes automatically.
    resumed = sg.evaluate_budget(config, state, _DAY2_MORNING)
    assert resumed["allowed"] is True
    assert resumed["reason"] == "ok"
    assert resumed["budget_state"]["window_key"] == "2026-05-02"
    assert resumed["budget_state"]["spent_tokens"] == 0


# ==========================================================================
# Behaviour: null per_day ceiling never blocks (unbounded). The per-tick ceiling
# is REMOVED, so even a config still carrying per_tick_tokens never blocks on it.
# ==========================================================================

def test_evaluate_budget_null_ceiling_never_blocks():
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": None, "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 10 ** 9}
    out = sg.evaluate_budget(config, state, _DAY1_MORNING)
    assert out["allowed"] is True
    assert out["reason"] == "ok"


def test_evaluate_budget_ignores_legacy_per_tick():
    # A config still carrying per_tick_tokens never causes a per_tick block (the
    # per-tick ceiling is removed). per_day is what governs.
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": None, "per_tick_tokens": 1,
                   "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 0}
    out = sg.evaluate_budget(config, state, _DAY1_MORNING)
    assert out["allowed"] is True
    assert out["reason"] == "ok"


# ==========================================================================
# Behaviour: the DEFAULT config ships per_day=null (NO LIMIT), so evaluate_budget
# NEVER blocks regardless of how much spend is injected.
# ==========================================================================

def test_default_config_never_blocks_on_per_day():
    config = sg.DEFAULT_GOVERNANCE
    state = {"window_key": "2026-05-01", "spent_tokens": 10 ** 12}
    out = sg.evaluate_budget(config, state, _DAY1_MORNING)
    assert out["allowed"] is True
    assert out["reason"] == "ok"


# ==========================================================================
# Behaviour: evaluate_budget on a fresh/empty budget state seeds the window
# and reports allowed (no prior spend; the default per_day is null/NO LIMIT).
# ==========================================================================

def test_evaluate_budget_fresh_state_seeds_window():
    config = sg.DEFAULT_GOVERNANCE
    out = sg.evaluate_budget(config, {}, _DAY1_MORNING)
    assert out["allowed"] is True
    assert out["reason"] == "ok"
    assert out["budget_state"]["window_key"] == "2026-05-01"
    assert out["budget_state"]["spent_tokens"] == 0


# ==========================================================================
# E2E Behaviour: record_spend accumulates spend within a window; a later
# now in a new window rolls over first (resets) then records.
# ==========================================================================

def test_record_spend_accumulates_within_window():
    state = {"window_key": "2026-05-01", "spent_tokens": 0}
    state = sg.record_spend(state, _DAY1_MORNING, 1000)
    assert state["window_key"] == "2026-05-01"
    assert state["spent_tokens"] == 1000

    state = sg.record_spend(state, _DAY1_EVENING, 2500)
    assert state["window_key"] == "2026-05-01"
    assert state["spent_tokens"] == 3500


def test_record_spend_rolls_over_window_then_records():
    state = {"window_key": "2026-05-01", "spent_tokens": 3500}
    state = sg.record_spend(state, _DAY2_MORNING, 400)
    assert state["window_key"] == "2026-05-02"
    assert state["spent_tokens"] == 400


# ==========================================================================
# Behaviour: the evaluate/record contract holds together — accumulate via
# record_spend across ticks, then evaluate_budget blocks once the accumulated
# per-day spend reaches the ceiling, and auto-resumes the next day.
# ==========================================================================

def test_budget_accounting_contract_evaluate_plus_record():
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": 3000, "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 0}

    assert sg.evaluate_budget(config, state, _DAY1_MORNING)["allowed"] is True
    state = sg.record_spend(state, _DAY1_MORNING, 2000)

    assert sg.evaluate_budget(config, state, _DAY1_MORNING)["allowed"] is True
    state = sg.record_spend(state, _DAY1_MORNING, 1500)  # total 3500 >= 3000

    blocked = sg.evaluate_budget(config, state, _DAY1_EVENING)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "per_day_exhausted"

    resumed = sg.evaluate_budget(config, state, _DAY2_MORNING)
    assert resumed["allowed"] is True
    assert resumed["budget_state"]["spent_tokens"] == 0


# ==========================================================================
# E2E Behaviour: no-AskUserQuestion -> ABORTED. The helper latches ABORTED via
# lifecycle-dispositions (read back == ABORTED) and invokes the escalate seam
# when provided. ABORTED is a TRUE latch (faults do NOT auto-resume).
# ==========================================================================

def test_abort_on_would_block_latches_aborted_and_escalates():
    with tempfile.TemporaryDirectory() as runtime_dir:
        captured = {}

        def escalate(reason):
            captured["reason"] = reason

        marker = sg.abort_on_would_block(
            runtime_dir, "AskUserQuestion in autonomous mode",
            escalate=escalate)

        assert ld.read_disposition(runtime_dir) == ld.Disposition.ABORTED
        assert captured["reason"] == "AskUserQuestion in autonomous mode"
        assert marker == ld.Disposition.ABORTED


def test_abort_on_would_block_no_escalate_is_noop():
    with tempfile.TemporaryDirectory() as runtime_dir:
        marker = sg.abort_on_would_block(runtime_dir, "would block")
        assert ld.read_disposition(runtime_dir) == ld.Disposition.ABORTED
        assert marker == ld.Disposition.ABORTED


# ==========================================================================
# Invariant: evaluate_budget and record_spend do not mutate their input state.
# ==========================================================================

def test_evaluate_budget_does_not_mutate_input_state():
    config = {
        "schema_version": "2.0.0",
        "mode": "propose",
        "budget": {"per_day_tokens": 200000, "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 200000}
    sg.evaluate_budget(config, state, _DAY2_MORNING)
    assert state["window_key"] == "2026-05-01"
    assert state["spent_tokens"] == 200000


def test_record_spend_does_not_mutate_input_state():
    state = {"window_key": "2026-05-01", "spent_tokens": 100}
    sg.record_spend(state, _DAY1_MORNING, 50)
    assert state["spent_tokens"] == 100
