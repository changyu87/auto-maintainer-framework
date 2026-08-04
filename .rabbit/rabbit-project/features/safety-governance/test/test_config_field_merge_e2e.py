#!/usr/bin/env python3
"""Field-level (3-way) config merge for safety-governance (#357, deferred #336).

v0.8.0 whole-file override FREEZES an overridden config.json: once a user
overrides the file it never receives keys added to a later shipped default.
#357 resolves that per-FIELD via merge_config(base, theirs, mine):

  - a NEW default key the override does not set is ADOPTED (the unfreeze);
  - a user-set value is PRESERVED;
  - a key the user changed that the new default ALSO changed is surfaced as a
    CONFLICT, keeping the user value (never silently overwritten);
  - whole-file behaviour remains when field-merge is not applicable (no shipped
    default => `mine` == base => a no-op merge).

These tests exercise merge_config directly (the pure mechanism) and through
load_config (the reader that wires it), pointing the shipped-default seam
(DEFAULT_CONFIG_DIR) at a temp fixture so the source tree — which ships no
default-config/ — does not gate the behaviour under test.

Owner: changyu87
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# safety_governance imports lifecycle_dispositions (which imports fsm_contracts);
# put the sibling srcs on the path like the other test modules do.
_LD_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "lifecycle-dispositions", "src")
if _LD_SRC not in sys.path:
    sys.path.insert(0, _LD_SRC)
_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import safety_governance as sg  # noqa: E402


def _config_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "config.json")


def _write_json(path, payload):
    import json
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(payload, f)


# ==========================================================================
# Unit: merge_config — the pure 3-way mechanism.
# ==========================================================================

def test_merge_adopts_new_default_key_theirs_lacks():
    """A key ONLY the new shipped default has (theirs never set it) is ADOPTED
    — the unfreeze (acceptance #1)."""
    base = {"mode": "propose"}
    theirs = {"mode": "auto-merge"}
    mine = {"mode": "propose", "new_knob": "shipped_value"}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["new_knob"] == "shipped_value"
    # The user value on an existing key is preserved.
    assert merged["mode"] == "auto-merge"
    assert conflicts == []


def test_merge_preserves_user_set_value():
    """A user-set value the shipped default did NOT change is KEPT (acceptance
    #1: user-set values are preserved)."""
    base = {"mode": "propose"}
    theirs = {"mode": "auto-merge"}
    mine = {"mode": "propose"}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["mode"] == "auto-merge"
    assert conflicts == []


def test_merge_adopts_default_change_when_user_did_not_touch_key():
    """theirs == base (user never changed the key) => ADOPT the new default
    value."""
    base = {"threshold": 5}
    theirs = {"threshold": 5}
    mine = {"threshold": 7}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["threshold"] == 7
    assert conflicts == []


def test_merge_flags_conflict_and_keeps_user_value():
    """Both sides changed the SAME key to DIFFERENT values => CONFLICT: keep the
    user value (never silently overwrite) and RECORD it (acceptance #2)."""
    base = {"threshold": 5}
    theirs = {"threshold": 9}     # user changed it
    mine = {"threshold": 7}       # new default also changed it, differently
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["threshold"] == 9  # user value kept, not overwritten
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["path"] == "threshold"
    assert c["base"] == 5 and c["theirs"] == 9 and c["mine"] == 7


def test_merge_no_conflict_when_both_changed_to_same_value():
    """Both sides changed the key to the SAME value => agree, no conflict."""
    base = {"threshold": 5}
    theirs = {"threshold": 7}
    mine = {"threshold": 7}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["threshold"] == 7
    assert conflicts == []


def test_merge_keeps_user_only_key():
    """A key only the user has (not in the default) is KEPT."""
    base = {"mode": "propose"}
    theirs = {"mode": "propose", "user_extra": 1}
    mine = {"mode": "propose"}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["user_extra"] == 1
    assert conflicts == []


def test_merge_recurses_into_nested_dicts():
    """Nested dicts (e.g. budget) merge per-field: a new nested default key is
    adopted, a user nested value is kept."""
    base = {"budget": {"per_day_tokens": None, "window_tz": "local"}}
    theirs = {"budget": {"per_day_tokens": 500000, "window_tz": "local"}}
    mine = {"budget": {"per_day_tokens": None, "window_tz": "local",
                       "grace_tokens": 100}}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["budget"]["per_day_tokens"] == 500000   # user value kept
    assert merged["budget"]["grace_tokens"] == 100        # new key adopted
    assert conflicts == []


def test_merge_nested_conflict_path_is_dotted():
    """A conflict inside a nested dict is reported with a dotted path."""
    base = {"budget": {"per_day_tokens": 100}}
    theirs = {"budget": {"per_day_tokens": 200}}
    mine = {"budget": {"per_day_tokens": 300}}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["budget"]["per_day_tokens"] == 200
    assert len(conflicts) == 1
    assert conflicts[0]["path"] == "budget.per_day_tokens"


def test_merge_is_noop_when_mine_equals_base():
    """When the shipped default == base (no release change) the merge returns
    theirs verbatim over the shared keys — the whole-file behaviour (acceptance
    #3)."""
    base = {"mode": "propose", "budget": {"per_day_tokens": None}}
    theirs = {"mode": "auto-merge", "budget": {"per_day_tokens": 500000}}
    mine = {"mode": "propose", "budget": {"per_day_tokens": None}}
    merged, conflicts = sg.merge_config(base, theirs, mine)
    assert merged["mode"] == "auto-merge"
    assert merged["budget"]["per_day_tokens"] == 500000
    assert conflicts == []


def test_merge_does_not_mutate_inputs():
    """Pure: none of base/theirs/mine is mutated."""
    base = {"budget": {"per_day_tokens": None}}
    theirs = {"budget": {"per_day_tokens": 500000}}
    mine = {"budget": {"per_day_tokens": None, "grace_tokens": 100}}
    sg.merge_config(base, theirs, mine)
    assert base == {"budget": {"per_day_tokens": None}}
    assert theirs == {"budget": {"per_day_tokens": 500000}}
    assert mine == {"budget": {"per_day_tokens": None, "grace_tokens": 100}}


# ==========================================================================
# E2E: load_config wires merge_config against the shipped default.
# ==========================================================================

def test_load_config_override_adopts_new_shipped_default_key():
    """An overridden config.json that predates a shipped default which adds a
    new key ADOPTS that key (the #357 unfreeze), while the user's mode is
    preserved."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # User override: only sets mode (predates newer keys).
        _write_json(_config_path(project_dir), {"mode": "propose"})
        # Shipped default adds an explicit regression_command the user lacks.
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"mode": "auto-merge",
                     "regression_command": "make test"})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        # User-set value preserved (mode not overwritten by the shipped default,
        # because the user explicitly moved it away from the base 'propose'? No —
        # base is 'propose', theirs is 'propose', so theirs == base => adopt
        # mine 'auto-merge'). This asserts the ADOPT-when-untouched rule.
        assert config["mode"] == "auto-merge"
        # The new shipped key the override lacked is adopted.
        assert config["regression_command"] == "make test"


def test_load_config_override_preserves_user_value_over_shipped_default():
    """A user value that differs from BOTH the base and an unchanged shipped
    default is preserved (theirs != base, mine == base => keep theirs)."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # User set a finite ceiling (base default is null).
        _write_json(_config_path(project_dir),
                    {"budget": {"per_day_tokens": 500000}})
        # Shipped default leaves per_day_tokens at the base null.
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"mode": "auto-merge"})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        # User ceiling preserved.
        assert config["budget"]["per_day_tokens"] == 500000
        # New shipped mode adopted (user never set mode; base propose == theirs).
        assert config["mode"] == "auto-merge"


