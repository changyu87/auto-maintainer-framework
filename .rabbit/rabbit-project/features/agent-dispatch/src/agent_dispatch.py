#!/usr/bin/env python3
"""agent-dispatch — the deterministic helper library for the agent-adapter
mechanism (DESIGN §2.8, §3.4.6).

It owns the agent-adapter schema and every *deterministic* step around an
in-session subagent dispatch: classify an adapter-map entry, validate the
agent-adapter schema, build invocation envelope(s), render an envelope to a
structured-markdown prompt, validate the subagent's returned text against the
target slot schema, collect dispatch outputs into the target slot value, and
compute the closed-vocabulary route signal.

It dispatches NOTHING. Issuing the `Agent` call is the session's job (the
executor, DESIGN §2.8 — a later slice). Every function here is a PURE,
deterministic, effect-free function of its arguments: no `Agent` dispatch, no
model call, no network, no filesystem, no wall clock, no randomness. The
`tick_id` / `mode` carried in an envelope's `context` are passed in, not read.

Public surface:
  - AGENT_ADAPTER_SCHEMA_VERSION — the agent-adapter schema version string.
  - is_agent_entry(entry) -> bool — string entry = script factory (False);
    dict with kind == "agent" = agent-adapter (True).
  - validate_agent_adapter(entry) — raise ValueError on any schema violation.
  - build_envelopes(adapter, slot_values, tick_context, state) -> [envelope].
  - render(envelope) -> str — deterministic structured-markdown prompt.
  - validate_output(returned_text, slot_schema) -> (ok, parsed | error).
  - collect_outputs(adapter_entry, outputs) -> slot_value.
  - compute_signal(rule, slot_value) -> signal.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the agent-adapter schema or
  invocation-envelope reaches a breaking major version, or when subagent
  dispatch moves to a transport other than the in-session Agent tool. See
  docs/spec.md.
"""

import json

# The versioned agent-adapter schema. Distinct from the feature version; bumped
# on a breaking change to the adapter / envelope field set.
AGENT_ADAPTER_SCHEMA_VERSION = "1.0.0"

# Closed vocabulary for signal.rule (spec §"The agent-adapter schema").
_SIGNAL_RULES = ("nonempty_else_empty", "blocked_if_any", "always_ok")

# A blocked element carries one of these status values.
_BLOCKED_STATUSES = ("blocked",)


def is_agent_entry(entry):
    """A string adapter-map entry is a script factory address; a dict with
    `kind == "agent"` is an agent-adapter. Anything else is not an agent
    entry."""
    if isinstance(entry, str):
        return False
    if isinstance(entry, dict):
        return entry.get("kind") == "agent"
    return False


def validate_agent_adapter(entry):
    """Validate the well-formedness of an agent-adapter object. Raise a clear,
    locatable ValueError on any violation. Returns None on success.

    Requires: a `manifest` with non-empty `reads`/`writes`/`emits` lists; at
    least one `dispatch` entry; each dispatch entry has a str `subagent_type`,
    a list `inputs`, a `cardinality` in the closed vocabulary, and a str
    `writes`; an optional str `task`; a `signal.rule` in the closed set.
    """
    if not isinstance(entry, dict):
        raise ValueError("agent-adapter must be a dict")
    if entry.get("kind") != "agent":
        raise ValueError("agent-adapter must have kind == 'agent'")

    manifest = entry.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError("agent-adapter requires a 'manifest' dict")
    for field in ("reads", "writes", "emits"):
        val = manifest.get(field)
        if not isinstance(val, list) or not val:
            raise ValueError(
                f"manifest.{field} must be a non-empty list")

    dispatch = entry.get("dispatch")
    if not isinstance(dispatch, list) or not dispatch:
        raise ValueError(
            "agent-adapter requires at least one 'dispatch' entry")
    for i, d in enumerate(dispatch):
        if not isinstance(d, dict):
            raise ValueError(f"dispatch[{i}] must be a dict")
        if not isinstance(d.get("subagent_type"), str):
            raise ValueError(
                f"dispatch[{i}].subagent_type must be a str")
        if not isinstance(d.get("inputs"), list):
            raise ValueError(f"dispatch[{i}].inputs must be a list")
        if not isinstance(d.get("writes"), str):
            raise ValueError(f"dispatch[{i}].writes must be a str")
        if "task" in d and not isinstance(d["task"], str):
            raise ValueError(f"dispatch[{i}].task must be a str when present")
        _validate_cardinality(d.get("cardinality"), i)

    signal = entry.get("signal")
    if not isinstance(signal, dict):
        raise ValueError("agent-adapter requires a 'signal' dict")
    rule = signal.get("rule")
    if rule not in _SIGNAL_RULES:
        raise ValueError(
            f"signal.rule {rule!r} is not in the closed vocabulary "
            f"{_SIGNAL_RULES}")


