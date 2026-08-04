#!/usr/bin/env python3
"""tick-orchestrator — the external router/runner over a route.json.

This is the ONLY component that reads route.json; no state knows it exists. It
executes a declarative route over states that conform to fsm-contracts. This
first milestone is a minimal slice:

  1. resolve_next(route, state, signal) -> next_state — pure transition
     resolution. States never call this; the router does.
  2. run(route, states, ctx, vocab, start) — the run loop: run the current
     state's run(ctx) -> read StateResult.signal -> apply writes to TickContext
     (under fsm-contracts manifest+vocab enforcement) -> resolve_next -> repeat
     until a terminal state is reached.
  3. structural route validators run before a tick:
       - validate_signals     — every edge signal is in the source state's
         declared emits; every transition target exists in the state set.
       - validate_data_readiness — on every path reaching a state, each slot it
         reads was written by a predecessor (or seeded as an initial slot).

Routing is DATA, not code: this module embeds NO concrete state name and NO
maintainer-domain logic. Journaling, checkpointing, disposition selection, and
single-writer mutex are deferred to other features (see docs/spec.md).

It CONSUMES fsm-contracts (TickContext, StateResult, SignalVocabulary,
StateManifest, apply_result, validate_route, CheckResult); it does not
re-implement them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Retired/superseded when the declarative-route execution
  model is replaced, or folded into a larger lifecycle-core runtime that the
  project migrates to (see feature.json / docs/spec.md).
"""

from collections import namedtuple

import fsm_contracts as fc


class RouteError(Exception):
    """Raised when the route data cannot resolve a transition the run loop
    needs (e.g. no edge for a (state, signal) pair)."""


# The outcome of a run loop: the terminal state it halted at, the ordered path
# of states visited (including the terminal), and the ordered signals emitted.
# Machine-first record; human-readable views derive from it.
RunResult = namedtuple("RunResult", ["final_state", "path", "signals"])


def _edge_lookup(route):
    """Build a (state, signal) -> next mapping from the route's edge list."""
    return {(e["state"], e["signal"]): e["next"] for e in route["edges"]}


def resolve_next(route, state, signal):
    """Pure transition resolution: map (state, signal) -> successor via route
    DATA. The decoupling core. States never call this; the router does.

    Raises RouteError when no edge is declared for (state, signal)."""
    table = _edge_lookup(route)
    key = (state, signal)
    if key not in table:
        raise RouteError(
            f"no route edge for state '{state}' on signal '{signal}'")
    return table[key]


def run(route, states, ctx, vocab, start):
    """Execute the route over `states` from `start` until a terminal state.

    `states` maps each state name to a (manifest, run_callable) pair; each
    run_callable has the uniform fsm-contracts signature run(TickContext) ->
    StateResult. The loop:

      run current state -> apply its StateResult to the blackboard under
      manifest+vocabulary enforcement -> resolve_next -> repeat, halting the
      moment a terminal state is reached (a terminal state never run()s).

    Returns a RunResult(final_state, path, signals)."""
    terminal = set(route["terminal"])
    current = start
    path = [current]
    signals = []

    while current not in terminal:
        manifest, run_state = states[current]
        result = run_state(ctx)
        # Commit under the contract: writes within manifest.writes, signal
        # within manifest.emits AND the closed vocabulary; blackboard updated.
        fc.apply_result(ctx, manifest, result, vocab)
        signals.append(result.signal)
        current = resolve_next(route, current, result.signal)
        path.append(current)

    return RunResult(final_state=current, path=path, signals=signals)


def validate_signals(route, manifests):
    """Structural validator (signal-validity): every edge's signal is in the
    source state's declared emits, and every transition target exists in the
    state set. `manifests` maps state name -> StateManifest."""
    messages = []
    state_set = set(route["states"])
    for i, e in enumerate(route["edges"]):
        src = e["state"]
        signal = e["signal"]
        target = e["next"]
        if src not in manifests:
            messages.append(
                f"edge[{i}] source state '{src}' has no manifest")
        elif signal not in manifests[src].emits:
            messages.append(
                f"edge[{i}] signal '{signal}' is not in state '{src}' "
                f"declared emits {manifests[src].emits}")
        if target not in state_set:
            messages.append(
                f"edge[{i}] target '{target}' is not in the state set")
    if messages:
        return fc.CheckResult(False, messages)
    return fc.CheckResult(True, ["OK: every edge signal is declared and every "
                                 "target exists"])


def validate_data_readiness(route, manifests, start, initial):
    """Structural validator (data-readiness): on every path reaching a state,
    each slot it reads was written by a predecessor (or seeded in `initial`).

    Computes, per state, the intersection of slots guaranteed-written on ALL
    paths from `start` to that state (a forward dataflow fixpoint over the
    edge graph), then checks each state's reads against that guaranteed set.
    `manifests` maps state name -> StateManifest; `initial` lists slots seeded
    before the run begins."""
    initial_set = frozenset(initial)
    predecessors = {s: [] for s in route["states"]}
    for e in route["edges"]:
        if e["next"] in predecessors:
            predecessors[e["next"]].append(e["state"])

    def produced(state):
        """Slots guaranteed written AFTER `state` runs: what was available
        before it plus what its manifest writes."""
        writes = set(manifests[state].writes) if state in manifests else set()
        return available[state] | writes

    # available[s] = slots guaranteed written on EVERY path reaching s, BEFORE
    # s runs. `start` is seeded with the initial slots and stays fixed. Other
    # states begin optimistically (the universe) and intersect down to a
    # fixpoint over their predecessors' produced sets.
    universe = set(initial_set)
    for m in manifests.values():
        universe.update(m.writes)

    available = {s: set(universe) for s in route["states"]}
    available[start] = set(initial_set)

    changed = True
    while changed:
        changed = False
        for s in route["states"]:
            if s == start:
                continue
            preds = predecessors[s]
            if not preds:
                # Unreachable state: nothing is guaranteed reaching it.
                incoming = set()
            else:
                incoming = None
                for p in preds:
                    incoming = produced(p) if incoming is None \
                        else (incoming & produced(p))
            if incoming != available[s]:
                available[s] = incoming
                changed = True

    messages = []
    for s in route["states"]:
        if s not in manifests:
            continue
        for slot in manifests[s].reads:
            if slot not in available[s]:
                messages.append(
                    f"state '{s}' reads slot '{slot}' which is not guaranteed "
                    f"written on every path reaching it")
    if messages:
        return fc.CheckResult(False, messages)
    return fc.CheckResult(True, ["OK: every read slot is written by a "
                                 "predecessor on all paths"])
