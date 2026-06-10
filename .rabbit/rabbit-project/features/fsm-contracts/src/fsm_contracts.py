#!/usr/bin/env python3
"""fsm-contracts — the machine-first contract layer for the tick FSM.

Pure data shapes + structural validation. ZERO execution logic, ZERO
maintainer-domain payloads. This module defines the seam every tick state and
the external router conform to:

  1. TickContext      — open/extensible registry of named, typed, versioned slots.
  2. StateResult      — the { signal, writes, journal } outcome envelope.
  3. SignalVocabulary — a closed, declared signal set (treated as data).
  4. StateManifest    — a state's declared { reads, writes, emits }.
  5. validate_route   — the route.json transition-table shape validator.

Plus apply_result(), which enforces the bounded-scope contract (a state may
write only slots in its manifest.writes and emit only signals in its
manifest.emits, drawn from a closed vocabulary) when committing a StateResult
to the blackboard.

What is DELIBERATELY absent (owned elsewhere, see docs/spec.md):
  - transition resolution / run loop   -> tick-orchestrator
  - anchor-invariant validation        -> lifecycle core
  - concrete maintainer slot payloads  -> consumer state features

Version: 0.1.0
Owner: changyu87
Deprecation criterion: superseded when the tick-FSM contract schema reaches a
  breaking major version (see feature.json / docs/spec.md).
"""

from collections import namedtuple


class ContractError(Exception):
    """Raised when a value violates a structural rule of the contract."""


# A lightweight, uniform structural-validation result. `passed` is the boolean
# verdict; `messages` carries human-readable detail derived from the machine
# verdict (philosophy §1: the machine verdict is primary).
CheckResult = namedtuple("CheckResult", ["passed", "messages"])


# --------------------------------------------------------------------------
# 1. TickContext — open/extensible registry of named, typed, versioned slots
# --------------------------------------------------------------------------

