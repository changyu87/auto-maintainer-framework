#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's JSON tick CLI.

This cycle adds a JSON **tick CLI** to run_tick.py so the (later) executor skill
can drive the yield/resume loop deterministically: a `--step` mode that runs
until the next pause/done and prints a JSON envelope, and a `--resume` mode
(NO file argument) that reads the paused agent-state's subagent-WRITTEN OUTPUT
FILES at the checkpoint's output_paths. The CLI is a THIN deterministic wrapper
around the EXISTING run_tick structured returns (Slice C) — it adds NO new tick
logic. It consumes every sibling UNCHANGED; edits live ONLY in scheduling
(run_tick.py).

The CLI envelope contract (the settled shape, printed as a SINGLE JSON object on
stdout, nothing else on stdout):

  - done            -> {"status":"done", "signal":"<idle|halt|...>",
                        "trace":"<the one-line trace string>"}
  - paused          -> {"status":"paused", "state":"<name>",
                        "dispatches":[{subagent_type, prompt, writes, output_path,
                                       signal_rule, cardinality, item?}...]}
  - invalid_output  -> {"status":"invalid_output", "state":..., "reason":...}

The human trace line that bare `run_tick` prints to stdout MUST NOT pollute the
`--step`/`--resume` stdout — it is captured into the JSON `trace` field; stdout
is PURE JSON (the skill parses stdout). A bare invocation (no flags) is UNCHANGED
— it prints the human trace and behaves exactly as before (regression).

Tests drive the CLI by calling rt.main(argv) IN-PROCESS with a temp runtime
(the --runtime-dir/--state/--journal/--project-dir flags point the durable files
at a tmp dir) and stub the PULL source by monkeypatching rt.DEFAULT_PULL_SOURCE,
so the suite touches no network. stdout is captured to assert it is pure JSON.

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  A. --step on a pure-SCRIPT default route -> stdout is valid JSON
     {"status":"done","signal":"idle",...}; no non-JSON noise on stdout.
  B. --step on an AGENT route -> {"status":"paused","state":"TRIAGE",
     "dispatches":[...]} with a rendered prompt + writes + output_path; stdout
     pure JSON.
  C. WRITE canned outputs to each paused dispatch's output_path, --resume (no
     file arg) -> applies + advances -> next JSON (paused again or done); a full
     step->resume->resume->done sequence reaches {"status":"done"} with the slot
     persisted.
  D. a MISSING output file on --resume -> {"status":"invalid_output", ...}
     (no crash, documented exit code 1).
  E. bare run_tick.py (no args) still prints the human trace + behaves as before.
  F. ALL existing scheduling tests stay green (run by the shared runner).

Owner: changyu87
"""

import contextlib
import io
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

import durable_state as ds  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402


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


@contextlib.contextmanager
def _stub_pull_source():
    """Monkeypatch the module-level DEFAULT_PULL_SOURCE so the CLI (which passes
    no source) pulls fixture issues instead of hitting the live gh CLI."""
    saved = rt.DEFAULT_PULL_SOURCE
    rt.DEFAULT_PULL_SOURCE = _stub_source()
    try:
        yield
    finally:
        rt.DEFAULT_PULL_SOURCE = saved


def _temp_runtime():
    root = tempfile.mkdtemp(prefix="sched-cli-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return root, runtime_dir, state_path, journal_path


def _step_argv(runtime_dir, state_path, journal_path, project_dir=None):
    argv = ["--step", "--runtime-dir", runtime_dir, "--state", state_path,
            "--journal", journal_path]
    if project_dir is not None:
        argv += ["--project-dir", project_dir]
    return argv


def _resume_argv(runtime_dir, state_path, journal_path, project_dir=None):
    """--resume takes NO file argument now: it reads the checkpoint's
    subagent-written output files at their output_paths."""
    argv = ["--resume", "--runtime-dir", runtime_dir,
            "--state", state_path, "--journal", journal_path]
    if project_dir is not None:
        argv += ["--project-dir", project_dir]
    return argv


def _write_outputs_from_envelope(envelope, contents):
    """Simulate the subagent: WRITE each content string to the matching paused
    dispatch's output_path (parsed from the CLI's paused JSON envelope)."""
    for d, content in zip(envelope["dispatches"], contents):
        path = d["output_path"]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)


def _run_main(argv):
    """Call rt.main(argv), capturing stdout. Returns (exit_code, stdout_str)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rt.main(argv)
    return code, buf.getvalue()


