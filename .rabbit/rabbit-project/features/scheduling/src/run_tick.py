#!/usr/bin/env python3
"""run_tick — the deterministic single-tick runner for the maintainer loop.

One invocation = one tick. This is the script-tier core of the scheduling
feature (spec-rules §1). Slice 3 makes the route DATA: instead of hardcoding the
state graph, run_tick defines the shipped DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP and
hands them to adapter-wiring's build_loop, which loads a project-local override
(else the default), resolves each routed port to its built-in adapter via the
factory convention, validates the wiring at LOAD, and returns (route, states)
ready for tick_orchestrator.run.

The shipped DEFAULT_ROUTE is the read-and-idle spine

    GUARD -> DRAIN -> PULL -> PERSIST -> EXIT

over the already-implemented anchors:

  - GUARD / EXIT come from lifecycle-dispositions (entry gate + single-writer
    mutex; terminal disposition selection + mutex release);
  - DRAIN / PERSIST come from durable-state (crash-recovery replay; durable
    flush);
  - PULL / TRIAGE come from work-intake (PULL writes work_items; TRIAGE gates
    them into work_orders). TRIAGE is in DEFAULT_ADAPTER_MAP — resolvable — even
    though the default route omits it, so inserting it is a pure route.json edit.

Route-as-data override: a project-local
``${project_dir}/.auto-maintainer/route.json`` (and optional
``adapter-map.json``) overrides the defaults. Inserting TRIAGE between PULL and
PERSIST is a DATA edit; adapter-wiring resolves it from the map and validates it
at load. A broken override (e.g. TRIAGE before PULL, or an unknown port) raises a
locatable WiringError BEFORE any tick body runs.

Read-and-idle (spec slice 2): with only read stages and no act stage yet, the
tick seeds the EXIT outcome to "empty" so EXIT selects IDLE rather than refire.
A pure read produces nothing to act on, so refiring would busy-loop; the
heartbeat re-pulls on the next interval instead. EXIT's refire/idle becomes
work-driven again once an act stage lands.

PULL's issue source is the live `gh` CLI in production but is INJECTABLE so tests
pass a stub over fixture issues (no network) — the determinism seam.

Runtime paths (durable state, journal, disposition + lock markers) are injected
so tests use a temp dir and the on-disk files are the only source of truth.

scheduling CONSUMES fsm-contracts, tick-orchestrator, durable-state,
lifecycle-dispositions, work-intake, and adapter-wiring UNCHANGED; it never edits
or forks them.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the tick interval becomes
  config-driven and this slice's hardcoding is removed.
"""

import argparse
import contextlib
import io
import json
import os
import sys
from datetime import datetime

# Consume the sibling features via sys.path, exactly as the other feature
# sources/tests do. Resolve them relative to this file's feature dir so the
# runner works both in the worktree and from the shipped plugin layout. In the
# installed plugin `lib/` the modules are flat siblings of this file (already on
# the path once _SRC is inserted).
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_FEATURE_DIR = os.path.dirname(_SRC)
_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import fsm_contracts as fc  # noqa: E402
import tick_orchestrator as to  # noqa: E402
import durable_state as ds  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import work_intake as wi  # noqa: E402
import adapter_wiring as aw  # noqa: E402
import prioritize as pr  # noqa: E402
import implement as im  # noqa: E402
import safety_governance as sg  # noqa: E402
import agent_dispatch as ad  # noqa: E402


# The production PULL issue source: work-intake's live `gh` CLI adapter. Tests
# inject a stub instead so the suite touches no network; the shipped run_tick
# (no injected source) pulls real open issues.
DEFAULT_PULL_SOURCE = wi.gh_issue_source

# The durable-state document keys under which the last tick's pulled work_items
# (and triaged work_orders, when the active route produced them) are persisted,
# so status.py can report the counts without re-running the loop.
WORK_ITEMS_KEY = "work_items"
WORK_ORDERS_KEY = "work_orders"
EXECUTION_PLAN_KEY = "execution_plan"
HANDOFFS_KEY = "handoffs"

# The durable-state document key under which the safety-governance budget window
# {window_key, spent_tokens} is persisted. Unlike the four read-product keys
# above, BUDGET is a durable CROSS-TICK fact (like the counter), NOT a per-tick
# ephemeral read product (#64): a tick within the same window carries the
# accumulated spend forward; only a window rollover (inside sg.evaluate_budget)
# resets spent_tokens.
BUDGET_KEY = "budget"

# The durable-state document key under which the agent yield/resume CHECKPOINT is
# persisted while a tick is PAUSED at an agent-state (DESIGN §2.8 executor
# protocol). It is the SOLE source of truth for the paused dispatch (crash-safety):
# {next_state, slots (full live TickContext slot snapshot), path, signals,
#  pending: {state, writes, schema_ref, signal_rule, cardinality}}. It is cleared
# the moment the driver reaches the terminal. Unlike the read products it is a
# transient cross-invocation handoff, NOT a #64 read product.
TICK_CHECKPOINT_KEY = "tick_checkpoint"

# DONE is the true terminal: tick-orchestrator HALTS the moment it reaches a
# terminal state and NEVER run()s it, so EXIT must be a NON-terminal state that
# the loop actually runs (selecting the disposition + releasing the mutex). Its
# disposition signals (refire/idle/break/halt) all route to DONE. HALTED is the
# short-circuit terminal for GUARD's latch/restart path.
_DONE = "DONE"
_HALTED = "HALTED"


# --------------------------------------------------------------------------
# Built-in adapter factories (the adapter-wiring factory convention).
#
# Each factory(runtime) -> (StateManifest, run_callable) WRAPS the existing
# sibling adapter UNCHANGED. `runtime` is a dict carrying the resolved
# runtime_dir (for GUARD/EXIT marker I/O), the injectable PULL source, and the
# TRIAGE reference time. adapter-wiring resolves a routed port to its factory by
# the "run_tick:make_<port>" address in DEFAULT_ADAPTER_MAP.
# --------------------------------------------------------------------------

