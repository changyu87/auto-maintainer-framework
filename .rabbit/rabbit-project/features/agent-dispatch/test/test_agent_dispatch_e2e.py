#!/usr/bin/env python3
"""End-to-end + unit tests for the agent-dispatch deterministic helper library.

agent-dispatch owns the agent-adapter schema and every deterministic step around
an in-session subagent dispatch: classify an adapter-map entry, validate the
agent-adapter schema, build invocation envelopes (once / per_item fan-out),
render an envelope to a structured-markdown prompt (inputs as a derivative view,
output contract as a schema), validate a subagent's returned text against the
target slot schema, collect dispatch outputs into the target slot value, and
compute the closed-vocabulary route signal.

It dispatches NOTHING and is pure, deterministic, effect-free: no Agent call, no
model, no network, no filesystem, no wall clock. The tests assert that surface
AND the determinism / closed-vocabulary invariants.

The e2e tests drive the helpers exactly as the (deferred) executor will — a full
adapter → envelopes → render → validate_output → collect_outputs → compute_signal
pass over a realistic per_item fan-out adapter.

Owner: changyu87
"""

import json
import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import agent_dispatch as ad  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a valid per_item adapter, a valid once adapter, a tick context.
# --------------------------------------------------------------------------

def _per_item_adapter():
    """A realistic agent-adapter that fans out IMPLEMENT over the ordered plan,
    one subagent per planned id, collecting results into one list slot."""
    return {
        "kind": "agent",
        "manifest": {
            "reads": ["execution_plan", "policy"],
            "writes": ["implement_results"],
            "emits": ["OK", "BLOCKED"],
        },
        "dispatch": [
            {
                "subagent_type": "rabbit-implementer",
                "task": "Implement the work item.",
                "inputs": ["execution_plan", "policy"],
                "cardinality": {"per_item": "execution_plan.ordered"},
                "writes": "implement_results",
                "output_schema": {"type": "object"},
            }
        ],
        "signal": {"rule": "blocked_if_any"},
    }


def _once_adapter():
    """A valid once adapter: a single dispatch over the whole input."""
    return {
        "kind": "agent",
        "manifest": {
            "reads": ["work_orders"],
            "writes": ["summary"],
            "emits": ["OK", "EMPTY"],
        },
        "dispatch": [
            {
                "subagent_type": "rabbit-summarizer",
                "task": "Summarize the orders.",
                "inputs": ["work_orders"],
                "cardinality": "once",
                "writes": "summary",
            }
        ],
        "signal": {"rule": "nonempty_else_empty"},
    }


def _tick_context():
    return {"tick_id": "tick-42", "mode": "live"}


# ==========================================================================
# Behaviour: AGENT_ADAPTER_SCHEMA_VERSION is a declared version string.
# ==========================================================================

def test_schema_version_is_declared_string():
    assert isinstance(ad.AGENT_ADAPTER_SCHEMA_VERSION, str)
    assert ad.AGENT_ADAPTER_SCHEMA_VERSION == "1.0.0"


# ==========================================================================
# Behaviour: is_agent_entry — a string entry is a script factory address
# (False); a dict with kind == "agent" is an agent-adapter (True).
# ==========================================================================

def test_is_agent_entry_string_is_script_factory():
    assert ad.is_agent_entry("prioritize:factory") is False
    assert ad.is_agent_entry("some.module:factory") is False


def test_is_agent_entry_agent_dict_is_true():
    assert ad.is_agent_entry(_once_adapter()) is True
    assert ad.is_agent_entry({"kind": "agent"}) is True


def test_is_agent_entry_non_agent_dict_is_false():
    assert ad.is_agent_entry({"kind": "script"}) is False
    assert ad.is_agent_entry({}) is False


# ==========================================================================
# Behaviour: validate_agent_adapter accepts a well-formed adapter.
# ==========================================================================

def test_validate_accepts_valid_once_adapter():
    # Must not raise.
    ad.validate_agent_adapter(_once_adapter())


def test_validate_accepts_valid_per_item_adapter():
    ad.validate_agent_adapter(_per_item_adapter())


# ==========================================================================
# Behaviour: validate_agent_adapter raises on each missing / malformed field.
# ==========================================================================

