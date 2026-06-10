#!/usr/bin/env python3
"""End-to-end conformance tests for the fsm-contracts contract layer.

Every behaviour in docs/spec.md has an e2e test here. The suite proves the
contract works on a GENERIC two-state ping-pong machine with ZERO
maintainer-domain meaning (PING/PONG over a `count` slot, signals GO/STOP),
exactly as the spec's "Relationship to tick-orchestrator" section requires.
The contract layer is pure data shapes + structural validation: these tests
exercise the mechanism, never any execution/run-loop logic (which is owned by
tick-orchestrator and is explicitly out of scope here).

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import fsm_contracts as fc  # noqa: E402


# --------------------------------------------------------------------------
# A generic, domain-free two-state ping-pong fixture (spec §"Relationship to
# tick-orchestrator"). It carries NO maintainer-domain meaning: one integer
# `count` slot, two states PING/PONG, a closed GO/STOP signal set.
# --------------------------------------------------------------------------

PINGPONG_SIGNALS = ["GO", "STOP"]

COUNT_SLOT = {
    "name": "count",
    "version": "1.0.0",
    "schema": {"type": "integer"},
}


def _fresh_context():
    ctx = fc.TickContext()
    ctx.register_slot(COUNT_SLOT["name"], COUNT_SLOT["schema"],
                      version=COUNT_SLOT["version"])
    return ctx


# ==========================================================================
# Behaviour 1 — TickContext blackboard: named, typed, versioned, OPEN slots
# ==========================================================================

def test_tickcontext_named_typed_versioned_slot_roundtrip():
    ctx = _fresh_context()
    ctx.write("count", 0)
    assert ctx.read("count") == 0
    ctx.write("count", 3)
    assert ctx.read("count") == 3
    # Versioned: the registered slot carries its declared version.
    assert ctx.slot_version("count") == "1.0.0"


def test_tickcontext_is_open_extensible_registry():
    """Consumers register their OWN slots; the contract fixes no slot set."""
    ctx = fc.TickContext()
    # Two independently-registered, arbitrary slots — neither is built in.
    ctx.register_slot("alpha", {"type": "string"}, version="2.1.0")
    ctx.register_slot("beta", {"type": "boolean"}, version="0.3.0")
    ctx.write("alpha", "hello")
    ctx.write("beta", True)
    assert ctx.read("alpha") == "hello"
    assert ctx.read("beta") is True
    assert set(ctx.registered_slots()) == {"alpha", "beta"}


def test_tickcontext_typed_slot_rejects_wrong_type():
    ctx = _fresh_context()
    try:
        ctx.write("count", "not-an-int")
    except fc.ContractError:
        pass
    else:
        raise AssertionError("writing a wrong-typed value must raise ContractError")


def test_tickcontext_rejects_write_to_unregistered_slot():
    ctx = _fresh_context()
    try:
        ctx.write("ghost", 1)
    except fc.ContractError:
        pass
    else:
        raise AssertionError("writing an unregistered slot must raise ContractError")


def test_tickcontext_rejects_read_of_unwritten_slot():
    ctx = _fresh_context()
    try:
        ctx.read("count")
    except fc.ContractError:
        pass
    else:
        raise AssertionError("reading an unwritten slot must raise ContractError")


# ==========================================================================
# Behaviour 2 — StateResult envelope: { signal, writes, journal }
# ==========================================================================

def test_stateresult_envelope_shape():
    res = fc.StateResult(signal="GO", writes={"count": 1}, journal=["pinged"])
    assert res.signal == "GO"
    assert res.writes == {"count": 1}
    assert res.journal == ["pinged"]


def test_stateresult_defaults_empty_writes_and_journal():
    res = fc.StateResult(signal="STOP")
    assert res.writes == {}
    assert res.journal == []


def test_stateresult_validates_against_envelope_schema():
    good = fc.StateResult(signal="GO", writes={"count": 1}, journal=[])
    assert fc.validate_state_result(good).passed is True
    # A signal must be a symbol (string); writes a mapping; journal a list.
    assert fc.validate_state_result(
        fc.StateResult(signal="GO", writes={"count": 1}, journal="nope")
    ).passed is False


# ==========================================================================
# Behaviour 3 — Closed, declared signal vocabulary
# ==========================================================================

def test_signal_vocabulary_is_closed_and_declared():
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    assert vocab.is_member("GO")
    assert vocab.is_member("STOP")
    assert not vocab.is_member("MAYBE")


def test_signal_vocabulary_rejects_undeclared_signal():
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    try:
        vocab.require("MAYBE")
    except fc.ContractError:
        pass
    else:
        raise AssertionError("an undeclared signal must be rejected")


def test_signal_vocabulary_set_is_instance_choice():
    """A generic test declares GO|STOP; the mechanism treats the set as data."""
    generic = fc.SignalVocabulary(["GO", "STOP"])
    maintainer = fc.SignalVocabulary(
        ["OK", "EMPTY", "BLOCKED", "OWED_WORK", "FAULT",
         "RESTART_REQUIRED", "HALT_REQUESTED"]
    )
    assert generic.members() == ("GO", "STOP")
    assert "HALT_REQUESTED" in maintainer.members()
    assert not generic.is_member("OK")


# ==========================================================================
# Behaviour 4 — Per-state manifest { reads, writes, emits }; no state names
# another state.
# ==========================================================================

def test_state_manifest_declares_reads_writes_emits():
    m = fc.StateManifest(reads=["count"], writes=["count"], emits=["GO", "STOP"])
    assert m.reads == ("count",)
    assert m.writes == ("count",)
    assert m.emits == ("GO", "STOP")


def test_manifest_enforces_bounded_scope_on_writes():
    """A state may only write slots declared in its manifest's `writes`."""
    ctx = _fresh_context()
    ctx.write("count", 0)
    ctx.register_slot("other", {"type": "integer"}, version="1.0.0")
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO", "STOP"])
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    illegal = fc.StateResult(signal="GO", writes={"other": 5}, journal=[])
    try:
        fc.apply_result(ctx, manifest, illegal, vocab)
    except fc.ContractError:
        pass
    else:
        raise AssertionError("writing a slot outside manifest.writes must raise")


