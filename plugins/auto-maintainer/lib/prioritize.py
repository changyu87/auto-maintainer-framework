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

Same-feature serialization (#214, the v1-minimal slice of DESIGN §2.2 / §3.8.6):
IMPLEMENT fans out one implementer PER ordered work_order IN PARALLEL, each
branching from the same `main`. Two work_orders that touch the SAME feature each
bump that feature's SHARED metadata (feature.json version, docs/CHANGELOG.md,
docs/contract.md) and regenerate the committed plugins/ tree, so the moment one
auto-PR lands the others CONFLICT on that shared metadata (observed: #205/#207
collided after #208 merged; #206 survived only because it targeted a different
feature). PRIORITIZE is the choke point that decides what fans out, so it
SERIALIZES same-feature work: it keeps at most ONE accepted work_order per
feature in `ordered` (the FIFO-first one wins the slot), DEFERRING the rest of
that feature's orders to a later tick. A deferred order is simply absent from the
plan this tick — it is never acted, never recorded in the acted-ledger, so the
loop re-pulls + re-prioritizes it next tick; once the head order's PR merges, the
next same-feature order becomes the head and fans out then. CROSS-feature orders
stay PARALLEL (one per distinct feature fans out in the same tick), so the
non-colliding case is unaffected. The plan surface is UNCHANGED (no `groups`
key — grouping stays [v2]); serialization only SHRINKS `ordered`.

A work_order's FEATURE is inferred deterministically from its declared
blast-radius: a `Component:` line in the issue body (the convention the loop's
own issues already use, e.g. `Component: scheduling.`), falling back to the
work_order's labels, then to a per-order unique bucket (so an order with no
declarable feature is never serialized against another — it stays parallel).

Public surface:
  - ExecutionPlan slot schema — `EXECUTION_PLAN_SCHEMA_VERSION` +
    `EXECUTION_PLAN_SLOT` (this feature OWNS the slot, mirroring how work-intake
    owns WorkItem/WorkOrder).
  - PRIORITIZE_MANIFEST — the state's {reads, writes, emits} manifest.
  - PRIORITIZE_SIGNALS — the closed signal set PRIORITIZE may emit (OK | EMPTY).
  - run(ctx) -> StateResult — the PRIORITIZE state callable.
  - factory(runtime) -> (StateManifest, run_callable) — the adapter-wiring
    factory convention; scheduling.run_tick maps PRIORITIZE through it.

Version: 0.2.0
Owner: changyu87
Deprecation criterion: Superseded when ordering ceases to be deterministic
  (e.g. a model-backed prioritizer adapter replaces the default), or when the
  ExecutionPlan schema reaches a breaking major version. See docs/spec.md.
"""

import re

# fsm-contracts is a sibling feature; tests inject its src/ onto sys.path
# exactly as the work-intake adapters do, so importing by module name resolves
# the sibling src/ on the path.
# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
import os  # noqa: E402
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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


# A `Component:` (or `Component.`) declaration in an issue body names the
# work_order's blast-radius feature(s) — the convention the maintainer's own
# issues already use, e.g. a trailing `Component: scheduling.` line. The match
# is case-insensitive and tolerant of `:` or `.` after the keyword; the captured
# text runs to end-of-line.
_COMPONENT_RE = re.compile(r"(?im)^\s*component\s*[:.]\s*(?P<value>.+?)\s*$")

# A declared component value may name SEVERAL features (a work_order whose blast
# radius spans more than one, e.g. `verify-integrate + scheduling`). Such an
# order shares a feature with EITHER, so it must serialize against both. Split
# on the connectors the issues use (`+`, `,`, `&`, the word `and`, `/`).
_FEATURE_SPLIT_RE = re.compile(r"\s*(?:\+|,|&|/|\band\b)\s*", re.IGNORECASE)

# Trailing punctuation a declared feature token may carry (e.g. the `.` ending a
# `Component: scheduling.` sentence) — stripped so `scheduling` and `scheduling.`
# are the SAME feature.
_FEATURE_STRIP = " \t.;:"


def _features_for(order):
    """The set of blast-radius feature keys an accepted WorkOrder dict belongs
    to, for same-feature serialization (#214). Deterministic, derived ONLY from
    the order's declared scope (no model, no I/O):

      1. Every feature named in a `Component:`/`Component.` line of the order's
         `body` (split on `+`/`,`/`&`/`and`/`/` so a multi-feature blast radius
         serializes against each), normalized (lowercased, trimmed of trailing
         punctuation).
      2. Else every label on the order (labels often name the component).
      3. Else a per-order UNIQUE sentinel keyed on the order id, so an order with
         NO declarable feature is never grouped with another — it stays parallel
         (the safe default: serialize only when we can prove a shared feature).

    Returns a set of lowercase feature strings (never empty)."""
    body = order.get("body") or ""
    features = set()
    for m in _COMPONENT_RE.finditer(body):
        for tok in _FEATURE_SPLIT_RE.split(m.group("value")):
            key = tok.strip(_FEATURE_STRIP).lower()
            if key:
                features.add(key)
    if features:
        return features
    labels = order.get("labels") or []
    label_features = {str(lbl).strip(_FEATURE_STRIP).lower()
                      for lbl in labels if str(lbl).strip(_FEATURE_STRIP)}
    if label_features:
        return label_features
    # No declarable feature — give the order its own private bucket so it never
    # serializes against anything else (unique by id; id is unique per order).
    return {"\x00order:" + str(order.get("id"))}


def _serialize_same_feature(accepted):
    """Keep at most ONE accepted order per feature, in FIFO order (#214).

    `accepted` is the FIFO list of accepted WorkOrder dicts. Walking it in order,
    an order is KEPT (fans out this tick) iff NONE of its features
    (`_features_for`) has already been claimed by an earlier kept order; the
    moment an order is kept it CLAIMS all of its features. A later order that
    shares ANY feature with a kept one is DEFERRED (dropped from this plan) — it
    re-enters a future tick once the head order's PR lands. Cross-feature orders
    (disjoint feature sets) are ALL kept, so they fan out in parallel.

    Returns the filtered list of kept order dicts, preserving FIFO order. Pure /
    deterministic: same input -> same output."""
    claimed = set()
    kept = []
    for order in accepted:
        features = _features_for(order)
        if features & claimed:
            continue  # a same-feature order already won this tick — defer
        claimed |= features
        kept.append(order)
    return kept


def run(ctx):
    """The PRIORITIZE state. Reads the `work_orders` slot, keeps only the
    accepted orders, SERIALIZES same-feature orders (keeps at most one per
    feature per tick — #214), preserves TRIAGE's identity / FIFO order among the
    kept orders, back-fills `status[id] = "pending"` for each, writes the
    `execution_plan` slot, and emits OK when the plan has at least one entry else
    EMPTY.

    Pure function of `work_orders`: same input -> byte-identical plan. No model,
    no wall-clock, no randomness, no network, no filesystem.
    """
    orders = ctx.read("work_orders") or []
    accepted = [o for o in orders if o.get("decision") == "accepted"]
    kept = _serialize_same_feature(accepted)
    ordered = [o["id"] for o in kept]
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