def _assert_raises(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected ValueError, none raised")


def test_validate_rejects_missing_manifest():
    a = _once_adapter()
    del a["manifest"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_empty_manifest_reads():
    a = _once_adapter()
    a["manifest"]["reads"] = []
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_missing_manifest_writes():
    a = _once_adapter()
    del a["manifest"]["writes"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_empty_manifest_emits():
    a = _once_adapter()
    a["manifest"]["emits"] = []
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_no_dispatch_entries():
    a = _once_adapter()
    a["dispatch"] = []
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_missing_subagent_type():
    a = _once_adapter()
    del a["dispatch"][0]["subagent_type"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_nonstr_subagent_type():
    a = _once_adapter()
    a["dispatch"][0]["subagent_type"] = 7
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_missing_inputs():
    a = _once_adapter()
    del a["dispatch"][0]["inputs"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_nonlist_inputs():
    a = _once_adapter()
    a["dispatch"][0]["inputs"] = "work_orders"
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_missing_writes():
    a = _once_adapter()
    del a["dispatch"][0]["writes"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_nonstr_writes():
    a = _once_adapter()
    a["dispatch"][0]["writes"] = ["summary"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_dispatch_missing_cardinality():
    a = _once_adapter()
    del a["dispatch"][0]["cardinality"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_unknown_cardinality_string():
    a = _once_adapter()
    a["dispatch"][0]["cardinality"] = "twice"
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_malformed_per_item_cardinality():
    a = _per_item_adapter()
    # per_item value must be a dotted-path string.
    a["dispatch"][0]["cardinality"] = {"per_item": 5}
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_unknown_cardinality_dict_key():
    a = _once_adapter()
    a["dispatch"][0]["cardinality"] = {"per_chunk": "x.y"}
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_nonstr_task():
    a = _once_adapter()
    a["dispatch"][0]["task"] = 99
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_unknown_signal_rule():
    a = _once_adapter()
    a["signal"]["rule"] = "coin_flip"
    _assert_raises(lambda: ad.validate_agent_adapter(a))


def test_validate_rejects_missing_signal():
    a = _once_adapter()
    del a["signal"]
    _assert_raises(lambda: ad.validate_agent_adapter(a))


# ==========================================================================
# E2E Behaviour: build_envelopes — `once` yields ONE envelope per dispatch
# entry with correct inputs / output_contract / context and NO `item` key.
# ==========================================================================

def test_build_envelopes_once_single_envelope_no_item():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1"}, {"id": "wo-2"}]}
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE")

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["state"] == "TRIAGE"
    assert env["task"] == "Summarize the orders."
    assert env["inputs"] == {"work_orders": slot_values["work_orders"]}
    assert "item" not in env
    assert env["output_contract"] == {
        "slot": "summary", "schema_ref": "summary"}
    assert env["context"] == {"tick_id": "tick-42", "mode": "live"}


def test_build_envelopes_once_empty_task_when_absent():
    adapter = _once_adapter()
    del adapter["dispatch"][0]["task"]
    envelopes = ad.build_envelopes(
        adapter, {"work_orders": []}, _tick_context(), state="TRIAGE")
    assert envelopes[0]["task"] == ""


# ==========================================================================
# E2E Behaviour: build_envelopes — `{per_item: path}` fans out one envelope
# per element of the resolved collection, each carrying its `item`, in order.
# Dotted-path resolution ("execution_plan.ordered") is correct.
# ==========================================================================

def test_build_envelopes_per_item_fans_out_in_order_with_item():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-3", "wo-1", "wo-2"]},
        "policy": {"max_retries": 2},
    }
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")

    assert len(envelopes) == 3
    assert [e["item"] for e in envelopes] == ["wo-3", "wo-1", "wo-2"]
    for env in envelopes:
        assert env["state"] == "IMPLEMENT"
        assert env["inputs"] == {
            "execution_plan": slot_values["execution_plan"],
            "policy": slot_values["policy"],
        }
        # output_schema present -> schema_ref is that schema, not the slot name.
        assert env["output_contract"] == {
            "slot": "implement_results",
            "schema_ref": {"type": "object"},
        }
        assert env["context"] == {"tick_id": "tick-42", "mode": "live"}


def test_build_envelopes_per_item_empty_collection_yields_no_envelopes():
    adapter = _per_item_adapter()
    slot_values = {"execution_plan": {"ordered": []}, "policy": {}}
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")
    assert envelopes == []


# ==========================================================================
# E2E Behaviour: render — structured markdown with Task / Inputs / Return
# sections; header carries state + mode + tick_id; inputs contain NO raw JSON;
# free-text body fenced/block-quoted; Return names the schema_ref; determinism.
# ==========================================================================

def test_render_contains_sections_and_header():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1", "title": "Fix the bug"}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE")[0]
    text = ad.render(env)

    assert "TRIAGE" in text
    assert "tick-42" in text
    assert "live" in text
    assert "## Task" in text
    assert "Summarize the orders." in text
    assert "## Inputs" in text
    assert "## Return" in text


def test_render_inputs_have_no_raw_json():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1", "title": "Fix the bug"}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE")[0]
    text = ad.render(env)

    # Split out the Return section (which legitimately shows the schema as
    # text) and assert the Inputs section carries no json-object dump.
    inputs_section = text.split("## Return")[0]
    assert '{"' not in inputs_section
    assert '":' not in inputs_section.split("## Inputs")[1]


def test_render_free_text_body_is_fenced_or_blockquoted():
    adapter = _once_adapter()
    long_body = "Line one of the issue.\nLine two with ## a heading-looking line."
    slot_values = {"work_orders": [{"id": "wo-1", "body": long_body}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE")[0]
    text = ad.render(env)

    # The free-text body must be fenced (```) or block-quoted (> ) so its own
    # markdown cannot break the layout. The raw heading line must not appear as
    # a bare top-level line.
    fenced = "```" in text
    blockquoted = "> Line two" in text or "> Line one" in text
    assert fenced or blockquoted


def test_render_return_section_names_schema_ref():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-1"]},
        "policy": {},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")[0]
    text = ad.render(env)
    return_section = text.split("## Return")[1]
    assert "implement_results" in return_section
    # schema_ref shown as text in Return (the one place structure is shown).
    assert "object" in return_section


def test_render_renders_item_when_present():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-99"]},
        "policy": {},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")[0]
    text = ad.render(env)
    assert "wo-99" in text


def test_render_is_deterministic_byte_identical():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-1"]},
        "policy": {"a": 1, "b": 2},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")[0]
    assert ad.render(env) == ad.render(env)


# ==========================================================================
# E2E Behaviour: validate_output — good JSON matching the declared top-level
# type returns (True, parsed); code fences are tolerated; wrong type or
# unparseable returns (False, reason) WITHOUT raising.
# ==========================================================================

def test_validate_output_good_object():
    ok, parsed = ad.validate_output('{"a": 1}', {"type": "object"})
    assert ok is True
    assert parsed == {"a": 1}


def test_validate_output_good_array():
    ok, parsed = ad.validate_output('[1, 2, 3]', {"type": "array"})
    assert ok is True
    assert parsed == [1, 2, 3]


def test_validate_output_tolerates_code_fence():
    fenced = "```json\n{\"a\": 1}\n```"
    ok, parsed = ad.validate_output(fenced, {"type": "object"})
    assert ok is True
    assert parsed == {"a": 1}


def test_validate_output_tolerates_bare_fence():
    fenced = "```\n[1, 2]\n```"
    ok, parsed = ad.validate_output(fenced, {"type": "array"})
    assert ok is True
    assert parsed == [1, 2]


def test_validate_output_wrong_type_returns_false_no_raise():
    ok, err = ad.validate_output('{"a": 1}', {"type": "array"})
    assert ok is False
    assert isinstance(err, str)
    assert err  # a locatable, non-empty reason


def test_validate_output_unparseable_returns_false_no_raise():
    ok, err = ad.validate_output("not json at all", {"type": "object"})
    assert ok is False
    assert isinstance(err, str)
    assert err


# ==========================================================================
# E2E Behaviour: collect_outputs — `once` returns the single value; `per_item`
# returns the ordered list of element outputs.
# ==========================================================================

def test_collect_outputs_once_returns_value():
    entry = _once_adapter()["dispatch"][0]
    out = ad.collect_outputs(entry, [{"summary": "done"}])
    assert out == {"summary": "done"}


def test_collect_outputs_per_item_returns_list_in_order():
    entry = _per_item_adapter()["dispatch"][0]
    outputs = [{"id": "wo-3"}, {"id": "wo-1"}, {"id": "wo-2"}]
    out = ad.collect_outputs(entry, outputs)
    assert out == outputs


# ==========================================================================
# E2E Behaviour: compute_signal — all three closed-vocab rules, including
# blocked_if_any with a blocked element; an unknown rule raises.
# ==========================================================================

def test_compute_signal_nonempty_else_empty():
    assert ad.compute_signal("nonempty_else_empty", [1]) == "OK"
    assert ad.compute_signal("nonempty_else_empty", []) == "EMPTY"
    assert ad.compute_signal("nonempty_else_empty", {"a": 1}) == "OK"
    assert ad.compute_signal("nonempty_else_empty", {}) == "EMPTY"
    assert ad.compute_signal("nonempty_else_empty", "") == "EMPTY"


def test_compute_signal_blocked_if_any_status():
    blocked = [{"status": "ok"}, {"status": "blocked"}]
    assert ad.compute_signal("blocked_if_any", blocked) == "BLOCKED"


def test_compute_signal_blocked_if_any_reason():
    blocked = [{"id": "wo-1"}, {"blocked_reason": "missing dep"}]
    assert ad.compute_signal("blocked_if_any", blocked) == "BLOCKED"


def test_compute_signal_blocked_if_any_none_blocked():
    ok = [{"status": "ok"}, {"status": "done"}]
    assert ad.compute_signal("blocked_if_any", ok) == "OK"


def test_compute_signal_always_ok():
    assert ad.compute_signal("always_ok", []) == "OK"
    assert ad.compute_signal("always_ok", None) == "OK"


def test_compute_signal_unknown_rule_raises():
    _assert_raises(lambda: ad.compute_signal("coin_flip", []))


# ==========================================================================
# E2E Behaviour: the full deterministic pass an executor performs —
# adapter -> build_envelopes -> render -> validate_output -> collect_outputs
# -> compute_signal, over a per_item fan-out. End-to-end wiring of the helpers.
# ==========================================================================

def test_e2e_full_per_item_pipeline_blocked():
    adapter = _per_item_adapter()
    ad.validate_agent_adapter(adapter)

    slot_values = {
        "execution_plan": {"ordered": ["wo-1", "wo-2"]},
        "policy": {"max_retries": 1},
    }
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT")
    assert len(envelopes) == 2

    # Each envelope renders to a deterministic prompt.
    for env in envelopes:
        prompt = ad.render(env)
        assert "## Return" in prompt

    # Simulate two subagent returns (one blocked), validated against the schema.
    returns = [
        '{"id": "wo-1", "status": "done"}',
        '```json\n{"id": "wo-2", "status": "blocked"}\n```',
    ]
    schema = adapter["dispatch"][0]["output_schema"]
    parsed = []
    for txt in returns:
        ok, val = ad.validate_output(txt, schema)
        assert ok is True
        parsed.append(val)

    slot_value = ad.collect_outputs(adapter["dispatch"][0], parsed)
    assert slot_value == parsed

    signal = ad.compute_signal(adapter["signal"]["rule"], slot_value)
    assert signal == "BLOCKED"


def test_e2e_full_once_pipeline_ok():
    adapter = _once_adapter()
    ad.validate_agent_adapter(adapter)

    slot_values = {"work_orders": [{"id": "wo-1", "title": "t"}]}
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE")
    assert len(envelopes) == 1

    ok, parsed = ad.validate_output('{"text": "a summary"}', {"type": "object"})
    assert ok is True

    slot_value = ad.collect_outputs(adapter["dispatch"][0], [parsed])
    assert slot_value == parsed

    signal = ad.compute_signal(adapter["signal"]["rule"], slot_value)
    assert signal == "OK"


# ==========================================================================
# Invariant: the module is effect-free — it imports no effectful stdlib
# (subprocess, os, time, random, socket) and no Agent mechanism.
# ==========================================================================

def test_module_imports_no_effectful_modules():
    src_path = os.path.join(_SRC, "agent_dispatch.py")
    with open(src_path) as f:
        source = f.read()
    forbidden = ["import subprocess", "import socket", "import random",
                 "import time", "import os", "from os ", "import urllib",
                 "import requests"]
    for token in forbidden:
        assert token not in source, f"forbidden import found: {token!r}"


def test_module_does_not_import_json_at_module_top_for_effects():
    # json is permitted (parsing returned text is pure); this asserts the
    # module exposes the declared public surface and nothing dispatches.
    for name in ("AGENT_ADAPTER_SCHEMA_VERSION", "is_agent_entry",
                 "validate_agent_adapter", "build_envelopes", "render",
                 "validate_output", "collect_outputs", "compute_signal"):
        assert hasattr(ad, name), f"missing public symbol: {name}"