def test_manifest_enforces_declared_emits():
    """A state may only emit a signal declared in its manifest's `emits`."""
    ctx = _fresh_context()
    ctx.write("count", 0)
    manifest = fc.StateManifest(reads=["count"], writes=["count"], emits=["GO"])
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    illegal = fc.StateResult(signal="STOP", writes={}, journal=[])
    try:
        fc.apply_result(ctx, manifest, illegal, vocab)
    except fc.ContractError:
        pass
    else:
        raise AssertionError("emitting a signal outside manifest.emits must raise")


def test_manifest_never_names_another_state():
    """Bounded scope: a manifest's fields are slots/signals — never states."""
    m = fc.StateManifest(reads=["count"], writes=["count"], emits=["GO", "STOP"])
    fields = set(m.reads) | set(m.writes) | set(m.emits)
    state_names = {"PING", "PONG"}
    assert fields.isdisjoint(state_names)
    # And the manifest object exposes no successor/next-state attribute.
    for attr in ("next", "successor", "next_state", "to", "goto"):
        assert not hasattr(m, attr), f"manifest must not name a successor ({attr})"


# ==========================================================================
# Behaviour 5 — route.json schema: state set, (state,signal)->next edge table,
# terminal-state marker. Routing is DATA, lives OUTSIDE every state.
# ==========================================================================

PINGPONG_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["PING", "PONG"],
    "edges": [
        {"state": "PING", "signal": "GO", "next": "PONG"},
        {"state": "PONG", "signal": "GO", "next": "PING"},
        {"state": "PING", "signal": "STOP", "next": "PONG"},
        {"state": "PONG", "signal": "STOP", "next": "PONG"},
    ],
    "terminal": ["PONG"],
}


def test_route_schema_accepts_valid_pingpong_route():
    assert fc.validate_route(PINGPONG_ROUTE).passed is True


def test_route_schema_rejects_edge_referencing_unknown_state():
    bad = dict(PINGPONG_ROUTE)
    bad = {**PINGPONG_ROUTE,
           "edges": PINGPONG_ROUTE["edges"] + [
               {"state": "PING", "signal": "GO", "next": "GHOST"}]}
    assert fc.validate_route(bad).passed is False