def test_load_config_surfaces_conflict_on_stderr_and_keeps_user_value():
    """A key the user changed that the shipped default ALSO changed to a
    different value keeps the USER value and WARNS on stderr (acceptance #2)."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # base heartbeat.interval_minutes is 10. User set 7; shipped set 5 —
        # all three differ, so a genuine 3-way conflict is recorded.
        _write_json(_config_path(project_dir),
                    {"heartbeat": {"interval_minutes": 7}})
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"heartbeat": {"interval_minutes": 5}})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        # User value kept, not silently overwritten by the shipped 5.
        assert config["heartbeat"]["interval_minutes"] == 7
        # Conflict surfaced.
        err = buf.getvalue()
        assert "conflict" in err.lower()
        assert "heartbeat.interval_minutes" in err


def test_load_config_no_shipped_default_is_whole_file_behaviour():
    """No shipped default-config/config.json => `mine` is the embedded default
    == base => the merge is a no-op and the override is read exactly as the
    pre-#357 whole-file overlay (acceptance #3)."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # shipped_dir has NO config.json -> _shipped_default() is None.
        _write_json(_config_path(project_dir),
                    {"mode": "auto-merge",
                     "budget": {"per_day_tokens": 500000}})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        try:
            config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        assert config["mode"] == "auto-merge"
        assert config["budget"]["per_day_tokens"] == 500000
        # Backfilled from defaults exactly as before.
        assert config["budget"]["window_tz"] == "local"
        assert config["heartbeat"]["interval_minutes"] == 10


