#!/usr/bin/env python3
"""prioritize — the deterministic PRIORITIZE adapter state.

The third read-side adapter (DESIGN §1.1, §2.6): turn the validated
`work_orders` TRIAGE produced into a concrete, ordered `execution_plan` the
downstream IMPLEMENT state consumes
(`PULL -> work_items -> TRIAGE -> work_orders -> PRIORITIZE -> execution_plan`).

PRIORITIZE is DETERMINISTIC — one of the spec's non-model states. It decides
execution order and back-fills a per-order status into the plan. It performs NO
outward effect: it reads and writes blackboard slots only, never the tracker or
the filesystem.

Public surface:
  - ExecutionPlan slot schema — `EXECUTION_PLAN_SCHEMA_VERSION` +
    `EXECUTION_PLAN_SLOT` (this feature OWNS the slot, mirroring how work-intake
    owns WorkItem/WorkOrder).
  - PRIORITIZE_MANIFEST — the state's {reads, writes, emits} manifest.
  - PRIORITIZE_SIGNALS — the closed signal set PRIORITIZE may emit (OK | EMPTY).
  - run(ctx) -> StateResult — the PRIORITIZE state callable.
  - factory(runtime) -> (StateManifest, run_callable) — the adapter-wiring
    factory convention; scheduling.run_tick maps PRIORITIZE through it.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when ordering ceases to be deterministic
  (e.g. a model-backed prioritizer adapter replaces the default), or when the
  ExecutionPlan schema reaches a breaking major version. See docs/spec.md.
"""

# fsm-contracts is a sibling feature; tests inject its src/ onto sys.path
# exactly as the work-intake adapters do, so importing by module name resolves
# the sibling src/ on the path.
import fsm_contracts as fc


# The versioned ExecutionPlan schema (machine-first; bumped on a breaking change
# to the field set). Slot-schema version, distinct from the feature version.
EXECUTION_PLAN_SCHEMA_VERSION = "1.0.0"

# The fsm-contracts slot descriptor. `execution_plan` is an object slot (a dict
# with `ordered` + `status`); the slot version tracks the schema version.
# Mirrors work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
EXECUTION_PLAN_SLOT = {
    "name": "execution_plan",
    "schema": {"type": "object"},
    "version": EXECUTION_PLAN_SCHEMA_VERSION,
}

# Closed signal set PRIORITIZE emits: OK when the plan has any entry, else EMPTY.
PRIORITIZE_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope contract): reads work_orders, writes
# execution_plan, emits OK | EMPTY.
PRIORITIZE_MANIFEST = fc.StateManifest(
    reads=["work_orders"], writes=["execution_plan"], emits=PRIORITIZE_SIGNALS)


def run(ctx):
    """The PRIORITIZE state. Reads the `work_orders` slot, keeps only the
    accepted orders, preserves TRIAGE's identity / FIFO order, back-fills
    `status[id] = "pending"` for each, writes the `execution_plan` slot, and
    emits OK when the plan has at least one entry else EMPTY.

    Pure function of `work_orders`: same input -> byte-identical plan. No model,
    no wall-clock, no randomness, no network, no filesystem.
    """
    orders = ctx.read("work_orders") or []
    ordered = [o["id"] for o in orders if o.get("decision") == "accepted"]
    status = {oid: "pending" for oid in ordered}
    plan = {
        "schema_version": EXECUTION_PLAN_SCHEMA_VERSION,
        "ordered": ordered,
        "status": status,
    }
    signal = "OK" if ordered else "EMPTY"
    return fc.StateResult(signal=signal, writes={"execution_plan": plan})


def factory(runtime):  # noqa: ARG001 - PRIORITIZE binds no runtime config
    """The adapter-wiring factory: `factory(runtime) -> (StateManifest,
    run_callable)`. PRIORITIZE is deterministic and binds no runtime config, so
    `runtime` is unused; it returns the static manifest and the `run` callable
    so scheduling.run_tick can map PRIORITIZE in DEFAULT_ADAPTER_MAP."""
    return PRIORITIZE_MANIFEST, run
