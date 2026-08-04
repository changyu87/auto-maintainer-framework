#!/usr/bin/env python3
"""End-to-end tests for the §3.11.5 loopback opt-out wiring in scheduling.

This slice connects safety-governance's `work_own_filings` knob (merged) to
work-intake's `Pull` `work_own_filings` param (merged): `make_pull(runtime)`
reads `sg.work_own_filings(runtime['governance'])` and binds it onto the
constructed `wi.Pull`, while keeping the injectable `source` binding unchanged.

Behaviour under test (both directions):
  - DEFAULT / explicit-True config -> the bound Pull INCLUDES loop-filed items
    (the loop works its OWN filings, the default-on behaviour).
  - explicit-False config -> the bound Pull EXCLUDES loop-filed items (they stay
    open for human triage; exclusion at PULL, never a TRIAGE reject).

Verified at TWO levels:
  1. make_pull binds the flag — asserted via the constructed Pull's
     `work_own_filings` attribute AND via running the bound Pull over a mixed
     work_items batch (one normal + one loop-filed item).
  2. e2e run_tick over the default PULL route (GUARD->DRAIN->PULL->PERSIST->EXIT)
     against a project-local config.json: a default config persists the
     loop-filed open issue into work_items; a work_own_filings=false config
     excludes it.

scheduling CONSUMES safety-governance + work-intake UNCHANGED (imported via
sys.path). It does NOT edit or fork them.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import work_intake as wi  # noqa: E402
import safety_governance as sg  # noqa: E402
import run_tick as rt  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a MIXED batch: one normal open issue (#7) and one LOOP-FILED open
# issue (#42, carrying the loop's provenance label). is_loop_filed(#42) is True.
# --------------------------------------------------------------------------

_NORMAL_ISSUE = {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z",
}

# A discovery the loop itself filed: carries the LOOP_FILED_LABEL provenance.
_LOOP_FILED_ISSUE = {
    "number": 42,
    "title": "Follow-up the loop surfaced",
    "body": "Filed by the autonomous maintainer.",
    "url": "https://github.com/acme/widget/issues/42",
    "state": "OPEN",
    "labels": [{"name": wi.LOOP_FILED_LABEL}],
    "author": {"login": "auto-maintainer"},
    "createdAt": "2026-05-04T08:00:00Z",
    "updatedAt": "2026-05-04T08:00:00Z",
}

_MIXED_FIXTURE = json.dumps([_NORMAL_ISSUE, _LOOP_FILED_ISSUE])


def _mixed_source():
    """An injectable PULL source returning the mixed batch (no network)."""
    items = wi.parse_gh_issues(_MIXED_FIXTURE)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="scheduling-pull-own-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _run_bound_pull(pull):
    """Run a constructed Pull over the mixed batch and return the written
    work_items numbers, exercising the bound work_own_filings flag end-to-end."""
    ctx = fc.TickContext()
    result = pull.run(ctx)
    return [item["number"] for item in result.writes["work_items"]]


# --------------------------------------------------------------------------
# Level 1 — make_pull binds the flag from runtime['governance'].
# --------------------------------------------------------------------------

def test_make_pull_default_config_includes_loop_filed():
    """make_pull with the DEFAULT governance (work_own_filings absent -> True)
    binds a Pull that INCLUDES loop-filed items: the bound Pull over the mixed
    batch keeps both #7 and #42."""
    runtime = {"source": _mixed_source(), "governance": sg.DEFAULT_GOVERNANCE}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__  # the Pull instance whose bound method is `run`
    assert pull._work_own_filings is True, pull._work_own_filings
    numbers = _run_bound_pull(pull)
    assert numbers == [7, 42], numbers


def test_make_pull_explicit_true_includes_loop_filed():
    """An explicit work_own_filings=True config also includes loop-filed items."""
    runtime = {"source": _mixed_source(),
               "governance": {"work_own_filings": True}}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._work_own_filings is True, pull._work_own_filings
    assert _run_bound_pull(pull) == [7, 42]


def test_make_pull_false_config_excludes_loop_filed():
    """make_pull with work_own_filings=False binds a Pull that EXCLUDES the
    loop-filed item: the bound Pull over the mixed batch drops #42, keeps #7."""
    runtime = {"source": _mixed_source(),
               "governance": {"work_own_filings": False}}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._work_own_filings is False, pull._work_own_filings
    numbers = _run_bound_pull(pull)
    assert numbers == [7], numbers


def test_make_pull_keeps_injectable_source_binding():
    """The injectable source binding is unchanged: make_pull still binds the
    runtime['source'] callable (the determinism seam tests rely on)."""
    src = _mixed_source()
    runtime = {"source": src, "governance": sg.DEFAULT_GOVERNANCE}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._source is src, "make_pull must bind the injected source"


def test_make_pull_defaults_source_when_runtime_omits_it():
    """make_pull with no injected source falls back to DEFAULT_PULL_SOURCE
    (work-intake's live gh source) — the existing default-source binding is
    preserved alongside the new flag binding."""
    runtime = {"governance": sg.DEFAULT_GOVERNANCE}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._source is rt.DEFAULT_PULL_SOURCE
    assert pull._work_own_filings is True


# --------------------------------------------------------------------------
# Level 2 — e2e run_tick over the default PULL route honors the config.
# --------------------------------------------------------------------------

def _write_config(project_dir, config):
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(am_dir, exist_ok=True)
    with open(os.path.join(am_dir, "config.json"), "w") as f:
        json.dump(config, f)


def test_run_tick_default_config_persists_loop_filed_item():
    """e2e: a default config (no work_own_filings key) runs the default PULL
    route and PERSISTS the loop-filed open issue (#42) into work_items alongside
    the normal one — the loop works its own filings by default."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-own-")
    # No config.json -> documented defaults (work_own_filings True).
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, project_dir=project_dir,
                         source=_mixed_source())
    assert signal == "idle", signal
    items = rt.persisted_work_items(state_path)
    numbers = sorted(i["number"] for i in items)
    assert numbers == [7, 42], numbers


def test_run_tick_false_config_excludes_loop_filed_item():
    """e2e: a project-local config.json with work_own_filings=false runs the
    default PULL route and EXCLUDES the loop-filed open issue (#42) — only the
    normal issue (#7) is persisted into work_items."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-own-")
    _write_config(project_dir, {"work_own_filings": False})
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, project_dir=project_dir,
                         source=_mixed_source())
    assert signal == "idle", signal
    items = rt.persisted_work_items(state_path)
    numbers = sorted(i["number"] for i in items)
    assert numbers == [7], numbers


def test_run_tick_explicit_true_config_persists_loop_filed_item():
    """e2e: an explicit work_own_filings=true config also persists the loop-filed
    item (the default-on knob, explicitly set)."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-own-")
    _write_config(project_dir, {"work_own_filings": True})
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=_mixed_source())
    items = rt.persisted_work_items(state_path)
    numbers = sorted(i["number"] for i in items)
    assert numbers == [7, 42], numbers