def make_guard(runtime):
    """GUARD anchor (lifecycle-dispositions): entry gate + single-writer mutex,
    bound to the runtime marker dir."""
    guard = ld.Guard(runtime["runtime_dir"])
    return guard.manifest, guard.run


def make_exit(runtime):
    """EXIT anchor (lifecycle-dispositions): tick-outcome -> disposition +
    signal, releasing the mutex, bound to the runtime marker dir."""
    exit_state = ld.Exit(runtime["runtime_dir"])
    return exit_state.manifest, exit_state.run


def make_drain(runtime):  # noqa: ARG001 - DRAIN reads its paths from ctx slots
    """DRAIN anchor (durable-state): crash-recovery replay. Reads its file paths
    from the seeded TickContext slots, so the runtime carries no extra config."""
    return ds.DRAIN_MANIFEST, ds.drain_run


def make_persist(runtime):  # noqa: ARG001 - PERSIST reads its paths from ctx
    """PERSIST anchor (durable-state): durable flush. Reads its file paths from
    the seeded TickContext slots."""
    return ds.PERSIST_MANIFEST, ds.persist_run


def make_pull(runtime):
    """PULL adapter (work-intake): fetch OPEN issues into the work_items slot.
    Binds the injectable issue source (default = work-intake's live gh source)."""
    source = runtime.get("source") or DEFAULT_PULL_SOURCE
    pull = wi.Pull(source=source)
    return wi.PULL_MANIFEST, pull.run


def make_triage(runtime):
    """TRIAGE adapter (work-intake): the deterministic validity gate mapping
    work_items -> work_orders. Binds the injectable reference time (default =
    now), so staleness is deterministic under test."""
    triage = wi.Triage(now=runtime.get("now"))
    return wi.TRIAGE_MANIFEST, triage.run


def make_prioritize(runtime):  # noqa: ARG001 - PRIORITIZE binds no runtime config
    """PRIORITIZE adapter (prioritize): the deterministic ordering gate mapping
    work_orders -> execution_plan. Binds no runtime config, so `runtime` is
    unused; returns the sibling manifest + run callable unchanged."""
    return pr.PRIORITIZE_MANIFEST, pr.run


def make_implement(runtime):  # noqa: ARG001 - IMPLEMENT binds no runtime config
    """IMPLEMENT adapter (implement, dry-run): the deterministic, INERT act
    state mapping execution_plan -> handoffs. Binds no runtime config, so
    `runtime` is unused; returns the sibling manifest + run callable unchanged."""
    return im.IMPLEMENT_MANIFEST, im.run


# --------------------------------------------------------------------------
# The shipped DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP (route-as-data).
#
# DEFAULT_ROUTE is the read-and-idle spine GUARD->DRAIN->PULL->PERSIST->EXIT
# plus the DONE/HALTED terminals — matching the prior hardcoded behaviour.
# DEFAULT_ADAPTER_MAP maps every KNOWN port (incl. TRIAGE) + the terminals to a
# "run_tick:make_<port>" factory address, so a route.json edit inserting TRIAGE
# wires it with NO code change.
# --------------------------------------------------------------------------

DEFAULT_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "PERSIST", "EXIT", _DONE, _HALTED],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": _HALTED},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": _HALTED},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "PERSIST"},
        {"state": "PULL", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": _DONE},
        {"state": "EXIT", "signal": "idle", "next": _DONE},
        {"state": "EXIT", "signal": "break", "next": _DONE},
        {"state": "EXIT", "signal": "halt", "next": _DONE},
    ],
    "terminal": [_DONE, _HALTED],
}

# Every known port -> its built-in factory address. TRIAGE, PRIORITIZE, and
# IMPLEMENT are included even though DEFAULT_ROUTE omits them (the
# ports-and-adapters promise: insert the act chain by data, no code change).
# The terminals are addressed too so adapter-wiring can resolve every state in a
# route (terminals never run(), but their manifests must resolve for validation).
DEFAULT_ADAPTER_MAP = {
    "GUARD": "run_tick:make_guard",
    "DRAIN": "run_tick:make_drain",
    "PULL": "run_tick:make_pull",
    "TRIAGE": "run_tick:make_triage",
    "PRIORITIZE": "run_tick:make_prioritize",
    "IMPLEMENT": "run_tick:make_implement",
    "PERSIST": "run_tick:make_persist",
    "EXIT": "run_tick:make_exit",
    _DONE: "run_tick:make_terminal",
    _HALTED: "run_tick:make_terminal",
}


def make_terminal(runtime):  # noqa: ARG001 - terminals never run()
    """A terminal anchor (DONE/HALTED): resolvable so the validator sees its
    (empty) manifest, but it never run()s — tick-orchestrator halts on reaching
    a terminal state."""
    manifest = fc.StateManifest(reads=[], writes=[], emits=[])

    def _run(ctx):  # pragma: no cover - terminal states never execute
        raise ld.LifecycleError("terminal state must never run()")

    return manifest, _run


# The closed signal vocabulary spanning every state in the route. It is a
# superset covering both the default spine and TRIAGE's OK/EMPTY signals.
_VOCAB = fc.SignalVocabulary([
    "OK", "EMPTY", "BLOCKED", "HALT_REQUESTED", "RESTART_REQUIRED",
    "refire", "idle", "break", "halt",
])

# Slots seeded into the TickContext BEFORE the route runs (the data-readiness
# `initial` set). PULL writes work_items, TRIAGE writes work_orders — those are
# produced by predecessors, not seeded.
_INITIAL_SLOTS = ["state_path", "journal_path", "counter", "tick_outcome"]