def test_load_config_schema_version_never_conflicts():
    """schema_version is loader-owned metadata EXCLUDED from the 3-way merge: a
    stale user schema_version + a frozen shipped schema_version, both differing
    from the current base, records NO conflict and emits NO 'schema_version'
    stderr warning; the loaded schema_version is ALWAYS the current constant."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # User override carries a stale schema_version (2.7.0); shipped default
        # carries the frozen 2.2.0; the embedded base is the current 2.9.0 —
        # all three differ, which pre-fix produced a spurious conflict.
        _write_json(_config_path(project_dir),
                    {"schema_version": "2.7.0", "mode": "propose"})
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"schema_version": "2.2.0", "mode": "auto-merge"})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        err = buf.getvalue()
        # No spurious schema_version conflict warning.
        assert "schema_version" not in err
        # The loaded schema_version is always the current constant (unchanged
        # behaviour — _overlay normalizes it).
        assert config["schema_version"] == sg.GOVERNANCE_SCHEMA_VERSION


def test_load_config_real_knob_conflict_still_warns_with_schema_version_stale():
    """A real knob conflict (mode changed 3 ways) STILL warns even when the
    override also carries a stale schema_version — the noise fix removes only
    the schema_version conflict, never a genuine knob conflict."""
    original = sg.DEFAULT_CONFIG_DIR
    with tempfile.TemporaryDirectory() as project_dir, \
            tempfile.TemporaryDirectory() as shipped_dir:
        # base mode is 'propose'. User set 'dry-run'; shipped set 'auto-merge'
        # => a genuine 3-way conflict on mode. schema_version also differs.
        _write_json(_config_path(project_dir),
                    {"schema_version": "2.7.0", "mode": "dry-run"})
        _write_json(os.path.join(shipped_dir, "config.json"),
                    {"schema_version": "2.2.0", "mode": "auto-merge"})
        sg.DEFAULT_CONFIG_DIR = shipped_dir
        buf = io.StringIO()
        try:
            with redirect_stderr(buf):
                config = sg.load_config(project_dir)
        finally:
            sg.DEFAULT_CONFIG_DIR = original
        err = buf.getvalue()
        # The genuine mode conflict is surfaced, keeping the user value.
        assert "conflict" in err.lower()
        assert "mode" in err
        assert config["mode"] == "dry-run"
        # But schema_version is NOT among the surfaced conflicts.
        assert "schema_version" not in err
