#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's SHIPPED executor assets.

This cycle SHIPS two plugin assets into scheduling's ship/ tree (so the
packaging build's _copy_tree(ship_dir, plugin_root) collects them verbatim):

  - ship/skills/tick/SKILL.md          -> plugin skills/tick/SKILL.md
      the `tick` executor skill that drives run_tick.py --step/--resume and
      presses the Agent button at agent-states (DESIGN §3.4.6 / §2.8).
  - ship/agents/auto-maintainer-echo.md -> plugin agents/auto-maintainer-echo.md
      the domain-free proof triager subagent: echoes work_items -> accepted
      work_orders.

These tests prove the SHIPPED wiring is real and valid config a user can drop
in — not just that the files exist:

  1. Both ship files exist and parse (YAML frontmatter): the skill's name is
     `tick`; the agent's name is `auto-maintainer-echo`.
  2. The echo-TRIAGE AGENT wiring VALIDATES: a project-local adapter-map
     override mapping TRIAGE to an agent-adapter entry dispatching
     subagent_type=auto-maintainer-echo, plus a route
     GUARD->DRAIN->PULL->TRIAGE->PRIORITIZE->IMPLEMENT->PERSIST->EXIT (TRIAGE
     agent; PRIORITIZE/IMPLEMENT script), is ACCEPTED by adapter_wiring.build_loop
     — TRIAGE resolves to an AgentState and data-readiness is satisfied. This
     proves the shipped echo wiring is valid drop-in config.
  3. Executable end-to-end: that agent route, run through run_tick with a canned
     echo work_orders output (reusing the proven resume path), advances PAST
     TRIAGE (PRIORITIZE runs, the loop pauses at the script-derived next state),
     proving the shipped echo adapter is wireable + executable via the engine
     with no real Agent dispatch.

scheduling CONSUMES adapter-wiring (build_loop) + work-intake + the loop-core
features UNCHANGED via sys.path; it does NOT edit or fork them. The asset CONTENT
is authored + skill-creator-validated by the orchestrator and placed verbatim;
these tests assert the wiring, not the prose.

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
             "prioritize", "implement", "agent-dispatch", "safety-governance"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


_SHIP_DIR = os.path.join(_FEATURE_DIR, "ship")
_TICK_SKILL = os.path.join(_SHIP_DIR, "skills", "tick", "SKILL.md")
_START_SKILL = os.path.join(_SHIP_DIR, "skills", "start", "SKILL.md")
_ECHO_AGENT = os.path.join(_SHIP_DIR, "agents", "auto-maintainer-echo.md")


# --------------------------------------------------------------------------
# Minimal YAML-frontmatter reader: parses the leading `---`-delimited block as
# flat `key: value` pairs. No third-party deps (run.py imports plain modules).
# --------------------------------------------------------------------------

def _parse_frontmatter(path):
    with open(path, "r") as f:
        text = f.read()
    assert text.startswith("---\n"), ("missing frontmatter open", path)
    body = text[4:]
    end = body.index("\n---\n")
    block = body[:end]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


# --------------------------------------------------------------------------
# Behaviour 1 — the two ship files exist and parse with the right name.
# --------------------------------------------------------------------------

def test_ship_tick_skill_exists_and_names_tick():
    assert os.path.isfile(_TICK_SKILL), _TICK_SKILL
    fields = _parse_frontmatter(_TICK_SKILL)
    assert fields.get("name") == "tick", fields


def test_ship_echo_agent_exists_and_names_auto_maintainer_echo():
    assert os.path.isfile(_ECHO_AGENT), _ECHO_AGENT
    fields = _parse_frontmatter(_ECHO_AGENT)
    assert fields.get("name") == "auto-maintainer-echo", fields


def test_ship_assets_carry_lifecycle_metadata():
    """Lifecycle rules: each shipped skill/agent declares version + owner +
    deprecation_criterion in its frontmatter."""
    for path in (_TICK_SKILL, _ECHO_AGENT):
        fields = _parse_frontmatter(path)
        for key in ("version", "owner", "deprecation_criterion"):
            assert fields.get(key), (path, key, fields)


