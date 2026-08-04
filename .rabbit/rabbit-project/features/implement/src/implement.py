#!/usr/bin/env python3
"""implement — the dry-run IMPLEMENT adapter state.

The first tick state that *acts* on work (DESIGN §1.1, §2.6) — shipped here as
the DRY-RUN reference adapter, the `dry-run` rung of the trust ladder
(DESIGN §2.3, §3.8.2). It turns the `execution_plan` PRIORITIZE produced into a
list of `handoffs`, WITHOUT performing any work
(`... PRIORITIZE -> execution_plan -> IMPLEMENT -> handoffs`).

IMPLEMENT (dry-run) is DETERMINISTIC and INERT — no model, no diff, no branch,
no PR, no commit, no tracker write, no filesystem effect. It reads and writes
blackboard slots only. Its job is to prove the act-side seam (the Handoff
schema, the execution_plan -> handoffs wiring, the signal) with ZERO repo risk.
The model-backed implement-then-PR doer (the `propose` rung, DESIGN §3.6.3) is a
separate, swappable adapter deferred to a later milestone.

The manifest reads `execution_plan` and `work_orders`; the dry-run run() uses
only `execution_plan` and IGNORES `work_orders`. `work_orders` is declared so
the ACTING IMPLEMENT's per-item dispatch can join each execution_plan.ordered id
to its full WorkOrder record. DESIGN §2.6 also lists `workspace` for
IMPLEMENT, but `workspace` is the isolated worktree the model-backed doer
consumes; the dry-run adapter does no isolated code work, so it deliberately
does NOT read `workspace` — keeping the route validator's data-readiness check
satisfiable without any predecessor writing `workspace`.

Public surface:
  - Handoff slot schema — `HANDOFF_SCHEMA_VERSION` + `HANDOFFS_SLOT` (this
    feature OWNS the slot, mirroring how work-intake owns WorkItem/WorkOrder).
  - IMPLEMENT_MANIFEST — the state's {reads, writes, emits} manifest.
  - IMPLEMENT_SIGNALS — the closed signal set IMPLEMENT may emit (OK | BLOCKED).
  - run(ctx) -> StateResult — the IMPLEMENT state callable.
  - factory(runtime) -> (StateManifest, run_callable) — the adapter-wiring
    factory convention; scheduling.run_tick maps IMPLEMENT through it.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the model-backed implement-then-PR doer
  (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the
  Handoff schema reaches a breaking major version. See docs/spec.md.
"""

import collections

# fsm-contracts is a sibling feature; tests inject its src/ onto sys.path
# exactly as the work-intake adapters do, so importing by module name resolves
# the sibling src/ on the path.
import fsm_contracts as fc


# The versioned Handoff schema (machine-first; bumped on a breaking change to
# the field set). Slot-schema version, distinct from the feature version.
# 1.1.0 (additive, backward-compatible): adds the `concerns` field — a list of
# self-flagged doubts the implementer wants a reviewer/human to look harder at
# (analogous to superpowers' DONE_WITH_CONCERNS; auto-maintainer-framework#212).
# It mirrors `discovered_work` (always present, defaults to an empty list); the
# dry-run reference adapter flags nothing, so `concerns` is always [].
# 1.2.0 (additive, backward-compatible): adds `already_done` to the `status`
# value space — a TERMINAL already-satisfied outcome the doer emits when the
# requested change is already present on `main`, carrying its evidence in
# artifact {kind:"already-on-main", ref:<commit-sha>}. No existing field or
# status changed; a prior consumer that does not recognize `already_done` simply
# treats it as a non-`opened` handoff (which it is).
HANDOFF_SCHEMA_VERSION = "1.2.0"

# The fsm-contracts slot descriptor. `handoffs` is an array slot (a list of
# Handoff dicts); the slot version tracks the schema version. Mirrors
# work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
HANDOFFS_SLOT = {
    "name": "handoffs",
    "schema": {"type": "array"},
    "version": HANDOFF_SCHEMA_VERSION,
}

# Closed signal set IMPLEMENT emits: OK when handoffs were produced (or the plan
# was empty), BLOCKED when a plan entry was malformed.
IMPLEMENT_SIGNALS = ["OK", "BLOCKED"]

# Per-state manifest (bounded-scope contract): reads execution_plan and
# work_orders (NOT workspace), writes handoffs, emits OK | BLOCKED. The dry-run
# adapter uses only execution_plan; work_orders is declared so the ACTING
# IMPLEMENT's per-item dispatch can join each execution_plan.ordered id to its
# full WorkOrder record via agent_dispatch.build_envelopes.
IMPLEMENT_MANIFEST = fc.StateManifest(
    reads=["execution_plan", "work_orders"], writes=["handoffs"],
    emits=IMPLEMENT_SIGNALS)


