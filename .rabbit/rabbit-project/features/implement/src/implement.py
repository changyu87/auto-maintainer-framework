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

It reads `execution_plan` ONLY. DESIGN §2.6 also lists `workspace` for
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

# fsm-contracts is a sibling feature; tests inject its src/ onto sys.path
# exactly as the work-intake adapters do, so importing by module name resolves
# the sibling src/ on the path.
import fsm_contracts as fc


# The versioned Handoff schema (machine-first; bumped on a breaking change to
# the field set). Slot-schema version, distinct from the feature version.
HANDOFF_SCHEMA_VERSION = "1.0.0"

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

# Per-state manifest (bounded-scope contract): reads execution_plan (NOT
# workspace), writes handoffs, emits OK | BLOCKED.
IMPLEMENT_MANIFEST = fc.StateManifest(
    reads=["execution_plan"], writes=["handoffs"], emits=IMPLEMENT_SIGNALS)


def _planned_handoff(work_order_id):
    """A `planned` handoff: the dry-run rung performed no work, so status is
    always `planned` and the artifact is always `none`."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "work_order_id": work_order_id,
        "status": "planned",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
        "blocked_reason": None,
    }


def _blocked_handoff(work_order_id, reason):
    """A `blocked` handoff for a malformed plan entry: no work could be turned
    into a handoff, so blocked_reason is set and status is `blocked`."""
    return {
        "schema_version": HANDOFF_SCHEMA_VERSION,
        "work_order_id": work_order_id,
        "status": "blocked",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
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


def factory(runtime):  # noqa: ARG001 - IMPLEMENT binds no runtime config
    """The adapter-wiring factory: `factory(runtime) -> (StateManifest,
    run_callable)`. The dry-run IMPLEMENT is deterministic and binds no runtime
    config, so `runtime` is unused; it returns the static manifest and the `run`
    callable so scheduling.run_tick can map IMPLEMENT in DEFAULT_ADAPTER_MAP."""
    return IMPLEMENT_MANIFEST, run
