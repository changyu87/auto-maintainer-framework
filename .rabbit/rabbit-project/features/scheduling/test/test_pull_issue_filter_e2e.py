#!/usr/bin/env python3
"""End-to-end tests for the issue-filter wiring in scheduling.

This slice connects safety-governance's `issue_filter` config to work-intake's
`Pull` `issue_filter` param (both merged, this branch): `make_pull(runtime)` reads
`sg.issue_filter(runtime['governance'])` (the pure normalizer returning the
canonical `{labels: List[List[str]], title_pattern: str|None}`, default no-filter
when absent) and binds it onto the constructed `wi.Pull`, mirroring the existing
`work_own_filings` threading — while keeping the injectable `source` binding
unchanged.

Behaviour under test (both directions):
  - DEFAULT / absent config -> the bound Pull carries the no-filter canonical
    object {"labels": [], "title_pattern": None}, so PULL pulls every open issue
    (non-breaking).
  - configured issue_filter -> the bound Pull carries the NORMALIZED object, and
    that normalized object is what reaches the injected PULL source (the source is
    work-intake's single filter point; the filtering logic is work-intake's,
    consumed unchanged).

Verified at TWO levels:
  1. make_pull binds the normalizer's output — asserted via the constructed
     Pull's `_issue_filter` attribute (default AND configured).
  2. e2e run_tick over the default PULL route (GUARD->DRAIN->PULL->PERSIST->EXIT)
     against a project-local config.json: a RECORDING source captures the
     issue_filter it is called with, proving the normalized object threads all the
     way from make_pull -> Pull -> source.

scheduling CONSUMES safety-governance + work-intake UNCHANGED (imported via
sys.path). It does NOT edit or fork them; the edit lives ONLY in make_pull.

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

import work_intake as wi  # noqa: E402
import safety_governance as sg  # noqa: E402
import run_tick as rt  # noqa: E402


# A single open issue fixture (its shape is irrelevant to the filter-threading
# assertion; the recording source captures the filter object regardless).
_ISSUE = {
    "number": 7,
    "title": "[bot] Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "A"}, {"name": "B"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z",
}

# A configured issue_filter (DNF labels + a title_pattern), as it would appear in
# the central config.json. sg.issue_filter normalizes it into the canonical shape.
_CONFIGURED_FILTER = {"labels": [["A", "B"]], "title_pattern": "^\\[bot\\]"}
_NORMALIZED = sg.issue_filter({"issue_filter": _CONFIGURED_FILTER})
_NO_FILTER = sg.issue_filter({})


def _recording_source():
    """An injectable PULL source that RECORDS the issue_filter it is called with
    and returns the single fixture item (no network)."""
    items = wi.parse_gh_issues(json.dumps([_ISSUE]))
    seen = {}

    def source(repo=None, issue_filter=None):
        seen["issue_filter"] = issue_filter
        return list(items)
    source.seen = seen
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="scheduling-pull-filter-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


def _write_config(project_dir, config):
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(am_dir, exist_ok=True)
    with open(os.path.join(am_dir, "config.json"), "w") as f:
        json.dump(config, f)


# --------------------------------------------------------------------------
# Level 1 — make_pull binds sg.issue_filter(runtime['governance']) onto Pull.
# --------------------------------------------------------------------------

def test_make_pull_default_config_binds_no_filter():
    """make_pull with the DEFAULT governance (issue_filter absent) binds a Pull
    whose _issue_filter is the canonical no-filter object, so PULL pulls every
    open issue."""
    runtime = {"source": _recording_source(),
               "governance": sg.DEFAULT_GOVERNANCE}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__  # the Pull instance whose bound method is `run`
    assert pull._issue_filter == _NO_FILTER, pull._issue_filter
    assert pull._issue_filter == {"labels": [], "title_pattern": None,
                                  "exclude_labels": []}


def test_make_pull_configured_binds_normalized_filter():
    """make_pull with a configured issue_filter binds a Pull whose _issue_filter
    is safety-governance's NORMALIZED canonical object."""
    runtime = {"source": _recording_source(),
               "governance": {"issue_filter": _CONFIGURED_FILTER}}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._issue_filter == _NORMALIZED, pull._issue_filter
    assert pull._issue_filter == {"labels": [["A", "B"]],
                                  "title_pattern": "^\\[bot\\]",
                                  "exclude_labels": []}


def test_make_pull_keeps_work_own_filings_and_source():
    """The new issue_filter binding is ADDITIVE: make_pull still binds the
    work_own_filings toggle and the injected source (the existing threading is
    preserved alongside the new one)."""
    src = _recording_source()
    runtime = {"source": src,
               "governance": {"work_own_filings": False,
                              "issue_filter": _CONFIGURED_FILTER}}
    _manifest, run = rt.make_pull(runtime)
    pull = run.__self__
    assert pull._source is src
    assert pull._work_own_filings is False
    assert pull._issue_filter == _NORMALIZED


# --------------------------------------------------------------------------
# Level 2 — e2e run_tick: the normalized filter threads through to the source.
# --------------------------------------------------------------------------

def test_run_tick_configured_filter_reaches_source():
    """e2e: a project-local config.json carrying an issue_filter runs the default
    PULL route and the NORMALIZED filter object reaches the injected PULL source
    — proving make_pull -> Pull -> source threading end-to-end."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-filter-")
    _write_config(project_dir, {"issue_filter": _CONFIGURED_FILTER})
    src = _recording_source()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, project_dir=project_dir,
                         source=src)
    assert signal == "idle", signal
    assert src.seen["issue_filter"] == _NORMALIZED, src.seen


def test_run_tick_default_config_passes_no_filter_to_source():
    """e2e: with no config.json (documented defaults) the default PULL route
    passes the canonical no-filter object to the source, so every open issue is
    pulled (non-breaking)."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-filter-")
    src = _recording_source()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, project_dir=project_dir,
                source=src)
    assert src.seen["issue_filter"] == _NO_FILTER, src.seen
    assert src.seen["issue_filter"] == {"labels": [], "title_pattern": None,
                                        "exclude_labels": []}