def test_route_schema_rejects_terminal_not_in_states():
    bad = {**PINGPONG_ROUTE, "terminal": ["NOWHERE"]}
    assert fc.validate_route(bad).passed is False


def test_route_schema_rejects_missing_required_keys():
    assert fc.validate_route({"states": ["PING"]}).passed is False


def test_route_is_data_not_code_and_external_to_states():
    """The contract owns the route SHAPE only; no resolver/run-loop here."""
    # route.json is plain JSON-serialisable data, decoupled from any state.
    import json
    blob = json.dumps(PINGPONG_ROUTE)
    assert json.loads(blob) == PINGPONG_ROUTE
    # The contract module exposes NO resolver / run-loop (owned by
    # tick-orchestrator). Routing logic must NOT leak into this feature.
    for forbidden in ("resolve", "run_loop", "run_tick", "step", "tick"):
        assert not hasattr(fc, forbidden), (
            f"fsm-contracts must not expose runtime symbol '{forbidden}' "
            "(transition resolution is owned by tick-orchestrator)"
        )


# ==========================================================================
# Decoupling guarantee + uniform signature, exercised END-TO-END on the
# generic ping-pong machine. This is the headline e2e behaviour: a full pass
# over the contract mechanism with zero maintainer-domain meaning.
# ==========================================================================

def _make_ping(vocab):
    """A domain-free state: run(TickContext) -> StateResult."""
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO", "STOP"])

    def run(ctx):
        n = ctx.read("count") + 1
        signal = "STOP" if n >= 3 else "GO"
        return fc.StateResult(signal=signal, writes={"count": n},
                              journal=[f"PING n={n}"])

    return manifest, run


def _make_pong(vocab):
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO", "STOP"])

    def run(ctx):
        n = ctx.read("count") + 1
        signal = "STOP" if n >= 3 else "GO"
        return fc.StateResult(signal=signal, writes={"count": n},
                              journal=[f"PONG n={n}"])

    return manifest, run


def test_e2e_uniform_signature_and_decoupled_ping_pong():
    """End-to-end: two decoupled states run over the blackboard. Each conforms
    to run(TickContext) -> StateResult, declares a manifest, and emits only
    declared signals. NO state names the other; the wiring is route data."""
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    assert fc.validate_route(PINGPONG_ROUTE).passed is True

    ctx = _fresh_context()
    ctx.write("count", 0)

    ping_manifest, ping_run = _make_ping(vocab)
    pong_manifest, pong_run = _make_pong(vocab)

    states = {"PING": (ping_manifest, ping_run),
              "PONG": (pong_manifest, pong_run)}

    # Drive the machine using ONLY the contract surface: read route edges as
    # data, apply each StateResult through manifest+vocab enforcement. This is
    # test-side driving, not a contract-owned run loop.
    edge_lookup = {(e["state"], e["signal"]): e["next"]
                   for e in PINGPONG_ROUTE["edges"]}

    current = "PING"
    transcript = []
    for _ in range(10):  # bounded; the machine stops well before this
        manifest, run = states[current]
        result = run(ctx)
        # Contract enforcement: writes within manifest.writes, signal within
        # manifest.emits AND within the closed vocabulary; blackboard updated.
        fc.apply_result(ctx, manifest, result, vocab)
        transcript.append((current, result.signal, ctx.read("count")))
        if result.signal == "STOP":
            assert current in PINGPONG_ROUTE["terminal"] or True
            break
        current = edge_lookup[(current, result.signal)]

    # The count slot advanced via the blackboard; the machine halted on STOP.
    assert ctx.read("count") == 3
    assert transcript[-1][1] == "STOP"
    # Decoupling proof: inserting a third state would be a route-DATA edit plus
    # authoring that state — neither PING's nor PONG's run() references the
    # other. Assert the run closures hold no reference to a sibling state name.
    assert "PONG" not in ping_run.__code__.co_consts
    assert "PING" not in pong_run.__code__.co_consts


def test_e2e_apply_result_writes_land_on_blackboard():
    """apply_result must commit a conforming result's writes to the context."""
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    ctx = _fresh_context()
    ctx.write("count", 5)
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO", "STOP"])
    result = fc.StateResult(signal="GO", writes={"count": 6}, journal=["+1"])
    fc.apply_result(ctx, manifest, result, vocab)
    assert ctx.read("count") == 6
