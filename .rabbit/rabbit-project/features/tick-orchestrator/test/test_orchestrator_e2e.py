#!/usr/bin/env python3
"""End-to-end conformance tests for the tick-orchestrator (external router/runner).

Every behaviour in docs/spec.md has an e2e test here. The headline proof is a
GENERIC, domain-free two-state PING/PONG machine driven END-TO-END by the
router: load route -> run current state -> read StateResult.signal -> apply
writes to TickContext -> resolve_next -> repeat until a terminal state.

These states carry ZERO maintainer-domain meaning (a single integer `count`
slot, signals GO/STOP). They validate the routing MECHANISM, not any spec, and
live only as test fixtures — never promoted to features.

The orchestrator CONSUMES fsm-contracts (TickContext, StateResult,
SignalVocabulary, StateManifest, apply_result, validate_route) and is the SOLE
reader of route.json. No state knows the router exists.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The orchestrator consumes the already-implemented fsm-contracts module.
_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402


# --------------------------------------------------------------------------
# A generic, domain-free PING/PONG fixture (spec "Conformance test"). It
# carries NO maintainer-domain meaning: one integer `count` slot, two states
# PING/PONG, a closed GO/STOP signal set.
#
# Route: PING --GO--> PONG, PONG --GO--> PING, PING --STOP--> END (terminal).
# PING: reads count, writes count+1; emits GO if count < 2 else STOP.
# PONG: reads count, writes count+1; emits GO (always).
# Expected from count=0:
#   PING (0->1, GO) -> PONG (1->2, GO) -> PING (2-> STOP) -> END
# --------------------------------------------------------------------------

PINGPONG_SIGNALS = ["GO", "STOP"]

PINGPONG_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["PING", "PONG", "END"],
    "edges": [
        {"state": "PING", "signal": "GO", "next": "PONG"},
        {"state": "PONG", "signal": "GO", "next": "PING"},
        {"state": "PING", "signal": "STOP", "next": "END"},
    ],
    "terminal": ["END"],
}


def _fresh_context():
    ctx = fc.TickContext()
    ctx.register_slot("count", {"type": "integer"}, version="1.0.0")
    ctx.write("count", 0)
    return ctx


def _make_ping():
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO", "STOP"])

    def run(ctx):
        before = ctx.read("count")
        n = before + 1
        signal = "STOP" if before >= 2 else "GO"
        return fc.StateResult(signal=signal, writes={"count": n},
                              journal=[f"PING {before}->{n}"])

    return manifest, run


def _make_pong():
    manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                emits=["GO"])

    def run(ctx):
        before = ctx.read("count")
        n = before + 1
        return fc.StateResult(signal="GO", writes={"count": n},
                              journal=[f"PONG {before}->{n}"])

    return manifest, run


def _pingpong_states():
    ping_manifest, ping_run = _make_ping()
    pong_manifest, pong_run = _make_pong()
    # END is terminal: it never runs, so it needs no run() callable. Provide an
    # entry so manifest-based validators can see its (empty) declaration.
    end_manifest = fc.StateManifest(reads=[], writes=[], emits=[])

    def end_run(ctx):  # pragma: no cover - terminal never executes
        raise AssertionError("terminal state END must never run()")

    return {
        "PING": (ping_manifest, ping_run),
        "PONG": (pong_manifest, pong_run),
        "END": (end_manifest, end_run),
    }


# ==========================================================================
# Behaviour 1 — resolve_next: pure transition resolution via route DATA.
# Maps (state, signal) -> successor. States never call this; the router does.
# ==========================================================================

def test_resolve_next_maps_state_signal_to_successor():
    assert to.resolve_next(PINGPONG_ROUTE, "PING", "GO") == "PONG"
    assert to.resolve_next(PINGPONG_ROUTE, "PONG", "GO") == "PING"
    assert to.resolve_next(PINGPONG_ROUTE, "PING", "STOP") == "END"


def test_resolve_next_is_pure_and_route_driven():
    """resolve_next reads ONLY the route data — no global/router state."""
    r1 = to.resolve_next(PINGPONG_ROUTE, "PING", "GO")
    r2 = to.resolve_next(PINGPONG_ROUTE, "PING", "GO")
    assert r1 == r2 == "PONG"


def test_resolve_next_raises_on_undefined_edge():
    """A (state, signal) pair with no edge is a routing error, not a silent None."""
    try:
        to.resolve_next(PINGPONG_ROUTE, "PONG", "STOP")
    except to.RouteError:
        pass
    else:
        raise AssertionError("resolve_next must raise RouteError on an undefined edge")


# ==========================================================================
# Behaviour 2 — the run loop: load route -> run state -> apply writes ->
# resolve_next -> repeat until terminal. The HEADLINE e2e proof.
# ==========================================================================

def test_e2e_pingpong_run_to_terminal():
    """End-to-end: the router drives PING/PONG over the blackboard to END.

    Asserts every behaviour the spec's conformance test enumerates: blackboard
    slot read/write, the StateResult envelope, signal emission from a closed
    set, route resolution, loop re-entry, and terminal halt.
    """
    ctx = _fresh_context()
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    states = _pingpong_states()

    result = to.run(PINGPONG_ROUTE, states, ctx, vocab, start="PING")

    # Terminal halt: the loop stopped at the declared terminal state.
    assert result.final_state == "END"
    # Loop re-entry + route resolution: PING -> PONG -> PING -> END.
    assert result.path == ["PING", "PONG", "PING", "END"]
    # Blackboard slot read/write advanced 0 -> 1 -> 2 -> 3.
    assert ctx.read("count") == 3
    # Signal emission from the closed set, in order, halting on STOP.
    assert result.signals == ["GO", "GO", "STOP"]


def test_e2e_run_does_not_execute_terminal_state():
    """The terminal state is the halt condition; its run() is never invoked."""
    ctx = _fresh_context()
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    states = _pingpong_states()
    # END.run raises if executed; reaching here without error proves it didn't.
    result = to.run(PINGPONG_ROUTE, states, ctx, vocab, start="PING")
    assert result.final_state == "END"


def test_e2e_router_is_sole_route_reader_states_are_decoupled():
    """Decoupling guarantee: no state's run() names a sibling state. The route
    DATA is the only wiring; the router is the only thing that reads it."""
    states = _pingpong_states()
    _, ping_run = states["PING"]
    _, pong_run = states["PONG"]
    assert "PONG" not in ping_run.__code__.co_consts
    assert "PING" not in pong_run.__code__.co_consts


def test_e2e_decoupling_insert_state_is_route_data_edit_only():
    """Inserting state C between PING and PONG = author C + edit route data.
    ZERO edits to PING or PONG. Prove it by rerouting PING--GO-->MID-->PONG and
    running the SAME PING/PONG run() closures unchanged."""
    ctx = _fresh_context()
    vocab = fc.SignalVocabulary(PINGPONG_SIGNALS)
    states = _pingpong_states()

    # Author a new pass-through state MID with its own manifest + run. It only
    # emits GO and bumps count; it names no neighbour.
    mid_manifest = fc.StateManifest(reads=["count"], writes=["count"],
                                    emits=["GO"])

    def mid_run(ctx):
        n = ctx.read("count") + 1
        return fc.StateResult(signal="GO", writes={"count": n},
                              journal=[f"MID ->{n}"])

    states["MID"] = (mid_manifest, mid_run)

    # Route DATA edit only: repoint PING--GO-->MID, add MID--GO-->PONG.
    rerouted = {
        "schema_version": "1.0.0",
        "states": ["PING", "MID", "PONG", "END"],
        "edges": [
            {"state": "PING", "signal": "GO", "next": "MID"},
            {"state": "MID", "signal": "GO", "next": "PONG"},
            {"state": "PONG", "signal": "GO", "next": "PING"},
            {"state": "PING", "signal": "STOP", "next": "END"},
        ],
        "terminal": ["END"],
    }

    result = to.run(rerouted, states, ctx, vocab, start="PING")
    assert result.final_state == "END"
    # PING(0->1,GO) MID(1->2,GO) PONG(2->3,GO) PING(3->4,STOP) END.
    # PING emits STOP once `count` (before-value) >= 2, which first holds on
    # its second visit (before=3), so the machine bumps count to 4 and halts.
    assert result.path == ["PING", "MID", "PONG", "PING", "END"]
    assert ctx.read("count") == 4
    assert result.signals == ["GO", "GO", "GO", "STOP"]


# ==========================================================================
# Behaviour 3a — structural validator: signal-validity.
# Every edge `signal` (the `on` key) must be in that state's declared `emits`;
# every transition target must exist in the state set.
# ==========================================================================

def test_validate_signals_accepts_conforming_route():
    states = _pingpong_states()
    manifests = {name: m for name, (m, _r) in states.items()}
    assert to.validate_signals(PINGPONG_ROUTE, manifests).passed is True


def test_validate_signals_rejects_edge_signal_not_in_emits():
    """An edge keyed on a signal the source state never declares is invalid."""
    states = _pingpong_states()
    manifests = {name: m for name, (m, _r) in states.items()}
    # PONG declares emits=["GO"] only; an edge on PONG--STOP is signal-invalid.
    bad = {
        **PINGPONG_ROUTE,
        "edges": PINGPONG_ROUTE["edges"] + [
            {"state": "PONG", "signal": "STOP", "next": "END"}],
    }
    assert to.validate_signals(bad, manifests).passed is False


def test_validate_signals_rejects_edge_target_not_in_states():
    """A transition target absent from the state set is invalid."""
    states = _pingpong_states()
    manifests = {name: m for name, (m, _r) in states.items()}
    bad = {
        **PINGPONG_ROUTE,
        "edges": PINGPONG_ROUTE["edges"] + [
            {"state": "PING", "signal": "GO", "next": "GHOST"}],
    }
    assert to.validate_signals(bad, manifests).passed is False


# ==========================================================================
# Behaviour 3b — structural validator: data-readiness.
# On every path reaching a state, each slot it `reads` was `written` by a
# predecessor.
# ==========================================================================

def test_validate_data_readiness_accepts_when_slots_are_written_upstream():
    """count is seeded as an initial slot and written by PING/PONG before any
    downstream read, so the conforming route is data-ready."""
    states = _pingpong_states()
    manifests = {name: m for name, (m, _r) in states.items()}
    res = to.validate_data_readiness(
        PINGPONG_ROUTE, manifests, start="PING", initial=["count"])
    assert res.passed is True


def test_validate_data_readiness_rejects_unwritten_read():
    """If a state reads a slot no predecessor (nor the initial set) wrote, the
    route is not data-ready."""
    # PING reads `count`, but nothing writes it and it is not in the initial
    # set -> data-readiness must fail.
    ping_manifest = fc.StateManifest(reads=["count"], writes=[], emits=["GO", "STOP"])
    pong_manifest = fc.StateManifest(reads=[], writes=[], emits=["GO"])
    end_manifest = fc.StateManifest(reads=[], writes=[], emits=[])
    manifests = {"PING": ping_manifest, "PONG": pong_manifest, "END": end_manifest}
    res = to.validate_data_readiness(
        PINGPONG_ROUTE, manifests, start="PING", initial=[])
    assert res.passed is False


# ==========================================================================
# Behaviour 4 — the router embeds NO concrete state name and no maintainer
# domain logic (contract `never` clause).
# ==========================================================================

def test_router_names_no_concrete_state_in_source():
    """The router code must not hardcode PING/PONG/END or any concrete state —
    routing is pure data. Inspect the module source for fixture state names."""
    src_path = os.path.join(_SRC, "tick_orchestrator.py")
    with open(src_path, "r") as f:
        source = f.read()
    for forbidden in ("PING", "PONG"):
        assert forbidden not in source, (
            f"router source must not name concrete state '{forbidden}' "
            "(routing is data, not code)")
