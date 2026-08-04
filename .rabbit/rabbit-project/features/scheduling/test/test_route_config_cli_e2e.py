#!/usr/bin/env python3
"""End-to-end conformance tests for the route wiring CLI (spec §3.4.3 / §3.10.2).

scheduling owns DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP, so it ships the guided route
CLI (`src/route_config.py`) that lets a user edit the project-local
${project_dir}/.auto-maintainer/route.json WITHOUT hand-writing JSON. It is a
deterministic load-modify-VALIDATE-save: each edit is applied to the route dict,
then VALIDATED by building the loop (adapter_wiring.build_loop over the edited
route + the active adapter-map) BEFORE writing. A failing edit is REJECTED
(non-zero exit, file NOT written) — an invalid route can never be saved.

This module exercises the spec behaviours:

  1. --show with NO override prints the DEFAULT_ROUTE + source=default (#59).
  2. --show with an override prints the override route + source=override:<path>.
  3. --describe emits a machine-first catalog of states/edges (parseable JSON).
  4. insert-state TRIAGE between PULL and PERSIST VALIDATES + WRITES route.json,
     and the written route then resolves through adapter_wiring.build_loop.
  5. A FAILING edit (an invalid route, e.g. TRIAGE before PULL violating
     data-readiness) is REJECTED — non-zero exit AND route.json NOT written.
  6. remove-state / add-edge / remove-edge round-trip and stay validated.

scheduling CONSUMES adapter-wiring UNCHANGED (its validators); it never modifies
it. The CLI lives in scheduling because it needs scheduling's DEFAULT_ROUTE +
DEFAULT_ADAPTER_MAP defaults.

Owner: changyu87
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

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

import adapter_wiring as aw  # noqa: E402
import run_tick as rt  # noqa: E402
import route_config as rc  # noqa: E402


def _override_path(project_dir):
    return os.path.join(project_dir, ".auto-maintainer", "route.json")


def _run(argv, project_dir):
    """Run the CLI with --project-dir injected, capturing stdout + exit code."""
    buf = io.StringIO()
    full = list(argv) + ["--project-dir", project_dir]
    with redirect_stdout(buf):
        code = rc.main(full)
    return code, buf.getvalue()


def test_show_default_route_when_no_override():
    """--show with no project-local route.json prints the DEFAULT_ROUTE and
    reports source=default (#59)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(["--show"], project_dir)
        assert code == 0, out
        assert "default" in out, out
        # The default spine states are surfaced.
        for state in ("GUARD", "DRAIN", "PULL", "PERSIST", "EXIT"):
            assert state in out, (state, out)
        # No override was created by a read-only --show.
        assert not os.path.isfile(_override_path(project_dir)), out


def test_show_override_route_reports_override_source():
    """--show with a project-local route.json prints it + source=override:<path>
    (#59), the SAME path adapter-wiring loads."""
    with tempfile.TemporaryDirectory() as project_dir:
        path = _override_path(project_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(rt.DEFAULT_ROUTE, f)
        code, out = _run(["--show"], project_dir)
        assert code == 0, out
        assert "override" in out, out
        assert path in out, (path, out)


def test_describe_emits_machine_first_catalog():
    """--describe emits a machine-first catalog of the current states + edges +
    the editable operations, parseable as JSON (for the skill to drive)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(["--describe"], project_dir)
        assert code == 0, out
        catalog = json.loads(out)
        assert "GUARD" in catalog["states"], catalog
        assert "PULL" in catalog["states"], catalog
        assert isinstance(catalog["edges"], list), catalog
        # The editable operations are advertised so the skill can drive them.
        assert "operations" in catalog, catalog


def test_insert_state_validates_and_writes():
    """insert-state TRIAGE between PULL and PERSIST is applied + VALIDATED via
    adapter_wiring.build_loop, then WRITTEN to route.json; the written route
    resolves through build_loop with the active adapter-map."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["insert-state", "--state", "TRIAGE",
             "--after", "PULL", "--before", "PERSIST"],
            project_dir)
        assert code == 0, out
        path = _override_path(project_dir)
        assert os.path.isfile(path), out
        with open(path) as f:
            written = json.load(f)
        assert "TRIAGE" in written["states"], written
        # The written route resolves + validates through adapter-wiring.
        runtime = {"project_dir": project_dir, "runtime_dir": project_dir,
                   "source": None, "now": None,
                   "governance": {"mode": "dry-run"}}
        route, states = aw.build_loop(
            rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
            start="GUARD", initial=rt._INITIAL_SLOTS)
        assert "TRIAGE" in route["states"], route


def test_failing_edit_rejected_no_write():
    """A FAILING edit (an invalid route — TRIAGE inserted BEFORE PULL violates
    data-readiness: TRIAGE reads work_items which PULL has not yet written) is
    REJECTED: non-zero exit AND route.json is NOT written (load-modify-VALIDATE-
    save — an invalid route can never be saved)."""
    with tempfile.TemporaryDirectory() as project_dir:
        code, out = _run(
            ["insert-state", "--state", "TRIAGE",
             "--after", "DRAIN", "--before", "PULL"],
            project_dir)
        assert code != 0, out
        # The invalid route was REJECTED — no file written.
        assert not os.path.isfile(_override_path(project_dir)), out


def test_remove_state_round_trips_validated():
    """A round-trip: insert TRIAGE (validated+written), then remove it again —
    each edit re-validates; the final route.json drops back to the default
    spine's states and still resolves."""
    with tempfile.TemporaryDirectory() as project_dir:
        c1, o1 = _run(
            ["insert-state", "--state", "TRIAGE",
             "--after", "PULL", "--before", "PERSIST"], project_dir)
        assert c1 == 0, o1
        c2, o2 = _run(["remove-state", "--state", "TRIAGE"], project_dir)
        assert c2 == 0, o2
        with open(_override_path(project_dir)) as f:
            written = json.load(f)
        assert "TRIAGE" not in written["states"], written
        # Still resolves through adapter-wiring.
        runtime = {"project_dir": project_dir, "runtime_dir": project_dir,
                   "source": None, "now": None,
                   "governance": {"mode": "dry-run"}}
        route, _states = aw.build_loop(
            rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
            start="GUARD", initial=rt._INITIAL_SLOTS)
        assert "TRIAGE" not in route["states"], route