# --------------------------------------------------------------------------
# Agent-route fixtures (mirror the yield/resume e2e fixtures): an agent TRIAGE +
# agent IMPLEMENT with a SCRIPT PRIORITIZE between them, wired by config so the
# CLI loads them via the project-local route.json + adapter-map.json.
# --------------------------------------------------------------------------

_TRIAGE_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_items"], "writes": ["work_orders"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "triage-doer",
            "inputs": ["work_items"],
            "writes": "work_orders",
            "cardinality": "once",
            "task": "Triage the work_items into accepted work_orders.",
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}

_IMPLEMENT_AGENT = {
    "kind": "agent",
    "manifest": {"reads": ["execution_plan"], "writes": ["handoffs"],
                 "emits": ["OK", "BLOCKED"]},
    "dispatch": [
        {
            "subagent_type": "implement-doer",
            "inputs": ["execution_plan"],
            "writes": "handoffs",
            "cardinality": {"per_item": "execution_plan.ordered"},
            "task": "Implement one work_order.",
        }
    ],
    "signal": {"rule": "blocked_if_any"},
}

_AGENT_ROUTE = {
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


def _agent_map():
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = dict(_TRIAGE_AGENT)
    amap["IMPLEMENT"] = dict(_IMPLEMENT_AGENT)
    return amap


def _setup_agent_project():
    """A project dir wired with the agent route + adapter-map override and the
    runtime paths under it. Returns (project_dir, runtime_dir, state_path,
    journal_path)."""
    project_dir = tempfile.mkdtemp(prefix="sched-cliproj-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_AGENT_ROUTE, f)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(_agent_map(), f)
    runtime_dir = cfg
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, runtime_dir, state_path, journal_path


_CANNED_WORK_ORDERS = json.dumps([
    {"schema_version": "1.0.0", "id": "wo-acme/widget#7",
     "work_item_id": "acme/widget#7", "title": "Crash on empty config",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
    {"schema_version": "1.0.0", "id": "wo-acme/widget#9",
     "work_item_id": "acme/widget#9", "title": "Add retry knob",
     "body": "", "url": "", "labels": [], "decision": "accepted",
     "reason": "", "created_at": ""},
])


def _canned_handoff(work_order_id):
    return json.dumps({
        "schema_version": "1.0.0", "work_order_id": work_order_id,
        "status": "planned", "artifact": {"kind": "none", "ref": None},
        "discovered_work": [], "blocked_reason": None,
    })


# ==========================================================================
# Behaviour A — --step on a pure-SCRIPT default route -> done JSON, pure stdout.
# ==========================================================================

def test_step_pure_script_route_emits_done_json():
    _root, runtime_dir, state_path, journal_path = _temp_runtime()
    with _stub_pull_source():
        code, out = _run_main(_step_argv(runtime_dir, state_path, journal_path))
    # stdout MUST be pure JSON — a single object, no human trace noise.
    envelope = json.loads(out)
    assert envelope["status"] == "done", envelope
    assert envelope["signal"] == "idle", envelope
    # The human trace is captured into the JSON `trace` field, not on stdout raw.
    assert "trace" in envelope, envelope
    assert "[tick]" in envelope["trace"], envelope["trace"]
    assert code == 0, code
    # The work_items were pulled + persisted by the underlying tick.
    assert rt.persisted_work_items_count(state_path) == 2


def test_step_stdout_is_pure_json_only():
    """stdout carries EXACTLY one JSON object and nothing else (the skill parses
    stdout) — no leading/trailing human trace lines."""
    _root, runtime_dir, state_path, journal_path = _temp_runtime()
    with _stub_pull_source():
        _code, out = _run_main(_step_argv(runtime_dir, state_path, journal_path))
    stripped = out.strip()
    # Exactly one JSON document: json.loads on the WHOLE stdout succeeds and there
    # is no extra non-whitespace content (the skill parses stdout). The done
    # envelope folds the human trace into the JSON `trace` field, so `[tick]`
    # appears ONLY inside that JSON string value, never as a raw stdout line.
    parsed = json.loads(stripped)
    assert isinstance(parsed, dict), parsed
    # The raw trace is NOT emitted as its own stdout line outside the JSON: the
    # only line of stdout is the JSON object itself.
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    assert len(lines) == 1, lines
    assert lines[0] == json.dumps(parsed), lines[0]
    # Whatever `[tick]` text exists is carried inside the JSON `trace` value.
    assert "[tick]" in parsed["trace"], parsed


# ==========================================================================
# Behaviour B — --step on an AGENT route -> paused JSON with rendered dispatch.
# ==========================================================================

def test_step_agent_route_emits_paused_json():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        code, out = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                         project_dir=project_dir))
    envelope = json.loads(out)
    assert envelope["status"] == "paused", envelope
    assert envelope["state"] == "TRIAGE", envelope
    assert len(envelope["dispatches"]) == 1, envelope
    d = envelope["dispatches"][0]
    assert d["subagent_type"] == "triage-doer", d
    assert d["writes"] == "work_orders", d
    assert d["signal_rule"] == "nonempty_else_empty", d
    assert d["cardinality"] == "once", d
    # The dispatch carries an output_path under dispatch-out/ (file-based resume).
    assert "output_path" in d, d
    assert os.path.join(runtime_dir, "dispatch-out") in d["output_path"], d
    # The prompt is RENDERED markdown surfaced through the CLI verbatim.
    assert d["prompt"].startswith("# Dispatch: TRIAGE"), d["prompt"][:60]
    assert code == 0, code
    # No trace noise on stdout for a pause.
    assert "[tick]" not in out, out


# ==========================================================================
# Behaviour C — --resume <file> applies + advances; full step->resume->resume->done.
# ==========================================================================

def test_resume_advances_to_next_pause():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        _c, o1 = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                      project_dir=project_dir))
        # WRITE the canned work_orders to the TRIAGE output_path, THEN resume
        # (--resume takes no file arg — it reads the output files).
        _write_outputs_from_envelope(json.loads(o1), [_CANNED_WORK_ORDERS])
        code, out = _run_main(_resume_argv(runtime_dir, state_path,
                                           journal_path, project_dir=project_dir))
    envelope = json.loads(out)
    assert envelope["status"] == "paused", envelope
    assert envelope["state"] == "IMPLEMENT", envelope
    # IMPLEMENT is per_item over execution_plan.ordered (2 orders) -> 2 dispatches.
    assert len(envelope["dispatches"]) == 2, envelope
    for d in envelope["dispatches"]:
        assert d["subagent_type"] == "implement-doer", d
        assert "item" in d, d
        assert "output_path" in d, d
    assert code == 0, code


