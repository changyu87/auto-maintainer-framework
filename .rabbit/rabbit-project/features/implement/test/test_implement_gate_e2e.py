#!/usr/bin/env python3
"""End-to-end tests for the IMPLEMENT deterministic correctness gate (DESIGN
§3.6.3, FT-A of the loop redesign).

The implementer (a model agent) is no longer TRUSTED to merely *assert* "I ran
the tests, they passed" in its Handoff (the #255 rubber-stamp lesson). Instead
IMPLEMENT becomes a DETERMINISTIC correctness gate:

  - `test_gate.py` runs the TARGET feature's `test/run.py` via subprocess and
    records a machine-checkable verdict {feature, passed, returncode, summary}
    to a known artifact path. The verdict is the SCRIPT's recorded result of an
    actual subprocess run — never a model's prose.
  - `implement.py` grows a deterministic validity predicate over an `opened`
    handoff: an opened handoff carrying a PASSING script-produced test-verdict is
    VALID; one with a missing or failing verdict is INVALID.

These tests are e2e in that they drive the gate script against REAL temporary
target features (a passing one and a failing one), read the recorded verdict
artifact from disk, and feed it back through the handoff validity predicate.

Owner: changyu87
"""

import json
import os
import subprocess
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_GATE = os.path.join(_SRC, "test_gate.py")

import implement as impl  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — build a throwaway target feature whose test/run.py passes or fails.
# --------------------------------------------------------------------------

_PASSING_RUN = """\
import sys
print("1 passed, 0 failed")
sys.exit(0)
"""

_FAILING_RUN = """\
import sys
print("0 passed, 1 failed")
sys.exit(1)
"""


def _make_target(tmp, name, run_body):
    feature_dir = os.path.join(tmp, "features", name)
    os.makedirs(os.path.join(feature_dir, "test"))
    with open(os.path.join(feature_dir, "test", "run.py"), "w") as f:
        f.write(run_body)
    return feature_dir


def _run_gate(feature_dir, verdict_path, extra=None):
    """Invoke the gate script as a subprocess exactly as the implementer
    subagent would: it runs the target's test/run.py and writes the verdict to
    the named artifact path. `extra` appends further CLI args (e.g.
    --project-dir / --test-command). Returns the gate process result."""
    argv = [sys.executable, _GATE, feature_dir, "--verdict-out", verdict_path]
    if extra:
        argv.extend(extra)
    return subprocess.run(argv, capture_output=True, text=True)


def _make_config(tmp, value):
    """Write ${tmp}/.auto-maintainer/config.json with implement_test_command set
    to `value` and return the project_dir (tmp)."""
    cfg_dir = os.path.join(tmp, ".auto-maintainer")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.json"), "w") as f:
        json.dump({"implement_test_command": value}, f)
    return tmp


# ==========================================================================
# E2E Behaviour: the gate records a PASSING verdict for a passing target — and
# the verdict is the SCRIPT's recorded subprocess result.
# ==========================================================================

def test_gate_records_pass_for_passing_target():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(target, verdict_path)

        # The gate exits 0 when the target suite passes.
        assert proc.returncode == 0, proc.stderr
        assert os.path.isfile(verdict_path), "gate must write the verdict artifact"

        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["feature"] == "greenfeat"
        assert verdict["passed"] is True
        assert verdict["returncode"] == 0
        assert isinstance(verdict["summary"], str) and verdict["summary"]


# ==========================================================================
# E2E Behaviour: the gate records a FAILING verdict for a failing target, and
# exits nonzero so a caller can detect the failure deterministically.
# ==========================================================================

def test_gate_records_fail_for_failing_target():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "redfeat", _FAILING_RUN)
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(target, verdict_path)

        assert proc.returncode != 0, "gate must exit nonzero on a failing target"
        assert os.path.isfile(verdict_path), (
            "gate must STILL write a verdict artifact on failure")

        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["feature"] == "redfeat"
        assert verdict["passed"] is False
        assert verdict["returncode"] != 0


# ==========================================================================
# E2E Behaviour: a missing target test/run.py is a deterministic gate failure
# (recorded as passed=False), never a silent pass.
# ==========================================================================

def test_gate_missing_runpy_is_a_failed_verdict():
    with tempfile.TemporaryDirectory() as tmp:
        feature_dir = os.path.join(tmp, "features", "norun")
        os.makedirs(feature_dir)  # no test/run.py at all
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(feature_dir, verdict_path)

        assert proc.returncode != 0
        assert os.path.isfile(verdict_path)
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is False


# ==========================================================================
# E2E Behaviour: the verdict is byte-deterministic — running the gate twice on
# the same passing target yields an equal recorded verdict.
# ==========================================================================

def test_gate_verdict_is_deterministic():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        path_a = os.path.join(tmp, "a.json")
        path_b = os.path.join(tmp, "b.json")

        _run_gate(target, path_a)
        _run_gate(target, path_b)

        with open(path_a) as f:
            a = json.load(f)
        with open(path_b) as f:
            b = json.load(f)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ==========================================================================
