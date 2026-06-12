#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling (slice 2: real PULL work).

scheduling is the INTEGRATION cycle: it composes the already-implemented
lifecycle-core anchors (GUARD/EXIT from lifecycle-dispositions, DRAIN/PERSIST
from durable-state) plus work-intake's real PULL state into a single tick loop
driven through tick-orchestrator. The slice-1 DEMO_WORK stub is retired; PULL is
real read work. Every behaviour in docs/spec.md has an e2e test here:

  1. run_tick — one invocation = one tick: assembles the route
     GUARD->DRAIN->PULL->PERSIST->EXIT and the states map, seeds a TickContext
     from durable state, runs tick_orchestrator.run(...), persists the pulled
     work_items count, and returns the EXIT disposition signal.
  2. PULL integration (read-and-idle) — PULL fetches the repo's open issues via
     an INJECTABLE source (tests inject a stub; production defaults to
     work-intake's live gh source) and writes the work_items slot. After PULL +
     PERSIST, EXIT selects IDLE (NOT refire) regardless of how many items were
     pulled — a pure read has no act stage, so refiring would busy-loop.
  3. HEADLINE multi-tick run — invoking run_tick repeatedly re-pulls the current
     open issues idempotently (a read has no owed mutation); each tick persists
     the pulled count and idles.
  4. Crash-safety — a pure read has NO owed mutation to DRAIN: DRAIN remains a
     no-op for PULL. The route still flows correctly across ticks.
  5. HEADLINE STOPPED latches — after stop, GUARD halts and the tick does NOT
     pull (work_items is not re-read/persisted past the latch).
  6. Control scripts (#29/#30) — status.py reads the REAL disposition + the
     persisted work_items count (no slice-1 stub) and reports a sane "not
     started" state when no runtime dir exists; stop.py latches STOPPED via the
     lifecycle-dispositions API using run_tick's runtime-path resolution; both
     import self-contained from the flat shipped lib/ layout. The shipped
     start/stop/status skills invoke ${CLAUDE_PLUGIN_ROOT}/lib/{run_tick,stop,
     status}.py and hand-roll NO Python.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state,
lifecycle-dispositions, and work-intake UNCHANGED (imported via sys.path). It
does NOT edit or fork them.

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

# Consume the already-implemented sibling features via sys.path, exactly as the
# other feature tests do. Do NOT edit/fork any of them.
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402,F401
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402
import status as st  # noqa: E402
import stop as sp  # noqa: E402
import start as sa  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a stub PULL issue source over two fixture issues (no network).
# --------------------------------------------------------------------------

GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}, {"name": "p1"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  },
  {
    "number": 9,
    "title": "Add retry knob",
    "body": "",
    "url": "https://github.com/acme/widget/issues/9",
    "state": "OPEN",
    "labels": [],
    "author": {"login": "hubber"},
    "createdAt": "2026-05-03T08:00:00Z",
    "updatedAt": "2026-05-03T08:00:00Z"
  }
]"""


def _stub_source(json_text=GH_JSON_FIXTURE):
    """An injectable PULL source returning fixture WorkItems with NO network.

    The injectable contract is callable(repo) -> list[WorkItem]; the stub
    ignores repo and parses a fixed gh-shaped payload, so the suite is
    deterministic (spec-rules §1: the live-gh edge is isolated behind injection).
    """
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


def _paths():
    """A fresh temp dir with injectable runtime/state/journal paths."""
    root = tempfile.mkdtemp(prefix="scheduling-rt-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# --------------------------------------------------------------------------
# Behaviour 1+2 — run_tick over the real PULL route (read-and-idle).
# --------------------------------------------------------------------------

def test_run_tick_pulls_issues_persists_count_and_idles():
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source())
    # Read-and-idle: a pure read has no act stage, so EXIT idles (NOT refire)
    # regardless of how many items were pulled.
    assert signal == "idle", signal
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE
    # The pulled work_items count is persisted into durable state.
    assert rt.persisted_work_items_count(state_path) == 2


def test_run_tick_traverses_full_pull_route():
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source(),
                         return_run_result=True)
    assert result.path[:5] == ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"], \
        result.path
    # EXIT runs (it is not terminal); the run halts at the DONE terminal after
    # EXIT selects the disposition + releases the mutex.
    assert result.final_state == "DONE", result.path


def test_run_tick_releases_mutex_after_exit():
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    assert not ld.lock_is_held(runtime_dir)


def test_run_tick_idles_even_when_no_issues_pulled():
    """Read-and-idle holds for an EMPTY pull too: zero items still idles (the
    loop relies on the next heartbeat, never busy-firing)."""
    runtime_dir, state_path, journal_path = _paths()
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source("[]"))
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 0


def test_run_tick_writes_work_items_into_blackboard():
    """The PULL state writes the real work_items slot (full WorkItem dicts) into
    the tick blackboard, mapped from the pulled issues."""
    runtime_dir, state_path, journal_path = _paths()
    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source(),
                         return_run_result=True)
    assert "PULL" in result.path, result.path
    # The persisted snapshot carries the pulled items, schema-versioned.
    items = rt.persisted_work_items(state_path)
    assert len(items) == 2, items
    assert items[0]["number"] == 7
    assert items[0]["labels"] == ["bug", "p1"]
    assert items[0]["schema_version"] == wi.WORK_ITEM_SCHEMA_VERSION


# --------------------------------------------------------------------------
# Behaviour 3 — HEADLINE multi-tick run: re-pulls idempotently, idles each tick.
# --------------------------------------------------------------------------

def test_multi_tick_run_repulls_idempotently_and_idles():
    runtime_dir, state_path, journal_path = _paths()
    source = _stub_source()
    for _ in range(4):
        sig = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                          journal_path=journal_path, source=source)
        # Each tick re-pulls the current open issues (a read is idempotent) and
        # idles; the persisted count stays the fixture count.
        assert sig == "idle", sig
        assert rt.persisted_work_items_count(state_path) == 2
        assert not ld.lock_is_held(runtime_dir)
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE


# --------------------------------------------------------------------------
# Behaviour 4 — crash-safety: a pure read has NO owed mutation. DRAIN is a
# no-op for PULL; the route still flows correctly tick-to-tick.
# --------------------------------------------------------------------------

def test_drain_is_noop_for_pull_pure_read():
    """A PULL tick journals no mutation intent, so there is nothing for DRAIN to
    reconcile: the journal stays empty and ticks remain stable."""
    runtime_dir, state_path, journal_path = _paths()
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    # No owed work was recorded by a pure read.
    assert ds.Journal(journal_path).unconfirmed() == []
    # A second tick still completes and re-persists the same count.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    assert rt.persisted_work_items_count(state_path) == 2
    assert ds.Journal(journal_path).unconfirmed() == []


# --------------------------------------------------------------------------
# Behaviour 5 — HEADLINE STOPPED latches: after stop, GUARD halts and the tick
# does NOT pull.
# --------------------------------------------------------------------------

def test_stopped_disposition_latches_guard_halts_no_pull():
    runtime_dir, state_path, journal_path = _paths()

    # One clean tick pulls and persists the count.
    rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                journal_path=journal_path, source=_stub_source())
    assert rt.persisted_work_items_count(state_path) == 2

    # A human stop latches STOPPED.
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)

    # The next tick: GUARD halts; PULL never runs. We use a source that would
    # raise if invoked, proving the latched tick does no pull.
    def _exploding_source(repo=None):
        raise AssertionError("PULL must not run while STOPPED is latched")

    result = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_exploding_source,
                         return_run_result=True)
    assert "PULL" not in result.path, result.path
    # STOPPED stays latched (a halt does not clear it).
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


def test_run_tick_returns_halt_signal_when_stopped():
    runtime_dir, state_path, journal_path = _paths()
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    signal = rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                         journal_path=journal_path, source=_stub_source())
    assert signal == "halt", signal


# --------------------------------------------------------------------------
# Default source — the SHIPPED run_tick pulls real issues: when no source is
# injected, run_tick uses work-intake's live gh source. We assert the wiring
# (the default callable is work-intake's gh source) WITHOUT touching the network.
# --------------------------------------------------------------------------

def test_default_pull_source_is_work_intake_gh_source():
    assert rt.DEFAULT_PULL_SOURCE is wi.gh_issue_source


# --------------------------------------------------------------------------
# Shipped control skills exist with the documented command names.
# --------------------------------------------------------------------------

def _ship_skill(name):
    return os.path.join(_FEATURE_DIR, "ship", "skills", name, "SKILL.md")


def test_start_and_stop_skills_are_shipped():
    start = _ship_skill("start")
    stop = _ship_skill("stop")
    assert os.path.isfile(start), start
    assert os.path.isfile(stop), stop
    start_body = open(start).read()
    stop_body = open(stop).read()
    assert "auto-maintainer:start" in start_body, start_body[:200]
    assert "auto-maintainer:stop" in stop_body, stop_body[:200]
    assert "run_tick.py" in start_body, start_body[:400]
    assert "STOPPED" in stop_body, stop_body[:400]


def test_no_skill_references_retired_demo_work():
    """The DEMO_WORK stub is retired; no shipped control skill may still
    describe it (stale-route guard)."""
    for name in ("start", "stop", "status"):
        body = open(_ship_skill(name)).read()
        assert "DEMO_WORK" not in body, (name, body)


# --------------------------------------------------------------------------
# Regression (auto-maintainer-framework#24) — the shipped /start and /stop
# skills must invoke run_tick at its INSTALLED path.
# --------------------------------------------------------------------------

_INSTALL_PATH = "${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py"


def test_start_skill_references_install_correct_run_tick_path():
    body = open(_ship_skill("start")).read()
    assert _INSTALL_PATH in body, body
    assert "src/run_tick.py" not in body, body


def test_stop_skill_has_no_bare_src_run_tick_path():
    body = open(_ship_skill("stop")).read()
    assert "src/run_tick.py" not in body, body
    if "run_tick.py" in body:
        assert _INSTALL_PATH in body, body


# --------------------------------------------------------------------------
# Regression (auto-maintainer-framework#24) — run_tick, invoked with NO injected
# paths (the installed case), defaults its durable-state / journal / disposition
# location to a writable per-project runtime dir. Injected paths still win.
# --------------------------------------------------------------------------

def test_resolve_runtime_paths_prefers_claude_project_dir():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        runtime_dir, state_path, journal_path = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    expected_dir = os.path.join(project_dir, ".auto-maintainer")
    assert runtime_dir == expected_dir, runtime_dir
    assert state_path.startswith(expected_dir + os.sep), state_path
    assert journal_path.startswith(expected_dir + os.sep), journal_path


def test_resolve_runtime_paths_falls_back_to_cwd_when_no_project_dir():
    cwd = tempfile.mkdtemp(prefix="scheduling-cwd-")
    old_env = os.environ.pop("CLAUDE_PROJECT_DIR", None)
    old_cwd = os.getcwd()
    os.chdir(cwd)
    try:
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        os.chdir(old_cwd)
        if old_env is not None:
            os.environ["CLAUDE_PROJECT_DIR"] = old_env
    assert runtime_dir == os.path.join(os.path.realpath(cwd), ".auto-maintainer") \
        or runtime_dir == os.path.join(cwd, ".auto-maintainer"), runtime_dir


def test_run_tick_injected_paths_still_win_over_default():
    """Injected paths take precedence over the env/cwd default — the temp-path
    injection capability the rest of the suite relies on is preserved."""
    runtime_dir, state_path, journal_path = _paths()
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(runtime_dir=runtime_dir, state_path=state_path,
                    journal_path=journal_path, source=_stub_source())
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # The injected state advanced; the default .auto-maintainer dir was NOT used.
    assert rt.persisted_work_items_count(state_path) == 2
    assert not os.path.exists(os.path.join(project_dir, ".auto-maintainer"))


# --------------------------------------------------------------------------
# Control script: stop.py (#30) — deterministic STOPPED latch, no hand-rolled
# Python. stop.py owns the state write via the lifecycle-dispositions API and
# resolves the runtime dir exactly as run_tick does.
# --------------------------------------------------------------------------

def test_stop_latches_stopped_readable_by_lifecycle_api():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sp.stop()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    assert ld.read_disposition(am_dir) == ld.Disposition.STOPPED


def test_stop_resolves_runtime_dir_the_same_way_as_run_tick():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sp.stop()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


def test_stop_then_tick_does_not_pull_end_to_end():
    """End-to-end: stop.py latches STOPPED, and a subsequent tick (same runtime
    dir) halts in GUARD without pulling."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir

    def _exploding_source(repo=None):
        raise AssertionError("PULL must not run while STOPPED is latched")

    try:
        sp.stop()
        signal = rt.run_tick(source=_exploding_source)
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert signal == "halt", signal
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED


# --------------------------------------------------------------------------
# Control script: status.py (#29) — deterministic, reads the REAL disposition +
# persisted work_items count (no hardcoded slice-1 stub).
# --------------------------------------------------------------------------

def test_status_reports_real_disposition_and_work_items_after_ticks():
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())  # pull 2, disposition IDLE
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # The status line reports the REAL persisted work_items count (2) and the
    # disposition (IDLE), not a stub, and no longer the old counter wording.
    assert "work_items=2" in line, line
    assert ld.Disposition.IDLE in line, line
    assert "no loop configured" not in line, line
    assert "counter=" not in line, line


def test_status_reports_not_started_when_no_runtime_dir():
    """When the loop was never started (no runtime dir), status reports a sane
    'not started' state: default IDLE disposition + work_items 0, no crash."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert not os.path.isdir(runtime_dir), runtime_dir
    assert ld.Disposition.IDLE in line, line
    assert "work_items=0" in line, line


# A project-local route inserting TRIAGE between PULL and PERSIST (used by the
# #64 status regression below: a TRIAGE tick then a default tick).
_TRIAGE_ROUTE_FOR_STATUS = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PERSIST"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def test_status_reports_current_tick_work_orders_not_stale():
    """#64 + #69: status.py reports the CURRENT tick's read products. After a
    TRIAGE tick (work_orders>0) followed by a DEFAULT tick (no TRIAGE) in the
    same runtime dir, status must show the CURRENT tick's work_orders=0 — not
    the stale TRIAGE count (#64), and not by OMITTING the field (#69). status
    ALWAYS reports work_orders, including 0, so a reader can distinguish 'no
    TRIAGE routed' from 'TRIAGE ran, found nothing' and status never diverges
    from the tick trace, which always prints work_orders=N."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-status64-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        cfg = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(cfg, exist_ok=True)
        route_file = os.path.join(cfg, "route.json")
        # Tick 1: TRIAGE route -> work_orders persisted.
        with open(route_file, "w") as f:
            json.dump(_TRIAGE_ROUTE_FOR_STATUS, f)
        rt.run_tick(source=_stub_source())
        _rt_dir, state_path, _j = rt.resolve_runtime_paths()
        assert rt.persisted_work_orders_count(state_path) == 2
        # Tick 2: default route (override removed) -> work_orders reset to 0.
        os.remove(route_file)
        rt.run_tick(source=_stub_source())
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    # status ALWAYS reports work_orders (#69); after the default tick the value
    # is the CURRENT tick's 0 (#64), not the stale TRIAGE count of 2.
    assert "work_orders=0" in line, line
    assert "work_orders=2" not in line, line
    assert "work_items=2" in line, line


def test_status_shows_work_orders_zero_after_default_tick():
    """#69 repro: after a DEFAULT-route tick (no TRIAGE) persists work_orders=0,
    status MUST report work_orders=0 explicitly — never drop the field. Dropping
    it (the old conditional) made status diverge from the tick trace, which
    always prints work_orders=N, so a reader could not tell 'no TRIAGE routed'
    from 'TRIAGE ran, found nothing'."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-status69-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())  # default route: no TRIAGE
        _rt, state_path, _j = rt.resolve_runtime_paths()
        assert rt.persisted_work_orders_count(state_path) == 0
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert "work_orders=0" in line, line
    assert "work_items=2" in line, line


def test_status_shows_work_orders_count_after_triage_tick():
    """#69: after a TRIAGE-route tick persists work_orders=N (N>0), status shows
    work_orders=N — the same count the tick trace prints."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-status69-triage-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        cfg = os.path.join(project_dir, ".auto-maintainer")
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(_TRIAGE_ROUTE_FOR_STATUS, f)
        rt.run_tick(source=_stub_source())  # TRIAGE route: work_orders > 0
        _rt, state_path, _j = rt.resolve_runtime_paths()
        n = rt.persisted_work_orders_count(state_path)
        assert n > 0
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert f"work_orders={n}" in line, (n, line)


def test_status_field_order_matches_tick_trace_convention():
    """#69: status field presence + order matches the tick-trace convention —
    disposition, then work_items, then work_orders, then route, then
    runtime_dir — so status and the trace never diverge. work_orders is
    UNCONDITIONAL (the field is always present, even at 0)."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-status-order-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())  # default route -> work_orders=0
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    for field in ("disposition=", "work_items=", "work_orders=", "route=",
                  "runtime_dir="):
        assert field in line, (field, line)
    # Order: disposition < work_items < work_orders < route < runtime_dir.
    assert (line.index("disposition=") < line.index("work_items=")
            < line.index("work_orders=") < line.index("route=")
            < line.index("runtime_dir=")), line


def test_status_reflects_stopped_after_stop():
    """status.py reads the marker stop.py wrote: STOPPED is reported."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        rt.run_tick(source=_stub_source())
        sp.stop()
        line = st.status_line()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert ld.Disposition.STOPPED in line, line


def test_status_line_includes_runtime_dir():
    """The status line names the runtime dir it read from, for operator clarity."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        line = st.status_line()
        runtime_dir, _, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert runtime_dir in line, (runtime_dir, line)


# --------------------------------------------------------------------------
# Self-contained import (#29/#30) — status.py and stop.py must resolve their
# imports from the shipped flat `lib/` layout alone, where run_tick.py and the
# consumed sibling modules are FLAT SIBLINGS.
# --------------------------------------------------------------------------

def _materialize_plugin_lib():
    """Copy the scheduling scripts + every consumed sibling module into a single
    flat dir, mirroring the installed plugin `lib/` layout."""
    import shutil
    lib = tempfile.mkdtemp(prefix="scheduling-lib-")
    for fn in ("run_tick.py", "status.py", "stop.py", "start.py"):
        shutil.copy(os.path.join(_SRC, fn), os.path.join(lib, fn))
    for dep, mod in (("fsm-contracts", "fsm_contracts.py"),
                     ("tick-orchestrator", "tick_orchestrator.py"),
                     ("durable-state", "durable_state.py"),
                     ("lifecycle-dispositions", "lifecycle_dispositions.py"),
                     ("work-intake", "work_intake.py"),
                     ("adapter-wiring", "adapter_wiring.py")):
        shutil.copy(os.path.join(_FEATURES, dep, "src", mod),
                    os.path.join(lib, mod))
    return lib


def _import_from_lib(lib, modname):
    import importlib.util
    path = os.path.join(lib, modname + ".py")
    spec = importlib.util.spec_from_file_location("libtest_" + modname, path)
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    sys.path.insert(0, lib)
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path[:] = saved
    return module


def test_run_tick_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "run_tick")
        assert hasattr(mod, "run_tick")
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


def test_status_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "status")
        assert hasattr(mod, "status_line")
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


def test_stop_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "stop")
        assert hasattr(mod, "stop")
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


# --------------------------------------------------------------------------
# Shipped control skills are script-backed (#29/#30): start/stop/status invoke
# ${CLAUDE_PLUGIN_ROOT}/lib/{run_tick,stop,status}.py and hand-roll NO Python.
# --------------------------------------------------------------------------

_STATUS_INSTALL = "${CLAUDE_PLUGIN_ROOT}/lib/status.py"
_STOP_INSTALL = "${CLAUDE_PLUGIN_ROOT}/lib/stop.py"


def test_status_skill_is_shipped_and_script_backed():
    status = _ship_skill("status")
    assert os.path.isfile(status), status
    body = open(status).read()
    assert "auto-maintainer:status" in body, body[:300]
    assert _STATUS_INSTALL in body, body
    assert "src/status.py" not in body, body
    assert "no loop configured" not in body, body


def test_stop_skill_invokes_stop_script_not_handrolled_python():
    body = open(_ship_skill("stop")).read()
    assert _STOP_INSTALL in body, body
    assert "src/stop.py" not in body, body
    assert "python3 - <<" not in body, body
    assert "write_disposition(" not in body, body
    assert "import durable_state" not in body, body
    assert "from durable_state" not in body, body


def test_start_skill_has_no_handrolled_python():
    body = open(_ship_skill("start")).read()
    assert "python3 - <<" not in body, body
    assert "import durable_state" not in body, body
    assert "from durable_state" not in body, body


def test_control_skills_have_no_inline_python_heredoc():
    for name in ("start", "stop", "status"):
        body = open(_ship_skill(name)).read()
        assert "python3 - <<" not in body, (name, body)
        assert "<<EOF" not in body, (name, body)


# --------------------------------------------------------------------------
# Control script: start.py (#44) — deterministic fresh-start + tick #1.
#
# A latched STOPPED disposition (from a prior /stop) must NOT block /start:
# start IS the human resume (§1.2), so start.py clears the STOPPED latch to a
# runnable state and then runs tick #1. A latched ABORTED is a fault: start.py
# REFUSES (non-zero, no tick) and tells the user to investigate — it NEVER
# silently clears a fault. A clean state (IDLE/absent/RUNNING) ticks normally.
# start.py reuses run_tick's path resolution + the lifecycle API and must NOT
# duplicate the route — it imports/calls run_tick for the tick itself.
# --------------------------------------------------------------------------


def test_start_after_stopped_clears_latch_and_ticks():
    """#44: start after a latched STOPPED clears the latch and runs tick #1 —
    work_items get pulled/persisted and the disposition is no longer STOPPED."""
    runtime_dir, state_path, journal_path = _paths()
    # A prior /stop latched STOPPED.
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)

    signal = sa.start(runtime_dir=runtime_dir, state_path=state_path,
                      journal_path=journal_path, source=_stub_source())

    # Tick #1 actually ran (read-and-idle): the pull persisted, EXIT idled.
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 2
    # The STOPPED latch was cleared — the loop is runnable again.
    assert ld.read_disposition(runtime_dir) != ld.Disposition.STOPPED


def test_start_after_aborted_refuses_and_does_not_tick():
    """#44: start after a latched ABORTED REFUSES — it raises, runs NO tick, and
    leaves the ABORTED latch in place (a fault is never silently cleared)."""
    runtime_dir, state_path, journal_path = _paths()
    ld.write_disposition(runtime_dir, ld.Disposition.ABORTED)

    def _exploding_source(repo=None):
        raise AssertionError("start.py must not tick while ABORTED is latched")

    raised = False
    try:
        sa.start(runtime_dir=runtime_dir, state_path=state_path,
                 journal_path=journal_path, source=_exploding_source)
    except sa.StartRefused:
        raised = True
    assert raised, "start.py must refuse (raise StartRefused) on ABORTED"
    # No tick ran: no work_items were persisted.
    assert rt.persisted_work_items_count(state_path) == 0
    # The fault latch stays in place.
    assert ld.read_disposition(runtime_dir) == ld.Disposition.ABORTED


def test_start_when_clean_ticks_normally():
    """#44: start on a clean state (no marker -> default IDLE) ticks normally."""
    runtime_dir, state_path, journal_path = _paths()
    signal = sa.start(runtime_dir=runtime_dir, state_path=state_path,
                      journal_path=journal_path, source=_stub_source())
    assert signal == "idle", signal
    assert rt.persisted_work_items_count(state_path) == 2
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE


def test_start_main_exits_nonzero_on_aborted():
    """End-to-end via the CLI entrypoint: `python3 start.py` against a latched
    ABORTED runtime dir exits NON-ZERO and runs no tick (the #44 fault refusal
    surfaces as a process failure, not a silent clear)."""
    import subprocess
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    am_dir = os.path.join(project_dir, ".auto-maintainer")
    ld.write_disposition(am_dir, ld.Disposition.ABORTED)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    proc = subprocess.run(
        [sys.executable, os.path.join(_SRC, "start.py")],
        capture_output=True, text=True, env=env)
    assert proc.returncode != 0, (proc.returncode, proc.stdout, proc.stderr)
    # The fault latch stays ABORTED — start did not clear it.
    assert ld.read_disposition(am_dir) == ld.Disposition.ABORTED


def test_start_resolves_runtime_dir_the_same_way_as_run_tick():
    """start.py with no injected paths resolves the runtime dir exactly as
    run_tick does (reuses resolve_runtime_paths), then ticks."""
    project_dir = tempfile.mkdtemp(prefix="scheduling-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sa.start(source=_stub_source())
        runtime_dir, state_path, _ = rt.resolve_runtime_paths()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert rt.persisted_work_items_count(state_path) == 2
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE


def test_start_imports_self_contained_from_plugin_lib():
    lib = _materialize_plugin_lib()
    saved = dict(sys.modules)
    try:
        mod = _import_from_lib(lib, "start")
        assert hasattr(mod, "start")
        assert hasattr(mod, "StartRefused")
    finally:
        for k in list(sys.modules):
            if k not in saved:
                del sys.modules[k]


# --------------------------------------------------------------------------
# Shipped /start skill is script-backed for tick #1 (#44): it invokes
# ${CLAUDE_PLUGIN_ROOT}/lib/start.py (NOT inline Python) for the first tick,
# then schedules the recurring heartbeat that re-runs run_tick.py (NOT start.py
# — no reset per tick).
# --------------------------------------------------------------------------

_START_INSTALL = "${CLAUDE_PLUGIN_ROOT}/lib/start.py"


def test_start_skill_invokes_start_script_for_tick_one():
    body = open(_ship_skill("start")).read()
    # Tick #1 goes through start.py (clears STOPPED / refuses ABORTED first).
    assert _START_INSTALL in body, body
    assert "src/start.py" not in body, body


def test_start_skill_heartbeat_uses_run_tick_not_start():
    """The recurring heartbeat re-runs run_tick.py (no per-tick reset), so the
    start skill still references the installed run_tick.py for the heartbeat."""
    body = open(_ship_skill("start")).read()
    assert _INSTALL_PATH in body, body  # ${CLAUDE_PLUGIN_ROOT}/lib/run_tick.py


def test_start_skill_has_no_handrolled_disposition_clear():
    """#44 regression: the skill must not hand-roll the STOPPED-clear in Python
    (the #30-class prompt-tier drift). start.py owns that logic."""
    body = open(_ship_skill("start")).read()
    assert "write_disposition(" not in body, body
    assert "import lifecycle_dispositions" not in body, body
    assert "from lifecycle_dispositions" not in body, body
    assert "import durable_state" not in body, body