def test_full_step_resume_resume_done_sequence_persists_slots():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        # step -> PAUSE at TRIAGE
        _c1, o1 = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                       project_dir=project_dir))
        e1 = json.loads(o1)
        assert e1["state"] == "TRIAGE"
        # WRITE work_orders to TRIAGE output_path, resume -> PAUSE at IMPLEMENT
        _write_outputs_from_envelope(e1, [_CANNED_WORK_ORDERS])
        _c2, o2 = _run_main(_resume_argv(runtime_dir, state_path,
                                         journal_path, project_dir=project_dir))
        e2 = json.loads(o2)
        assert e2["state"] == "IMPLEMENT"
        # WRITE two per-item handoffs to the IMPLEMENT output_paths, resume -> DONE
        _write_outputs_from_envelope(
            e2, [_canned_handoff(d["item"]) for d in e2["dispatches"]])
        code, o3 = _run_main(_resume_argv(runtime_dir, state_path,
                                          journal_path, project_dir=project_dir))
    envelope = json.loads(o3)
    assert envelope["status"] == "done", envelope
    assert envelope["signal"] == "idle", envelope
    assert "[tick]" in envelope["trace"], envelope["trace"]
    assert code == 0, code
    # The slots were persisted on reaching the terminal (#64 read products).
    assert rt.persisted_work_orders_count(state_path) == 2
    assert rt.persisted_execution_plan_count(state_path) == 2
    assert rt.persisted_handoffs_count(state_path) == 2
    # Checkpoint cleared at the terminal.
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc or doc[rt.TICK_CHECKPOINT_KEY] in (
        None, {}), doc


