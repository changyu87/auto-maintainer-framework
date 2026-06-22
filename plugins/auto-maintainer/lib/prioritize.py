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

PRIORITIZE also SERIALIZES same-feature work orders (issue #214): IMPLEMENT fans
out one implementer per planned order in parallel, each branching off the same
`main`; two orders that touch the SAME feature would each bump that feature's
shared metadata (feature.json/CHANGELOG/contract + the built plugin tree) off
the same base and collide on merge. To prevent that, PRIORITIZE keeps at most
one order per target feature per tick (FIFO-first wins the slot) and defers the
rest to a later tick. A feature is detected from AUTHORITATIVE signals only —
`feature:`/`component:`-prefixed labels, a `Component:` body line, and a
conventional title prefix (`name:` / `type(scope):`, for label-less issues) —
never generic labels nor a bare conventional-commit type; orders with no
provable feature stay parallel.

Public surface:
  - ExecutionPlan slot schema — `EXECUTION_PLAN_SCHEMA_VERSION` +
    `EXECUTION_PLAN_SLOT` (this feature OWNS the slot, mirroring how work-intake
    owns WorkItem/WorkOrder).
  - PRIORITIZE_MANIFEST — the state's {reads, writes, emits} manifest.
  - PRIORITIZE_SIGNALS — the closed signal set PRIORITIZE may emit (OK | EMPTY).
  - run(ctx) -> StateResult — the PRIORITIZE state callable.
  - factory(runtime) -> (StateManifest, run_callable) — the adapter-wiring
    factory convention; scheduling.run_tick maps PRIORITIZE through it.

Version: 0.3.0
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


# The label-prefix convention that authoritatively declares a work order's
# target feature: `feature:<name>` (the maintainer's own filing convention, e.g.
# `feature:scheduling`) or `component:<name>`. ONLY prefixed labels name a
# feature — generic labels (`bug`, `enhancement`, `filed-by:...`, `priority:*`)
# are NOT feature keys, so two `enhancement`-labelled orders for different
# features are never serialized against each other (issue #214 detection fix).
_FEATURE_LABEL_PREFIXES = ("feature:", "component:")

# The `Component:`/`Feature:` line convention in a free-form issue body, e.g.
# "Component: scheduling" or "Scope: ... Component: scheduling, prioritize".
# Captures the remainder of the line for splitting into one or more features.
_COMPONENT_LINE_RE = re.compile(
    r"^\s*(?:component|feature)s?\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# Connectors that separate multiple features in a multi-feature blast radius.
# Deliberately EXCLUDES the word "and" — an "and" split can mis-cut a feature
# name (e.g. "command-and-control") and is unsafe to infer a shared radius from
# (issue #214 guidance item 3); a real multi-feature radius uses punctuation.
_FEATURE_SPLIT_RE = re.compile(r"[+,&/]")

# The conventional title-prefix convention that names a work order's target
# feature for LABEL-LESS issues (issue #257), matching `name: ...` (the bare
# prefix, e.g. `scheduling: ...`) and `type(scope): ...` (a conventional-commit
# header, e.g. `feat(scheduling): ...` / `fix(work-intake): ...`). The optional
# `(scope)` is captured separately so a scoped header yields the scope as the
# feature; a bare prefix yields the name. The title's `Type: x` blast-radius is
# distinct from a `Component:` body line and never spans multiple features.
_TITLE_PREFIX_RE = re.compile(r"^\s*([^\s():]+)\s*(?:\(([^)]+)\))?\s*:")

# Bare conventional-commit TYPES used without a scope (e.g. `fix: x`,
# `docs: y`). These are NOT feature names — grouping on a bare type would
# over-serialize unrelated work orders (the #216 regression), so a `name:`
# prefix whose name is a bare type claims NO feature. A `type(scope):` header is
# unaffected: the scope (not the type) is the feature key.
_CONVENTIONAL_COMMIT_TYPES = frozenset({
    "feat", "fix", "docs", "chore", "refactor",
    "test", "perf", "build", "ci", "style",
})


def _normalize_feature(token):
    """Normalize a raw feature token to a comparison key: trimmed, lower-cased.
    Returns None for an empty/whitespace token so it never claims a feature."""
    key = token.strip().lower()
    return key or None


def _title_feature(title):
    """Derive the target feature from a conventional title prefix, or None.

    Recognizes `name: ...` (take `name`, e.g. `scheduling: ...`) and
    `type(scope): ...` (take `scope`, e.g. `feat(scheduling): ...` /
    `fix(work-intake): ...`). A bare conventional-commit type used WITHOUT a
    scope (`fix: x`, `docs: y`) names NO feature, so unrelated `fix:`/`docs:`
    orders are not falsely grouped (the #216 over-serialization regression); a
    scoped header is unaffected because the scope, not the type, is the key.
    """
    match = _TITLE_PREFIX_RE.match(title or "")
    if not match:
        return None
    name, scope = match.group(1), match.group(2)
    if scope is not None:
        # `type(scope):` — the scope is the feature, whatever the type.
        return _normalize_feature(scope)
    # `name:` — a bare conventional-commit type names no feature.
    if name.strip().lower() in _CONVENTIONAL_COMMIT_TYPES:
        return None
    return _normalize_feature(name)


def _features_for(order):
    """The set of target features an accepted order's blast radius touches, from
    AUTHORITATIVE signals only:

    1. `feature:<name>` / `component:<name>` prefixed labels (the filing
       convention) — generic labels are ignored.
    2. a `Component:`/`Feature:` line in the issue body, split on +,&/, into one
       or more feature names (never on the word "and").
    3. a conventional title prefix — `name:` or `type(scope):` — which covers
       LABEL-LESS issues (issue #257) that carry no feature label or body line;
       a bare conventional-commit type (`fix:`, `docs:`) names no feature.

    Returns an EMPTY set when no feature is provable. An order with no provable
    feature is never serialized against any other order (it stays parallel),
    because serialization must rest on a *proven* shared feature, never a guess.
    """
    features = set()

    for label in order.get("labels") or []:
        if not isinstance(label, str):
            continue
        low = label.lower()
        for prefix in _FEATURE_LABEL_PREFIXES:
            if low.startswith(prefix):
                name = _normalize_feature(label[len(prefix):])
                if name:
                    features.add(name)
                break

    for match in _COMPONENT_LINE_RE.finditer(order.get("body") or ""):
        for token in _FEATURE_SPLIT_RE.split(match.group(1)):
            name = _normalize_feature(token)
            if name:
                features.add(name)

    title_feature = _title_feature(order.get("title"))
    if title_feature:
        features.add(title_feature)

    return features


def _serialize_same_feature(orders):
    """Given the accepted orders in FIFO order, return the ids to fan out this
    tick: at most ONE order per target feature, the FIFO-first claiming the
    feature's slot. Later orders that share an already-claimed feature are
    DEFERRED (omitted) so two PRs never bump the same feature's shared metadata
    off the same base; once the head order's PR merges, the deferred order is
    re-pulled and fans out next tick.

    Orders with no provable feature (empty feature set) carry no shared blast
    radius and stay parallel — they are always kept. Cross-feature orders
    (disjoint feature sets) also stay parallel.
    """
    kept = []
    claimed = set()
    for order in orders:
        features = _features_for(order)
        if features and (features & claimed):
            # A feature this order touches is already claimed by an earlier
            # order this tick — defer it to avoid the shared-metadata collision.
            continue
        claimed |= features
        kept.append(order["id"])
    return kept


def run(ctx):
    """The PRIORITIZE state. Reads the `work_orders` slot, keeps only the
    accepted orders, preserves TRIAGE's identity / FIFO order, serializes orders
    that share a target feature (at most one per feature per tick; the rest
    deferred to a later tick), back-fills `status[id] = "pending"` for every
    fanned-out order, writes the `execution_plan` slot, and emits OK when the
    plan has at least one entry else EMPTY.

    Pure function of `work_orders`: same input -> byte-identical plan. No model,
    no wall-clock, no randomness, no network, no filesystem.
    """
    orders = ctx.read("work_orders") or []
    accepted = [o for o in orders if o.get("decision") == "accepted"]
    ordered = _serialize_same_feature(accepted)
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