def _seed_context(state_path, journal_path, route):
    """A TickContext seeded from durable state plus the read-and-idle outcome.

    Registers the durable-state plumbing slots (counter/state_path/journal_path)
    that DRAIN/PERSIST read, the work_items slot PULL writes, the tick_outcome
    slot EXIT reads, and — when the active route includes them — the work_orders
    slot TRIAGE writes, the execution_plan slot PRIORITIZE writes, and the
    handoffs slot IMPLEMENT writes. tick_outcome is seeded "empty" so EXIT
    selects IDLE after the read stages (read-and-idle): the dry-run IMPLEMENT is
    INERT, so even an act-path tick leaves no remaining work and still idles.
    """
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])
    if "TRIAGE" in route["states"]:
        ctx.register_slot(
            wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
            version=wi.WORK_ORDERS_SLOT["version"])
    if "PRIORITIZE" in route["states"]:
        ctx.register_slot(
            pr.EXECUTION_PLAN_SLOT["name"], pr.EXECUTION_PLAN_SLOT["schema"],
            version=pr.EXECUTION_PLAN_SLOT["version"])
    if "IMPLEMENT" in route["states"]:
        ctx.register_slot(
            im.HANDOFFS_SLOT["name"], im.HANDOFFS_SLOT["schema"],
            version=im.HANDOFFS_SLOT["version"])
    ctx.write("state_path", state_path)
    ctx.write("journal_path", journal_path)
    ctx.write("counter", ds.DurableState(state_path).load()["counter"])
    # Read-and-idle: no act stage, so the tick always idles after the reads.
    ctx.write("tick_outcome", "empty")
    return ctx