_JSON_TYPE_CHECKERS = {
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


class TickContext:
    """The blackboard: a machine-first record of named, typed, versioned slots.

    A state reads the slots it needs and writes products back into named slots.
    The registry is OPEN/EXTENSIBLE: consumers register their own slots; the
    contract defines the slot MECHANISM, not a fixed slot set.
    """

    def __init__(self):
        self._schemas = {}   # slot name -> JSON-Schema-style type declaration
        self._versions = {}  # slot name -> version string
        self._values = {}    # slot name -> written value

    def register_slot(self, name, schema, version):
        if not isinstance(name, str) or not name:
            raise ContractError("slot name must be a non-empty string")
        if not isinstance(schema, dict) or "type" not in schema:
            raise ContractError(
                f"slot '{name}' schema must be a dict with a 'type' key")
        if schema["type"] not in _JSON_TYPE_CHECKERS:
            raise ContractError(
                f"slot '{name}' has unsupported type '{schema['type']}'")
        if not isinstance(version, str) or not version:
            raise ContractError(f"slot '{name}' requires a version string")
        self._schemas[name] = schema
        self._versions[name] = version

    def registered_slots(self):
        return tuple(self._schemas.keys())

    def slot_version(self, name):
        if name not in self._versions:
            raise ContractError(f"slot '{name}' is not registered")
        return self._versions[name]

    def write(self, name, value):
        if name not in self._schemas:
            raise ContractError(f"slot '{name}' is not registered")
        declared = self._schemas[name]["type"]
        if not _JSON_TYPE_CHECKERS[declared](value):
            raise ContractError(
                f"slot '{name}' expects type '{declared}', got "
                f"{type(value).__name__}")
        self._values[name] = value

    def read(self, name):
        if name not in self._schemas:
            raise ContractError(f"slot '{name}' is not registered")
        if name not in self._values:
            raise ContractError(f"slot '{name}' has not been written")
        return self._values[name]


# --------------------------------------------------------------------------
# 2. StateResult — the { signal, writes, journal } outcome envelope
# --------------------------------------------------------------------------

class StateResult:
    """The uniform outcome of every state: it reports WHAT happened, never
    decides WHAT runs next."""

    __slots__ = ("signal", "writes", "journal")

    def __init__(self, signal, writes=None, journal=None):
        self.signal = signal
        self.writes = {} if writes is None else writes
        self.journal = [] if journal is None else journal


def validate_state_result(result):
    """Structural validation of a StateResult envelope."""
    messages = []
    if not isinstance(result, StateResult):
        return CheckResult(False, ["not a StateResult instance"])
    if not isinstance(result.signal, str) or not result.signal:
        messages.append("signal must be a non-empty symbol (string)")
    if not isinstance(result.writes, dict):
        messages.append("writes must be a mapping of slot -> value")
    if not isinstance(result.journal, list):
        messages.append("journal must be a list")
    if messages:
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: StateResult envelope is well-formed"])


# --------------------------------------------------------------------------
# 3. SignalVocabulary — a closed, declared signal set (treated as data)
# --------------------------------------------------------------------------

class SignalVocabulary:
    """A closed, declared set of signal symbols. The MECHANISM treats the set
    as data; the specific members are an instance choice."""

    def __init__(self, members):
        if not members or not all(isinstance(s, str) and s for s in members):
            raise ContractError(
                "a signal vocabulary must be a non-empty set of symbols")
        # Preserve declaration order, dedupe.
        seen = []
        for s in members:
            if s not in seen:
                seen.append(s)
        self._members = tuple(seen)

    def members(self):
        return self._members

    def is_member(self, signal):
        return signal in self._members

    def require(self, signal):
        if signal not in self._members:
            raise ContractError(
                f"signal '{signal}' is not in the declared vocabulary "
                f"{self._members}")


# --------------------------------------------------------------------------
# 4. StateManifest — a state's declared { reads, writes, emits }
# --------------------------------------------------------------------------

class StateManifest:
    """A state's bounded-scope declaration: the slots it reads, the slots it
    writes, and the signals it emits. NO state names another state — a manifest
    references slots and signals only, never successors."""

    __slots__ = ("reads", "writes", "emits")

    def __init__(self, reads, writes, emits):
        self.reads = tuple(reads)
        self.writes = tuple(writes)
        self.emits = tuple(emits)


# --------------------------------------------------------------------------
# apply_result — commit a StateResult to the blackboard under manifest +
# vocabulary enforcement (the bounded-scope contract).
# --------------------------------------------------------------------------

def apply_result(ctx, manifest, result, vocab):
    """Validate a StateResult against the state's manifest and the closed
    signal vocabulary, then commit its writes to the TickContext blackboard.

    Enforces:
      - the envelope is well-formed;
      - the emitted signal is in manifest.emits AND in the vocabulary;
      - every written slot is declared in manifest.writes;
      - writes type-check against the registered slot schemas (via ctx.write).
    """
    verdict = validate_state_result(result)
    if not verdict.passed:
        raise ContractError(
            "malformed StateResult: " + "; ".join(verdict.messages))

    vocab.require(result.signal)
    if result.signal not in manifest.emits:
        raise ContractError(
            f"state emitted '{result.signal}' which is not in its declared "
            f"emits {manifest.emits}")

    for slot in result.writes:
        if slot not in manifest.writes:
            raise ContractError(
                f"state wrote slot '{slot}' which is not in its declared "
                f"writes {manifest.writes}")

    for slot, value in result.writes.items():
        ctx.write(slot, value)


# --------------------------------------------------------------------------
# 5. validate_route — the route.json transition-table shape validator
# --------------------------------------------------------------------------

_ROUTE_REQUIRED_KEYS = ("states", "edges", "terminal")


def validate_route(route):
    """Validate the route.json SHAPE: a state set, a (state, signal) -> next
    edge table, and a terminal-state marker. Routing is DATA and lives OUTSIDE
    every state. This validates the shape only; resolving/executing the table
    is owned by tick-orchestrator and is NOT done here."""
    messages = []
    if not isinstance(route, dict):
        return CheckResult(False, ["route must be a JSON object"])

    for key in _ROUTE_REQUIRED_KEYS:
        if key not in route:
            messages.append(f"missing required key '{key}'")
    if messages:
        return CheckResult(False, messages)

    states = route["states"]
    edges = route["edges"]
    terminal = route["terminal"]

    if not isinstance(states, list) or not states or \
            not all(isinstance(s, str) and s for s in states):
        messages.append("'states' must be a non-empty list of state symbols")
    state_set = set(states) if isinstance(states, list) else set()

    if not isinstance(edges, list):
        messages.append("'edges' must be a list of edge objects")
    else:
        for i, e in enumerate(edges):
            if not isinstance(e, dict):
                messages.append(f"edge[{i}] must be an object")
                continue
            for k in ("state", "signal", "next"):
                if k not in e:
                    messages.append(f"edge[{i}] missing key '{k}'")
            if "state" in e and e["state"] not in state_set:
                messages.append(
                    f"edge[{i}] 'state' references unknown state "
                    f"'{e['state']}'")
            if "next" in e and e["next"] not in state_set:
                messages.append(
                    f"edge[{i}] 'next' references unknown state "
                    f"'{e['next']}'")
            if "signal" in e and (not isinstance(e["signal"], str)
                                  or not e["signal"]):
                messages.append(f"edge[{i}] 'signal' must be a symbol")

    if not isinstance(terminal, list):
        messages.append("'terminal' must be a list of state symbols")
    else:
        for t in terminal:
            if t not in state_set:
                messages.append(
                    f"terminal state '{t}' is not in the state set")

    if messages:
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: route.json shape is well-formed"])
