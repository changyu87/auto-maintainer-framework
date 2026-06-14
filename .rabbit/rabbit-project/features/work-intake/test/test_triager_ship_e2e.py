#!/usr/bin/env python3
"""End-to-end tests for shipping the auto-maintainer-triager subagent + wiring.

work-intake owns the TRIAGE domain and the WorkOrder schema, so it ships the
real triage JUDGE — `ship/agents/auto-maintainer-triager.md`. The build's
`ship/` collection copies `ship/agents/` -> the plugin's `agents/` with NO build
change, so the shipped file IS the deployed subagent definition.

These tests are CONTENT + WIRING level and fully deterministic — the subagent
itself is an LLM and is NOT invoked here:

  1. The shipped triager file exists and parses: frontmatter `name` is
     `auto-maintainer-triager`; `tools` includes `Write` plus the read-only
     `Read`/`Grep`/`Glob`; and the body is PROTOCOL-FREE (it bakes in NO JSON
     schema / output_path / `dispatch-result.json` / file-format details — those
     are carried by the prompt the agent-dispatch renderer produces).

  2. The TRIAGE -> triager AGENT-ADAPTER wiring VALIDATES via
     `adapter_wiring.build_loop`: a project-local adapter-map maps `TRIAGE` to an
     agent entry (subagent_type `auto-maintainer:auto-maintainer-triager`,
     reads `work_items`, writes `work_orders`, emits OK|EMPTY) over a real route
     `GUARD -> DRAIN -> PULL -> TRIAGE -> PERSIST -> EXIT`, and the validator
     ACCEPTS it (TRIAGE resolves to an AgentState; data-readiness satisfied
     because PULL writes the `work_items` TRIAGE reads).

The wiring imports adapter-wiring + agent-dispatch UNCHANGED (test-only imports);
nothing in work-intake's own source is touched by this cycle.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)

# Consume the wiring + dispatch mechanisms + fsm-contracts + tick-orchestrator
# UNCHANGED via sys.path (test-only imports; work-intake source is untouched).
for _dep in ("fsm-contracts", "tick-orchestrator", "agent-dispatch",
             "adapter-wiring"):
    _dep_src = os.path.join(_FEATURES_DIR, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import adapter_wiring as aw  # noqa: E402

_SHIP_AGENT = os.path.join(
    _FEATURE_DIR, "ship", "agents", "auto-maintainer-triager.md")


# --------------------------------------------------------------------------
# A tiny, dependency-free frontmatter splitter (no PyYAML; matches the
# no-third-party-deps constraint of run.py). Returns (frontmatter_text, body).
# --------------------------------------------------------------------------

def _split_frontmatter(text):
    assert text.startswith("---\n"), "file must open with a --- frontmatter fence"
    rest = text[len("---\n"):]
    end = rest.index("\n---\n")
    return rest[:end], rest[end + len("\n---\n"):]


def _frontmatter_value(fm_text, key):
    """Return the raw (string) value for a top-level `key:` line."""
    for line in fm_text.splitlines():
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip()
    raise KeyError(key)


# ==========================================================================
# Behaviour: the shipped triager file exists at ship/agents/ and parses.
# ==========================================================================

def test_triager_ship_file_exists():
    assert os.path.isfile(_SHIP_AGENT), (
        f"expected shipped triager at {_SHIP_AGENT}")


def test_triager_frontmatter_name_is_auto_maintainer_triager():
    with open(_SHIP_AGENT) as f:
        fm, _body = _split_frontmatter(f.read())
    assert _frontmatter_value(fm, "name") == "auto-maintainer-triager"


def test_triager_tools_include_write_and_readonly():
    with open(_SHIP_AGENT) as f:
        fm, _body = _split_frontmatter(f.read())
    tools_raw = _frontmatter_value(fm, "tools")
    # Frontmatter authors the tools as an inline list: [Read, Grep, Glob, Write].
    tools = {t.strip() for t in tools_raw.strip("[]").split(",")}
    assert "Write" in tools, f"triager must carry the Write tool, got {tools}"
    for ro in ("Read", "Grep", "Glob"):
        assert ro in tools, f"triager must carry read-only tool {ro}, got {tools}"


# ==========================================================================
# Behaviour: the body is PROTOCOL-FREE — it bakes in NO output schema /
# output_path / dispatch-result filename / file-format details. Those are
# carried by the agent-dispatch-rendered prompt, NOT the subagent definition.
# ==========================================================================

def test_triager_body_is_protocol_free():
    with open(_SHIP_AGENT) as f:
        _fm, body = _split_frontmatter(f.read())
    lowered = body.lower()
    # No baked-in machine protocol leaks: no dispatch-result filename, no
    # output_path / output_contract key, no schema_version, no JSON-Schema
    # descriptor tokens. The triager learns all of this from the prompt.
    forbidden = [
        "dispatch-result.json",
        "output_path",
        "output_contract",
        "schema_version",
        '"type": "array"',
        '"type":"array"',
    ]
    for token in forbidden:
        assert token not in lowered, (
            f"triager body must be protocol-free; found baked-in {token!r}")
    # And it does not embed a literal JSON object/array schema block of its own.
    assert "```json" not in lowered, (
        "triager body must not bake in a JSON schema/output block; the prompt "
        "carries the output example")


# ==========================================================================
# E2E Behaviour: the TRIAGE -> triager agent-adapter wiring VALIDATES.
# build_loop loads + resolves + validates a route GUARD -> DRAIN -> PULL ->
# TRIAGE -> PERSIST -> EXIT whose TRIAGE port is the triager agent entry. PULL
# writes `work_items`; TRIAGE (agent) reads `work_items`, writes `work_orders`;
# PERSIST reads `work_orders`. The validator must ACCEPT (TRIAGE resolves to an
# AgentState; data-readiness satisfied).
# ==========================================================================

# Script-factory stubs for the non-agent states. Each is "module:factory"
# addressable and returns (StateManifest, run). They carry no domain meaning;
# only their manifests participate in the LOAD-time validation.
_STUB_GUARD = '''
import fsm_contracts as fc
def make(runtime):
    manifest = fc.StateManifest(reads=[], writes=[], emits=["OK"])
    def run(ctx):
        return fc.StateResult(signal="OK", writes={})
    return manifest, run
'''

_STUB_DRAIN = '''
import fsm_contracts as fc
def make(runtime):
    manifest = fc.StateManifest(reads=[], writes=[], emits=["OK"])
    def run(ctx):
        return fc.StateResult(signal="OK", writes={})
    return manifest, run
'''

_STUB_PULL = '''
import fsm_contracts as fc
def make(runtime):
    manifest = fc.StateManifest(reads=[], writes=["work_items"], emits=["OK"])
    def run(ctx):
        return fc.StateResult(signal="OK", writes={"work_items": []})
    return manifest, run
'''

_STUB_PERSIST = '''
import fsm_contracts as fc
def make(runtime):
    manifest = fc.StateManifest(reads=["work_orders"], writes=[], emits=["DONE"])
    def run(ctx):
        ctx.read("work_orders")
        return fc.StateResult(signal="DONE", writes={})
    return manifest, run
'''

_STUB_EXIT = '''
import fsm_contracts as fc
def make(runtime):
    manifest = fc.StateManifest(reads=[], writes=[], emits=[])
    def run(ctx):  # pragma: no cover - terminal never executes
        raise AssertionError("terminal EXIT must never run()")
    return manifest, run
'''


def _write_stub_modules(dirpath):
    for name, body in (
        ("wi_stub_guard.py", _STUB_GUARD),
        ("wi_stub_drain.py", _STUB_DRAIN),
        ("wi_stub_pull.py", _STUB_PULL),
        ("wi_stub_persist.py", _STUB_PERSIST),
        ("wi_stub_exit.py", _STUB_EXIT),
    ):
        with open(os.path.join(dirpath, name), "w") as f:
            f.write(body)
    if dirpath not in sys.path:
        sys.path.insert(0, dirpath)


# The route the maintainer runs: a TRIAGE agent-state threaded after PULL.
_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "PERSIST", "EXIT"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "DONE", "next": "EXIT"},
    ],
    "terminal": ["EXIT"],
}


def _triager_agent_entry():
    """The TRIAGE port as the triager agent-adapter. The `output_example` is a
    CONCRETE bare-array example of WorkOrders (one accepted, one rejected) to
    mimic — NOT a JSON-Schema descriptor (agent-dispatch's
    validate_agent_adapter rejects descriptors, #119)."""
    return {
        "kind": "agent",
        "manifest": {
            "reads": ["work_items"],
            "writes": ["work_orders"],
            "emits": ["OK", "EMPTY"],
        },
        "dispatch": [
            {
                "subagent_type": "auto-maintainer:auto-maintainer-triager",
                "task": "Triage each work_item: accept genuine, actionable "
                        "maintenance tasks; reject spam/off-topic/malformed "
                        "with a specific reason.",
                "inputs": ["work_items"],
                "cardinality": "once",
                "writes": "work_orders",
                "output_example": [
                    {
                        "id": "wo-acme/widget#7",
                        "work_item_id": "acme/widget#7",
                        "title": "Crash on empty config",
                        "body": "Steps to reproduce ...",
                        "url": "https://github.com/acme/widget/issues/7",
                        "labels": ["bug"],
                        "decision": "accepted",
                        "reason": "",
                        "created_at": "2026-05-01T10:00:00Z",
                    },
                    {
                        "id": "wo-acme/widget#8",
                        "work_item_id": "acme/widget#8",
                        "title": "BUY CHEAP FOLLOWERS",
                        "body": "visit spam.example",
                        "url": "https://github.com/acme/widget/issues/8",
                        "labels": [],
                        "decision": "rejected",
                        "reason": "advertising spam, unrelated to this repo",
                        "created_at": "2026-05-02T10:00:00Z",
                    },
                ],
            }
        ],
        "signal": {"rule": "nonempty_else_empty"},
    }


def _adapter_map():
    return {
        "GUARD": "wi_stub_guard:make",
        "DRAIN": "wi_stub_drain:make",
        "PULL": "wi_stub_pull:make",
        "TRIAGE": _triager_agent_entry(),
        "PERSIST": "wi_stub_persist:make",
        "EXIT": "wi_stub_exit:make",
    }


def _runtime(project_dir):
    return {"project_dir": project_dir}


def test_triage_triager_wiring_validates_via_build_loop():
    """build_loop loads + resolves + validates the route whose TRIAGE port is the
    triager agent entry. The validator ACCEPTS: TRIAGE resolves to an AgentState,
    and data-readiness holds (PULL writes the `work_items` TRIAGE reads)."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        cfg = os.path.join(proj, ".auto-maintainer")
        os.makedirs(cfg)
        with open(os.path.join(cfg, "route.json"), "w") as f:
            json.dump(_ROUTE, f)
        with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
            json.dump(_adapter_map(), f)

        # Defaults are ignored: project-local route + map win.
        route, states = aw.build_loop(
            _ROUTE, _adapter_map(), _runtime(proj),
            start="GUARD", initial=[])

        assert route == _ROUTE
        assert set(states) == set(_ROUTE["states"])

        # TRIAGE resolved to an AgentState wired to the triager subagent.
        manifest, second = states["TRIAGE"]
        assert isinstance(second, aw.AgentState)
        assert list(manifest.reads) == ["work_items"]
        assert list(manifest.writes) == ["work_orders"]
        assert set(manifest.emits) == {"OK", "EMPTY"}
        assert (second.dispatch[0]["subagent_type"]
                == "auto-maintainer:auto-maintainer-triager")

        # The script states keep their run callables.
        assert callable(states["PULL"][1])
        assert callable(states["PERSIST"][1])


def test_triage_triager_wiring_validate_directly_accepts():
    """validate_wiring directly ACCEPTS the resolved manifests (signal-valid,
    data-ready, anchor-conforming) for the TRIAGE-agent route."""
    with tempfile.TemporaryDirectory() as proj:
        _write_stub_modules(proj)
        states = aw.resolve_states(_ROUTE, _adapter_map(), _runtime(proj))
        manifests = {name: m for name, (m, _s) in states.items()}
        res = aw.validate_wiring(_ROUTE, manifests, start="GUARD", initial=[])
        assert res.passed is True, res.messages