def _validate_cardinality(cardinality, i):
    """Validate a dispatch entry's cardinality. Closed vocabulary: the string
    "once", or a single-key dict {"per_item": "<dotted path str>"}."""
    if cardinality == "once":
        return
    if isinstance(cardinality, dict):
        if set(cardinality.keys()) != {"per_item"}:
            raise ValueError(
                f"dispatch[{i}].cardinality dict must have exactly the "
                "key 'per_item'")
        if not isinstance(cardinality["per_item"], str) \
                or not cardinality["per_item"]:
            raise ValueError(
                f"dispatch[{i}].cardinality.per_item must be a non-empty "
                "dotted-path str")
        return
    raise ValueError(
        f"dispatch[{i}].cardinality {cardinality!r} is not in the closed "
        "vocabulary ('once' | {{'per_item': '<path>'}})")


def _resolve_path(slot_values, dotted):
    """Resolve a dotted path (e.g. "execution_plan.ordered") against the
    slot_values mapping. Pure dict/attr traversal — no eval, no I/O."""
    cur = slot_values
    for part in dotted.split("."):
        cur = cur[part]
    return cur


def _schema_ref(entry):
    """The schema_ref an envelope's output_contract carries: the dispatch
    entry's explicit `output_schema` when present, else its `writes` slot
    name (the slot's own schema is named by the slot)."""
    if "output_schema" in entry:
        return entry["output_schema"]
    return entry["writes"]


def build_envelopes(adapter, slot_values, tick_context, state):
    """Produce the invocation envelope(s) the executor will dispatch.

    `slot_values` is a {slot_name: value} mapping for the slots the adapter
    reads. `tick_context` is {"tick_id": ..., "mode": ...}. `state` is the FSM
    state name the dispatch runs under.

    For each dispatch entry: `cardinality == "once"` yields ONE envelope;
    `{"per_item": path}` resolves `path` against `slot_values` and yields ONE
    envelope per element of the resolved collection, in order, each carrying
    its `item`.

    Envelope shape:
      { "state", "task", "inputs", "item"?, "output_contract": {"slot",
        "schema_ref"}, "context": {"tick_id", "mode"} }
    The `item` key is omitted for `once` dispatches.
    """
    context = {
        "tick_id": tick_context["tick_id"],
        "mode": tick_context["mode"],
    }
    envelopes = []
    for entry in adapter["dispatch"]:
        inputs = {slot: slot_values[slot] for slot in entry["inputs"]}
        output_contract = {
            "slot": entry["writes"],
            "schema_ref": _schema_ref(entry),
        }
        base = {
            "state": state,
            "task": entry.get("task", ""),
            "inputs": inputs,
            "output_contract": output_contract,
            "context": context,
        }
        cardinality = entry["cardinality"]
        if cardinality == "once":
            envelopes.append(dict(base))
        else:
            collection = _resolve_path(slot_values, cardinality["per_item"])
            for element in collection:
                env = dict(base)
                env["item"] = element
                envelopes.append(env)
    return envelopes


# --- render: structured-markdown derivative view --------------------------

def _is_free_text(value):
    """Heuristic: a string is free-text (needs fencing) when it is multi-line
    or long, so its own markdown can't break the prompt layout."""
    return isinstance(value, str) and ("\n" in value or len(value) > 80)


def _render_scalar(value):
    """Render a scalar as inline markdown. Free-text strings are fenced so
    their embedded markdown cannot break the layout."""
    if _is_free_text(value):
        return "\n```\n" + value + "\n```\n"
    return f"`{value}`" if not isinstance(value, str) else value


def _render_value(value, depth):
    """Generic value -> markdown renderer: headings / key-value / bulleted
    lists for dicts / lists / scalars. Emits NO raw JSON. Deterministic:
    dict keys are emitted in insertion order (envelope dicts are built
    deterministically)."""
    lines = []
    indent = "  " * depth
    if isinstance(value, dict):
        for key in value:
            sub = value[key]
            if isinstance(sub, (dict, list)):
                lines.append(f"{indent}- **{key}:**")
                lines.extend(_render_value(sub, depth + 1))
            else:
                lines.append(f"{indent}- **{key}:** {_render_scalar(sub)}")
    elif isinstance(value, list):
        for element in value:
            if isinstance(element, (dict, list)):
                lines.append(f"{indent}-")
                lines.extend(_render_value(element, depth + 1))
            else:
                lines.append(f"{indent}- {_render_scalar(element)}")
    else:
        lines.append(f"{indent}{_render_scalar(value)}")
    return lines