# E2E Behaviour: a configured implement_test_command (a shell command string)
# is run INSTEAD of test/run.py — a passing command yields passed=True with the
# command's final output line as the summary.
# ==========================================================================

def test_config_command_string_passing_is_run():
    with tempfile.TemporaryDirectory() as tmp:
        # A feature dir with NO test/run.py — the config command is what runs.
        feature_dir = os.path.join(tmp, "features", "cfgfeat")
        os.makedirs(feature_dir)
        project_dir = _make_config(tmp, "echo custom-suite-ok")
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(feature_dir, verdict_path,
                         extra=["--project-dir", project_dir])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["feature"] == "cfgfeat"
        assert verdict["passed"] is True
        assert verdict["returncode"] == 0
        assert verdict["summary"] == "custom-suite-ok"


# ==========================================================================
# E2E Behaviour: a configured command that FAILS (nonzero exit) yields
# passed=False and a nonzero gate exit — the command is the source of truth.
# ==========================================================================

def test_config_command_string_failing_is_run():
    with tempfile.TemporaryDirectory() as tmp:
        feature_dir = os.path.join(tmp, "features", "cfgfeat")
        os.makedirs(feature_dir)
        project_dir = _make_config(tmp, "echo boom && exit 3")
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(feature_dir, verdict_path,
                         extra=["--project-dir", project_dir])

        assert proc.returncode != 0
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is False
        assert verdict["returncode"] == 3
        assert verdict["summary"] == "boom"


# ==========================================================================
# E2E Behaviour: the config command runs with cwd = the feature dir, so a
# command referencing a file in the feature dir resolves against it.
# ==========================================================================

def test_config_command_runs_with_feature_dir_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        feature_dir = os.path.join(tmp, "features", "cfgfeat")
        os.makedirs(feature_dir)
        with open(os.path.join(feature_dir, "marker.txt"), "w") as f:
            f.write("hi")
        project_dir = _make_config(tmp, "cat marker.txt")
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(feature_dir, verdict_path,
                         extra=["--project-dir", project_dir])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True
        assert verdict["summary"] == "hi"


# ==========================================================================
# E2E Behaviour: the sentinel 'none' / 'skip' (case-insensitive) SKIPS the
# gate — a passed=True no-op verdict that does NOT touch test/run.py even when
# the feature has none.
# ==========================================================================

def test_config_none_skips_the_gate():
    for sentinel in ("none", "skip", "NONE", "Skip"):
        with tempfile.TemporaryDirectory() as tmp:
            # No test/run.py at all: default mode would FAIL; skip must NOT.
            feature_dir = os.path.join(tmp, "features", "norun")
            os.makedirs(feature_dir)
            project_dir = _make_config(tmp, sentinel)
            verdict_path = os.path.join(tmp, "verdict.json")

            proc = _run_gate(feature_dir, verdict_path,
                             extra=["--project-dir", project_dir])

            assert proc.returncode == 0, f"{sentinel}: {proc.stderr}"
            with open(verdict_path) as f:
                verdict = json.load(f)
            assert verdict["passed"] is True, sentinel
            assert verdict["returncode"] == 0
            assert verdict["summary"] == (
                "implement test-gate skipped (implement_test_command=none)")


# ==========================================================================
# E2E Behaviour: null/absent implement_test_command -> the historical run.py
# default. An absent config file, an absent key, and an explicit null all fall
# back to running test/run.py.
# ==========================================================================

def test_absent_config_file_uses_runpy_default():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        verdict_path = os.path.join(tmp, "verdict.json")

        # project_dir with NO .auto-maintainer/config.json.
        proc = _run_gate(target, verdict_path, extra=["--project-dir", tmp])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True
        assert verdict["summary"] == "1 passed, 0 failed"


def test_absent_key_uses_runpy_default():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        cfg_dir = os.path.join(tmp, ".auto-maintainer")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            json.dump({"some_other_key": "x"}, f)
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(target, verdict_path, extra=["--project-dir", tmp])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True
        assert verdict["summary"] == "1 passed, 0 failed"


def test_explicit_null_uses_runpy_default():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        project_dir = _make_config(tmp, None)
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(target, verdict_path,
                         extra=["--project-dir", project_dir])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True


# ==========================================================================
# E2E Behaviour: an unreadable / malformed config.json is tolerated — it falls
# back to the run.py default and never crashes the gate.
# ==========================================================================

def test_malformed_config_falls_back_to_runpy_default():
    with tempfile.TemporaryDirectory() as tmp:
        target = _make_target(tmp, "greenfeat", _PASSING_RUN)
        cfg_dir = os.path.join(tmp, ".auto-maintainer")
        os.makedirs(cfg_dir)
        with open(os.path.join(cfg_dir, "config.json"), "w") as f:
            f.write("{ this is not valid json ")
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(target, verdict_path, extra=["--project-dir", tmp])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True


# ==========================================================================
# E2E Behaviour: --test-command OVERRIDES the config value (the determinism
# seam the tests drive) — the CLI wins over config.json.
# ==========================================================================