def resolve_runtime_paths():
    """Resolve the default runtime paths for an INVOCATION WITH NO INJECTED
    PATHS — the installed-plugin case where the skill runs run_tick with cwd =
    the user's project and no temp dir is wired in.

    The durable-state / journal / disposition+lock markers default to a writable
    per-project runtime dir: ``${CLAUDE_PROJECT_DIR}/.auto-maintainer/`` when
    that env var is set, else ``.auto-maintainer/`` under cwd. Tests inject a
    temp dir instead, so this default is used only when nothing is injected.
    Returns ``(runtime_dir, state_path, journal_path)``.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    runtime_dir = os.path.join(project_dir, ".auto-maintainer")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "tick-journal.jsonl")
    return runtime_dir, state_path, journal_path


def _resolve_project_dir():
    """The project dir adapter-wiring reads the override config from: the
    CLAUDE_PROJECT_DIR env var when set, else cwd (the same anchor
    resolve_runtime_paths uses)."""
    return os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


# The project-local override config dir + route filename — the SAME constants
# adapter-wiring's loader uses, so route_source reports exactly what build_loop
# actually reads (#59). Kept in lock-step with adapter_wiring._CONFIG_DIRNAME /
# _ROUTE_FILENAME (consumed-unchanged, so referenced by value, not import).
_OVERRIDE_CONFIG_DIRNAME = ".auto-maintainer"
_OVERRIDE_ROUTE_FILENAME = "route.json"


def route_source(project_dir=None):
    """The SOURCE of the route this tick runs, so a misplaced/absent override is
    visible rather than silently ignored (#59).

    Returns ``("override", "<abs path>")`` when a project-local
    ``${project_dir}/.auto-maintainer/route.json`` exists — the SAME path
    adapter-wiring's loader reads — else ``("default", None)``. ``project_dir``
    defaults to the CLAUDE_PROJECT_DIR / cwd anchor, exactly as the loader and
    resolve_runtime_paths resolve it, so the reported source agrees with the
    route actually loaded. This is the single source of truth: status.py reuses
    it rather than re-probing for route.json.
    """
    if project_dir is None:
        project_dir = _resolve_project_dir()
    path = os.path.join(
        project_dir, _OVERRIDE_CONFIG_DIRNAME, _OVERRIDE_ROUTE_FILENAME)
    if os.path.isfile(path):
        return ("override", path)
    return ("default", None)


def route_source_label(project_dir=None):
    """The route source as a single trace/status token: ``default`` or
    ``override:<abs path>`` (#59)."""
    label, path = route_source(project_dir)
    if label == "override":
        return f"override:{path}"
    return label


def persisted_work_items(state_path):
    """The last pull's work_items snapshot persisted in durable state (a list of
    WorkItem dicts), or [] when the loop never ran a pull."""
    doc = ds.DurableState(state_path).load()
    return doc.get(WORK_ITEMS_KEY, [])


def persisted_work_items_count(state_path):
    """The count of work_items pulled by the last tick, from durable state."""
    return len(persisted_work_items(state_path))


def persisted_work_orders(state_path):
    """The last tick's work_orders snapshot persisted in durable state (a list of
    WorkOrder dicts), or [] when the active route produced none (e.g. the default
    route has no TRIAGE)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(WORK_ORDERS_KEY, [])


def persisted_work_orders_count(state_path):
    """The count of work_orders the last tick produced, from durable state."""
    return len(persisted_work_orders(state_path))


def persisted_execution_plan(state_path):
    """The last tick's execution_plan snapshot persisted in durable state (an
    object {ordered, status}), or {} when the active route produced none (e.g.
    the default route has no PRIORITIZE)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(EXECUTION_PLAN_KEY, {})


def persisted_execution_plan_count(state_path):
    """The count of ordered entries in the last tick's execution_plan, from
    durable state (len of the plan's `ordered` list; 0 when absent or empty)."""
    plan = persisted_execution_plan(state_path)
    return len(plan.get("ordered", []))


def persisted_handoffs(state_path):
    """The last tick's handoffs snapshot persisted in durable state (a list of
    Handoff dicts), or [] when the active route produced none (e.g. the default
    route has no IMPLEMENT)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(HANDOFFS_KEY, [])


def persisted_handoffs_count(state_path):
    """The count of handoffs the last tick produced, from durable state."""
    return len(persisted_handoffs(state_path))


def persisted_budget_state(state_path):
    """The durable budget window {window_key, spent_tokens} persisted in durable
    state, or {} when the loop never evaluated a budget. Unlike the read products
    this is a durable CROSS-TICK fact (see BUDGET_KEY)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(BUDGET_KEY, {})


def persisted_tick_checkpoint(state_path):
    """The durable PAUSED checkpoint persisted under TICK_CHECKPOINT_KEY while a
    tick is paused at an agent-state, or {} when no tick is paused. The
    checkpoint is the SOLE source of truth for the paused dispatch (crash-safety
    re-emit)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(TICK_CHECKPOINT_KEY, {})


def _budget_clock(now):
    """The tz-aware `now` the budget window keys off.

    safety-governance never reads the wall clock — `now` is always injected. Use
    the injected `now` when it is tz-aware; otherwise default to the host
    local-aware now (datetime.now().astimezone()), so window_key never sees a
    naive datetime. A naive injected `now` likewise falls back to the host-local
    now (the budget clock must always be tz-aware)."""
    if now is not None and now.tzinfo is not None:
        return now
    return datetime.now().astimezone()


def governance_fields(gov, budget_state, budget=None):
    """Render the always-shown governance surface as a trace/status token string.

    Returns ``mode=<mode> budget=<spent>/<ceiling-or-"none"> win=<window_key>``,
    appending ``budget_paused=<reason>`` when `budget` (an sg.evaluate_budget
    result) reports allowed=False. A null per_day ceiling renders as "none"
    (unlimited). Shared by the tick trace and status.py so the two never diverge
    (#69). `budget_state` is the {window_key, spent_tokens} window; an empty
    state renders 0 spend with an empty window key.
    """
    mode = gov.get("mode", "")
    ceiling = gov.get("budget", {}).get("per_day_tokens")
    ceiling_str = "none" if ceiling is None else str(ceiling)
    spent = budget_state.get("spent_tokens", 0)
    win = budget_state.get("window_key", "")
    field = f"mode={mode} budget={spent}/{ceiling_str} win={win}"
    if budget is not None and not budget.get("allowed", True):
        field += f" budget_paused={budget.get('reason', '')}"
    return field


def governance_status(project_dir, state_path):
    """The governance surface for status.py: loads gov + the durable budget
    window and renders the same always-shown field string the tick trace prints
    (#69). Reads only — never writes or advances the budget.

    The budget is evaluated AT the persisted window (a clock on the persisted
    window_key's date), NOT at the host wall-clock now, so status reports the
    last tick's durable exhaustion rather than rolling the window over to today
    (which would mask a paused window). When no budget was ever persisted the
    window is empty and the budget reads as allowed (nothing spent).
    """
    gov = sg.load_governance(project_dir)
    budget_state = persisted_budget_state(state_path)
    clock = _clock_for_window(budget_state.get("window_key"))
    budget = sg.evaluate_budget(gov, budget_state, clock)
    return governance_fields(gov, budget_state, budget)


def _clock_for_window(window_key):
    """A tz-aware datetime whose LOCAL date is `window_key` (an ISO date string),
    so sg.evaluate_budget keys to the persisted window without rolling it over.
    Falls back to the host local-aware now when `window_key` is absent/unparsable
    (no persisted window yet)."""
    local_now = datetime.now().astimezone()
    if not window_key:
        return local_now
    try:
        d = datetime.strptime(window_key, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return local_now
    return datetime(d.year, d.month, d.day, 12, 0, 0, tzinfo=local_now.tzinfo)


# --------------------------------------------------------------------------
# Agent yield/resume seam (DESIGN §2.8 executor protocol).
#
# A route that contains agent-states pauses at each agent-state (emitting a
# rendered dispatch request) and resumes when given the dispatch result. The
# driver below mirrors tick_orchestrator.run but is agent-aware: it runs SCRIPT
# states inline and YIELDS a PAUSED result at an AGENT state. run_tick NEVER
# calls the Agent tool — the executor (a later slice) performs the dispatch and
# feeds the raw outputs back via resume_dispatch.
# --------------------------------------------------------------------------

# The known slot descriptors, indexed by slot name, so resume can look up the
# JSON-Schema of an agent-state's `writes` slot to validate the returned text
# (agent_dispatch.validate_output). These mirror the slots _seed_context
# registers; built from the sibling SLOT descriptors (consumed unchanged).
_SLOT_SCHEMAS = {
    wi.WORK_ITEMS_SLOT["name"]: wi.WORK_ITEMS_SLOT["schema"],
    wi.WORK_ORDERS_SLOT["name"]: wi.WORK_ORDERS_SLOT["schema"],
    pr.EXECUTION_PLAN_SLOT["name"]: pr.EXECUTION_PLAN_SLOT["schema"],
    im.HANDOFFS_SLOT["name"]: im.HANDOFFS_SLOT["schema"],
}


def _snapshot_slots(ctx):
    """A {slot_name: value} snapshot of every WRITTEN slot in the TickContext, so
    the live blackboard can be checkpointed and restored across invocations."""
    snapshot = {}
    for name in ctx.registered_slots():
        try:
            snapshot[name] = ctx.read(name)
        except fc.ContractError:
            # An unwritten slot has no value to snapshot.
            continue
    return snapshot


def _restore_slots(ctx, slots):
    """Restore a checkpointed slot snapshot onto a freshly-seeded TickContext.

    Only slots the context already registered are restored (the route's seeded
    set); a checkpoint slot for a stage the current route omits is ignored."""
    for name, value in slots.items():
        if name in ctx.registered_slots():
            ctx.write(name, value)


def _pause_result(name, agentstate, slot_values, tick_id, mode):
    """Build the PAUSED result for an agent-state: render the dispatch
    envelope(s) via agent-dispatch and shape the per-dispatch records the
    executor consumes. Returns the PAUSED dict.

    `slot_values` is the {slot: value} mapping for the agent-state's read slots.
    A `once` dispatch yields one record (no `item`); a {per_item: path} dispatch
    yields one record per resolved element, each carrying its `item`.
    """
    entry = agentstate.entry
    envelopes = ad.build_envelopes(
        entry, slot_values, {"tick_id": tick_id, "mode": mode}, state=name)
    signal_rule = entry["signal"]["rule"]
    dispatches = []
    # build_envelopes flattens all dispatch entries; this slice's agent-states
    # carry a single dispatch entry, so the per-envelope cardinality/writes come
    # from that entry. Pair each envelope with its dispatch entry's metadata.
    dispatch_entry = entry["dispatch"][0]
    for env in envelopes:
        rec = {
            "subagent_type": dispatch_entry["subagent_type"],
            "prompt": ad.render(env),
            "writes": dispatch_entry["writes"],
            "schema_ref": env["output_contract"]["schema_ref"],
            "signal_rule": signal_rule,
            "cardinality": dispatch_entry["cardinality"],
        }
        if "item" in env:
            rec["item"] = env["item"]
        dispatches.append(rec)
    return {"status": "paused", "state": name, "dispatches": dispatches}


def _write_checkpoint(state_path, name, ctx, path, signals, agentstate):
    """Journal-free durable checkpoint of the PAUSED tick under
    TICK_CHECKPOINT_KEY. The slots snapshot is the full live blackboard; pending
    carries the agent-state's dispatch metadata so resume can validate + apply."""
    entry = agentstate.entry
    dispatch_entry = entry["dispatch"][0]
    doc = ds.DurableState(state_path).load()
    doc[TICK_CHECKPOINT_KEY] = {
        "next_state": name,
        "slots": _snapshot_slots(ctx),
        "path": list(path),
        "signals": list(signals),
        "pending": {
            "state": name,
            "writes": dispatch_entry["writes"],
            "schema_ref": dispatch_entry.get("output_schema",
                                             dispatch_entry["writes"]),
            "signal_rule": entry["signal"]["rule"],
            "cardinality": dispatch_entry["cardinality"],
        },
    }
    ds.DurableState(state_path).save(doc)


def _clear_checkpoint(state_path):
    """Clear the PAUSED checkpoint on reaching the terminal."""
    doc = ds.DurableState(state_path).load()
    if TICK_CHECKPOINT_KEY in doc:
        del doc[TICK_CHECKPOINT_KEY]
        ds.DurableState(state_path).save(doc)


def _emit_pause_from_checkpoint(state_path, agentstates, tick_id, mode):
    """Build the PAUSED result by RE-LOADING the just-written checkpoint and
    rendering from its restored slot snapshot.

    Rendering from the durable checkpoint (rather than the live ctx) makes the
    first emission and a crash-safety re-emit BYTE-IDENTICAL: both go through the
    same save/load round-trip, so the rendered prompt is independent of the live
    in-process slot key order (DurableState.save sorts keys). The checkpoint is
    the SOLE source of truth for the paused dispatch."""
    checkpoint = persisted_tick_checkpoint(state_path)
    name = checkpoint["pending"]["state"]
    agentstate = agentstates[name]
    slots = checkpoint["slots"]
    slot_values = {slot: slots[slot] for slot in agentstate.manifest.reads}
    return _pause_result(name, agentstate, slot_values, tick_id, mode)


def _drive_agent_tick(route, states, ctx, journal_path, state_path,
                      current, mode, tick_id, agentstates, path, signals):
    """Walk the route from `current` SCRIPT-state-by-SCRIPT-state, pausing at the
    first AGENT state with a durable checkpoint. SCRIPT states run inline exactly
    as tick_orchestrator.run does (impl(ctx) + fc.apply_result + resolve_next).

    `path`/`signals` are the accumulators (already containing the segments walked
    in prior invocations); they are extended in place. Returns (PAUSED dict,
    None) when it pauses at an agent-state, or (None, RunResult) when it reaches
    the terminal with no agent-state ahead.
    """
    terminal = set(route["terminal"])
    journal = ds.Journal(journal_path)

    while current not in terminal:
        second = states[current][1]
        if current in agentstates:
            agentstate = agentstates[current]
            # record-before-act: journal the pending dispatch, then checkpoint.
            journal.record({
                "dedup_key": f"agent-dispatch:{tick_id}:{current}",
                "state": current,
                "writes": agentstate.entry["dispatch"][0]["writes"],
            })
            _write_checkpoint(state_path, current, ctx, path, signals,
                              agentstate)
            # Render the PAUSE from the just-written checkpoint so a fresh PAUSE
            # and a crash-safety re-emit are byte-identical (both round-trip the
            # slots through the durable store).
            return _emit_pause_from_checkpoint(
                state_path, agentstates, tick_id, mode), None
        manifest = states[current][0]
        result = second(ctx)
        fc.apply_result(ctx, manifest, result, _VOCAB)
        signals.append(result.signal)
        current = to.resolve_next(route, current, result.signal)
        path.append(current)

    return None, to.RunResult(final_state=current, path=path, signals=signals)


def _resume_agent_state(route, states, ctx, checkpoint, resume_dispatch,
                        agentstates):
    """Validate + apply the resumed agent-state's outputs, then return the next
    SCRIPT-walk position. On a validation failure returns
    ({"status": "invalid_output", ...}, None, None) so the caller re-emits a
    re-dispatchable PAUSE without clearing the checkpoint.

    Returns (None, next_state, signal) on success."""
    name = checkpoint["pending"]["state"]
    pending = checkpoint["pending"]
    writes = pending["writes"]
    dispatch_entry = agentstates[name].entry["dispatch"][0]

    # For a `once` dispatch the single output IS the whole slot value, so it is
    # validated against the slot schema. For a {per_item: ...} dispatch each
    # output is ONE ELEMENT that collect_outputs assembles into the array slot;
    # validating each element against the array slot schema would wrongly reject
    # a single object, so per-item outputs are validated as generic JSON (no
    # top-level type check).
    if dispatch_entry["cardinality"] == "once":
        out_schema = _SLOT_SCHEMAS.get(writes, {})
    else:
        out_schema = {}

    validated = []
    for output_text in resume_dispatch:
        ok, parsed = ad.validate_output(output_text, out_schema)
        if not ok:
            return ({"status": "invalid_output", "state": name,
                     "reason": parsed}, None, None)
        validated.append(parsed)

    slot_value = ad.collect_outputs(dispatch_entry, validated)
    ctx.write(writes, slot_value)
    signal = ad.compute_signal(pending["signal_rule"], slot_value)
    next_state = to.resolve_next(route, name, signal)
    return None, next_state, signal


def _run_agent_tick(route, states, agentstates, ctx_seed, state_path,
                    journal_path, resume_dispatch, mode):
    """Drive a tick over a route that contains agent-states (DESIGN §2.8).

    Three cases, all keyed off the durable checkpoint (the source of truth):

      - RESUME (resume_dispatch given): load the checkpoint, restore the slots,
        validate + apply the agent outputs, and continue driving from the
        successor state. A validation failure returns an "invalid_output" dict
        (checkpoint left intact, re-dispatchable).
      - CRASH-SAFETY RE-EMIT (no resume_dispatch, checkpoint present): restore
        the slots and re-emit the SAME PAUSED dispatch from the checkpoint.
      - FRESH (no resume_dispatch, no checkpoint): seed a fresh context and drive
        from GUARD until the first agent-state PAUSE or the terminal.

    Returns a 3-tuple: (early_dict, result, ctx). `early_dict` is the PAUSED /
    invalid_output dict to return directly (then result/ctx are None); when the
    driver reaches the terminal `early_dict` is None and (result, ctx) carry the
    RunResult + the final TickContext for read-product persistence.
    """
    checkpoint = persisted_tick_checkpoint(state_path)
    # The tick id keys the dispatch journal dedup; the durable counter is the
    # deterministic per-tick anchor (no wall clock).
    tick_id = ds.DurableState(state_path).load().get("counter", 0)

    if resume_dispatch is not None:
        # RESUME: the checkpoint must exist (a resume without a prior PAUSE is a
        # caller error). Restore the blackboard, then validate + apply.
        ctx = ctx_seed()
        _restore_slots(ctx, checkpoint["slots"])
        early, next_state, _signal = _resume_agent_state(
            route, states, ctx, checkpoint, resume_dispatch, agentstates)
        if early is not None:
            return early, None, None
        path = list(checkpoint["path"]) + [next_state]
        signals = list(checkpoint["signals"])
        return _drive_agent_tick(
            route, states, ctx, journal_path, state_path, next_state, mode,
            tick_id, agentstates, path, signals) + (ctx,)

    if checkpoint:
        # CRASH-SAFETY RE-EMIT: re-issue the SAME PAUSED dispatch idempotently
        # from the durable checkpoint (the source of truth).
        return _emit_pause_from_checkpoint(
            state_path, agentstates, tick_id, mode), None, None

    # FRESH: drive from GUARD.
    ctx = ctx_seed()
    return _drive_agent_tick(
        route, states, ctx, journal_path, state_path, "GUARD", mode, tick_id,
        agentstates, ["GUARD"], []) + (ctx,)


def run_tick(runtime_dir=None, state_path=None, journal_path=None,
             project_dir=None, source=None, now=None, tick_spend=0,
             return_run_result=False, resume_dispatch=None):
    """Run exactly ONE tick of the maintainer loop and return the EXIT
    disposition signal (or the raw RunResult when return_run_result=True).

    Resolves the runtime, hands DEFAULT_ROUTE + DEFAULT_ADAPTER_MAP to
    adapter_wiring.build_loop (which loads a project-local override else the
    default, resolves each routed port to its built-in factory, and validates the
    wiring at LOAD), seeds a TickContext from durable state, and runs
    tick_orchestrator.run(...). A broken override route raises WiringError BEFORE
    any tick body runs.

    On the clean path the route's read stages run (PULL fetches the repo's open
    issues into work_items; TRIAGE, when present, gates them into work_orders),
    PERSIST flushes durable state, and EXIT selects IDLE (read-and-idle) +
    releases the mutex; "idle" is returned. On the GUARD short-circuit path the
    run halts at HALTED with the latched disposition untouched and "halt" is
    returned (no read stage runs).

    After a clean tick the pulled work_items (and work_orders, when the active
    route produced them) are persisted into durable state so status.py can report
    the counts without re-running.

    Injected paths win: when any of runtime_dir/state_path/journal_path is None
    (the installed case) the missing ones fall back to resolve_runtime_paths().
    `project_dir` likewise defaults to the CLAUDE_PROJECT_DIR / cwd anchor.
    `source` is the injectable PULL issue source; `now` the injectable TRIAGE
    reference time.

    Agent yield/resume seam (DESIGN §2.8): when the resolved route contains >=1
    agent-state, the tick PAUSES at each agent-state and returns a PAUSED dict
    (the dispatch request) after durably checkpointing; the executor (a later
    slice) performs the Agent dispatch and feeds the raw outputs back via
    `resume_dispatch`. On resume the outputs are validated + applied and the
    driver continues to the next pause or the terminal. A pure-script route is
    UNCHANGED — it runs via tick_orchestrator.run and returns the signal string.

    Prints a one-line tick trace (state path, work_items/work_orders counts,
    disposition, and the route source — default vs override:<path>, #59).
    """
    if runtime_dir is None or state_path is None or journal_path is None:
        _rt, _state, _journal = resolve_runtime_paths()
        runtime_dir = runtime_dir if runtime_dir is not None else _rt
        state_path = state_path if state_path is not None else _state
        journal_path = journal_path if journal_path is not None else _journal
    if project_dir is None:
        project_dir = _resolve_project_dir()

    # Load governance once per tick (project-local governance.json else the
    # documented defaults). Threaded into the runtime dict so future acting
    # adapters can consult permits/budget; this slice only loads + surfaces +
    # persists (act-skip enforcement is deferred to the acting doer).
    gov = sg.load_governance(project_dir)

    # The runtime dict the factory convention binds: GUARD/EXIT read runtime_dir;
    # PULL the injectable source; TRIAGE the reference time. build_loop reads
    # project_dir for the override-config location. `governance` carries the
    # loaded config (the existing keys are preserved, no regression).
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": runtime_dir,
        "source": source,
        "now": now,
        "governance": gov,
    }

    # The budget readiness gate is evaluated at FRESH tick start ONLY, not on
    # resume (spec). A resume reuses the persisted budget window without rolling
    # it over. `is_resume` is true when the executor feeds dispatch results back
    # OR when a prior PAUSE left a durable checkpoint (crash-safety re-emit).
    persisted_checkpoint = persisted_tick_checkpoint(state_path)
    is_resume = resume_dispatch is not None or bool(persisted_checkpoint)

    if is_resume:
        # Reuse the persisted budget window verbatim (no fresh-start gate).
        new_budget_state = persisted_budget_state(state_path)
        budget = sg.evaluate_budget(
            gov, new_budget_state, _clock_for_window(
                new_budget_state.get("window_key")))
    else:
        # Evaluate + persist the durable, cross-tick budget window. The tz-aware
        # budget clock is the injected `now` when tz-aware, else the host
        # local-aware now. evaluate_budget rolls the prior state over to this
        # window (auto-resume on a new local day) and reports allowance over the
        # injected tick_spend (0 in production). The returned budget_state is the
        # one to persist; record the actual spend when the tick acted.
        budget_clock = _budget_clock(now)
        prior_budget_state = persisted_budget_state(state_path)
        budget = sg.evaluate_budget(gov, prior_budget_state, budget_clock,
                                    tick_spend=tick_spend)
        new_budget_state = budget["budget_state"]
        if tick_spend:
            new_budget_state = sg.record_spend(new_budget_state, budget_clock,
                                               tick_spend)

    # Route-as-data: load (override else default) -> resolve -> validate. A bad
    # override raises WiringError here, before any tick body runs.
    route, states = aw.build_loop(
        DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=_INITIAL_SLOTS)

    # The agent-states resolved in this route ({name: AgentState}). When empty,
    # the route is pure-script and runs the UNCHANGED tick_orchestrator.run path.
    agentstates = {name: second for name, (m, second) in states.items()
                   if isinstance(second, aw.AgentState)}

    if agentstates:
        agent_outcome = _run_agent_tick(
            route, states, agentstates, ctx_seed=lambda: _seed_context(
                state_path, journal_path, route),
            state_path=state_path, journal_path=journal_path,
            resume_dispatch=resume_dispatch, mode=gov.get("mode", ""))
        if agent_outcome[0] is not None:
            # PAUSED or invalid_output: return the structured dict directly. The
            # executor re-invokes run_tick with resume_dispatch to continue.
            return agent_outcome[0]
        result = agent_outcome[1]
        ctx = agent_outcome[2]
        # The driver reached the terminal — clear the PAUSED checkpoint.
        _clear_checkpoint(state_path)
    else:
        ctx = _seed_context(state_path, journal_path, route)
        result = to.run(route, states, ctx, _VOCAB, start="GUARD")

    if result.final_state == _DONE:
        # EXIT ran and emitted the disposition-selecting signal last. Persist the
        # CURRENT tick's read-product snapshot, OVERWRITING any prior values
        # (#64): work_items/work_orders/execution_plan/handoffs are EPHEMERAL —
        # each tick they reflect ONLY what THIS tick's route produced (PULL writes
        # work_items; TRIAGE, when routed, writes work_orders; PRIORITIZE, when
        # routed, writes execution_plan; IMPLEMENT, when routed, writes handoffs).
        # A route without a producing stage persists that product empty (NOT a
        # stale value carried forward from an earlier act-path tick); the
        # principle is symmetric across all four products. The durable CROSS-TICK
        # facts (counter/journal/disposition/schema_version) are left untouched —
        # only the read-product snapshot resets per tick.
        signal = result.signals[-1]
        doc = ds.DurableState(state_path).load()
        doc[WORK_ITEMS_KEY] = (
            ctx.read(wi.WORK_ITEMS_SLOT["name"])
            if "PULL" in route["states"] else [])
        doc[WORK_ORDERS_KEY] = (
            ctx.read(wi.WORK_ORDERS_SLOT["name"])
            if "TRIAGE" in route["states"] else [])
        doc[EXECUTION_PLAN_KEY] = (
            ctx.read(pr.EXECUTION_PLAN_SLOT["name"])
            if "PRIORITIZE" in route["states"] else {})
        doc[HANDOFFS_KEY] = (
            ctx.read(im.HANDOFFS_SLOT["name"])
            if "IMPLEMENT" in route["states"] else [])
        # The durable, cross-tick budget window (NOT a #64 read product): persist
        # the evaluated/recorded budget_state so the window rollover + spend
        # accrual survive across ticks.
        doc[BUDGET_KEY] = new_budget_state
        ds.DurableState(state_path).save(doc)
    else:
        # GUARD short-circuited (STOPPED/ABORTED/RESTART_NEEDED): the tick did no
        # read/PERSIST work, but the budget window is still a durable cross-tick
        # fact — persist its rollover/state so it stays current even on a halt.
        signal = "halt"
        doc = ds.DurableState(state_path).load()
        doc[BUDGET_KEY] = new_budget_state
        ds.DurableState(state_path).save(doc)

    disposition = ld.read_disposition(runtime_dir)
    work_items_count = persisted_work_items_count(state_path)
    work_orders_count = persisted_work_orders_count(state_path)
    execution_plan_count = persisted_execution_plan_count(state_path)
    handoffs_count = persisted_handoffs_count(state_path)
    # The route source (#59): default vs the project-local override path, so a
    # misplaced/absent route.json is visible in the trace, not silently ignored.
    # Resolved from the SAME project_dir build_loop loaded the route from.
    route_src = route_source_label(project_dir)
    # Governance surface (#69 style — always shown): the trust mode + a compact
    # budget field, plus a budget_paused indicator when the budget is exhausted.
    # Placed after the existing fields; all current fields/order are preserved.
    gov_fields = governance_fields(gov, new_budget_state, budget)
    sys.stdout.write(
        f"[tick] path={'->'.join(result.path)} work_items={work_items_count} "
        f"work_orders={work_orders_count} "
        f"execution_plan={execution_plan_count} handoffs={handoffs_count} "
        f"disposition={disposition} "
        f"signal={signal} route={route_src} {gov_fields}\n")

    if return_run_result:
        return result
    return signal


# --------------------------------------------------------------------------
# JSON tick CLI (the --step / --resume executor seam).
#
# A THIN deterministic wrapper around the EXISTING run_tick(...) structured
# returns (the yield/resume seam above) — it adds NO new tick logic. The later
# executor skill drives the yield/resume loop by calling --step, then feeding the
# paused agent-state's subagent outputs back via --resume <file>. stdout is PURE
# JSON in those modes (the skill parses stdout); the human trace run_tick writes
# is captured into the JSON `trace` field, never leaked raw. Bare invocation (no
# --step/--resume) is UNCHANGED: it prints the human trace, so pure-script bash
# callers keep working.
# --------------------------------------------------------------------------


def _run_tick_paths(args):
    """The keyword path args for run_tick() from the parsed CLI flags (a temp
    runtime under test, else None -> the production defaults via
    resolve_runtime_paths). Mirrors run_tick's own None-fallback behaviour."""
    return {
        "runtime_dir": args.runtime_dir,
        "state_path": args.state,
        "journal_path": args.journal,
        "project_dir": args.project_dir,
    }


def _step_envelope(result, trace):
    """Map a run_tick(...) structured return to the CLI JSON envelope.

    run_tick returns either a disposition signal STRING (a clean terminal) or a
    structured dict (paused / invalid_output). A string -> the done envelope
    carrying the captured one-line trace; a dict is already the settled
    paused/invalid_output shape and is surfaced verbatim (a pause emits no trace).
    """
    if isinstance(result, dict):
        return result
    return {"status": "done", "signal": result, "trace": trace}


def main(argv=None):
    """The JSON tick CLI entrypoint. Returns the process exit code.

    Bare (no --step/--resume): run one tick and print the HUMAN trace (unchanged).
    --step: run to the next pause/terminal, print the JSON envelope to stdout.
    --resume <file>: feed the file's JSON array of raw subagent outputs back via
    run_tick(resume_dispatch=...), print the same envelope shape.

    Exit codes: done/paused -> 0; invalid_output (bad agent output OR a
    malformed/missing --resume file) -> 1.
    """
    parser = argparse.ArgumentParser(
        description="Run one maintainer tick (JSON --step/--resume seam).")
    parser.add_argument(
        "--step", action="store_true",
        help="run to the next pause/terminal and print a JSON envelope")
    parser.add_argument(
        "--resume", metavar="FILE",
        help="resume a paused tick from FILE (a JSON array of raw subagent "
             "output strings, in dispatch order); print a JSON envelope")
    parser.add_argument("--runtime-dir", dest="runtime_dir")
    parser.add_argument("--state", dest="state")
    parser.add_argument("--journal", dest="journal")
    parser.add_argument("--project-dir", dest="project_dir")
    args = parser.parse_args(argv)

    paths = _run_tick_paths(args)

    # Bare mode (no JSON flags): UNCHANGED — print the one-line human trace.
    if not args.step and not args.resume:
        run_tick(**paths)
        return 0

    resume_dispatch = None
    if args.resume:
        # Read the dispatch outputs. A missing/malformed file is surfaced as an
        # invalid_output envelope (no crash), never a traceback.
        try:
            with open(args.resume) as f:
                resume_dispatch = json.load(f)
        except (OSError, ValueError) as exc:
            sys.stdout.write(json.dumps({
                "status": "invalid_output", "state": None,
                "reason": f"could not read resume file: {exc}"}) + "\n")
            return 1
        if not isinstance(resume_dispatch, list):
            sys.stdout.write(json.dumps({
                "status": "invalid_output", "state": None,
                "reason": "resume file must contain a JSON array of output "
                          "strings"}) + "\n")
            return 1

    # Capture run_tick's one-line human trace so it does NOT pollute stdout (the
    # skill parses stdout as pure JSON); fold it into the envelope `trace` field.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = run_tick(resume_dispatch=resume_dispatch, **paths)
    trace = buf.getvalue().strip()

    envelope = _step_envelope(result, trace)
    sys.stdout.write(json.dumps(envelope) + "\n")
    return 1 if envelope.get("status") == "invalid_output" else 0


if __name__ == "__main__":
    # Production entrypoint: the scheduling skills invoke this once per tick from
    # the installed plugin. With no flags it defaults its durable file locations
    # to the writable per-project runtime dir
    # (${CLAUDE_PROJECT_DIR}/.auto-maintainer/ else .auto-maintainer/ under cwd)
    # via resolve_runtime_paths(), loads the project-local route override (else
    # the default spine), and pulls real open issues via the live gh source. The
    # --step/--resume flags give the executor skill the JSON yield/resume seam.
    sys.exit(main())