def render(envelope):
    """Render an envelope to a deterministic structured-markdown prompt
    (DESIGN §3.4.6).

    `## Inputs` is a readable DERIVATIVE VIEW (generic slot -> markdown; free-
    text fields fenced) — NO raw JSON. `## Return` states the target slot +
    schema_ref as text and instructs the subagent to return one JSON
    object/array matching that schema, because the return is the machine-first
    artifact the next state consumes.

    Deterministic: the same envelope renders to a byte-identical string.
    """
    ctx = envelope["context"]
    out = []
    out.append(
        f"# Dispatch: {envelope['state']} "
        f"(mode={ctx['mode']}, tick_id={ctx['tick_id']})")
    out.append("")

    out.append("## Task")
    out.append(envelope.get("task", "") or "(no task)")
    out.append("")

    out.append("## Inputs")
    if "item" in envelope:
        out.append("### item")
        out.extend(_render_value(envelope["item"], 0))
        out.append("")
    for slot in envelope["inputs"]:
        out.append(f"### {slot}")
        out.extend(_render_value(envelope["inputs"][slot], 0))
        out.append("")

    oc = envelope["output_contract"]
    schema_ref = oc["schema_ref"]
    if isinstance(schema_ref, str):
        schema_text = schema_ref
    else:
        schema_text = json.dumps(schema_ref, sort_keys=True)
    out.append("## Return")
    out.append(
        f"Return one JSON object/array matching this schema for slot "
        f"`{oc['slot']}`:")
    out.append("")
    out.append("```json")
    out.append(schema_text)
    out.append("```")

    return "\n".join(out)


# --- validate_output ------------------------------------------------------

def _strip_fences(text):
    """Strip an optional surrounding ```json ... ``` or ``` ... ``` fence."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    # Drop the opening fence line (``` or ```json) and a trailing fence line.
    lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def validate_output(returned_text, slot_schema):
    """Parse the subagent's returned text (tolerating code fences), then check
    that its top-level type matches `slot_schema` (e.g. {"type": "array"} ->
    list, {"type": "object"} -> dict).

    Returns (True, parsed) on success or (False, "<locatable reason>") on a
    parse / type mismatch. NEVER raises on bad model output — the executor
    re-dispatches on a (False, reason) result.
    """
    body = _strip_fences(returned_text)
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError) as e:
        return False, f"returned text is not valid JSON: {e}"

    expected = slot_schema.get("type") if isinstance(slot_schema, dict) \
        else None
    type_map = {"object": dict, "array": list, "string": str,
                "number": (int, float), "boolean": bool}
    if expected in type_map:
        if not isinstance(parsed, type_map[expected]):
            return False, (
                f"expected top-level type {expected!r}, got "
                f"{type(parsed).__name__}")
    return True, parsed


# --- collect_outputs ------------------------------------------------------

def collect_outputs(adapter_entry, outputs):
    """Assemble dispatch outputs into the target slot value. `once` -> the
    single output value; `{per_item: ...}` -> the ordered list of element
    outputs."""
    if adapter_entry["cardinality"] == "once":
        return outputs[0]
    return list(outputs)


# --- compute_signal -------------------------------------------------------

def _is_blocked_element(element):
    if not isinstance(element, dict):
        return False
    if element.get("status") in _BLOCKED_STATUSES:
        return True
    if element.get("blocked_reason"):
        return True
    return False


def compute_signal(rule, slot_value):
    """Apply a closed-vocabulary signal rule deterministically. The model never
    selects control flow.

    - nonempty_else_empty: "OK" if `slot_value` is truthy / non-empty else
      "EMPTY".
    - blocked_if_any: "BLOCKED" if any element is a dict with a blocked status
      or a blocked_reason, else "OK".
    - always_ok: "OK".

    An unknown rule raises ValueError.
    """
    if rule == "nonempty_else_empty":
        return "OK" if slot_value else "EMPTY"
    if rule == "blocked_if_any":
        elements = slot_value if isinstance(slot_value, list) else []
        if any(_is_blocked_element(e) for e in elements):
            return "BLOCKED"
        return "OK"
    if rule == "always_ok":
        return "OK"
    raise ValueError(
        f"signal rule {rule!r} is not in the closed vocabulary "
        f"{_SIGNAL_RULES}")
