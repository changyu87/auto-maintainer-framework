#!/usr/bin/env python3
"""End-to-end tests for status.py's injectable, tolerant OPEN-LOOP-PR probe.

status surfaces the loop's OWN open PRs and their merge posture so a human (or the
executor) can tell a PR that is PENDING auto-merge (waiting on CI/mergeability)
from one that is genuinely stuck — the exact confusion behind a live "nothing is
merging" misread. status.py gains a deterministic, INJECTABLE open-PR probe
(``DEFAULT_OPEN_PR_SOURCE``, overridable via a ``status_data`` param so tests stub
it with no network); the production probe shells
``gh pr list --label auto-maintainer --state open --json
number,mergeStateStatus,autoMergeRequest`` and returns a list of
``{number, auto_merge_enabled, merge_state}`` dicts (``auto_merge_enabled`` is True
iff the PR carries an ``autoMergeRequest``; ``merge_state`` is the raw
``mergeStateStatus``).

The probe is TOLERANT: ANY failure (no gh / no network / parse error) yields
``open_loop_prs=[]`` and NEVER crashes status (same tolerance as the release
probe). ``render_status`` shows, per PR, ``PR #<n> auto-merge pending
(<merge_state>)`` when auto-merge is enabled (else ``PR #<n> auto-merge off
(<merge_state>)``), and a quiet ``open loop PRs: none`` when the list is empty.
``--json`` emits the field; ``--line`` is unchanged.

scheduling CONSUMES run_tick + lifecycle-dispositions UNCHANGED via sys.path.

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

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import status as st  # noqa: E402


def _with_project_dir(project_dir, fn):
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old


def _data(project_dir, open_pr_source):
    """status_data with BOTH network probes stubbed (release + open-pr) so the
    test never touches the network."""
    return _with_project_dir(
        project_dir,
        lambda: st.status_data(
            release_probe=lambda: None, open_pr_source=open_pr_source))


# ==========================================================================
# status_data — the new open_loop_prs field with an injected probe.
# ==========================================================================

def test_status_data_exposes_open_loop_prs():
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    data = _data(project_dir, lambda: [])
    assert "open_loop_prs" in data, sorted(data)
    assert data["open_loop_prs"] == [], data


def test_status_data_open_loop_prs_reflects_probe():
    """A probe returning PRs yields open_loop_prs verbatim (number,
    auto_merge_enabled, merge_state)."""
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    prs = [
        {"number": 625, "auto_merge_enabled": True, "merge_state": "BLOCKED"},
        {"number": 626, "auto_merge_enabled": False, "merge_state": "CLEAN"},
    ]
    data = _data(project_dir, lambda: prs)
    assert data["open_loop_prs"] == prs, data


def test_status_data_open_pr_probe_error_is_tolerated_never_crashes():
    """A raising open-PR probe yields open_loop_prs=[] and does NOT crash
    status_data (same tolerance as the release probe)."""
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")

    def _boom():
        raise RuntimeError("gh: command not found")

    data = _data(project_dir, _boom)
    assert data["open_loop_prs"] == [], data


def test_default_open_pr_probe_never_crashes_status():
    """Calling status_data() with the DEFAULT (real gh) open-PR probe must never
    crash — it is tolerant regardless of gh/network availability."""
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    data = _with_project_dir(
        project_dir, lambda: st.status_data(release_probe=lambda: None))
    assert isinstance(data["open_loop_prs"], list), data


def test_default_open_pr_source_shape_from_gh_json():
    """DEFAULT_OPEN_PR_SOURCE maps raw gh JSON (number, mergeStateStatus,
    autoMergeRequest) to {number, auto_merge_enabled, merge_state}; a non-null
    autoMergeRequest -> auto_merge_enabled True. The gh shell is stubbed so no
    network."""
    gh_json = json.dumps([
        {"number": 700, "mergeStateStatus": "BLOCKED",
         "autoMergeRequest": {"enabledAt": "2026-08-16T00:00:00Z"}},
        {"number": 701, "mergeStateStatus": "CLEAN", "autoMergeRequest": None},
    ])
    orig = st._run_gh
    st._run_gh = lambda args, timeout=10: gh_json
    try:
        prs = st.DEFAULT_OPEN_PR_SOURCE()
    finally:
        st._run_gh = orig
    assert prs == [
        {"number": 700, "auto_merge_enabled": True, "merge_state": "BLOCKED"},
        {"number": 701, "auto_merge_enabled": False, "merge_state": "CLEAN"},
    ], prs


# ==========================================================================
# render_status — the per-PR pending/off lines and the empty 'none' case.
# ==========================================================================

def _base_data(project_dir):
    return _data(project_dir, lambda: [])


def test_render_status_shows_pending_line_when_auto_merge_enabled():
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    data = _base_data(project_dir)
    data["open_loop_prs"] = [
        {"number": 625, "auto_merge_enabled": True, "merge_state": "BLOCKED"}]
    out = st.render_status(data)
    assert "PR #625" in out, out
    assert "auto-merge pending" in out, out
    assert "BLOCKED" in out, out
    for ch in out:
        assert ord(ch) < 128 or ch in "→─", repr(ch)


def test_render_status_shows_off_line_when_auto_merge_disabled():
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    data = _base_data(project_dir)
    data["open_loop_prs"] = [
        {"number": 626, "auto_merge_enabled": False, "merge_state": "CLEAN"}]
    out = st.render_status(data)
    assert "PR #626" in out, out
    assert "auto-merge off" in out, out
    assert "CLEAN" in out, out


def test_render_status_shows_none_when_no_open_prs():
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    data = _base_data(project_dir)
    data["open_loop_prs"] = []
    out = st.render_status(data)
    assert "open loop PRs: none" in out, out


# ==========================================================================
# CLI --json emits the new field; --line stays byte-identical (unchanged).
# ==========================================================================

def _run_cli(args, project_dir):
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    env["PYTHONPATH"] = os.pathsep.join(
        [_SRC] + [os.path.join(_FEATURES, d, "src") for d in (
            "fsm-contracts", "tick-orchestrator", "durable-state",
            "lifecycle-dispositions", "work-intake", "adapter-wiring",
            "prioritize", "implement", "safety-governance", "agent-dispatch",
            "observability", "verify-integrate")]
        + [env.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, os.path.join(_SRC, "status.py")] + args,
        capture_output=True, text=True, env=env)


def test_cli_json_emits_open_loop_prs():
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    proc = _run_cli(["--json"], project_dir)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert "open_loop_prs" in data, sorted(data)
    assert isinstance(data["open_loop_prs"], list), data


def test_cli_line_unchanged_no_open_pr_field():
    """The legacy one-line status (--line) is unchanged — it carries no
    open_loop_prs field."""
    project_dir = tempfile.mkdtemp(prefix="sched-prs-")
    proc = _run_cli(["--line"], project_dir)
    assert proc.returncode == 0, proc.stderr
    assert "open_loop_prs" not in proc.stdout, proc.stdout
    assert "PR #" not in proc.stdout, proc.stdout