# ==========================================================================
# Behaviour D — invalid / missing output FILE -> invalid_output JSON, no crash.
# ==========================================================================

def test_resume_invalid_agent_output_emits_invalid_output_json():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        _c, o1 = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                      project_dir=project_dir))
        # work_orders expects an array; the subagent writes an OBJECT -> mismatch.
        _write_outputs_from_envelope(json.loads(o1),
                                     [json.dumps({"not": "an array"})])
        code, out = _run_main(_resume_argv(runtime_dir, state_path,
                                           journal_path, project_dir=project_dir))
    envelope = json.loads(out)
    assert envelope["status"] == "invalid_output", envelope
    assert envelope["state"] == "TRIAGE", envelope
    assert envelope.get("reason"), envelope
    # Documented exit code for invalid_output: nonzero (1).
    assert code == 1, code
    # Checkpoint intact (re-dispatchable).
    doc = ds.DurableState(state_path).load()
    assert doc.get(rt.TICK_CHECKPOINT_KEY) is not None, doc


def test_resume_unparseable_output_file_emits_invalid_output_json():
    """An output file whose content is not valid JSON -> a clean invalid_output
    envelope, never a crash/traceback."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        _c, o1 = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                      project_dir=project_dir))
        _write_outputs_from_envelope(json.loads(o1), ["this is not json {{{"])
        code, out = _run_main(_resume_argv(runtime_dir, state_path,
                                           journal_path, project_dir=project_dir))
    envelope = json.loads(out)
    assert envelope["status"] == "invalid_output", envelope
    assert envelope.get("reason"), envelope
    assert code == 1, code


def test_resume_missing_output_file_emits_invalid_output_json():
    """A --resume with NO output file written -> invalid_output naming the
    missing path (a missing write surfaces as invalid_output, never a crash)."""
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    with _stub_pull_source():
        _c, o1 = _run_main(_step_argv(runtime_dir, state_path, journal_path,
                                      project_dir=project_dir))
        out_path = json.loads(o1)["dispatches"][0]["output_path"]
        # No file written: resume reads the missing output_path -> invalid_output.
        code, out = _run_main(_resume_argv(runtime_dir, state_path,
                                           journal_path, project_dir=project_dir))
    envelope = json.loads(out)
    assert envelope["status"] == "invalid_output", envelope
    assert out_path in envelope.get("reason", ""), envelope
    assert code == 1, code


# ==========================================================================
# Behaviour E — bare run_tick.py (no args) still prints the human trace.
# ==========================================================================

def test_bare_main_no_args_prints_human_trace_regression():
    _root, runtime_dir, state_path, journal_path = _temp_runtime()
    # Bare mode honors the same path flags but emits the HUMAN trace (not JSON);
    # the no-flag behavior is unchanged — one tick, the one-line trace on stdout.
    argv = ["--runtime-dir", runtime_dir, "--state", state_path,
            "--journal", journal_path]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, code
    # The human one-line trace is printed verbatim on stdout (not wrapped in JSON).
    assert "[tick] path=GUARD->DRAIN->PULL->PERSIST->EXIT->DONE" in out, out
    assert "work_items=2" in out, out
    assert "signal=idle" in out, out
    # It is NOT a JSON envelope.
    try:
        json.loads(out.strip())
        raised = False
    except ValueError:
        raised = True
    assert raised, "bare mode must print the human trace, not JSON"
