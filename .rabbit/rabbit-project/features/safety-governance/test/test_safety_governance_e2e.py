#!/usr/bin/env python3
"""End-to-end + unit conformance tests for safety-governance (slice 1).

Every behaviour in docs/spec.md has a test here. The feature provides four
deterministic decision surfaces over a machine-first, versioned governance
config (DESIGN §3.8):

  1. Governance config + loader — GOVERNANCE_SCHEMA_VERSION, DEFAULT_GOVERNANCE,
     load_governance(project_dir): reads project-local
     ${project_dir}/.auto-maintainer/governance.json (absent => defaults),
     backfilling missing keys from defaults. null/absent ceiling => NO LIMIT.
  2. Trust-ladder gate — permits(effect_kind, mode) over the closed effect set
     {implement, open_pr, merge, file} and the closed mode set
     {dry-run, propose, gated-merge}. Unknown mode/effect => ValueError.
  3. Budget readiness gate (auto-resuming, NEVER a latch) — window_key(now),
     evaluate_budget(config, budget_state, now, tick_spend) reporting allowance,
     record_spend(budget_state, now, tokens) advancing the window's spend.
  4. No-AskUserQuestion -> ABORTED helper — abort_on_would_block(runtime_dir,
     reason, escalate) latches ABORTED via lifecycle-dispositions and invokes
     the escalation seam.

Determinism: `now` is always injected (tz-aware). No model, no network, no
wall clock, no filesystem writes except the ABORTED marker delegated to
lifecycle-dispositions.

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


# ==========================================================================
# Behaviour: the governance config schema is versioned and machine-first.
# DEFAULT_GOVERNANCE matches the spec's documented defaults.
# ==========================================================================

def test_schema_version_and_defaults():
    assert sg.GOVERNANCE_SCHEMA_VERSION == "1.1.0"
    d = sg.DEFAULT_GOVERNANCE
    assert d["mode"] == "propose"
    assert d["budget"]["per_tick_tokens"] is None
    # Default per_day is NO LIMIT (null) per explicit user decision; a finite
    # ceiling is opt-in via governance.json.
    assert d["budget"]["per_day_tokens"] is None
    assert d["budget"]["window_tz"] == "local"
    # maintainer_repo defaults to null (no maintainer tracker configured);
    # added in schema 1.1.0 (additive).
    assert d["maintainer_repo"] is None


# ==========================================================================
# E2E Behaviour: absent governance.json => documented defaults.
# ==========================================================================

def test_load_governance_defaults_when_absent():
    with tempfile.TemporaryDirectory() as project_dir:
        config = sg.load_governance(project_dir)
        assert config["mode"] == "propose"
        # Default per_day is null (NO LIMIT); a finite ceiling is opt-in.
        assert config["budget"]["per_day_tokens"] is None
        assert config["budget"]["per_tick_tokens"] is None
        assert config["budget"]["window_tz"] == "local"


# ==========================================================================
# E2E Behaviour: a project-local override is read; missing keys are backfilled
# from defaults; a present null ceiling is preserved (not overwritten).
# ==========================================================================

def test_load_governance_override_read_and_backfilled():
    with tempfile.TemporaryDirectory() as project_dir:
        amdir = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(amdir)
        # Override mode + per_day_tokens; omit per_tick_tokens + window_tz so
        # they backfill from defaults. Also set per_tick null explicitly below
        # in a separate test.
        with open(os.path.join(amdir, "governance.json"), "w") as f:
            json.dump({"mode": "gated-merge",
                       "budget": {"per_day_tokens": 500000}}, f)

        config = sg.load_governance(project_dir)
        assert config["mode"] == "gated-merge"
        assert config["budget"]["per_day_tokens"] == 500000
        # backfilled from defaults:
        assert config["budget"]["per_tick_tokens"] is None
        assert config["budget"]["window_tz"] == "local"


def test_load_governance_preserves_explicit_null_ceiling():
    with tempfile.TemporaryDirectory() as project_dir:
        amdir = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(amdir)
        with open(os.path.join(amdir, "governance.json"), "w") as f:
            json.dump({"budget": {"per_day_tokens": None}}, f)

        config = sg.load_governance(project_dir)
        # Explicit null per_day must survive backfill as null (NO LIMIT).
        assert config["budget"]["per_day_tokens"] is None
        # Untouched keys still backfill.
        assert config["budget"]["window_tz"] == "local"
        assert config["mode"] == "propose"


# ==========================================================================
# E2E Behaviour: maintainer_repo defaults to null when absent from the file,
# and an explicit value is PRESERVED through load_governance (a known top-level
# key, backfilled like the others). run_tick._repo_for_target routes
# maintainer-self -> this repo (§3.11.6).
# ==========================================================================

def test_load_governance_maintainer_repo_defaults_null_when_absent():
    with tempfile.TemporaryDirectory() as project_dir:
        amdir = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(amdir)
        with open(os.path.join(amdir, "governance.json"), "w") as f:
            json.dump({"mode": "propose"}, f)

        config = sg.load_governance(project_dir)
        assert config["maintainer_repo"] is None


def test_load_governance_preserves_explicit_maintainer_repo():
    with tempfile.TemporaryDirectory() as project_dir:
        amdir = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(amdir)
        with open(os.path.join(amdir, "governance.json"), "w") as f:
            json.dump({"maintainer_repo": "octo/tooling"}, f)

        config = sg.load_governance(project_dir)
        # Explicit value preserved (round-trip), other keys still backfilled.
        assert config["maintainer_repo"] == "octo/tooling"
        assert config["mode"] == "propose"
        assert config["budget"]["window_tz"] == "local"


# ==========================================================================
# Behaviour: trust-ladder gate — FULL truth table (3 modes x 4 effects).
# ==========================================================================

def test_permits_full_truth_table():
    expected = {
        "dry-run": {"implement": False, "open_pr": False,
                    "merge": False, "file": False},
        "propose": {"implement": True, "open_pr": True,
                    "merge": False, "file": True},
        "gated-merge": {"implement": True, "open_pr": True,
                        "merge": True, "file": True},
    }
    for mode, effects in expected.items():
        for effect_kind, allowed in effects.items():
            assert sg.permits(effect_kind, mode) is allowed, (
                f"permits({effect_kind!r}, {mode!r}) should be {allowed}")


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
    # An EXPLICIT finite per_day ceiling (opt-in via governance.json); the
    # default is null (NO LIMIT), so exhaustion is exercised with a finite
    # config built locally, not by relying on the default.
    config = {
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": None, "per_day_tokens": 200000,
                   "window_tz": "local"},
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
    # Explicit finite per_day ceiling (the default is null/NO LIMIT, which
    # would never block, so rollover-after-exhaustion needs a finite config).
    config = {
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": None, "per_day_tokens": 200000,
                   "window_tz": "local"},
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
# E2E Behaviour: finite per_tick — a tick whose spend exceeds the per-tick
# ceiling is blocked with the documented reason; within-ceiling is allowed.
# ==========================================================================

def test_evaluate_budget_per_tick_exceeded_blocks():
    config = {
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": 5000, "per_day_tokens": None,
                   "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 0}
    over = sg.evaluate_budget(config, state, _DAY1_MORNING, tick_spend=6000)
    assert over["allowed"] is False
    assert over["reason"] == "per_tick_exceeded"

    within = sg.evaluate_budget(config, state, _DAY1_MORNING, tick_spend=5000)
    assert within["allowed"] is True
    assert within["reason"] == "ok"


# ==========================================================================
# Behaviour: null ceilings never block (unbounded) for either dimension.
# ==========================================================================

def test_evaluate_budget_null_ceilings_never_block():
    config = {
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": None, "per_day_tokens": None,
                   "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 10 ** 9}
    out = sg.evaluate_budget(config, state, _DAY1_MORNING, tick_spend=10 ** 9)
    assert out["allowed"] is True
    assert out["reason"] == "ok"


# ==========================================================================
# Behaviour: the DEFAULT config ships per_day=null (NO LIMIT, per explicit user
# decision), so evaluate_budget NEVER blocks on per_day regardless of how much
# spend is injected. A finite ceiling is opt-in, not the default.
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
# now in a new window rolls over first (resets) then records. This pins the
# documented evaluate/record split: evaluate REPORTS allowance (pure decision,
# rolls the window for the returned state), record ADVANCES spent_tokens.
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
    # A new local day: the prior window's spend is dropped, then this tick's
    # tokens are recorded against the new window.
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
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": None, "per_day_tokens": 3000,
                   "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 0}

    # Tick 1: allowed, then record.
    assert sg.evaluate_budget(config, state, _DAY1_MORNING)["allowed"] is True
    state = sg.record_spend(state, _DAY1_MORNING, 2000)

    # Tick 2: still under ceiling, allowed, record pushes over.
    assert sg.evaluate_budget(config, state, _DAY1_MORNING)["allowed"] is True
    state = sg.record_spend(state, _DAY1_MORNING, 1500)  # total 3500 >= 3000

    # Tick 3: now exhausted -> blocked, no latch.
    blocked = sg.evaluate_budget(config, state, _DAY1_EVENING)
    assert blocked["allowed"] is False
    assert blocked["reason"] == "per_day_exhausted"

    # Next local day -> auto-resume.
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

        # Disposition latched ABORTED, read back through lifecycle-dispositions.
        assert ld.read_disposition(runtime_dir) == ld.Disposition.ABORTED
        # Escalation seam invoked with the reason.
        assert captured["reason"] == "AskUserQuestion in autonomous mode"
        # The returned marker signals the halt (documented + tested).
        assert marker == ld.Disposition.ABORTED


# ==========================================================================
# E2E Behaviour: the escalate seam is optional — default None is a no-op and
# the helper still latches ABORTED.
# ==========================================================================

def test_abort_on_would_block_no_escalate_is_noop():
    with tempfile.TemporaryDirectory() as runtime_dir:
        marker = sg.abort_on_would_block(runtime_dir, "would block")
        assert ld.read_disposition(runtime_dir) == ld.Disposition.ABORTED
        assert marker == ld.Disposition.ABORTED


# ==========================================================================
# Invariant: this lib performs NO filesystem write of its own except the
# ABORTED marker delegated to lifecycle-dispositions. evaluate_budget and
# record_spend never touch a runtime_dir, and they do not mutate their input
# state dict in place (callers persist the returned state).
# ==========================================================================

def test_evaluate_budget_does_not_mutate_input_state():
    config = {
        "schema_version": "1.0.0",
        "mode": "propose",
        "budget": {"per_tick_tokens": None, "per_day_tokens": 200000,
                   "window_tz": "local"},
    }
    state = {"window_key": "2026-05-01", "spent_tokens": 200000}
    sg.evaluate_budget(config, state, _DAY2_MORNING)
    # The original durable state object is unchanged; the rolled-over state is
    # only in the returned dict.
    assert state["window_key"] == "2026-05-01"
    assert state["spent_tokens"] == 200000


def test_record_spend_does_not_mutate_input_state():
    state = {"window_key": "2026-05-01", "spent_tokens": 100}
    sg.record_spend(state, _DAY1_MORNING, 50)
    assert state["spent_tokens"] == 100