def _read_text(path):
    with open(path, "r") as f:
        return f.read()


# --------------------------------------------------------------------------
# Behaviour — the REWORKED start skill (v0.2.0): executor-driven first tick +
# prompt-cron heartbeat, consuming start.py --clear-only for the latch-clear.
# --------------------------------------------------------------------------

def test_ship_start_skill_exists_and_names_start_v020():
    assert os.path.isfile(_START_SKILL), _START_SKILL
    fields = _parse_frontmatter(_START_SKILL)
    assert fields.get("name") == "start", fields
    assert fields.get("version") == "0.2.0", fields


def test_ship_start_skill_carries_lifecycle_metadata():
    fields = _parse_frontmatter(_START_SKILL)
    for key in ("version", "owner", "deprecation_criterion"):
        assert fields.get(key), (key, fields)


def test_ship_start_skill_body_drives_executor_and_clear_only_and_heartbeat():
    """The reworked start skill (1) clears the latch via start.py --clear-only,
    (2) runs the first tick THROUGH the /auto-maintainer:tick executor (so
    agent-state dispatches are fulfilled), and (3) schedules a recurring PROMPT
    heartbeat that keeps ticking."""
    body = _read_text(_START_SKILL)
    # (1) latch-clear via the merged --clear-only mode (no tick #1 in start.py).
    assert "start.py --clear-only" in body, "start skill must use start.py --clear-only"
    # (2) first tick driven through the executor skill, not a bare run_tick.
    assert "/auto-maintainer:tick" in body, "start skill must drive tick-1 via the executor"
    # (3) a recurring prompt heartbeat fires the executor each interval.
    assert "prompt" in body.lower(), "start skill must schedule a prompt heartbeat"
    assert "recurring" in body.lower(), "start skill must schedule a recurring heartbeat"


# --------------------------------------------------------------------------
# Behaviour — the HARDENED tick skill (v0.1.1, #100): the resume step mandates
# the Write tool writing to the ABSOLUTE dispatch-result.json path.
# --------------------------------------------------------------------------

def test_ship_tick_skill_version_is_0_1_1():
    fields = _parse_frontmatter(_TICK_SKILL)
    assert fields.get("version") == "0.1.1", fields


def test_ship_tick_skill_resume_step_mandates_write_tool_and_absolute_path():
    """#100 hardening: the resume step must use the `Write` tool with the
    absolute ${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json path,
    not an improvised python -c, so large/quoted/newline subagent outputs
    serialize faithfully."""
    body = _read_text(_TICK_SKILL)
    assert "`Write` tool" in body or "Write tool" in body, \
        "tick skill resume step must reference the Write tool"
    assert "${CLAUDE_PROJECT_DIR}/.auto-maintainer/dispatch-result.json" in body, \
        "tick skill must reference the absolute dispatch-result.json path"
    # The hardening calls out the absolute path explicitly.
    assert "absolute" in body.lower(), "tick skill must call out the absolute path"


# --------------------------------------------------------------------------
# The shipped echo-TRIAGE agent-adapter entry (the exact drop-in config a user
# would place in ${project}/.auto-maintainer/adapter-map.json for TRIAGE) and a
# route routing TRIAGE (agent) -> PRIORITIZE (script) -> IMPLEMENT (script).
# --------------------------------------------------------------------------

_ECHO_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "auto-maintainer-echo",
            "task": "echo each work_item as an accepted work_order",
            "inputs": ["work_items"],
            "cardinality": "once",
            "writes": "work_orders",
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}


_ECHO_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT",
               "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PRIORITIZE"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "PRIORITIZE"},
        {"state": "PRIORITIZE", "signal": "OK", "next": "IMPLEMENT"},
        {"state": "PRIORITIZE", "signal": "EMPTY", "next": "IMPLEMENT"},
        {"state": "IMPLEMENT", "signal": "OK", "next": "PERSIST"},
        {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


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
    items = wi.parse_gh_issues(json_text)

    def source(repo=None):
        return list(items)
    return source


def _echo_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_ECHO_TRIAGE_AGENT)
    return amap


