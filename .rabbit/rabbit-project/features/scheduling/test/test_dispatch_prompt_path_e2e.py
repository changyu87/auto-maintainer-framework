#!/usr/bin/env python3
"""End-to-end tests for FILE-REFERENCED dispatch prompts (prompt_path).

The pause used to emit each dispatch's fully-rendered invocation envelope INLINE
as dispatches[].prompt (ad.render(env)) in the --step stdout JSON. For a large
envelope the Bash tool truncates stdout, forcing the orchestrator to RE-READ the
whole output to relay the prompt verbatim — pulling the entire envelope into its
own context. Subagent OUTPUTS are already file-based (the runner writes/reads
files, keeping the orchestrator's context clean); the INPUT (the prompt) must be
symmetric.

This slice delivers each dispatch's rendered envelope by FILE REFERENCE:

  - _pause_result WRITES the rendered envelope (ad.render(env)) to a deterministic
    ABSOLUTE file under output_dir, named parallel to output_path:
    `<state>-<di>-<ii>.json` -> `<state>-<di>-<ii>.prompt.md`. The dispatch rec
    carries `prompt_path` and NO inline `prompt`.
  - The --step/--resume JSON CLI dispatch entries carry prompt_path (a small
    path) and NOT the multi-KB inline prompt, so --step stdout stays small.
  - The prompt file is written as part of the PAUSE (the same place stale output
    files are cleared), so a crash-safety re-emit (--step after invalid_output)
    reproduces the SAME prompt_path with BYTE-IDENTICAL content (rendered from the
    durable checkpoint round-trip, exactly like output_path).

Behaviours exercised (every one has an e2e test, per the E2E TEST RULE):

  A. _pause_result writes the rendered envelope to a deterministic absolute
     prompt_path under output_dir; the rec carries prompt_path (no inline prompt);
     the file content EQUALS ad.render(env).
  B. The --step JSON CLI dispatch entries carry prompt_path and do NOT carry the
     large inline prompt (the rendered envelope never appears in stdout).
  C. A crash-safety re-emit reproduces the same prompt_path with byte-identical
     file content.
  D. prompt_path is parallel to output_path (`.json` -> `.prompt.md`) and absolute.

scheduling CONSUMES adapter-wiring / work-intake / agent-dispatch UNCHANGED via
sys.path; it does NOT edit or fork them.

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

import work_intake as wi  # noqa: E402
import agent_dispatch as ad  # noqa: E402
import run_tick as rt  # noqa: E402


GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}],
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

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


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
    return amap


def _setup_agent_project():
    project_dir = tempfile.mkdtemp(prefix="sched-promptpath-")
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


def _first_pause():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    paused = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                         state_path=state_path, journal_path=journal_path,
                         source=_stub_source())
    return paused, runtime_dir, project_dir, state_path, journal_path


# ==========================================================================
# Behaviour A — _pause_result writes the rendered envelope to a deterministic
# absolute prompt_path; the rec carries prompt_path (no inline prompt).
# ==========================================================================

def test_pause_dispatch_carries_prompt_path_not_inline_prompt():
    paused, runtime_dir, *_ = _first_pause()
    assert paused["status"] == "paused", paused
    d = paused["dispatches"][0]
    # The rendered envelope is delivered by FILE REFERENCE.
    assert "prompt_path" in d, d
    assert "prompt" not in d, d
    # prompt_path is an ABSOLUTE file under ${runtime_dir}/dispatch-out/.
    assert os.path.isabs(d["prompt_path"]), d["prompt_path"]
    out_dir = os.path.join(runtime_dir, "dispatch-out")
    assert d["prompt_path"].startswith(out_dir), (d["prompt_path"], out_dir)
    # The prompt file actually exists on disk (written at the pause).
    assert os.path.isfile(d["prompt_path"]), d["prompt_path"]


def test_prompt_file_content_equals_rendered_envelope():
    """The file at prompt_path holds EXACTLY ad.render(env) — the same rendered
    markdown the inline prompt used to carry."""
    paused, _runtime_dir, _project_dir, state_path, _journal = _first_pause()
    d = paused["dispatches"][0]
    with open(d["prompt_path"]) as f:
        on_disk = f.read()
    # Rebuild the envelope exactly as _pause_result does (from the checkpoint's
    # restored slot snapshot + output_dir) and render it, then compare.
    cp = rt.persisted_tick_checkpoint(state_path)
    output_dir = cp["output_dir"]
    slots = cp["slots"]
    entry = _TRIAGE_AGENT
    slot_values = {"work_items": slots["work_items"]}
    envs = ad.build_envelopes(
        entry, slot_values, {"tick_id": cp["tick_id"], "mode": "propose"},
        state="TRIAGE", output_dir=output_dir)
    expected = ad.render(envs[0])
    assert on_disk == expected, (on_disk[:200], expected[:200])
    # And the rendered envelope names the dispatch's output_path.
    assert d["output_path"] in on_disk, on_disk


def test_prompt_path_is_parallel_to_output_path():
    """The naming is deterministic + parallel: output_path `<...>.json` yields the
    prompt file `<...>.prompt.md` in the SAME directory."""
    paused, *_ = _first_pause()
    d = paused["dispatches"][0]
    assert d["output_path"].endswith(".json"), d["output_path"]
    assert d["prompt_path"].endswith(".prompt.md"), d["prompt_path"]
    assert (d["prompt_path"]
            == d["output_path"][: -len(".json")] + ".prompt.md"), d


# ==========================================================================
# Behaviour B — the --step JSON CLI carries prompt_path, NOT the inline prompt;
# the rendered envelope never appears in stdout (no truncation hazard).
# ==========================================================================

@contextlib.contextmanager
def _stub_pull_source():
    saved = rt.DEFAULT_PULL_SOURCE
    rt.DEFAULT_PULL_SOURCE = _stub_source()
    try:
        yield
    finally:
        rt.DEFAULT_PULL_SOURCE = saved


def _run_main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rt.main(argv)
    return code, buf.getvalue()


def test_step_cli_dispatch_carries_prompt_path_and_no_inline_prompt():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    argv = ["--step", "--runtime-dir", runtime_dir, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, code
    envelope = json.loads(out)
    assert envelope["status"] == "paused", envelope
    d = envelope["dispatches"][0]
    assert "prompt_path" in d, d
    assert "prompt" not in d, d
    # The multi-KB rendered envelope text NEVER appears in stdout.
    assert "# Dispatch: TRIAGE" not in out, out
    assert "## Handoff" not in out, out
    # stdout is small: it carries the path, not the envelope body.
    with open(d["prompt_path"]) as f:
        rendered = f.read()
    assert len(out) < len(rendered), (len(out), len(rendered))


# ==========================================================================
# Behaviour C — crash-safety: a re-emit reproduces the same prompt_path with
# byte-identical file content (rendered from the durable checkpoint round-trip).
# ==========================================================================

def test_reemit_reproduces_same_prompt_path_byte_identical_content():
    project_dir, runtime_dir, state_path, journal_path = _setup_agent_project()
    first = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
    d1 = first["dispatches"][0]
    with open(d1["prompt_path"]) as f:
        content1 = f.read()
    # A fresh run_tick (no resume) finds the checkpoint and re-emits the SAME
    # paused dispatch — byte-identical prompt_path AND file content.
    again = rt.run_tick(project_dir=project_dir, runtime_dir=runtime_dir,
                        state_path=state_path, journal_path=journal_path,
                        source=_stub_source())
    d2 = again["dispatches"][0]
    assert d2["prompt_path"] == d1["prompt_path"], (d1, d2)
    with open(d2["prompt_path"]) as f:
        content2 = f.read()
    assert content2 == content1, (content1[:200], content2[:200])