def test_test_command_cli_overrides_config():
    with tempfile.TemporaryDirectory() as tmp:
        feature_dir = os.path.join(tmp, "features", "cfgfeat")
        os.makedirs(feature_dir)
        # Config says fail; the CLI override says pass — override must win.
        project_dir = _make_config(tmp, "exit 7")
        verdict_path = os.path.join(tmp, "verdict.json")

        proc = _run_gate(feature_dir, verdict_path, extra=[
            "--project-dir", project_dir,
            "--test-command", "echo overridden-ok"])

        assert proc.returncode == 0, proc.stderr
        with open(verdict_path) as f:
            verdict = json.load(f)
        assert verdict["passed"] is True
        assert verdict["summary"] == "overridden-ok"


# ==========================================================================
# Behaviour: the handoff validity predicate — an `opened` handoff is VALID only
# when it carries a PASSING script-produced verdict.
# ==========================================================================

def _passing_verdict():
    return {"feature": "greenfeat", "passed": True, "returncode": 0,
            "summary": "1 passed, 0 failed"}


def _failing_verdict():
    return {"feature": "redfeat", "passed": False, "returncode": 1,
            "summary": "0 passed, 1 failed"}


def test_opened_handoff_with_passing_verdict_is_valid():
    handoff = {
        "schema_version": impl.HANDOFF_SCHEMA_VERSION,
        "work_order_id": "wo-1",
        "status": "opened",
        "artifact": {"kind": "pr", "ref": "https://example/pr/1"},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": None,
        "test_verdict": _passing_verdict(),
    }
    result = impl.validate_handoff(handoff)
    assert result.valid is True
    assert result.reason is None


def test_opened_handoff_without_verdict_is_invalid():
    handoff = {
        "schema_version": impl.HANDOFF_SCHEMA_VERSION,
        "work_order_id": "wo-1",
        "status": "opened",
        "artifact": {"kind": "pr", "ref": "https://example/pr/1"},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": None,
        # no test_verdict
    }
    result = impl.validate_handoff(handoff)
    assert result.valid is False
    assert result.reason


def test_opened_handoff_with_failing_verdict_is_invalid():
    handoff = {
        "schema_version": impl.HANDOFF_SCHEMA_VERSION,
        "work_order_id": "wo-1",
        "status": "opened",
        "artifact": {"kind": "pr", "ref": "https://example/pr/1"},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": None,
        "test_verdict": _failing_verdict(),
    }
    result = impl.validate_handoff(handoff)
    assert result.valid is False
    assert result.reason


def test_non_opened_handoff_does_not_require_a_verdict():
    """The verdict evidence is REQUIRED only for an `opened` handoff. A `planned`
    (dry-run), `blocked`, or legacy `closed` handoff carries no PR, so it
    needs no test-verdict to be valid. (The doer no longer emits `closed` —
    reject disposition moved to TRIAGE — but validate_handoff stays tolerant of
    a legacy closed handoff for backward compatibility.)"""
    for status in ("planned", "blocked", "closed"):
        handoff = {
            "schema_version": impl.HANDOFF_SCHEMA_VERSION,
            "work_order_id": "wo-1",
            "status": status,
            "artifact": {"kind": "none", "ref": None},
            "discovered_work": [],
            "concerns": [],
            "blocked_reason": "x" if status == "blocked" else None,
        }
        result = impl.validate_handoff(handoff)
        assert result.valid is True, f"{status} should not require a verdict"


# ==========================================================================
# E2E Behaviour: the FULL gate seam end-to-end — run the gate against a real
# passing target, read its SCRIPT-produced verdict, embed it in an opened
# handoff, and assert the predicate accepts ONLY the script's recorded pass.
# A model that fabricated a passing verdict for a target that actually FAILS is
# caught because the script-produced verdict says passed=False.
# ==========================================================================

def test_gate_to_predicate_round_trip_accepts_only_real_pass():
    with tempfile.TemporaryDirectory() as tmp:
        # The honest path: target passes -> script verdict passes -> valid.
        green = _make_target(tmp, "greenfeat", _PASSING_RUN)
        gpath = os.path.join(tmp, "green.json")
        _run_gate(green, gpath)
        with open(gpath) as f:
            green_verdict = json.load(f)

        opened = {
            "schema_version": impl.HANDOFF_SCHEMA_VERSION,
            "work_order_id": "wo-1", "status": "opened",
            "artifact": {"kind": "pr", "ref": "https://example/pr/1"},
            "discovered_work": [], "concerns": [], "blocked_reason": None,
            "test_verdict": green_verdict,
        }
        assert impl.validate_handoff(opened).valid is True

        # The #255 attack: model claims opened, but the SCRIPT ran the actual
        # (failing) target — the recorded verdict is the source of truth and the
        # predicate rejects it.
        red = _make_target(tmp, "redfeat", _FAILING_RUN)
        rpath = os.path.join(tmp, "red.json")
        _run_gate(red, rpath)
        with open(rpath) as f:
            red_verdict = json.load(f)

        lying = dict(opened)
        lying["test_verdict"] = red_verdict
        assert impl.validate_handoff(lying).valid is False