def _write_json(project_dir, name, obj):
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, name), "w") as f:
        json.dump(obj, f)


def _setup_echo_project():
    project_dir = tempfile.mkdtemp(prefix="sched-echoship-")
    _write_json(project_dir, "route.json", _ECHO_ROUTE)
    _write_json(project_dir, "adapter-map.json", _echo_map())
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


# A canned VALID work_orders output (a JSON array) the shipped echo subagent
# would return from dispatching the TRIAGE agent — two accepted orders, exactly
# the echo shape (one work_order per work_item).
_CANNED_ECHO_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
    {"schema_version": "1.0.0", "id": "wo-acme/widget#9",
     "work_item_id": "acme/widget#9", "title": "Add retry knob",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])


# --------------------------------------------------------------------------
# Behaviour 2 — the echo-TRIAGE agent wiring VALIDATES (build_loop accepts it,
# resolving TRIAGE to an AgentState dispatching the shipped echo subagent).
# --------------------------------------------------------------------------

def test_echo_triage_wiring_validates_via_build_loop():
    project_dir, runtime_dir, _state, _journal = _setup_echo_project()
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": runtime_dir,
        "source": _stub_source(),
        "now": None,
        "governance": {},
    }
    # build_loop loads the project-local route + adapter-map, resolves, and
    # VALIDATES; an invalid wiring would raise WiringError. Acceptance == it
    # returns (route, states) with TRIAGE resolved to an AgentState.
    route, states = aw.build_loop(
        rt.DEFAULT_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=rt._INITIAL_SLOTS)
    assert "TRIAGE" in route["states"], route["states"]
    triage_second = states["TRIAGE"][1]
    assert isinstance(triage_second, aw.AgentState), triage_second
    # The resolved AgentState dispatches the SHIPPED echo subagent.
    dispatch = triage_second.dispatch
    assert dispatch[0]["subagent_type"] == "auto-maintainer-echo", dispatch
    assert dispatch[0]["writes"] == "work_orders", dispatch


def test_echo_triage_route_pauses_at_triage_for_echo_subagent():
    """A fresh run_tick on the echo-wired route PAUSES at TRIAGE and hands the
    executor a dispatch for the shipped auto-maintainer-echo subagent."""
    project_dir, runtime_dir, state_path, journal_path = _setup_echo_project()
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    assert isinstance(result, dict), result
    assert result["status"] == "paused", result
    assert result["state"] == "TRIAGE", result
    assert len(result["dispatches"]) == 1, result
    d = result["dispatches"][0]
    assert d["subagent_type"] == "auto-maintainer-echo", d
    assert d["writes"] == "work_orders", d
    assert d["signal_rule"] == "nonempty_else_empty", d
    assert d["cardinality"] == "once", d


# --------------------------------------------------------------------------
# Behaviour 3 — executable end-to-end: resume the echo TRIAGE with a canned echo
# output and the engine advances PAST TRIAGE (PRIORITIZE runs).
# --------------------------------------------------------------------------

def test_echo_triage_resume_advances_past_triage():
    """Reusing the proven injected resume path (no real Agent): feeding the
    shipped echo subagent's canned work_orders output back advances the tick PAST
    TRIAGE. PRIORITIZE + the dry-run IMPLEMENT are SCRIPT factories in the default
    map, so the route then runs to terminal and idles (read-and-idle). This proves
    the shipped echo adapter is wired AND executable end-to-end via the engine."""
    project_dir, runtime_dir, state_path, journal_path = _setup_echo_project()
    rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                state_path=state_path, journal_path=journal_path,
                source=_stub_source())
    result = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source(),
                         resume_dispatch=[_CANNED_ECHO_WORK_ORDERS])
    # Advanced past TRIAGE all the way to the terminal -> disposition signal STRING
    # (NOT still paused at TRIAGE, which would be a dict).
    assert result == "idle", result
    # The echo work_orders the shipped subagent produced were applied + persisted
    # (#64): one accepted work_order per input work_item.
    assert rt.persisted_work_orders_count(state_path) == 2