def _planned_handoff(work_order_id):
    """A `planned` handoff: the dry-run rung performed no work, so status is
    always `planned` and the artifact is always `none`. The dry-run adapter
    self-flags nothing, so `concerns` is always empty."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "work_order_id": work_order_id,
        "status": "planned",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": None,
    }


def _blocked_handoff(work_order_id, reason):
    """A `blocked` handoff for a malformed plan entry: no work could be turned
    into a handoff, so blocked_reason is set and status is `blocked`. The dry-run
    adapter self-flags nothing, so `concerns` is always empty."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "work_order_id": work_order_id,
        "status": "blocked",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": reason,
    }


def _entry_id(entry):
    """Extract the work_order_id from an ordered plan entry. PRIORITIZE writes a
    list of id strings; accept a `{"id": ...}` dict shape too. Returns the id
    string when present and non-empty, else None (malformed)."""
    if isinstance(entry, str):
        return entry or None
    if isinstance(entry, dict):
        oid = entry.get("id")
        return oid if isinstance(oid, str) and oid else None
    return None


def run(ctx):
    """The dry-run IMPLEMENT state. Reads the `execution_plan` slot, iterates
    `execution_plan["ordered"]` in order, and emits ONE handoff per entry:
    a `planned` handoff for a well-formed id, or a `blocked` handoff (with
    blocked_reason set) for a malformed entry. Processes the WHOLE plan — there
    is no budget cap. Writes the `handoffs` slot and emits OK, unless any entry
    was malformed in which case the state signal is BLOCKED. An empty plan
    yields handoffs=[] and OK.

    Pure function of `execution_plan`: same input -> byte-identical handoffs. No
    model, no wall-clock, no randomness, no network, no filesystem, no VCS.
    """
    plan = ctx.read("execution_plan") or {}
    ordered = plan.get("ordered") or []

    handoffs = []
    any_blocked = False
    for entry in ordered:
        oid = _entry_id(entry)
        if oid is None:
            any_blocked = True
            handoffs.append(_blocked_handoff(
                "", "malformed plan entry: missing or empty work_order_id"))
        else:
            handoffs.append(_planned_handoff(oid))

    signal = "BLOCKED" if any_blocked else "OK"
    return fc.StateResult(signal=signal, writes={"handoffs": handoffs})


# Result of the deterministic handoff validity predicate (DESIGN §3.6.3): a
# machine-checkable {valid, reason}. reason is None when valid, else a short
# string naming why the handoff is rejected.
ValidationResult = collections.namedtuple("ValidationResult", ["valid", "reason"])


def validate_handoff(handoff):
    """Deterministic validity predicate over a Handoff (DESIGN §3.6.3, FT-A).

    IMPLEMENT is the deterministic correctness gate: an `opened` handoff (an
    `accepted` order that opened a PR) is VALID only when it carries a
    `test_verdict` whose `passed` is True. The verdict is the SCRIPT-produced
    result recorded by test_gate.py — never the model's prose. An opened handoff
    with a missing or failing verdict is INVALID. A non-`opened` handoff
    (`planned` dry-run, `blocked`, `already_done`; legacy `closed`) opened no PR
    and so requires no verdict to be valid. An `already_done` handoff resolved
    the item without acting (the fix was already on `main`), so like
    `blocked`/`planned` it is VALID without a `test_verdict`. The doer no longer
    emits `closed` (reject disposition moved to TRIAGE), but the predicate stays
    tolerant of a legacy `closed` handoff for backward compatibility.

    Pure function of the handoff dict: same input -> same ValidationResult.
    """
    if handoff.get("status") != "opened":
        return ValidationResult(True, None)

    verdict = handoff.get("test_verdict")
    if not isinstance(verdict, dict):
        return ValidationResult(
            False, "opened handoff is missing the script-produced test_verdict")
    if verdict.get("passed") is not True:
        return ValidationResult(
            False, "opened handoff carries a failing test_verdict")
    return ValidationResult(True, None)


def factory(runtime):  # noqa: ARG001 - IMPLEMENT binds no runtime config
    """The adapter-wiring factory: `factory(runtime) -> (StateManifest,
    run_callable)`. The dry-run IMPLEMENT is deterministic and binds no runtime
    config, so `runtime` is unused; it returns the static manifest and the `run`
    callable so scheduling.run_tick can map IMPLEMENT in DEFAULT_ADAPTER_MAP."""
    return IMPLEMENT_MANIFEST, run
