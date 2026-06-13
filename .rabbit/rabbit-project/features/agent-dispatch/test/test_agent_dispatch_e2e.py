#!/usr/bin/env python3
"""End-to-end + unit tests for the agent-dispatch deterministic helper library.

agent-dispatch owns the agent-adapter schema and every deterministic step around
an in-session subagent dispatch: classify an adapter-map entry, validate the
agent-adapter schema, build invocation envelopes (once / per_item fan-out) that
carry a deterministic per-dispatch output_path, render an envelope to a
structured-markdown prompt whose `## Handoff` section is the SELF-CONTAINED
contract (embedded schema + write-to-file + one-line ack), validate the JSON
CONTENT a subagent wrote to its output file against the target schema, collect
dispatch outputs into the target slot value, and compute the closed-vocabulary
route signal.

It dispatches NOTHING and writes NOTHING: it only computes envelopes / paths /
strings and validates content passed to it. The output_path is a computed string;
the FILE is written by the subagent and read by the executor (run_tick), never by
this library. The tests assert that surface AND the determinism / closed-
vocabulary invariants.

The e2e tests drive the helpers exactly as the (deferred) executor will — a full
adapter -> envelopes -> render -> validate_output -> collect_outputs ->
compute_signal pass over a realistic per_item fan-out adapter.

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
    """A valid once adapter: a single dispatch over the whole input. No
    output_schema -> schema falls back to a coarse {"type": ...}."""
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


_OUT_DIR = "/tmp/rabbit-tick-outputs"


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
# Behaviour: validate_agent_adapter — output_schema is OPTIONAL. An entry WITH
# output_schema validates; an entry WITHOUT it still validates.
# ==========================================================================

def test_validate_accepts_entry_with_output_schema():
    a = _per_item_adapter()
    assert "output_schema" in a["dispatch"][0]
    ad.validate_agent_adapter(a)  # must not raise


def test_validate_accepts_entry_without_output_schema():
    a = _once_adapter()
    assert "output_schema" not in a["dispatch"][0]
    ad.validate_agent_adapter(a)  # must not raise


def test_validate_accepts_output_schema_as_example_shape():
    # output_schema may be a concrete example shape, not just a schema dict.
    a = _once_adapter()
    a["dispatch"][0]["output_schema"] = [{"id": "x"}]
    ad.validate_agent_adapter(a)  # must not raise


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
# entry with correct inputs / output_contract / context, a deterministic
# output_path == <output_dir>/<state>-0-0.json, and NO `item` key. An entry
# WITHOUT output_schema gets a coarse {"type": ...} fallback schema.
# ==========================================================================

def test_build_envelopes_once_single_envelope_no_item():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1"}, {"id": "wo-2"}]}
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)

    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["state"] == "TRIAGE"
    assert env["task"] == "Summarize the orders."
    assert env["inputs"] == {"work_orders": slot_values["work_orders"]}
    assert "item" not in env
    oc = env["output_contract"]
    assert oc["slot"] == "summary"
    # No output_schema on the entry -> coarse fallback {"type": ...}.
    assert isinstance(oc["schema"], dict)
    assert "type" in oc["schema"]
    assert oc["output_path"] == os.path.join(_OUT_DIR, "TRIAGE-0-0.json")
    assert env["context"] == {"tick_id": "tick-42", "mode": "live"}


def test_build_envelopes_once_empty_task_when_absent():
    adapter = _once_adapter()
    del adapter["dispatch"][0]["task"]
    envelopes = ad.build_envelopes(
        adapter, {"work_orders": []}, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)
    assert envelopes[0]["task"] == ""


def test_build_envelopes_once_no_output_schema_coarse_fallback():
    # work_orders -> writes "summary"; with no output_schema the fallback is a
    # coarse {"type": ...}; still a valid dict the schema block can render.
    adapter = _once_adapter()
    envelopes = ad.build_envelopes(
        adapter, {"work_orders": []}, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)
    schema = envelopes[0]["output_contract"]["schema"]
    assert schema == {"type": schema["type"]}
    assert schema["type"] in ("array", "object", "string", "number",
                              "boolean", "null")


# ==========================================================================
# E2E Behaviour: build_envelopes — `{per_item: path}` fans out one envelope
# per element of the resolved collection, each carrying its `item`, in order,
# with a per-item output_path <state>-0-0.json / -0-1 / -0-2 and the entry's
# explicit output_schema carried verbatim as output_contract.schema.
# ==========================================================================

def test_build_envelopes_per_item_fans_out_in_order_with_item():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-3", "wo-1", "wo-2"]},
        "policy": {"max_retries": 2},
    }
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)

    assert len(envelopes) == 3
    assert [e["item"] for e in envelopes] == ["wo-3", "wo-1", "wo-2"]
    expected_paths = [
        os.path.join(_OUT_DIR, "IMPLEMENT-0-0.json"),
        os.path.join(_OUT_DIR, "IMPLEMENT-0-1.json"),
        os.path.join(_OUT_DIR, "IMPLEMENT-0-2.json"),
    ]
    assert [e["output_contract"]["output_path"] for e in envelopes] \
        == expected_paths
    for env in envelopes:
        assert env["state"] == "IMPLEMENT"
        assert env["inputs"] == {
            "execution_plan": slot_values["execution_plan"],
            "policy": slot_values["policy"],
        }
        # output_schema present -> schema is that schema verbatim.
        assert env["output_contract"]["slot"] == "implement_results"
        assert env["output_contract"]["schema"] == {"type": "object"}
        assert env["context"] == {"tick_id": "tick-42", "mode": "live"}


def test_build_envelopes_per_item_empty_collection_yields_no_envelopes():
    adapter = _per_item_adapter()
    slot_values = {"execution_plan": {"ordered": []}, "policy": {}}
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)
    assert envelopes == []


def test_build_envelopes_output_paths_are_unique_per_dispatch():
    # Two dispatch entries, each `once`, must get distinct dispatch_index in
    # the output_path so files never collide.
    adapter = _once_adapter()
    adapter["dispatch"].append({
        "subagent_type": "rabbit-other",
        "task": "Other.",
        "inputs": ["work_orders"],
        "cardinality": "once",
        "writes": "summary",
    })
    envelopes = ad.build_envelopes(
        adapter, {"work_orders": []}, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)
    paths = [e["output_contract"]["output_path"] for e in envelopes]
    assert paths == [
        os.path.join(_OUT_DIR, "TRIAGE-0-0.json"),
        os.path.join(_OUT_DIR, "TRIAGE-1-0.json"),
    ]
    assert len(set(paths)) == len(paths)


# ==========================================================================
# E2E Behaviour: render — structured markdown with Task / Inputs / Handoff
# sections; header carries state + mode + tick_id; inputs contain NO raw JSON;
# free-text body fenced/block-quoted. The `## Handoff` section is the SELF-
# CONTAINED contract: the embedded schema (pretty JSON of the shape), the
# exact output_path, the "use your file-writing tool" instruction, and the
# one-line-ack / "do not include the JSON" instruction. Determinism holds.
# ==========================================================================

def test_render_contains_sections_and_header():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1", "title": "Fix the bug"}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)

    assert "TRIAGE" in text
    assert "tick-42" in text
    assert "live" in text
    assert "## Task" in text
    assert "Summarize the orders." in text
    assert "## Inputs" in text
    assert "## Handoff" in text
    assert "## Return" not in text  # old section name is gone


def test_render_inputs_have_no_raw_json():
    adapter = _once_adapter()
    slot_values = {"work_orders": [{"id": "wo-1", "title": "Fix the bug"}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)

    # Split out the Handoff section (which legitimately shows the schema as
    # JSON) and assert the Inputs section carries no json-object dump.
    inputs_section = text.split("## Handoff")[0]
    assert '{"' not in inputs_section
    assert '":' not in inputs_section.split("## Inputs")[1]


def test_render_free_text_body_is_fenced_or_blockquoted():
    adapter = _once_adapter()
    long_body = "Line one of the issue.\nLine two with ## a heading-looking line."
    slot_values = {"work_orders": [{"id": "wo-1", "body": long_body}]}
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)

    # The free-text body must be fenced (```) or block-quoted (> ) so its own
    # markdown cannot break the layout. The raw heading line must not appear as
    # a bare top-level line.
    fenced = "```" in text
    blockquoted = "> Line two" in text or "> Line one" in text
    assert fenced or blockquoted


def test_render_handoff_embeds_schema_path_write_and_ack():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-1"]},
        "policy": {},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)
    handoff = text.split("## Handoff")[1]

    # 1. The embedded schema — pretty JSON of the actual shape.
    schema = env["output_contract"]["schema"]
    pretty = json.dumps(schema, indent=2, sort_keys=True)
    assert pretty in handoff

    # 2. write-to-file: the exact output_path + a file-writing-tool instruction.
    path = env["output_contract"]["output_path"]
    assert path in handoff
    assert "file-writing tool" in handoff
    assert "Write a single JSON value" in handoff

    # 3. ack: one-line acknowledgement, do NOT include the JSON in the reply.
    assert "one-line acknowledgement" in handoff
    assert "Do NOT include the JSON" in handoff


def test_render_handoff_schema_fallback_shape_when_no_output_schema():
    # A once adapter with no output_schema -> the coarse {"type": ...} fallback
    # is the embedded schema shown in `## Handoff`.
    adapter = _once_adapter()
    env = ad.build_envelopes(
        adapter, {"work_orders": []}, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)
    handoff = text.split("## Handoff")[1]
    schema = env["output_contract"]["schema"]
    pretty = json.dumps(schema, indent=2, sort_keys=True)
    assert pretty in handoff


def test_render_renders_item_when_present():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-99"]},
        "policy": {},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)[0]
    text = ad.render(env)
    assert "wo-99" in text


def test_render_is_deterministic_byte_identical():
    adapter = _per_item_adapter()
    slot_values = {
        "execution_plan": {"ordered": ["wo-1"]},
        "policy": {"a": 1, "b": 2},
    }
    env = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)[0]
    assert ad.render(env) == ad.render(env)


# ==========================================================================
# E2E Behaviour: validate_output — good JSON content matching the declared
# top-level type returns (True, parsed); code fences are tolerated; wrong type
# or unparseable returns (False, reason) WITHOUT raising. A rich EXAMPLE schema
# (list / dict) derives the expected top-level type (array / object).
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


def test_validate_output_example_schema_list_means_array():
    # A rich example shape (a list) -> expected top-level type is array.
    ok, parsed = ad.validate_output('[{"id": "x"}]', [{"id": "example"}])
    assert ok is True
    assert parsed == [{"id": "x"}]
    ok2, err = ad.validate_output('{"a": 1}', [{"id": "example"}])
    assert ok2 is False
    assert isinstance(err, str)


def test_validate_output_example_schema_dict_means_object():
    # A rich example shape (a dict) -> expected top-level type is object.
    ok, parsed = ad.validate_output('{"id": "x"}', {"id": "example"})
    assert ok is True
    assert parsed == {"id": "x"}
    ok2, err = ad.validate_output('[1, 2]', {"id": "example"})
    assert ok2 is False
    assert isinstance(err, str)


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
# adapter -> build_envelopes -> render -> validate_output (of file content) ->
# collect_outputs -> compute_signal, over a per_item fan-out. End-to-end wiring
# of the helpers, including the self-contained `## Handoff` contract.
# ==========================================================================

def test_e2e_full_per_item_pipeline_blocked():
    adapter = _per_item_adapter()
    ad.validate_agent_adapter(adapter)

    slot_values = {
        "execution_plan": {"ordered": ["wo-1", "wo-2"]},
        "policy": {"max_retries": 1},
    }
    envelopes = ad.build_envelopes(
        adapter, slot_values, _tick_context(), state="IMPLEMENT",
        output_dir=_OUT_DIR)
    assert len(envelopes) == 2

    # Each envelope renders to a deterministic, self-contained Handoff prompt
    # carrying its own output_path.
    for env in envelopes:
        prompt = ad.render(env)
        assert "## Handoff" in prompt
        assert env["output_contract"]["output_path"] in prompt

    # Simulate the JSON CONTENT two subagents wrote to their output files (one
    # blocked), validated against the schema.
    file_contents = [
        '{"id": "wo-1", "status": "done"}',
        '```json\n{"id": "wo-2", "status": "blocked"}\n```',
    ]
    schema = adapter["dispatch"][0]["output_schema"]
    parsed = []
    for content in file_contents:
        ok, val = ad.validate_output(content, schema)
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
        adapter, slot_values, _tick_context(), state="TRIAGE",
        output_dir=_OUT_DIR)
    assert len(envelopes) == 1

    # No output_schema on this entry -> the coarse {"type": "array"} fallback
    # is the embedded schema; the subagent's file content must match it.
    schema = envelopes[0]["output_contract"]["schema"]
    assert schema == {"type": "array"}
    ok, parsed = ad.validate_output('[{"text": "a summary"}]', schema)
    assert ok is True

    slot_value = ad.collect_outputs(adapter["dispatch"][0], [parsed])
    assert slot_value == parsed

    signal = ad.compute_signal(adapter["signal"]["rule"], slot_value)
    assert signal == "OK"


# ==========================================================================
# Invariant: the module is effect-free at runtime — it dispatches no Agent,
# calls no model/network, and does not actually touch the filesystem. `os` is
# permitted: os.path.join is pure string manipulation (it computes output_path
# strings; the FILE is written by the subagent, not this library). The forbidden
# set is the genuinely effectful stdlib.
# ==========================================================================

def test_module_imports_no_effectful_modules():
    src_path = os.path.join(_SRC, "agent_dispatch.py")
    with open(src_path) as f:
        source = f.read()
    forbidden = ["import subprocess", "import socket", "import random",
                 "import time", "import urllib", "import requests"]
    for token in forbidden:
        assert token not in source, f"forbidden import found: {token!r}"
    # The library must not actually read/write/dispatch even though it imports
    # os for os.path.join — assert no effectful os calls appear.
    for effectful in ["os.system", "os.popen", "open(", "os.remove",
                      "os.makedirs", "os.mkdir", "os.write", "os.read"]:
        assert effectful not in source, \
            f"effectful os usage found: {effectful!r}"


def test_module_exposes_public_surface():
    for name in ("AGENT_ADAPTER_SCHEMA_VERSION", "is_agent_entry",
                 "validate_agent_adapter", "build_envelopes", "render",
                 "validate_output", "collect_outputs", "compute_signal"):
        assert hasattr(ad, name), f"missing public symbol: {name}"
