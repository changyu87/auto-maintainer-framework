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
lifecycle-dispositions, work-intake, adapter-wiring, verify-integrate, and
observability UNCHANGED; it never edits or forks them. Each tick run_tick also
emits a structured event log to ${runtime_dir}/events.jsonl via
observability.EventLog (the machine-first record of "what the loop did"), written
ALONGSIDE the existing one-line trace — purely additive, no existing behaviour
changes. The VERIFY/INTEGRATE/CLEANUP ports (verify-integrate) are pre-mapped in
DEFAULT_ADAPTER_MAP so the close-the-loop route wires by a pure route.json edit.

Version: 0.6.0
Owner: changyu87
Deprecation criterion: Superseded when scheduling moves to a different clock
  source (e.g. a native plugin cron API) or when the tick interval becomes
  config-driven and this slice's hardcoding is removed.
"""

import argparse
import contextlib
import hashlib
import io
import json
import os
import subprocess
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
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if os.path.isdir(_dep_src) and _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

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
import observability as ob  # noqa: E402
import verify_integrate as vi  # noqa: E402


# The production PULL issue source: work-intake's live `gh` CLI adapter. Tests
# inject a stub instead so the suite touches no network; the shipped run_tick
# (no injected source) pulls real open issues.
DEFAULT_PULL_SOURCE = wi.gh_issue_source

# The production REPORT filing sink: work-intake's live `gh issue create` adapter
# (mirrors DEFAULT_PULL_SOURCE). The outbound write-side of PULL. Tests override
# it with a stub so the suite touches no network; the shipped run_tick (no
# injected sink) files real tracker items through `gh`.
DEFAULT_REPORT_SINK = wi.gh_issue_file_sink


def gh_pr_state_source(pr_ref, repo=None, runner=subprocess.run):
    """Production PR-state source for the acted-ledger re-entry check (§3.8.5
    symmetric): shell the deterministic `gh` CLI for ONE PR's closed/merged
    state and return a ``{"state": <"OPEN"|"CLOSED"|"MERGED">, "merged": <bool>}``
    dict. `pr_ref` is the PR url or `owner/repo#N` ref recorded in the acted-ledger
    (`gh pr view` accepts either form); when `repo` is given it is passed via
    `--repo`, otherwise gh resolves it from the project default. `gh` carries its
    own auth.

    The subprocess `runner` is INJECTABLE (defaulting to subprocess.run) so the
    closed/merged check is deterministic and unit-testable with a fake — no
    network, the failure locatable to the fetch boundary (mirror of
    verify_integrate.gh_open_pr_source). The re-entry check queries the PR ONLY
    for ledger entries whose issue updated_at advanced, so the `gh` calls are
    bounded to changed issues."""
    cmd = ["gh", "pr", "view", pr_ref, "--json", "state,mergedAt"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout)
    merged = bool(data.get("mergedAt"))
    state = data.get("state", "")
    return {"state": state, "merged": merged}


# The production default PR-state source for the acted-ledger re-entry check.
# Tests inject a deterministic stub instead so the suite touches no network; the
# shipped run_tick (no injected source) queries real PR state through `gh`.
DEFAULT_PR_STATE_SOURCE = gh_pr_state_source

# The durable-state document keys under which the last tick's pulled work_items
# (and triaged work_orders, when the active route produced them) are persisted,
# so status.py can report the counts without re-running the loop.
WORK_ITEMS_KEY = "work_items"
WORK_ORDERS_KEY = "work_orders"
EXECUTION_PLAN_KEY = "execution_plan"
HANDOFFS_KEY = "handoffs"
# The verify-integrate close-the-loop read products: VERIFY writes verdicts (a
# list of Verdict dicts), INTEGRATE writes integration_result (an object
# {merged, skipped, errors}). Like the four above they are per-tick EPHEMERAL
# read products (#64): each tick they reflect ONLY what THIS tick's route
# produced (empty when the route omits VERIFY / INTEGRATE), never a stale
# carry-forward.
VERDICTS_KEY = "verdicts"
# REVIEW (the ADVISORY quality state, FT-C) writes review_findings (a list of
# DiscoveredIssue-conforming records). Like verdicts/integration_result it is a
# per-tick EPHEMERAL read product (#64): each tick it reflects ONLY what THIS
# tick's route produced (empty when the route omits REVIEW), never a stale
# carry-forward. The retired review_verdicts merge-gate slot is gone — REVIEW is
# advisory, so INTEGRATE no longer reads its output.
REVIEW_FINDINGS_KEY = "review_findings"
INTEGRATION_RESULT_KEY = "integration_result"

# The durable-state document key under which the safety-governance budget window
# {window_key, spent_tokens} is persisted. Unlike the four read-product keys
# above, BUDGET is a durable CROSS-TICK fact (like the counter), NOT a per-tick
# ephemeral read product (#64): a tick within the same window carries the
# accumulated spend forward; only a window rollover (inside sg.evaluate_budget)
# resets spent_tokens.
BUDGET_KEY = "budget"

# The durable-state document key under which the ACTED-LEDGER is persisted: a
# durable CROSS-TICK fact (like BUDGET_KEY, NOT a per-tick #64 read product)
# mapping {work_order_id: {"outcome": <handoff status>, "ref": <artifact ref or
# null>, "acted_at_updated_at": <issue updated_at at act time or null>}}. It
# records which work orders an ACTING agent-state already acted on, so a later
# tick that re-pulls the SAME work order does NOT re-dispatch it (no second PR) —
# idempotency (§3.2.4).
#
# Acted-ledger RE-ENTRY (§3.8.5 symmetric leak fix, #204): an `opened` entry
# stores the issue's `acted_at_updated_at` so the loop can re-attempt a still-valid
# issue whose auto-PR a human CLOSED. In the IMPLEMENT per_item filter an already-
# `opened` work order RE-ENTERS (its ledger entry cleared, the item re-dispatched)
# ONLY when BOTH (a) its PR `ref` is CLOSED-AND-NOT-MERGED and (b) the issue's
# current updated_at has ADVANCED past acted_at_updated_at. It stays LOCKED
# otherwise: merged (done), still-open PR (pending review), or closed-but-issue-
# unchanged (the human closed it without asking for a redo — respect it, no
# thrash). The PR is queried (gh_pr_state_source) ONLY for entries whose updated_at
# advanced, bounding the gh calls to changed issues.
# Load-modify-saved preserving every other durable key.
ACTED_LEDGER_KEY = "acted_ledger"

# The durable-state document key under which the BACKOFF-LEDGER is persisted: a
# durable CROSS-TICK fact (like ACTED_LEDGER_KEY, NOT a per-tick #64 read product)
# mapping {work_item_id: {"blocked_count": <int>, "deferred_at_updated_at": <iso
# updated_at or null>}}. It bounds-retries a work order the doer reports `blocked`
# (§3.8.5): each blocked resume increments blocked_count keyed on the SOURCE
# work_item_id; at the configured backoff.threshold the item is deferred
# (deferred_at_updated_at pinned to the issue's current updated_at) and escalated
# to a human once. The
# acted-ledger stays work_order_id-keyed and unchanged; backoff is the additive
# work_item_id-keyed retry state. Load-modify-saved preserving every other key.
BACKOFF_LEDGER_KEY = "backoff"

# The DEFAULT number of honest `blocked` attempts a work_item gets before it is
# deferred and escalated to a human (§3.8.5). Used only when the loaded config
# carries no backoff.threshold; the live threshold is CONFIG-DRIVEN, read from
# sg.load_config's backoff.threshold (default 5) via _backoff_threshold(gov).
BACKOFF_THRESHOLD_DEFAULT = 5


def _backoff_threshold(gov):
    """The configured backoff threshold K (§3.8.5): the consecutive-`blocked`
    count at which a work_item is deferred + escalated. Read from the loaded
    central config's backoff.threshold (sg.load_config), falling back to
    BACKOFF_THRESHOLD_DEFAULT when absent."""
    return (gov.get("backoff") or {}).get("threshold", BACKOFF_THRESHOLD_DEFAULT)

# The durable-state document key under which the TRIAGE MEMORY is persisted: a
# durable CROSS-TICK fact (like BACKOFF_LEDGER_KEY, NOT a per-tick #64 read
# product) mapping {work_item_id: {"updated_at": <iso>, "status": <done|deferred>}}
# (§3.5.3). It lets the triager skip re-judging issues that are already handled
# and UNCHANGED: an item is recorded `done` once the doer opened/closed it, or
# `deferred` once it reaches the backoff deferral. Recorded at the acting-state
# resume alongside the acted/backoff ledgers, from the same work_order_id ->
# work_item_id mapping + the item's current issue updated_at. Load-modify-saved
# preserving every other durable key.
TRIAGE_MEMORY_KEY = "triage_memory"

# The triage-memory statuses that, when the issue updated_at is UNCHANGED, cause a
# work_item to be filtered OUT of the TRIAGE dispatch (already handled — no need to
# re-judge). An `active` (accepted-not-done) item is NOT in this set, so it is
# always re-triaged (never starved).
_TRIAGE_SKIP_STATUSES = ("done", "deferred")

# The production escalation sink: observability's live `gh issue comment` adapter
# (mirrors DEFAULT_PULL_SOURCE / DEFAULT_REPORT_SINK). When a work_item reaches
# the configured backoff.threshold the deferral posts ONE issue comment naming
# the human through this sink. Tests inject a stub so the suite touches no
# network; the shipped run_tick (no injected sink) comments via `gh`.
DEFAULT_ESCALATE_SINK = ob.gh_comment_sink

# The durable-state document key under which the REPORT-LEDGER is persisted: a
# durable CROSS-TICK fact (like BUDGET_KEY / ACTED_LEDGER_KEY, NOT a per-tick #64
# read product) mapping {dedup_key: {"tracker_ref": <ref>, "url": <url>}}. It
# records which discoveries the outbound REPORT flush already filed, so a refire /
# DRAIN replay never files a duplicate (journaled-idempotency, §3.11.4).
# Load-modify-saved preserving every other durable key.
REPORT_LEDGER_KEY = "report_ledger"

# The durable-state document key under which the last tick's REPORT outcome
# {filed, skipped} is persisted, so status.py can surface reported=<filed>/
# <skipped> without re-running the loop (mirrors how the read-product counts are
# read back). A small durable last-tick fact, NOT a #64 read product.
LAST_REPORTED_KEY = "last_reported"

# The durable-state document key under which the last tick's TRIAGE filter outcome
# {judged, pulled} is persisted, so the terminal trace can surface
# triaged=<judged>/<pulled> even when TRIAGE paused in an earlier invocation than
# the one that reaches the terminal (§3.5.3). `judged` is the count of work_items
# fed to the TRIAGE subagent (after the skip-unchanged filter); `pulled` is the
# full PULL set. A small durable last-tick fact, NOT a #64 read product.
LAST_TRIAGED_KEY = "last_triaged"

# The durable-state document key under which the agent yield/resume CHECKPOINT is
# persisted while a tick is PAUSED at an agent-state (DESIGN §2.8 executor
# protocol). It is the SOLE source of truth for the paused dispatch (crash-safety):
# {next_state, slots (full live TickContext slot snapshot), path, signals,
#  output_dir, pending: {state, writes, signal_rule, cardinality,
#  dispatches:[{output_path, schema}...]}}. output_dir + the per-dispatch
# output_path are persisted so a crash-safety re-emit produces the byte-identical
# output_path and resume knows which subagent-written files to read. It is cleared
# the moment the driver reaches the terminal. Unlike the read products it is a
# transient cross-invocation handoff, NOT a #64 read product.
TICK_CHECKPOINT_KEY = "tick_checkpoint"

# The durable-state document key under which the per-tick TICK-ID COUNTER is
# persisted: a durable CROSS-TICK monotonic counter (like the work `counter`, NOT
# a per-tick #64 read product) that increments ONCE per fresh tick to give each
# tick a DISTINCT id (#112). It is SEPARATE from the work `counter`, which only
# advances when a state bumps it (a read-only route never does, so the work
# counter cannot discriminate read-only ticks). The id is assigned at FRESH tick
# start (next = last + 1), persisted here AND stamped into TICK_CHECKPOINT_KEY so
# a single tick's `--step` -> `--resume` (separate processes, no injected `now`)
# reads back the SAME id rather than recomputing it. Load-modify-save preserving
# every other durable key. Starts at 1 (the first tick is tick_id=1, never 0).
TICK_ID_COUNTER_KEY = "tick_id_counter"

# The structured event-log filename (observability §3.9.1). run_tick opens an
# observability.EventLog at ${runtime_dir}/<EVENTS_FILENAME> and appends one
# structured event per tick milestone. The log is the machine-first record of
# "what the loop did"; it is written ALONGSIDE the existing one-line trace (purely
# additive — no existing behaviour changes). observability is consumed UNCHANGED.
EVENTS_FILENAME = "events.jsonl"

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


def _work_remains(work_items, triage_memory, backoff_ledger, threshold, now):
    """Pure POOL predicate (§3.3.3 immediate-refire): True when next tick's TRIAGE
    would have ANY classify-valid, non-blocked issue to judge/act on, so the loop
    should run the NEXT tick IMMEDIATELY rather than wait the heartbeat interval.

    It first computes `candidates = _filter_triage_work_items(work_items,
    triage_memory)` — the SAME §3.5.3 skip-filter TRIAGE applies, which drops
    done/deferred-AND-unchanged items and KEEPS new/changed/active ones. A
    candidate counts as workable when it is BOTH:
      - TRIAGE-acceptable: the deterministic work-intake validity gate accepts it
        (well-formed + open + not-stale, keyed off the injectable `now` so
        staleness is deterministic). An invalid / closed / stale item never
        counts.
      - NOT blocked-and-unchanged: a `blocked`-deferred item whose backoff entry
        has reached the deferral threshold AND whose issue updated_at has NOT
        advanced past the deferral pin is INERT (re-dispatching it would thrash),
        so it does NOT count. A blocked item whose updated_at advanced past the
        pin (a human touched it) IS actionable again and counts.

    Reusing the skip-filter GUARANTEES no busy-loop: a done/deferred-unchanged or
    persistently-rejected (closed) item is filtered out and never refires; a
    blocked item only refires until the backoff threshold, then goes inert.

    `triage_memory` maps {work_item_id: {updated_at, status}} (TRIAGE_MEMORY_KEY).
    `backoff_ledger` maps {work_item_id: {blocked_count, deferred_at_updated_at}}
    (BACKOFF_LEDGER_KEY). `threshold` is the configured backoff threshold K. Pure
    and deterministic — no wall clock (the reference time is the injected `now`),
    no network, no durable-state read."""
    triage = wi.Triage(now=now)
    ledger = backoff_ledger or {}
    candidates = _filter_triage_work_items(work_items or [], triage_memory or {})
    for raw in candidates:
        item = wi.WorkItem.from_dict(raw)
        if triage.classify(item)[0] != "accepted":
            continue
        entry = ledger.get(item.id)
        if entry and entry.get("blocked_count", 0) >= threshold:
            # Blocked-and-unchanged when the issue updated_at is NOT strictly
            # after the deferral pin (GitHub updated_at is an ISO-8601
            # lexicographically-sortable timestamp, so a string `>` is the
            # correct chronological compare, mirroring the §3.8.5 re-entry gate).
            pin = entry.get("deferred_at_updated_at")
            if not (pin and (item.updated_at or "") > pin):
                continue  # deferred + unchanged -> inert, not actionable
        return True
    return False


def make_exit(runtime):
    """EXIT anchor (lifecycle-dispositions) WRAPPED with immediate-refire
    (§3.3.3): tick-outcome -> disposition + signal, releasing the mutex, bound to
    the runtime marker dir.

    When the inner EXIT would otherwise IDLE (tick_outcome == "empty") AND the
    route can ACT on work (an acting agent-state is present) AND the POOL still
    holds workable work (_work_remains over this tick's work_items + the durable
    triage memory + backoff ledger), the wrapper REWRITES the outcome to
    "work-remains" so EXIT selects RUNNING/refire and the loop runs the next tick
    immediately instead of waiting the heartbeat. It overrides ONLY the "empty"
    outcome — a restart/fault outcome (or an already-"work-remains" one) is
    delegated UNCHANGED. The read-and-idle default route + any empty-pool /
    all-done-pool tick keep IDLING: with no acting agent-state present the override
    never fires.

    The refire is POOL-based (NOT the old committed-work check): the question is
    "would next tick's TRIAGE have any classify-valid, non-blocked issue to
    judge/act on". _work_remains first applies the §3.5.3 skip-filter
    (triage_memory) so done/deferred-unchanged items are dropped (no busy-loop),
    then the TRIAGE-acceptability + backoff-deferred-unchanged gate. The old narrow
    committed-work (un-acted / un-handled work_order) gating is removed — the
    owner's no-PRIORITIZE route could essentially never trigger it.

    The acting-route gate lives here (not in _work_remains) because _work_remains
    is the pure, route-agnostic POOL predicate; whether the loop SHOULD refire on a
    workable pool depends on the route actually having an acting stage to do the
    work next tick (the dry-run inert IMPLEMENT / read-and-idle spine leaves no
    real work, so it idles, per spec)."""
    exit_state = ld.Exit(runtime["runtime_dir"])
    inner_run = exit_state.run
    manifest = fc.StateManifest(
        reads=["tick_outcome", wi.WORK_ITEMS_SLOT["name"], "state_path"],
        writes=["tick_outcome"],
        emits=list(exit_state.manifest.emits))

    def run(ctx):
        if (ctx.read("tick_outcome") == "empty"
                and runtime.get("has_acting_agent")):
            state_path = ctx.read("state_path")
            work_items = ctx.read(wi.WORK_ITEMS_SLOT["name"]) or []
            triage_memory = persisted_triage_memory(state_path)
            backoff = persisted_backoff_ledger(state_path)
            threshold = _backoff_threshold(runtime.get("governance") or {})
            if _work_remains(work_items, triage_memory, backoff, threshold,
                             runtime.get("now")):
                ctx.write("tick_outcome", "work-remains")
        return inner_run(ctx)

    return manifest, run


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
    Binds the injectable issue source (default = work-intake's live gh source)
    and the §3.11.5 loopback toggle `work_own_filings` from the loaded central
    config (default True = the loop works its own filings; an explicit false
    opt-out makes PULL drop loop-filed items so they stay open for human
    triage). safety-governance + work-intake are consumed UNCHANGED."""
    source = runtime.get("source") or DEFAULT_PULL_SOURCE
    pull = wi.Pull(
        source=source,
        work_own_filings=sg.work_own_filings(runtime.get("governance") or {}))
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


def make_verify(runtime):
    """VERIFY adapter (verify-integrate): the READ-ONLY act-side gate that lists
    the loop's open PRs (live `gh`, injectable) and writes one Verdict per PR into
    the `verdicts` slot. Binds no MERGE governance (VERIFY never merges); the PR
    source + default-branch resolver default to verify-integrate's live `gh`
    seams. Returns the sibling manifest + the state's bound run callable.

    Cross-feature complement (§3.7.6): binds the maintained project's
    `features_root` from the loaded central config so a cross-cutting-flagged tick
    (TRIAGE cross_cutting_risk.risk=True) runs each at-risk feature's run.py and
    gates only on a real complement failure. When `features_root` is unconfigured
    (the null default) it stays None and VERIFY conservatively gates a flagged
    tick (an unverifiable at-risk batch must never auto-merge)."""
    # Reference the module seams at factory-call time (not the def-time defaults)
    # so an injected/overridden source is honored.
    features_root = runtime["governance"].get("features_root")
    verify = vi.Verify(source=vi.gh_open_pr_source, repo=runtime.get("repo"),
                       default_branch_source=vi.gh_default_branch_source,
                       features_root=features_root)
    return vi.VERIFY_MANIFEST, verify.run


def make_review(runtime):  # noqa: ARG001 - REVIEW binds no runtime config
    """REVIEW adapter (verify-integrate): the ADVISORY quality state between
    VERIFY and INTEGRATE (FT-C, DESIGN §3.7.7). REVIEW is a NON-ACTING AGENT-state
    — the real reviewer is the `auto-maintainer-reviewer` subagent, wired by
    setting the REVIEW port to an agent entry (AGENT_PORT_TEMPLATES['REVIEW']) in
    the adapter map.

    This deterministic factory is the resolvable DEFAULT for the REVIEW port (so
    the port resolves for wiring validation even when DEFAULT_ROUTE omits it, the
    ports-and-adapters promise). Its run is a CONSERVATIVE no-op: it reads the
    verdicts slot and writes an EMPTY review_findings list. Because REVIEW is
    ADVISORY — NO LONGER a merge gate — it must ALWAYS continue to INTEGRATE: the
    no-op emits OK (not EMPTY) regardless of how many findings it produced, so a
    clean review never EMPTY-branches past the merge (the dogfood unmerged-PR
    fix). An absent/no-op reviewer costs only missed advisory notes, never an
    unsafe merge (INTEGRATE merges ok verdicts on its own deterministic gate +
    VERIFY + guardrails + the trust ladder). Wire the agent reviewer to emit real
    findings. Returns REVIEW_MANIFEST + the no-op run callable."""
    def _run(ctx):
        ctx.read("verdicts")
        return fc.StateResult(signal="OK", writes={"review_findings": []})
    return vi.REVIEW_MANIFEST, _run


def make_integrate(runtime):
    """INTEGRATE adapter (verify-integrate): the single highest-stakes act-side
    state, now a THIN merge (FT-C/D). Reads ONLY `verdicts`, merges each `ok`
    verdict's PR via the injectable merge sink ONLY at auto-merge, and writes the
    `integration_result` slot. REVIEW is ADVISORY, so INTEGRATE does NOT read
    `review_verdicts` and does NOT gate merge on a review approval — the merge
    rests on IMPLEMENT's deterministic run.py gate + VERIFY + guardrails + the
    trust ladder.

    Binds the loaded governance `mode` (runtime['governance']['mode']) so the
    trust ladder gates merge at auto-merge only (sg.permits) and the §3.8.1
    declarative backstop applies (sg.merge_guardrails over the resolved default
    branch). The default branch is resolved via verify-integrate's injectable
    `gh` resolver (the same seam VERIFY uses), so the guardrail's never-merge-
    wrong-base check agrees with VERIFY's verdict. At propose the would-merge
    intent is recorded under `skipped` and the sink is never called."""
    mode = runtime["governance"].get("mode", "")
    default_branch = vi.gh_default_branch_source(runtime.get("repo"))
    # Reference the module merge sink at factory-call time (not the def-time
    # default) so an injected/overridden sink is honored.
    integrate = vi.Integrate(mode=mode, merge_sink=vi.gh_pr_merge_sink,
                             repo=runtime.get("repo"),
                             default_branch=default_branch,
                             permits_fn=sg.permits,
                             guardrails_fn=sg.merge_guardrails)
    return vi.INTEGRATE_MANIFEST, integrate.run


def make_cleanup(runtime):  # noqa: ARG001 - CLEANUP binds no runtime config
    """CLEANUP adapter (verify-integrate): the v1-thin branch/release hygiene
    pass-through. Reads `integration_result`, writes nothing, emits OK. Binds no
    runtime config; returns the sibling manifest + the state's run callable."""
    cleanup = vi.Cleanup()
    return vi.CLEANUP_MANIFEST, cleanup.run


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

# Every known port -> its built-in factory address. TRIAGE, PRIORITIZE,
# IMPLEMENT, VERIFY, REVIEW, INTEGRATE, and CLEANUP are included even though
# DEFAULT_ROUTE omits them (the ports-and-adapters promise: insert the
# close-the-loop chain by data, no code change).
# The terminals are addressed too so adapter-wiring can resolve every state in a
# route (terminals never run(), but their manifests must resolve for validation).
DEFAULT_ADAPTER_MAP = {
    "GUARD": "run_tick:make_guard",
    "DRAIN": "run_tick:make_drain",
    "PULL": "run_tick:make_pull",
    "TRIAGE": "run_tick:make_triage",
    "PRIORITIZE": "run_tick:make_prioritize",
    "IMPLEMENT": "run_tick:make_implement",
    "VERIFY": "run_tick:make_verify",
    "REVIEW": "run_tick:make_review",
    "INTEGRATE": "run_tick:make_integrate",
    "CLEANUP": "run_tick:make_cleanup",
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
#
# cross_cutting_risk (FT-B/D §3.5.9) is in the `initial` set because VERIFY READS
# it but its producer TRIAGE is OPTIONAL on a route (a route may run VERIFY
# without a TRIAGE stage). _seed_context seeds it the no-risk default whenever
# TRIAGE OR VERIFY is routed, so declaring it initially-available keeps
# data-readiness honest for a VERIFY-without-TRIAGE route without requiring TRIAGE
# to be present. The retired review_verdicts slot is gone (REVIEW is advisory;
# INTEGRATE reads only verdicts).
#
# EXIT now READS work_items (the §3.3.3 immediate-refire wrapper inspects the
# pulled work_items to decide whether to refire), but work_items is NOT added to
# the `initial` set: PULL writes it and PULL precedes EXIT on every routed path,
# so data-readiness is already satisfied — AND keeping it OUT of `initial`
# preserves the read-before-write rejection of a bad TRIAGE-before-PULL override.
_INITIAL_SLOTS = ["state_path", "journal_path", "counter", "tick_outcome",
                  "cross_cutting_risk"]


def _seed_context(state_path, journal_path, route):
    """A TickContext seeded from durable state plus the read-and-idle outcome.

    Registers the durable-state plumbing slots (counter/state_path/journal_path)
    that DRAIN/PERSIST read, the work_items slot PULL writes, the tick_outcome
    slot EXIT reads, and — when the active route includes them — the work_orders
    slot TRIAGE writes, the execution_plan slot PRIORITIZE writes, the handoffs
    slot IMPLEMENT writes, the cross_cutting_risk slot TRIAGE writes / VERIFY
    reads, the verdicts + cross_check slots VERIFY writes, the review_findings
    slot REVIEW writes, and the integration_result slot INTEGRATE writes. Each
    producible read-product slot
    is WRITTEN with a schema-valid empty default (skipped-state safety): a route
    that SKIPS its producing state via a signal branch still leaves the slot
    readable so the terminal persist never crashes. tick_outcome is seeded
    "empty" so EXIT selects IDLE after the read stages (read-and-idle): the dry-run
    IMPLEMENT is INERT, so even an act-path tick leaves no remaining work and
    still idles.
    """
    ctx = fc.TickContext()
    ctx.register_slot("counter", {"type": "integer"}, version="1.0.0")
    ctx.register_slot("state_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("journal_path", {"type": "string"}, version="1.0.0")
    ctx.register_slot("tick_outcome", {"type": "string"}, version="1.0.0")
    # Producible read-product slots are SEEDED EMPTY (skipped-state safety): each
    # is not only registered but WRITTEN with a schema-valid empty default, so a
    # route that SKIPS its producing state via a signal branch (e.g. VERIFY EMPTY
    # -> PERSIST skips INTEGRATE, or TRIAGE EMPTY skips IMPLEMENT) still leaves the
    # slot readable. Without the seed the terminal's ctx.read(<slot>) raised a
    # ContractError("slot has not been written") and CRASHED the whole tick. A
    # state that DOES run overwrites its seeded empty unchanged. The object slots
    # use their required-key empty shapes (NOT bare {}) so the persisted value
    # matches what a running state produces.
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])
    ctx.write(wi.WORK_ITEMS_SLOT["name"], [])
    if "TRIAGE" in route["states"]:
        ctx.register_slot(
            wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
            version=wi.WORK_ORDERS_SLOT["version"])
        ctx.write(wi.WORK_ORDERS_SLOT["name"], [])
    if "PRIORITIZE" in route["states"]:
        ctx.register_slot(
            pr.EXECUTION_PLAN_SLOT["name"], pr.EXECUTION_PLAN_SLOT["schema"],
            version=pr.EXECUTION_PLAN_SLOT["version"])
        ctx.write(pr.EXECUTION_PLAN_SLOT["name"], {
            "schema_version": pr.EXECUTION_PLAN_SCHEMA_VERSION,
            "ordered": [],
            "status": {},
        })
    if "IMPLEMENT" in route["states"]:
        ctx.register_slot(
            im.HANDOFFS_SLOT["name"], im.HANDOFFS_SLOT["schema"],
            version=im.HANDOFFS_SLOT["version"])
        ctx.write(im.HANDOFFS_SLOT["name"], [])
    # cross_cutting_risk (FT-B/D §3.5.9): TRIAGE writes it; VERIFY reads it (its
    # conditional cross-feature complement run). Seeded the no-risk default
    # whenever TRIAGE OR VERIFY is routed, so a VERIFY-without-TRIAGE route still
    # has the slot to read (and a TRIAGE tick does not crash writing it).
    if "TRIAGE" in route["states"] or "VERIFY" in route["states"]:
        ctx.register_slot(
            wi.CROSS_CUTTING_RISK_SLOT["name"],
            wi.CROSS_CUTTING_RISK_SLOT["schema"],
            version=wi.CROSS_CUTTING_RISK_SLOT["version"])
        ctx.write(wi.CROSS_CUTTING_RISK_SLOT["name"],
                  wi.CrossCuttingRisk(risk=False).to_dict())
    if "VERIFY" in route["states"]:
        ctx.register_slot(
            vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
            version=vi.VERDICTS_SLOT["version"])
        ctx.write(vi.VERDICTS_SLOT["name"], [])
        # cross_check (FT-D §3.7.6): VERIFY's conditional complement-run product.
        # Seeded the empty (ran=False) default when VERIFY is routed.
        ctx.register_slot(
            vi.CROSS_CHECK_SLOT["name"], vi.CROSS_CHECK_SLOT["schema"],
            version=vi.CROSS_CHECK_SLOT["version"])
        ctx.write(vi.CROSS_CHECK_SLOT["name"],
                  vi.CrossCheck(ran=False).to_dict())
    # review_findings (FT-C): the ADVISORY REVIEW state's output (a list of
    # DiscoveredIssue-conforming records). Seeded empty ONLY when REVIEW is routed
    # (its producer). INTEGRATE no longer reads it — REVIEW is advisory, so an
    # INTEGRATE-without-REVIEW route does NOT need the slot.
    if "REVIEW" in route["states"]:
        ctx.register_slot(
            vi.REVIEW_FINDINGS_SLOT["name"], vi.REVIEW_FINDINGS_SLOT["schema"],
            version=vi.REVIEW_FINDINGS_SLOT["version"])
        ctx.write(vi.REVIEW_FINDINGS_SLOT["name"], [])
    if "INTEGRATE" in route["states"]:
        ctx.register_slot(
            vi.INTEGRATION_RESULT_SLOT["name"],
            vi.INTEGRATION_RESULT_SLOT["schema"],
            version=vi.INTEGRATION_RESULT_SLOT["version"])
        ctx.write(vi.INTEGRATION_RESULT_SLOT["name"], {
            "schema_version": vi.INTEGRATION_RESULT_SCHEMA_VERSION,
            "merged": [],
            "skipped": [],
            "errors": [],
        })
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


def persisted_review_findings(state_path):
    """The last tick's review_findings snapshot persisted in durable state (a
    list of DiscoveredIssue-conforming records), or [] when the active route
    produced none (no REVIEW stage). A per-tick #64 read product (FT-C)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(REVIEW_FINDINGS_KEY, [])


def persisted_budget_state(state_path):
    """The durable budget window {window_key, spent_tokens} persisted in durable
    state, or {} when the loop never evaluated a budget. Unlike the read products
    this is a durable CROSS-TICK fact (see BUDGET_KEY)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(BUDGET_KEY, {})


def persisted_acted_ledger(state_path):
    """The durable acted-ledger {work_order_id: {outcome, ref}} persisted under
    ACTED_LEDGER_KEY, or {} when no acting agent-state ever recorded an outcome.
    Like the budget window this is a durable CROSS-TICK fact (see
    ACTED_LEDGER_KEY), NOT a per-tick #64 read product."""
    doc = ds.DurableState(state_path).load()
    return doc.get(ACTED_LEDGER_KEY, {})


def persisted_backoff_ledger(state_path):
    """The durable backoff-ledger {work_item_id: {blocked_count,
    deferred_at_updated_at}} persisted under BACKOFF_LEDGER_KEY, or {} when no
    acting agent-state ever reported a blocked work order. Like the acted-ledger
    this is a durable CROSS-TICK fact (see BACKOFF_LEDGER_KEY), NOT a per-tick #64
    read product."""
    doc = ds.DurableState(state_path).load()
    return doc.get(BACKOFF_LEDGER_KEY, {})


def persisted_triage_memory(state_path):
    """The durable triage memory {work_item_id: {updated_at, status}} persisted
    under TRIAGE_MEMORY_KEY, or {} when no acting agent-state ever recorded an
    outcome. Like the backoff ledger this is a durable CROSS-TICK fact (see
    TRIAGE_MEMORY_KEY), NOT a per-tick #64 read product. `status` is `done`
    (opened/closed) or `deferred` (backoff)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(TRIAGE_MEMORY_KEY, {})


def persisted_last_reported(state_path):
    """The last tick's REPORT outcome {filed, skipped, errored} persisted under
    LAST_REPORTED_KEY, or {"filed": 0, "skipped": 0, "errored": 0} when no tick
    ran a flush. Lets status.py surface reported=<filed>/<skipped> without
    re-running. Backward-compatible: a doc persisted before errored was added (no
    `errored` key) still reads, and consumers default errored to 0."""
    doc = ds.DurableState(state_path).load()
    return doc.get(LAST_REPORTED_KEY, {"filed": 0, "skipped": 0, "errored": 0})


def persisted_report_ledger(state_path):
    """The durable report-ledger {dedup_key: {tracker_ref, url}} persisted under
    REPORT_LEDGER_KEY, or {} when the outbound REPORT flush never filed anything.
    Like the budget window + acted-ledger this is a durable CROSS-TICK fact (see
    REPORT_LEDGER_KEY), NOT a per-tick #64 read product."""
    doc = ds.DurableState(state_path).load()
    return doc.get(REPORT_LEDGER_KEY, {})


def persisted_tick_checkpoint(state_path):
    """The durable PAUSED checkpoint persisted under TICK_CHECKPOINT_KEY while a
    tick is paused at an agent-state, or {} when no tick is paused. The
    checkpoint is the SOLE source of truth for the paused dispatch (crash-safety
    re-emit)."""
    doc = ds.DurableState(state_path).load()
    return doc.get(TICK_CHECKPOINT_KEY, {})


def persisted_tick_id_counter(state_path):
    """The durable per-tick id counter persisted under TICK_ID_COUNTER_KEY, or 0
    when no tick has run yet (so the first FRESH tick assigns id 1). Like the work
    `counter` this is a durable CROSS-TICK fact (see TICK_ID_COUNTER_KEY), NOT a
    per-tick #64 read product."""
    doc = ds.DurableState(state_path).load()
    return doc.get(TICK_ID_COUNTER_KEY, 0)


def _assign_tick_id(state_path, resume, checkpoint):
    """Resolve THIS invocation's per-tick id (#112), distinct across ticks AND
    stable across a single tick's `--step` -> `--resume`.

    The id is assigned ONCE, at FRESH tick start, and then carried through the
    tick via the durable checkpoint — it is NEVER derived from the wall clock /
    injected `now` (the `--step` and `--resume` invocations are SEPARATE processes
    that do not inject `now`, so a clock-derived id would split one logical tick
    into two ids):

      - RESUME (`resume=True`) or CRASH-SAFETY RE-EMIT (a `checkpoint` is present):
        READ the id BACK from the checkpoint (`checkpoint["tick_id"]`), so the
        agent route's pause/resume pair shares one id. A legacy checkpoint written
        before this field existed falls back to the persisted counter.
      - FRESH (no resume, no checkpoint): increment the durable per-tick counter
        (load-modify-save ONLY TICK_ID_COUNTER_KEY, preserving every other key)
        and use the new value. The first ever tick gets id 1.

    Returns the integer tick id for this invocation.
    """
    if checkpoint:
        tid = checkpoint.get("tick_id")
        if tid is not None:
            return tid
        # Legacy checkpoint (pre-#112) carried no tick_id: fall back to the
        # persisted counter so a tick paused before the upgrade still resumes.
        return persisted_tick_id_counter(state_path)
    if resume:
        # A resume without a checkpoint is a caller error elsewhere; surface the
        # persisted counter rather than minting a new id.
        return persisted_tick_id_counter(state_path)
    # FRESH tick: mint the next id and persist it.
    doc = ds.DurableState(state_path).load()
    tid = doc.get(TICK_ID_COUNTER_KEY, 0) + 1
    doc[TICK_ID_COUNTER_KEY] = tid
    ds.DurableState(state_path).save(doc)
    return tid


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
# Structured event log (observability §3.9.1).
#
# A thin per-tick emitter bound to an observability.EventLog, the injected
# tz-aware `now` (the SAME clock the budget window keys off — never an implicit
# wall clock), and the durable counter as the tick_id. It append()s one
# structured event per tick milestone. observability owns the schema, the closed
# EVENT_KINDS vocabulary, and the monotonic seq (the file's line count, so a
# multi-invocation agent tick step->resume->done keeps one monotonic sequence).
# --------------------------------------------------------------------------

class _EventEmitter:
    """Appends structured tick events to an observability.EventLog.

    `now` is the tz-aware budget clock (the injected `now`), reused so the event
    `ts` is deterministic. `tick_id` is the durable counter. Every emitted `kind`
    is a member of observability.EVENT_KINDS — run_tick emits no kind outside it.
    """

    def __init__(self, runtime_dir, now, tick_id):
        self.log = ob.EventLog(os.path.join(runtime_dir, EVENTS_FILENAME))
        self.now = now
        self.tick_id = tick_id

    def emit(self, kind, *, state=None, signal=None, detail=None):
        self.log.append(kind, self.tick_id, state=state, signal=signal,
                        detail=detail, now=self.now)


# --------------------------------------------------------------------------
# Agent yield/resume seam (DESIGN §2.8 executor protocol).
#
# A route that contains agent-states pauses at each agent-state (emitting a
# rendered dispatch request naming an output_path) and resumes by READING the
# subagent-WRITTEN output FILE at that output_path (DESIGN §3.4.6 file-based
# context isolation). The driver below mirrors tick_orchestrator.run but is
# agent-aware: it runs SCRIPT states inline and YIELDS a PAUSED result at an AGENT
# state. run_tick NEVER calls the Agent tool — the executor performs the dispatch;
# the subagent writes its output file, then the executor calls run_tick(resume=True)
# which reads it.
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
    # REVIEW (FT-C) is an agent-state writing review_findings (a `once` dispatch
    # whose single output IS the whole array slot), so its output file is
    # validated against the array slot schema.
    vi.REVIEW_FINDINGS_SLOT["name"]: vi.REVIEW_FINDINGS_SLOT["schema"],
}


def _read_slot_or(ctx, name, default):
    """Read a TickContext slot, returning `default` when the slot is not
    registered in this route or not yet written. Lets the acting-state governance
    consult the work_orders / work_items slots without assuming the route seeded
    or populated them."""
    if name not in ctx.registered_slots():
        return default
    try:
        return ctx.read(name)
    except fc.ContractError:
        return default


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


def _pause_result(name, agentstate, slot_values, tick_id, mode, output_dir,
                  triage_memory=None):
    """Build the PAUSED result for an agent-state: render the dispatch
    envelope(s) via agent-dispatch (under `output_dir`) and shape the
    per-dispatch records the executor consumes. Returns the PAUSED dict.

    `slot_values` is the {slot: value} mapping for the agent-state's read slots.
    `output_dir` is the directory each envelope's `output_path` is computed under
    (DESIGN §3.4.6 file-based context isolation): the subagent WRITES its JSON
    output there and the executor reads it back on resume. A `once` dispatch
    yields one record (no `item`); a {per_item: path} dispatch yields one record
    per resolved element, each carrying its `item` and its own `output_path`.

    `triage_memory` (§3.5.3), when given and this agent-state reads work_items,
    FILTERS the work_items fed to build_envelopes — drop done/deferred-AND-
    unchanged items — for RENDER ONLY. The caller's `slot_values` (and thus the
    checkpoint snapshot) is NOT mutated, so the persisted work_items read product
    stays the full PULL set; only the dispatched subagent sees the filtered list.
    """
    entry = agentstate.entry
    if (triage_memory and wi.WORK_ITEMS_SLOT["name"] in slot_values):
        slot_values = dict(slot_values)
        slot_values[wi.WORK_ITEMS_SLOT["name"]] = _filter_triage_work_items(
            slot_values[wi.WORK_ITEMS_SLOT["name"]], triage_memory)
    envelopes = ad.build_envelopes(
        entry, slot_values, {"tick_id": tick_id, "mode": mode}, state=name,
        output_dir=output_dir)
    signal_rule = entry["signal"]["rule"]
    dispatches = []
    # build_envelopes flattens all dispatch entries; this slice's agent-states
    # carry a single dispatch entry, so the per-envelope cardinality/writes come
    # from that entry. Pair each envelope with its dispatch entry's metadata.
    dispatch_entry = entry["dispatch"][0]
    isolation = dispatch_entry.get("isolation")
    for env in envelopes:
        output_path = env["output_contract"]["output_path"]
        # Deliver the rendered invocation envelope by FILE REFERENCE (symmetric
        # with the file-based OUTPUT): WRITE ad.render(env) to a deterministic
        # ABSOLUTE file under output_dir, named parallel to output_path
        # ('<state>-<di>-<ii>.json' -> '<state>-<di>-<ii>.prompt.md'), set
        # prompt_path, and DROP the inline prompt. The orchestrator then never
        # receives/holds the multi-KB rendered envelope inline (which truncated
        # --step stdout and forced a re-read); it receives only the path. The
        # file is written here at the PAUSE, the same place stale OUTPUT files are
        # cleared, and OVERWRITTEN each emit — so a crash-safety re-emit (rendered
        # from the durable checkpoint round-trip, like output_path) reproduces the
        # SAME prompt_path with byte-identical content.
        prompt_path = _prompt_path_for(output_path)
        with open(prompt_path, "w") as f:
            f.write(ad.render(env))
        rec = {
            "subagent_type": dispatch_entry["subagent_type"],
            "prompt_path": prompt_path,
            "writes": dispatch_entry["writes"],
            "output_path": output_path,
            "signal_rule": signal_rule,
            "cardinality": dispatch_entry["cardinality"],
            # isolation + description + prompt_path let the executor call
            # Agent(subagent_type, description=..., prompt=<a short reference to
            # prompt_path>, isolation=...). isolation is the dispatch entry's
            # value (e.g. "worktree") or null when absent; description is the
            # entry's value, else a default.
            "isolation": isolation,
            "description": _dispatch_description(dispatch_entry, name, env),
        }
        if "item" in env:
            rec["item"] = env["item"]
        dispatches.append(rec)
    return {"status": "paused", "state": name, "dispatches": dispatches}


def _prompt_path_for(output_path):
    """The deterministic prompt-file path parallel to a dispatch's output_path:
    `<state>-<di>-<ii>.json` -> `<state>-<di>-<ii>.prompt.md` in the same
    dispatch-out/ directory. output_path is absolute (#143), so prompt_path is
    too."""
    return output_path[: -len(".json")] + ".prompt.md"


def _ref_of(value):
    """The `#NNN` issue/PR ref of a single dispatch value, or None.

    A string ref (e.g. a work-order id `wo-owner/repo#275` or a PR ref
    `owner/repo#276`) yields `#` + the substring after the LAST `#`. A dict
    prefers `pr_ref`, then `number`, then `id` (mirroring the per_item id
    precedence used elsewhere). Returns the `#`-prefixed ref string or None."""
    if isinstance(value, dict):
        for key in ("pr_ref", "number", "id"):
            if value.get(key) is not None:
                return _ref_of(value[key])
        return None
    if isinstance(value, int):
        return f"#{value}"
    if isinstance(value, str) and value:
        tail = value.rsplit("#", 1)[-1]
        return f"#{tail}" if tail else None
    return None


def _dispatch_refs(env):
    """The issue/PR refs this dispatch works on, as `#NNN` strings (pure,
    deterministic, no mutation of `env`).

    For a per_item dispatch (`env` carries `item`) the ref is that item's ref.
    For a once dispatch the refs are collected from `env['inputs']` — each input
    slot that holds a list-of-dicts contributes a ref per element (from `pr_ref`,
    e.g. REVIEW verdicts, or `number`, e.g. TRIAGE work_items). Refs are
    de-duped, order-preserving."""
    refs = []
    if "item" in env:
        ref = _ref_of(env["item"])
        if ref:
            refs.append(ref)
    else:
        for value in env.get("inputs", {}).values():
            if isinstance(value, list):
                for element in value:
                    ref = _ref_of(element)
                    if ref and ref not in refs:
                        refs.append(ref)
    return refs


# Cap on how many refs the description names before summarizing the rest.
_DISPATCH_REFS_CAP = 6


def _dispatch_description(dispatch_entry, name, env):
    """The description the executor passes to Agent for this dispatch.

    Branches on cardinality so parallel subagents show distinct names:

    - **per_item** (`env` carries `item`) → ALWAYS name the item ref. The base
      is the explicit `dispatch_entry['description']` when present, else the
      state `name`; the item ref is appended → `f"{base} {ref}"` (e.g.
      `auto-maintainer implement #275`). When the item yields NO derivable ref,
      fall back to the explicit description verbatim when present, else
      `f"{name} dispatch"`. The explicit description is a PREFIX here, never the
      verbatim whole — so a per_item fan-out never collapses to one static label.
    - **once** (no `item`) → (0) the explicit `description` wins verbatim; else
      NAME the collected refs (`f"{name} #276, #277"`); else `f"{name} dispatch"`.
      A long ref list is capped at `_DISPATCH_REFS_CAP` with a `+K more` suffix."""
    desc = dispatch_entry.get("description")
    if "item" in env:
        ref = _ref_of(env["item"])
        if ref:
            base = desc if desc else name
            return f"{base} {ref}"
        return desc if desc else f"{name} dispatch"
    if desc:
        return desc
    refs = _dispatch_refs(env)
    if not refs:
        return f"{name} dispatch"
    if len(refs) > _DISPATCH_REFS_CAP:
        shown = refs[:_DISPATCH_REFS_CAP]
        suffix = f", +{len(refs) - _DISPATCH_REFS_CAP} more"
    else:
        shown = refs
        suffix = ""
    return f"{name} {', '.join(shown)}{suffix}"


# The INERT handoff a dry-run trust-gated acting agent-state synthesizes per
# dispatch item (DESIGN §2.3 / §3.8.2 trust ladder): the model never acts, so the
# handoff is always `planned` with a `none` artifact. Mirrors the dry-run
# IMPLEMENT script's `_planned_handoff` shape (the closed handoff schema).
def _inert_planned_handoff(work_order_id):
    return {
        "work_order_id": work_order_id,
        "status": "planned",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": None,
    }


# The DEFERRED handoff a budget-exhausted acting agent-state synthesizes per
# not-yet-acted dispatch item: no work could be dispatched (the budget window is
# exhausted), so status is `blocked` with a budget reason. Mirrors the closed
# handoff schema (implement's _blocked_handoff shape). The items stay un-acted —
# they are NOT recorded in the acted-ledger, so they retry on a later window.
def _budget_blocked_handoff(work_order_id, reason):
    return {
        "work_order_id": work_order_id,
        "status": "blocked",
        "artifact": {"kind": "none", "ref": None},
        "discovered_work": [],
        "concerns": [],
        "blocked_reason": reason,
    }


def _item_id(item):
    """The work_order_id of a per_item dispatch element. PRIORITIZE writes
    execution_plan.ordered as a list of id strings; accept a {"id": ...} dict
    shape too (mirrors implement._entry_id). Returns the id string or None."""
    if isinstance(item, str):
        return item or None
    if isinstance(item, dict):
        return item.get("id") or None
    return None


def _work_order_to_work_item(work_orders):
    """A {work_order_id: work_item_id} map from a list of WorkOrder dicts, so the
    acting-state governance can key backoff on the SOURCE work_item_id while the
    acted-ledger stays work_order_id-keyed. A work_order missing either id is
    skipped."""
    mapping = {}
    for wo in work_orders or []:
        wo_id = wo.get("id")
        wi_id = wo.get("work_item_id")
        if wo_id and wi_id:
            mapping[wo_id] = wi_id
    return mapping


def _work_items_updated_at(work_items):
    """A {work_item_id: updated_at} map from a list of WorkItem dicts (the work
    item `id` IS the work_item_id, e.g. `owner/repo#N`). The deferral pins +
    compares against the issue's CURRENT updated_at, the durable, GitHub-native,
    session-independent retry trigger (§3.8.5). A work item missing its id is
    skipped."""
    mapping = {}
    for it in work_items or []:
        wi_id = it.get("id")
        if wi_id:
            mapping[wi_id] = it.get("updated_at", "")
    return mapping


def _is_deferred_unchanged(wi_id, backoff, wi_updated_at):
    """True when work_item `wi_id` is DEFERRED in the backoff ledger AND its issue
    updated_at is UNCHANGED since deferral (current updated_at ==
    deferred_at_updated_at) — the deferred-unchanged skip condition (§3.8.5). A
    deferred item whose updated_at has ADVANCED is NOT unchanged (it re-enters)."""
    entry = backoff.get(wi_id)
    if not entry:
        return False
    deferred_at = entry.get("deferred_at_updated_at")
    if not deferred_at:
        return False
    return wi_updated_at.get(wi_id, "") == deferred_at


def _acted_updated_at_advanced(entry, wi_id, wi_updated_at):
    """True when an acted-ledger `entry`'s SOURCE work_item updated_at has ADVANCED
    past the act-time pin (the §3.8.5-symmetric re-entry trigger, #204).

    Compares the issue's CURRENT updated_at (from `wi_updated_at`) against the
    entry's `acted_at_updated_at`. False when either is missing/empty (a null pin
    — e.g. a pre-#204 ledger entry — can never re-enter; the issue stays locked) or
    when the current value is not strictly greater (issue unchanged or regressed).
    GitHub's updated_at is an ISO-8601 lexicographically-sortable timestamp, so a
    string `>` is the correct chronological compare (mirrors the backoff pin)."""
    acted_at = entry.get("acted_at_updated_at")
    if not acted_at or not wi_id:
        return False
    current = wi_updated_at.get(wi_id, "")
    if not current:
        return False
    return current > acted_at


def _agentstate_collection(agentstate, slot_values):
    """Resolve the per_item collection (e.g. execution_plan.ordered) from
    `slot_values` for an acting agent-state, or [] when the dispatch is `once`
    (no per_item cardinality). Used to bound the acted-ledger re-entry PR query
    (#204) to the items actually in this tick's dispatch set."""
    dispatch_entry = agentstate.entry["dispatch"][0]
    cardinality = dispatch_entry["cardinality"]
    if not isinstance(cardinality, dict) or "per_item" not in cardinality:
        return []
    return ad._resolve_path(slot_values, cardinality["per_item"])


def _acted_reentry_wo_ids(collection, ledger, wo_to_wi, wi_updated_at,
                          pr_state_source):
    """The set of already-`opened` work_order_ids in `collection` that RE-ENTER
    the dispatch set (§3.8.5-symmetric leak fix, #204).

    An `opened` ledger entry re-enters when BOTH:
      - the issue's updated_at has ADVANCED past the entry's acted_at_updated_at
        (a human touched the issue after the auto-PR was opened), AND
      - the entry's PR `ref` is CLOSED-AND-NOT-MERGED (queried via the injectable
        `pr_state_source`).
    It stays LOCKED otherwise: merged (done), still-open PR (pending review), or
    closed-but-issue-unchanged (the human closed it without a redo — no thrash).

    The PR is queried ONLY for entries whose updated_at advanced, bounding the gh
    calls to changed issues. A `pr_state_source` raising / returning a malformed
    value is treated as "stay locked" (never crash the tick, never thrash). A
    non-`opened` ledger entry (e.g. a `closed` outcome) never re-enters. Returns a
    set of work_order_ids."""
    reenter = set()
    if pr_state_source is None:
        return reenter
    for el in collection:
        wo_id = _item_id(el)
        entry = ledger.get(wo_id)
        if not entry or entry.get("outcome") != "opened":
            continue
        wi_id = wo_to_wi.get(wo_id)
        if not _acted_updated_at_advanced(entry, wi_id, wi_updated_at):
            continue  # issue unchanged since the PR was opened — stay locked
        ref = entry.get("ref")
        if not ref:
            continue  # no PR ref to check — can't confirm closed; stay locked
        try:
            pr = pr_state_source(ref)
        except Exception:
            continue  # fetch failed — stay locked (no crash, no thrash)
        if not isinstance(pr, dict):
            continue
        # Re-enter ONLY a closed-and-not-merged PR; merged (done) / still-open
        # (pending review) stay locked.
        if pr.get("state") == "CLOSED" and not pr.get("merged"):
            reenter.add(wo_id)
    return reenter


def _per_item_filtered_values(agentstate, slot_values, ledger, backoff=None,
                              wo_to_wi=None, wi_updated_at=None,
                              reenter_wo_ids=None):
    """Drop already-acted AND deferred-unchanged items from an ACTING agent-state's
    per_item collection.

    Returns (filtered_slot_values, remaining_items, dropped_any, reenter_wi_ids).
    The per_item cardinality (e.g. {"per_item": "execution_plan.ordered"}) is
    resolved against `slot_values`; each element is filtered OUT when EITHER:
      - its work_order_id is already a key in `ledger` (already acted — never
        re-dispatch) AND it is NOT in `reenter_wo_ids` (the §3.8.5-symmetric
        acted-ledger re-entry set: an `opened` PR a human closed while the issue
        advanced — #204), OR
      - its SOURCE work_item_id (via `wo_to_wi`) is DEFERRED-and-UNCHANGED in
        `backoff` (deferred + the issue updated_at has not advanced — no thrash).
    An item whose work_item_id is deferred but whose issue updated_at has ADVANCED
    is NOT filtered (it re-enters); its work_item_id is collected into
    `reenter_wi_ids` so the caller can RESET its backoff entry (fresh attempts).
    An acted item whose work_order_id is in `reenter_wo_ids` re-enters too (the
    caller clears its acted-ledger entry).

    The returned `filtered_slot_values` is a shallow copy with the per_item
    collection's host slot replaced by a filtered shape, so ad.build_envelopes
    naturally yields one dispatch per REMAINING item. A `once` dispatch (no
    per_item) is returned unchanged with dropped_any=False (the filter is
    per_item-only)."""
    backoff = backoff or {}
    wo_to_wi = wo_to_wi or {}
    wi_updated_at = wi_updated_at or {}
    reenter_wo_ids = reenter_wo_ids or set()
    dispatch_entry = agentstate.entry["dispatch"][0]
    cardinality = dispatch_entry["cardinality"]
    if not isinstance(cardinality, dict) or "per_item" not in cardinality:
        return slot_values, None, False, []
    dotted = cardinality["per_item"]
    collection = ad._resolve_path(slot_values, dotted)

    remaining = []
    reenter_wi_ids = []
    for el in collection:
        wo_id = _item_id(el)
        if wo_id in ledger and wo_id not in reenter_wo_ids:
            continue  # already acted (and not re-entering) — never re-dispatch
        wi_id = wo_to_wi.get(wo_id)
        if wi_id and _is_deferred_unchanged(wi_id, backoff, wi_updated_at):
            continue  # deferred + unchanged — no thrash
        # A deferred item whose issue updated_at advanced re-enters with a reset.
        if wi_id and backoff.get(wi_id, {}).get("deferred_at_updated_at"):
            reenter_wi_ids.append(wi_id)
        remaining.append(el)

    if len(remaining) == len(collection):
        return slot_values, remaining, False, reenter_wi_ids
    # Rebuild the host slot with the per_item collection filtered down. The path
    # is `<slot>.<...>.<leaf>`; copy the chain and replace the leaf list.
    parts = dotted.split(".")
    slot_name = parts[0]
    filtered = dict(slot_values)
    if len(parts) == 1:
        filtered[slot_name] = remaining
    else:
        host = dict(slot_values[slot_name])
        cur = host
        for part in parts[1:-1]:
            cur[part] = dict(cur[part])
            cur = cur[part]
        cur[parts[-1]] = remaining
        filtered[slot_name] = host
    return filtered, remaining, True, reenter_wi_ids


# The COMPLETED outcomes that count as "acted" (idempotency, §3.2.4). A work
# order the doer reports `blocked` is NOT completed — it must stay retryable
# (§3.8.5 leak fix), so it is NEVER written to the acted-ledger.
_COMPLETED_OUTCOMES = ("opened", "closed")


def _record_acted_ledger(state_path, handoffs, wo_to_wi=None,
                         wi_updated_at=None):
    """Record each newly-COMPLETED handoff into the durable acted-ledger
    (idempotency, §3.2.4). For each handoff whose status is a completed outcome
    (`opened`/`closed`):
    ledger[work_order_id] = {"outcome": handoff["status"], "ref":
    handoff.get("artifact", {}).get("ref"), "acted_at_updated_at": <the issue's
    current updated_at>}. The `acted_at_updated_at` is the SOURCE work_item's
    updated_at (resolved via `wo_to_wi` -> `wi_updated_at`); it is the act-time
    issue state the §3.8.5-symmetric re-entry rule (#204) compares against so a
    human who CLOSES the auto-PR + UPDATES the issue gets the loop to re-attempt.
    Both maps default to {} (then acted_at_updated_at is null — back-compatible,
    a null pin can never re-enter since no current updated_at equals null<advance).
    Load-modify-save of ONLY ACTED_LEDGER_KEY, preserving every other durable key.
    A handoff with no work_order_id is skipped (nothing to key on).

    The §3.8.5 leak fix: a `blocked` (or any non-completed) handoff is NEVER
    recorded — previously it was, then filtered out as "already acted" forever
    (silent leak). A blocked item now stays retryable; its bounded-retry state
    lives in the backoff ledger instead (see _record_backoff_outcomes)."""
    wo_to_wi = wo_to_wi or {}
    wi_updated_at = wi_updated_at or {}
    doc = ds.DurableState(state_path).load()
    ledger = dict(doc.get(ACTED_LEDGER_KEY, {}))
    for h in handoffs:
        wo_id = h.get("work_order_id")
        if not wo_id:
            continue
        if h.get("status") not in _COMPLETED_OUTCOMES:
            continue
        wi_id = wo_to_wi.get(wo_id)
        ledger[wo_id] = {
            "outcome": h.get("status"),
            "ref": h.get("artifact", {}).get("ref"),
            "acted_at_updated_at": wi_updated_at.get(wi_id) if wi_id else None,
        }
    doc[ACTED_LEDGER_KEY] = ledger
    ds.DurableState(state_path).save(doc)


def _record_backoff_outcomes(state_path, handoffs, wo_to_wi, wi_updated_at,
                             escalate_sink, now, threshold):
    """Update the durable backoff ledger from an acting-state resume's handoffs
    (§3.8.5). For each handoff, mapped from work_order_id -> work_item_id via
    `wo_to_wi`:

      - a COMPLETED outcome (`opened`/`closed`) CLEARS the item's backoff entry
        (a success resets the counter).
      - a `blocked` outcome increments blocked_count. When it reaches the
        configured `threshold` (config-driven backoff.threshold, default 5) the
        item is DEFERRED: deferred_at_updated_at is pinned to the issue's current
        updated_at (from `wi_updated_at`) and a single escalation is posted on the
        issue via observability.escalate (the injectable `escalate_sink`; never
        raises, never crashes the tick).

    Load-modify-save of ONLY BACKOFF_LEDGER_KEY, preserving every other durable
    key. A handoff with no resolvable work_item_id is skipped."""
    doc = ds.DurableState(state_path).load()
    backoff = dict(doc.get(BACKOFF_LEDGER_KEY, {}))
    for h in handoffs:
        wo_id = h.get("work_order_id")
        wi_id = wo_to_wi.get(wo_id)
        if not wi_id:
            continue
        status = h.get("status")
        if status in _COMPLETED_OUTCOMES:
            backoff.pop(wi_id, None)
            continue
        if status != "blocked":
            continue
        entry = dict(backoff.get(wi_id) or {})
        count = int(entry.get("blocked_count", 0)) + 1
        entry["blocked_count"] = count
        entry.setdefault("deferred_at_updated_at", None)
        if count >= threshold and not entry.get("deferred_at_updated_at"):
            updated_at = wi_updated_at.get(wi_id, "")
            entry["deferred_at_updated_at"] = updated_at
            reason = h.get("blocked_reason")
            message = (f"auto-maintainer attempted {count} times and is "
                       f"blocked: {reason}. Needs human attention.")
            # escalate swallows sink errors (returns ok:False) and never raises,
            # so a failed escalation cannot crash the tick (§3.8.5).
            ob.escalate(wi_id, message, sink=escalate_sink, now=now)
        backoff[wi_id] = entry
    doc[BACKOFF_LEDGER_KEY] = backoff
    ds.DurableState(state_path).save(doc)


def _record_triage_memory(state_path, handoffs, wo_to_wi, wi_updated_at):
    """Upsert the durable triage memory from an acting-state resume's handoffs
    (§3.5.3). For each handoff, mapped from work_order_id -> work_item_id via
    `wo_to_wi`:

      - a COMPLETED outcome (`opened`/`closed`) records status='done'.
      - a `blocked` outcome that just reached the backoff DEFERRAL (its post-update
        backoff entry has a truthy `deferred_at_updated_at`) records
        status='deferred'.
      - any other outcome (blocked-but-not-yet-deferred) records NOTHING — the
        item stays `active` and MUST keep being triaged (never starved).

    `updated_at` is the item's CURRENT issue updated_at (from `wi_updated_at`), the
    same value the skip-unchanged filter compares against. Load-modify-save of ONLY
    TRIAGE_MEMORY_KEY, preserving every other durable key. Called AFTER
    _record_backoff_outcomes so the just-deferred items are visible in the backoff
    ledger. A handoff with no resolvable work_item_id is skipped."""
    doc = ds.DurableState(state_path).load()
    memory = dict(doc.get(TRIAGE_MEMORY_KEY, {}))
    backoff = doc.get(BACKOFF_LEDGER_KEY, {})
    changed = False
    for h in handoffs:
        wo_id = h.get("work_order_id")
        wi_id = wo_to_wi.get(wo_id)
        if not wi_id:
            continue
        status = h.get("status")
        if status in _COMPLETED_OUTCOMES:
            new_status = "done"
        elif status == "blocked" and backoff.get(wi_id, {}).get(
                "deferred_at_updated_at"):
            new_status = "deferred"
        else:
            # Blocked-but-not-yet-deferred (active): keep triaging it.
            continue
        memory[wi_id] = {"updated_at": wi_updated_at.get(wi_id, ""),
                         "status": new_status}
        changed = True
    if changed:
        doc[TRIAGE_MEMORY_KEY] = memory
        ds.DurableState(state_path).save(doc)


def _record_last_triaged(state_path, judged, pulled):
    """Persist the last tick's TRIAGE filter outcome {judged, pulled} under
    LAST_TRIAGED_KEY so the terminal trace can surface triaged=<judged>/<pulled>
    even though TRIAGE may have paused in an earlier invocation than the one that
    reaches the terminal. Load-modify-save of ONLY LAST_TRIAGED_KEY."""
    doc = ds.DurableState(state_path).load()
    doc[LAST_TRIAGED_KEY] = {"judged": judged, "pulled": pulled}
    ds.DurableState(state_path).save(doc)


def _filter_triage_work_items(work_items, triage_memory):
    """Drop done/deferred-AND-unchanged work_items from the list fed to a
    work_items-reading agent-state (TRIAGE), per §3.5.3. An item is dropped iff
    `triage_memory[item.id].status` is in _TRIAGE_SKIP_STATUSES AND its current
    `updated_at` EQUALS the remembered `updated_at` (handled + unchanged). NEW
    items (not in memory), CHANGED items (advanced updated_at), and `active` items
    (not recorded) are KEPT. Returns the filtered list (a new list; the input is
    not mutated). An empty memory returns the list unchanged (byte-identical)."""
    if not triage_memory:
        return list(work_items)
    kept = []
    for it in work_items:
        wi_id = it.get("id")
        remembered = triage_memory.get(wi_id)
        if (remembered
                and remembered.get("status") in _TRIAGE_SKIP_STATUSES
                and remembered.get("updated_at", "") == it.get("updated_at", "")):
            continue  # handled + unchanged — skip re-triage
        kept.append(it)
    return kept


def _reset_backoff_entries(state_path, wi_ids):
    """Reset (clear) the backoff entries for `wi_ids` — items that re-entered the
    dispatch set because their issue updated_at ADVANCED since deferral (§3.8.5).
    They get a fresh K attempts. Load-modify-save of ONLY BACKOFF_LEDGER_KEY."""
    if not wi_ids:
        return
    doc = ds.DurableState(state_path).load()
    backoff = dict(doc.get(BACKOFF_LEDGER_KEY, {}))
    changed = False
    for wi_id in wi_ids:
        if wi_id in backoff:
            backoff.pop(wi_id, None)
            changed = True
    if changed:
        doc[BACKOFF_LEDGER_KEY] = backoff
        ds.DurableState(state_path).save(doc)


def _clear_acted_entries(state_path, wo_ids):
    """Clear the acted-ledger entries for `wo_ids` — already-`opened` work orders
    whose auto-PR a human CLOSED while the issue advanced, so they RE-ENTER the
    dispatch set (§3.8.5-symmetric leak fix, #204). Clearing the entry lets the
    item be re-dispatched (and a fresh PR opened + re-recorded) on this tick.
    Load-modify-save of ONLY ACTED_LEDGER_KEY, preserving every other durable
    key. A wo_id not in the ledger is a no-op."""
    if not wo_ids:
        return
    doc = ds.DurableState(state_path).load()
    ledger = dict(doc.get(ACTED_LEDGER_KEY, {}))
    changed = False
    for wo_id in wo_ids:
        if ledger.pop(wo_id, None) is not None:
            changed = True
    if changed:
        doc[ACTED_LEDGER_KEY] = ledger
        ds.DurableState(state_path).save(doc)


# --------------------------------------------------------------------------
# Outbound REPORT flush (DESIGN §1.3 / §3.11): discovered_work -> tracker.
#
# REPORT is out-of-band — NOT a routed state. After the route completes at the
# tick TERMINAL, run_tick gathers the tick's discoveries (handoffs[].discovered_work
# + an optional ctx `discoveries` slot), normalizes each to a work-intake
# DiscoveredIssue (deriving a stable dedup_key), trust-gates filing on
# sg.permits('file', mode), and files the un-seen ones through work-intake's
# file_discoveries with a durable REPORT_LEDGER_KEY for journaled idempotency.
# work-intake + safety-governance are consumed UNCHANGED.
# --------------------------------------------------------------------------

def _derive_dedup_key(title, body, work_order_id=None):
    """A STABLE dedup_key for a discovery: a sha1 of title+'\\n'+body, prefixed by
    the source work_order_id when present, so the SAME discovery always yields the
    SAME key (a refire / DRAIN replay never double-files)."""
    digest = hashlib.sha1(f"{title}\n{body}".encode("utf-8")).hexdigest()
    if work_order_id:
        return f"{work_order_id}:{digest}"
    return digest


def _normalize_discovery(raw, fallback_wo_id=None):
    """Complete a raw discovery dict into a work-intake DiscoveredIssue.

    Fills the loop's defaults: filed_by="autonomous-maintainer", target="project",
    kind="task", severity="low". A missing dedup_key is DERIVED deterministically
    (a stable sha1 of title+body, optionally prefixed by the discovery's own
    work_order_id else `fallback_wo_id`). Consumes work-intake's DiscoveredIssue
    schema unchanged."""
    title = raw.get("title", "")
    body = raw.get("body", "")
    wo_id = raw.get("work_order_id") or fallback_wo_id
    dedup_key = raw.get("dedup_key") or _derive_dedup_key(title, body, wo_id)
    return wi.DiscoveredIssue(
        title=title,
        body=body,
        kind=raw.get("kind") or "task",
        severity=raw.get("severity") or "low",
        target=raw.get("target") or "project",
        dedup_key=dedup_key,
        filed_by=raw.get("filed_by") or "autonomous-maintainer",
    )


def _gather_discoveries(handoffs, discoveries_slot):
    """The raw discovery dicts this tick surfaced: every handoff's
    discovered_work[] (each tagged with its handoff's work_order_id as the dedup
    fallback) plus the optional ctx `discoveries` slot. Returns a list of
    (raw_dict, fallback_wo_id) pairs."""
    pairs = []
    for h in handoffs or []:
        wo_id = h.get("work_order_id")
        for raw in h.get("discovered_work", []) or []:
            pairs.append((raw, wo_id))
    for raw in discoveries_slot or []:
        pairs.append((raw, None))
    return pairs


def _repo_for_target(target, gov):
    """The destination repo for a DiscoveredIssue.target: `project` -> the gh
    default repo (None); `maintainer-self` -> the FIXED
    safety_governance.MAINTAINER_REPO (the upstream maintainer repo, §3.11.6) —
    NEVER the project repo, with NO fallback. The former governance.maintainer_repo
    config field is gone, so `gov` is ignored for maintainer-self routing."""
    if target == "maintainer-self":
        return sg.MAINTAINER_REPO
    return None


def _flush_report(state_path, handoffs, discoveries_slot, mode, gov, sink,
                  work_items=()):
    """Flush the tick's discoveries through work-intake's REPORT port at the
    terminal (out-of-band). Returns (filed_count, skipped_count, errored_count).

    Gathers + normalizes the discoveries, then trust-gates filing on
    sg.permits('file', mode):
      - NOT permitted (dry-run): does NOT file and leaves the ledger UNTOUCHED so
        a later armed tick files them; returns (0, <would-file count>, 0) — the
        intent (the would-file count) is surfaced via the returned skipped count.
      - permitted (propose / auto-merge): files each not-yet-known discovery via
        wi.file_discoveries (the per-target repo bound onto the injected sink),
        then RECORDS each filed {dedup_key: {tracker_ref, url}} into the durable
        REPORT_LEDGER_KEY (load-modify-save just that key). Returns
        (len(filed), len(skipped_existing)+len(skipped_open), len(errors)).

    `work_items` are the tick's PULLed open tracker items, passed to
    wi.file_discoveries as `known_open` so a discovery whose subject DUPLICATES
    an already-open issue is skipped (dedup-vs-open, DESIGN §3.5.4) rather than
    filed as duplicate noise. Those skips fold into the returned skipped count
    (so `reported=<filed>/<skipped>` reflects them and they are NOT filed).

    The errored count (from ReportResult.errors) makes a filing failure VISIBLE:
    a sink error (e.g. a missing tracker label) was caught into errors but the
    surface only showed reported=0/0, so a total filing failure looked like "no
    discoveries". Surfacing errored is the fix.
    """
    pairs = _gather_discoveries(handoffs, discoveries_slot)
    if not pairs:
        return 0, 0, 0
    normalized = [_normalize_discovery(raw, fallback_wo_id=wo_id)
                  for raw, wo_id in pairs]
    known = set(persisted_report_ledger(state_path))

    if not sg.permits("file", mode):
        # dry-run: log the intent (the would-file count) but DO NOT file and DO
        # NOT touch the ledger — a later armed tick files them. The would-file
        # count excludes discoveries that already duplicate an OPEN issue.
        would_file = len([
            d for d in normalized
            if d.dedup_key not in known
            and wi._match_open_issue(d, work_items) is None])
        return 0, would_file, 0

    def _routed_sink(discovery):
        return sink(discovery, repo=_repo_for_target(discovery.target, gov))

    result = wi.file_discoveries(
        normalized, sink=_routed_sink, known_dedup_keys=known,
        known_open=work_items)

    # Record each newly-filed discovery into the durable report-ledger
    # (load-modify-save ONLY REPORT_LEDGER_KEY, preserving every other key).
    if result.filed:
        doc = ds.DurableState(state_path).load()
        ledger = dict(doc.get(REPORT_LEDGER_KEY, {}))
        for entry in result.filed:
            ledger[entry["dedup_key"]] = {
                "tracker_ref": entry["tracker_ref"],
                "url": entry["url"],
            }
        doc[REPORT_LEDGER_KEY] = ledger
        ds.DurableState(state_path).save(doc)

    skipped = len(result.skipped_existing) + len(result.skipped_open)
    return len(result.filed), skipped, len(result.errors)


def _gate_acting_state(name, agentstate, slot_values, tick_id, mode,
                       output_dir):
    """Synthesize the INERT result for a trust-gated (not-permitted) acting
    agent-state — used in dry-run, where sg.permits(effect, mode) is False.

    Builds the per-dispatch items via ad.build_envelopes (to know the work-order
    ids / cardinality), produces one inert `planned` handoff per item (carrying
    the item's id when per_item, else null for `once`), collect_outputs them into
    the writes slot value, and computes the route signal. Returns
    (slot_name, slot_value, signal) for the driver to apply. No PAUSE, no
    checkpoint, no subagent dispatch — the model never decides whether to act.
    """
    entry = agentstate.entry
    dispatch_entry = entry["dispatch"][0]
    envelopes = ad.build_envelopes(
        entry, slot_values, {"tick_id": tick_id, "mode": mode},
        state=name, output_dir=output_dir)
    outputs = [_inert_planned_handoff(env.get("item")) for env in envelopes]
    slot_value = ad.collect_outputs(dispatch_entry, outputs)
    signal = ad.compute_signal(entry["signal"]["rule"], slot_value)
    return dispatch_entry["writes"], slot_value, signal


def _synthesize_acting_result(agentstate, outputs):
    """Collect a pre-built list of handoff `outputs` into the acting agent-state's
    writes slot value and compute the route signal — used on the doer-governance
    SKIP paths (budget pre-gate deferral; all-items-already-acted). Returns
    (slot_name, slot_value, signal) for the driver to apply, mirroring
    _gate_acting_state's contract. No PAUSE, no checkpoint, no subagent."""
    entry = agentstate.entry
    dispatch_entry = entry["dispatch"][0]
    slot_value = ad.collect_outputs(dispatch_entry, outputs)
    signal = ad.compute_signal(entry["signal"]["rule"], slot_value)
    return dispatch_entry["writes"], slot_value, signal


def _resume_schema(writes, cardinality):
    """The schema each subagent-written output file is validated against.

    For a `once` dispatch the single file IS the whole slot value, so it is
    validated against the slot schema. For a {per_item: ...} dispatch each file is
    ONE ELEMENT collect_outputs assembles into the array slot; validating each
    element against the array slot schema would wrongly reject a single object, so
    per-item files are validated as generic JSON (no top-level type check)."""
    if cardinality == "once":
        return _SLOT_SCHEMAS.get(writes, {})
    return {}


def _write_checkpoint(state_path, name, ctx, path, signals, agentstate,
                      output_dir, slot_values, tick_id, mode):
    """Journal-free durable checkpoint of the PAUSED tick under
    TICK_CHECKPOINT_KEY. The slots snapshot is the full live blackboard; pending
    carries the agent-state's dispatch metadata + each dispatch's `output_path`
    (the subagent-written output FILE) and validation `schema`, so resume can
    READ + validate + apply. `output_dir` is persisted so a crash-safety re-emit
    builds byte-identical output_paths."""
    entry = agentstate.entry
    dispatch_entry = entry["dispatch"][0]
    writes = dispatch_entry["writes"]
    cardinality = dispatch_entry["cardinality"]
    # The per-dispatch output_paths are the SAME ones the PAUSE renders (the
    # subagent writes there). Built from output_dir + the live slot values, which
    # are exactly what _pause_result renders from, so they agree byte-for-byte.
    # The triage memory (§3.5.3) is passed so the rendered work_items match the
    # filtered TRIAGE dispatch; it filters the RENDER only, never the snapshot.
    triage_memory = persisted_triage_memory(state_path)
    paused = _pause_result(name, agentstate, slot_values, tick_id, mode,
                           output_dir, triage_memory=triage_memory)
    schema = _resume_schema(writes, cardinality)
    dispatches = [{"output_path": d["output_path"], "schema": schema}
                  for d in paused["dispatches"]]
    # The stored slot snapshot is the full live blackboard, but the agent-state's
    # READ slots are overridden with `slot_values` — which may have been FILTERED
    # by the acted-ledger (already-acted per_item elements dropped). This keeps
    # _emit_pause_from_checkpoint's render + the resume's restored blackboard in
    # lock-step with the dispatched (un-acted) items, so a re-emit and the resume
    # both see only the remaining work. NOTE: the §3.5.3 work_items skip-filter is
    # NOT applied here — it filters the dispatch RENDER only (above), so the
    # snapshot keeps the full PULL work_items and the persisted read product stays
    # the full set.
    snapshot = _snapshot_slots(ctx)
    for slot in agentstate.manifest.reads:
        if slot in slot_values:
            snapshot[slot] = slot_values[slot]
    doc = ds.DurableState(state_path).load()
    doc[TICK_CHECKPOINT_KEY] = {
        "next_state": name,
        # The per-tick id (#112) is stamped into the checkpoint so a `--resume`
        # (a separate process, no injected `now`) reads back the SAME id this
        # tick was assigned at its fresh start, rather than recomputing it.
        "tick_id": tick_id,
        "slots": snapshot,
        "path": list(path),
        "signals": list(signals),
        "output_dir": output_dir,
        "pending": {
            "state": name,
            "writes": writes,
            "signal_rule": entry["signal"]["rule"],
            "cardinality": cardinality,
            "dispatches": dispatches,
        },
    }
    ds.DurableState(state_path).save(doc)


def _clear_checkpoint(state_path):
    """Clear the PAUSED checkpoint on reaching the terminal."""
    doc = ds.DurableState(state_path).load()
    if TICK_CHECKPOINT_KEY in doc:
        del doc[TICK_CHECKPOINT_KEY]
        ds.DurableState(state_path).save(doc)


def _checkpoint_compatible(checkpoint, ctx):
    """Whether a durable PAUSED `checkpoint` is compatible with the CURRENT
    (freshly-seeded) wiring carried by `ctx` (upgrade-in-flight crash guard).

    True when the checkpoint is empty/absent OR its pending dispatch `writes` slot
    is registered in `ctx.registered_slots()`. False when the pending writes slot
    is NOT registered — a retired/renamed slot after a plugin upgrade (e.g. a
    v0.7.0 checkpoint paused at REVIEW writing the retired `review_verdicts` slot
    that the redesigned loop replaced with `review_findings`). The adapter-map
    migration self-heals the live CONFIG, but the persisted checkpoint bypasses it;
    re-emitting / resuming such a checkpoint applies the subagent output to the
    unregistered slot and raises fc.ContractError. Pure + deterministic — no wall
    clock, no network."""
    if not checkpoint:
        return True
    writes = checkpoint.get("pending", {}).get("writes")
    if writes is None:
        return True
    return writes in ctx.registered_slots()


def _emit_pause_from_checkpoint(state_path, agentstates, tick_id, mode):
    """Build the PAUSED result by RE-LOADING the just-written checkpoint and
    rendering from its restored slot snapshot, then DELETE any pre-existing file
    at each dispatch's output_path.

    Rendering from the durable checkpoint (rather than the live ctx) makes the
    first emission and a crash-safety re-emit BYTE-IDENTICAL: both go through the
    same save/load round-trip, so the rendered prompt (and output_path) are
    independent of the live in-process slot key order (DurableState.save sorts
    keys). The checkpoint is the SOLE source of truth for the paused dispatch.

    Deleting any stale output file at the output_path is part of the PAUSE: a
    stale prior-tick file must never be misread on resume — a missing fresh write
    must surface as invalid_output, never a stale read."""
    checkpoint = persisted_tick_checkpoint(state_path)
    name = checkpoint["pending"]["state"]
    agentstate = agentstates[name]
    slots = checkpoint["slots"]
    output_dir = checkpoint["output_dir"]
    slot_values = {slot: slots[slot] for slot in agentstate.manifest.reads}
    # §3.5.3: the checkpoint snapshot keeps the FULL work_items; filter the render
    # from the durable triage memory so the re-emit matches the original filtered
    # dispatch byte-for-byte (memory is stable within a tick).
    triage_memory = persisted_triage_memory(state_path)
    paused = _pause_result(name, agentstate, slot_values, tick_id, mode,
                           output_dir, triage_memory=triage_memory)
    for d in paused["dispatches"]:
        # Stale-file safety: remove any pre-existing output file so a missing
        # fresh write cannot be misread as a valid (stale) output on resume.
        if os.path.isfile(d["output_path"]):
            os.remove(d["output_path"])
    return paused


def _drive_agent_tick(route, states, ctx, state_path,
                      current, mode, tick_id, agentstates, path, signals,
                      output_dir, gov, budget_clock, events=None,
                      pr_state_source=None):
    """Walk the route from `current` SCRIPT-state-by-SCRIPT-state, pausing at the
    first AGENT state with a durable checkpoint. SCRIPT states run inline exactly
    as tick_orchestrator.run does (impl(ctx) + fc.apply_result + resolve_next).

    `path`/`signals` are the accumulators (already containing the segments walked
    in prior invocations); they are extended in place. `output_dir` is the
    directory each agent-state dispatch's output_path is computed under. Returns
    (PAUSED dict, None) when it pauses at an agent-state, or (None, RunResult) when
    it reaches the terminal with no agent-state ahead.

    `events`, when given, is the structured-event emitter: a `state_run`+`signal`
    pair is emitted inline as each SCRIPT state runs, and a `pause`+`dispatch`
    pair when the driver pauses at an agent-state (the dispatch carries the
    subagent_type + writes in its detail). Event emission is purely additive — it
    never changes the walk, the signals, or the checkpoint.
    """
    terminal = set(route["terminal"])

    while current not in terminal:
        second = states[current][1]
        if current in agentstates:
            agentstate = agentstates[current]
            slot_values = {slot: ctx.read(slot)
                           for slot in agentstate.manifest.reads}
            # Skip-unchanged re-triage (§3.5.3): when this agent-state reads the
            # work_items slot (i.e. TRIAGE), the work_items fed to the subagent are
            # FILTERED (done/deferred-AND-unchanged items dropped) at RENDER time —
            # see `triage_memory` threaded into _pause_result / _write_checkpoint
            # below, which filters the dispatch input WITHOUT polluting the
            # checkpoint snapshot, so the persisted work_items read product stays
            # the full PULL set. Here we only record the judged/pulled counts
            # (LAST_TRIAGED_KEY) for the terminal trace, which may run in a later
            # invocation. Empty memory filters nothing (byte-identical).
            if wi.WORK_ITEMS_SLOT["name"] in slot_values:
                pulled = slot_values[wi.WORK_ITEMS_SLOT["name"]]
                judged = _filter_triage_work_items(
                    pulled, persisted_triage_memory(state_path))
                _record_last_triaged(state_path, len(judged), len(pulled))
            # Trust-gate an ACTING agent-state (DESIGN §2.3 / §3.8.2 trust
            # ladder): a dispatch entry with a truthy `effect` performs outward
            # effects, so it is dispatched ONLY when the trust mode permits that
            # effect. The decision is the deterministic lib's (sg.permits),
            # never the model's. When NOT permitted (e.g. dry-run, where permits
            # is False for every effect) the state does NOT pause/dispatch — it
            # synthesizes one INERT `planned` handoff per dispatch item and
            # CONTINUES the driver (no checkpoint, no spend, no subagent).
            effect = agentstate.entry["dispatch"][0].get("effect")
            if effect and not sg.permits(effect, mode):
                writes, slot_value, signal = _gate_acting_state(
                    current, agentstate, slot_values, tick_id, mode,
                    output_dir)
                ctx.write(writes, slot_value)
                signals.append(signal)
                if events is not None:
                    events.emit("state_run", state=current,
                                detail={"gated": mode})
                    events.emit("signal", state=current, signal=signal)
                current = to.resolve_next(route, current, signal)
                path.append(current)
                continue
            # Doer governance for a PERMITTED ACTING agent-state (effect present
            # and sg.permits True). ALL of this is acting-state-only — a
            # non-acting agent-state (no effect) skips straight to the PAUSE
            # below, unchanged.
            if effect:
                # 1. Budget pre-gate: evaluate the budget window BEFORE pausing.
                #    If the per-day window is exhausted (allowed False) do NOT
                #    pause/dispatch — synthesize one DEFERRED `blocked` handoff
                #    per not-yet-acted item, compute the signal, CONTINUE. NO
                #    spend, NO dispatch; the items stay un-acted (not recorded in
                #    the acted-ledger) so they retry on a later window.
                budget = sg.evaluate_budget(
                    gov, persisted_budget_state(state_path), budget_clock)
                if not budget.get("allowed", True):
                    reason = (f"budget exhausted "
                              f"({budget.get('reason', 'per_day_exhausted')})")
                    envelopes = ad.build_envelopes(
                        agentstate.entry, slot_values,
                        {"tick_id": tick_id, "mode": mode}, state=current,
                        output_dir=output_dir)
                    outputs = [_budget_blocked_handoff(env.get("item"), reason)
                               for env in envelopes]
                    writes, slot_value, signal = _synthesize_acting_result(
                        agentstate, outputs)
                    ctx.write(writes, slot_value)
                    signals.append(signal)
                    if events is not None:
                        events.emit("state_run", state=current,
                                    detail={"budget_blocked": reason})
                        events.emit("signal", state=current, signal=signal)
                    current = to.resolve_next(route, current, signal)
                    path.append(current)
                    continue
                # 2. Acted-ledger idempotency + backoff defer-skip: drop from the
                #    per_item dispatch set any work_order already acted (in the
                #    ledger — never re-dispatch / no second PR) OR whose SOURCE
                #    work_item is deferred-AND-unchanged in the backoff ledger
                #    (§3.8.5 — no thrash). A deferred item whose issue updated_at
                #    advanced re-enters and its backoff entry is RESET (fresh K
                #    attempts). An already-`opened` work order whose auto-PR a human
                #    CLOSED (not merged) WHILE the issue advanced ALSO re-enters
                #    (§3.8.5-symmetric leak fix, #204): its acted-ledger entry is
                #    CLEARED so it is re-dispatched. If NO items remain, do NOT
                #    pause — synthesize an inert (empty) result, compute the signal,
                #    CONTINUE.
                ledger = persisted_acted_ledger(state_path)
                backoff = persisted_backoff_ledger(state_path)
                wo_to_wi = _work_order_to_work_item(
                    _read_slot_or(ctx, wi.WORK_ORDERS_SLOT["name"], []))
                wi_updated_at = _work_items_updated_at(
                    _read_slot_or(ctx, wi.WORK_ITEMS_SLOT["name"], []))
                # Acted-ledger re-entry (#204): query the PR ONLY for `opened`
                # entries whose issue updated_at advanced (bounds the gh calls to
                # changed issues); a closed-and-not-merged PR re-enters.
                reenter_wo_ids = _acted_reentry_wo_ids(
                    _agentstate_collection(agentstate, slot_values),
                    ledger, wo_to_wi, wi_updated_at, pr_state_source)
                slot_values, remaining, dropped, reenter = \
                    _per_item_filtered_values(
                        agentstate, slot_values, ledger, backoff=backoff,
                        wo_to_wi=wo_to_wi, wi_updated_at=wi_updated_at,
                        reenter_wo_ids=reenter_wo_ids)
                # Re-entered (issue advanced) items get a fresh K attempts; a
                # re-entered acted work order has its acted-ledger entry cleared so
                # the re-dispatch can open + re-record a fresh PR.
                _reset_backoff_entries(state_path, reenter)
                _clear_acted_entries(state_path, reenter_wo_ids)
                if dropped and not remaining:
                    writes, slot_value, signal = _synthesize_acting_result(
                        agentstate, [])
                    ctx.write(writes, slot_value)
                    signals.append(signal)
                    if events is not None:
                        events.emit("state_run", state=current,
                                    detail={"all_acted": True})
                        events.emit("signal", state=current, signal=signal)
                    current = to.resolve_next(route, current, signal)
                    path.append(current)
                    continue
            # Checkpoint to durable state — the SOLE crash-safety source of
            # truth for the paused dispatch. The pause is deliberately NOT
            # journaled: the tick journal is durable-state's counter-
            # reconciliation ledger (drain_run reads `target_counter` from every
            # unconfirmed intent), an agent dispatch never touches the counter,
            # and a counter-less agent-dispatch intent would poison the NEXT
            # tick's DRAIN with KeyError 'target_counter' (#109).
            _write_checkpoint(state_path, current, ctx, path, signals,
                              agentstate, output_dir, slot_values, tick_id, mode)
            # Render the PAUSE from the just-written checkpoint so a fresh PAUSE
            # and a crash-safety re-emit are byte-identical (both round-trip the
            # slots through the durable store).
            paused = _emit_pause_from_checkpoint(
                state_path, agentstates, tick_id, mode)
            if events is not None:
                events.emit("pause", state=current)
                for d in paused["dispatches"]:
                    events.emit("dispatch", state=current, detail={
                        "subagent_type": d["subagent_type"],
                        "writes": d["writes"]})
            return paused, None
        manifest = states[current][0]
        result = second(ctx)
        fc.apply_result(ctx, manifest, result, _VOCAB)
        signals.append(result.signal)
        if events is not None:
            events.emit("state_run", state=current)
            events.emit("signal", state=current, signal=result.signal)
        current = to.resolve_next(route, current, result.signal)
        path.append(current)

    return None, to.RunResult(final_state=current, path=path, signals=signals)


def _resume_agent_state(route, states, ctx, checkpoint, agentstates):
    """READ each pending dispatch's subagent-WRITTEN output FILE, validate it,
    then apply the collected slot value and return the next SCRIPT-walk position
    (DESIGN §3.4.6 file-based context isolation).

    Each pending dispatch carries its `output_path` (the file the subagent wrote)
    and its validation `schema`. A MISSING output file returns
    ({"status": "invalid_output", reason naming the missing path}, None, None) so
    the caller re-emits a re-dispatchable PAUSE without clearing the checkpoint —
    a missing write surfaces as invalid_output, never a stale read or a crash. An
    invalid file content returns the same shape.

    Returns (None, next_state, signal) on success."""
    name = checkpoint["pending"]["state"]
    pending = checkpoint["pending"]
    writes = pending["writes"]
    dispatch_entry = agentstates[name].entry["dispatch"][0]

    validated = []
    for d in pending["dispatches"]:
        output_path = d["output_path"]
        if not os.path.isfile(output_path):
            return ({"status": "invalid_output", "state": name,
                     "reason": f"missing output file: {output_path}"},
                    None, None)
        with open(output_path) as f:
            content = f.read()
        ok, parsed = ad.validate_output(content, d["schema"])
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
                    resume, mode, output_dir, gov, budget_clock, tick_id,
                    events=None, route_src=None, escalate_sink=None,
                    escalate_now=None, pr_state_source=None):
    """Drive a tick over a route that contains agent-states (DESIGN §2.8, §3.4.6).

    Three cases, all keyed off the durable checkpoint (the source of truth):

      - RESUME (resume=True): load the checkpoint, restore the slots, READ the
        subagent-WRITTEN output FILES at the checkpoint's output_paths, validate +
        apply them, and continue driving from the successor state. A missing or
        invalid output file returns an "invalid_output" dict (checkpoint left
        intact, re-dispatchable).
      - CRASH-SAFETY RE-EMIT (no resume, checkpoint present): restore the slots and
        re-emit the SAME PAUSED dispatch (byte-identical output_path) from the
        checkpoint.
      - FRESH (no resume, no checkpoint): seed a fresh context and drive from GUARD
        until the first agent-state PAUSE or the terminal.

    `output_dir` is the directory each agent-state dispatch's output_path is
    computed under. Returns a 3-tuple: (early_dict, result, ctx). `early_dict` is
    the PAUSED / invalid_output dict to return directly (then result/ctx are None);
    when the driver reaches the terminal `early_dict` is None and (result, ctx)
    carry the RunResult + the final TickContext for read-product persistence.
    """
    checkpoint = persisted_tick_checkpoint(state_path)
    # `tick_id` is the per-tick id (#112) assigned ONCE by run_tick at fresh start
    # and carried through the checkpoint (read back on resume), so it is distinct
    # across ticks AND stable across a single tick's `--step` -> `--resume`. It is
    # NEVER the wall clock and never the work `counter` (which a read-only route
    # never bumps). It also seeds the rendered dispatch's {tick_id} slot value.

    # Stale/incompatible-checkpoint discard-and-fresh (upgrade-in-flight crash
    # guard). A checkpoint paused at an agent-state under an OLDER wiring carries a
    # `pending.writes` slot the CURRENT (migrated, seeded) wiring no longer
    # registers (e.g. a v0.7.0 REVIEW checkpoint writing the retired
    # `review_verdicts` slot). The adapter-map migration self-heals the live config
    # but the persisted checkpoint bypasses it: re-emitting it (--step) or applying
    # its output to the unregistered slot (--resume) raises fc.ContractError. So
    # BEFORE the resume / crash-safety-re-emit branches: seed a fresh ctx (the same
    # seed used downstream) and, when the checkpoint is present but NOT compatible,
    # _clear_checkpoint, record the discard, and FORCE the fresh path (resume=False
    # semantics) so the tick re-walks from GUARD and re-pauses with the CURRENT
    # writes slot. The fresh re-walk relies on the existing acted-ledger / re-entry
    # idempotency, so it never double-acts an already-open PR.
    if checkpoint and not _checkpoint_compatible(checkpoint, ctx_seed()):
        stale_pending = checkpoint.get("pending", {})
        _clear_checkpoint(state_path)
        if events is not None:
            events.emit("tick_start", detail={
                "source": route_src, "mode": mode,
                "checkpoint_discarded": {
                    "state": stale_pending.get("state"),
                    "writes": stale_pending.get("writes"),
                }})
        ctx = ctx_seed()
        return _drive_agent_tick(
            route, states, ctx, state_path, "GUARD", mode, tick_id,
            agentstates, ["GUARD"], [], output_dir, gov, budget_clock,
            events=events, pr_state_source=pr_state_source) + (ctx,)

    if resume:
        # RESUME: the checkpoint must exist (a resume without a prior PAUSE is a
        # caller error). Restore the blackboard, then READ + validate + apply the
        # subagent-written output files.
        ctx = ctx_seed()
        _restore_slots(ctx, checkpoint["slots"])
        resumed_name = checkpoint["pending"]["state"]
        early, next_state, _signal = _resume_agent_state(
            route, states, ctx, checkpoint, agentstates)
        if early is not None:
            return early, None, None
        if events is not None:
            events.emit("resume", state=resumed_name)
        # Doer governance on resume of an ACTING agent-state (effect present):
        # RECORD each newly-acted handoff into the durable acted-ledger
        # ({work_order_id: {outcome, ref}}) so a later tick that re-pulls the
        # SAME work order skips it (idempotency, §3.2.4). Spend metering is folded
        # into the budget window by run_tick's terminal persist (it knows the
        # `spent`), so this branch records ONLY the ledger.
        resumed_effect = (agentstates[resumed_name].entry["dispatch"][0]
                          .get("effect"))
        if resumed_effect:
            handoffs = ctx.read(checkpoint["pending"]["writes"])
            if isinstance(handoffs, list):
                # Map each handoff's work_order_id -> work_item_id via the
                # dispatched work_orders + each work_item's current updated_at;
                # the acted-ledger records the act-time updated_at (the §3.8.5-
                # symmetric re-entry pin, #204) and the backoff ledger keys on it.
                wo_to_wi = _work_order_to_work_item(
                    _read_slot_or(ctx, wi.WORK_ORDERS_SLOT["name"], []))
                wi_updated_at = _work_items_updated_at(
                    _read_slot_or(ctx, wi.WORK_ITEMS_SLOT["name"], []))
                _record_acted_ledger(state_path, handoffs, wo_to_wi,
                                     wi_updated_at)
                # Backoff governance (§3.8.5): count blocked outcomes / clear
                # successes on the work_item_id-keyed backoff ledger; at the
                # configured threshold defer + escalate once.
                _record_backoff_outcomes(
                    state_path, handoffs, wo_to_wi, wi_updated_at,
                    escalate_sink, escalate_now, _backoff_threshold(gov))
                # Triage memory (§3.5.3): upsert done (opened/closed) / deferred
                # (just-deferred) status keyed on work_item_id, so the next tick's
                # TRIAGE skips re-judging handled-and-unchanged issues. Recorded
                # AFTER the backoff ledger so just-deferred items are visible.
                _record_triage_memory(
                    state_path, handoffs, wo_to_wi, wi_updated_at)
        path = list(checkpoint["path"]) + [next_state]
        signals = list(checkpoint["signals"])
        return _drive_agent_tick(
            route, states, ctx, state_path, next_state, mode,
            tick_id, agentstates, path, signals, output_dir,
            gov, budget_clock, events=events,
            pr_state_source=pr_state_source) + (ctx,)

    if checkpoint:
        # CRASH-SAFETY RE-EMIT: re-issue the SAME PAUSED dispatch idempotently
        # from the durable checkpoint (the source of truth). A crash-safety
        # re-emit is NOT a fresh tick — no tick_start; the pause/dispatch were
        # already logged on the original PAUSE.
        return _emit_pause_from_checkpoint(
            state_path, agentstates, tick_id, mode), None, None

    # FRESH: drive from GUARD. Log tick_start (route source + mode) before the
    # walk.
    if events is not None:
        events.emit("tick_start", detail={"source": route_src, "mode": mode})
    ctx = ctx_seed()
    return _drive_agent_tick(
        route, states, ctx, state_path, "GUARD", mode, tick_id,
        agentstates, ["GUARD"], [], output_dir, gov, budget_clock,
        events=events, pr_state_source=pr_state_source) + (ctx,)


def run_tick(runtime_dir=None, state_path=None, journal_path=None,
             project_dir=None, source=None, now=None, tick_spend=0,
             return_run_result=False, resume=False, spent=0,
             report_sink=None, discoveries=None, escalate_sink=None,
             pr_state_source=None):
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

    Agent yield/resume seam (DESIGN §2.8, §3.4.6 file-based context isolation):
    when the resolved route contains >=1 agent-state, the tick PAUSES at each
    agent-state and returns a PAUSED dict (the dispatch request, each carrying an
    output_path under ${runtime_dir}/dispatch-out/) after durably checkpointing;
    any stale file at an output_path is deleted at pause. The executor performs the
    Agent dispatch; the subagent WRITES its JSON to output_path. The executor then
    calls run_tick(resume=True), which READS those output files, validates + applies
    them, and continues to the next pause or the terminal. A missing output file on
    resume returns an invalid_output dict (checkpoint intact). A pure-script route
    is UNCHANGED — it runs via tick_orchestrator.run and returns the signal string.

    Outbound REPORT flush (out-of-band — §3.11): at the terminal (the `done`
    path, on BOTH the pure-script and agent-driver paths), after the read products
    are persisted, run_tick flushes the tick's discoveries
    (handoffs[].discovered_work + the optional `discoveries` param) through
    work-intake's REPORT port, trust-gated on sg.permits('file', mode) and
    journal-idempotent via the durable REPORT_LEDGER_KEY. `report_sink` is the
    injectable filing sink (default DEFAULT_REPORT_SINK = work-intake's live `gh`
    sink); tests inject a stub so no network. The reported=<filed>/<skipped> token
    is appended to the trace and the tick_end event detail.

    Acted-ledger re-entry (§3.8.5-symmetric leak fix, #204): `pr_state_source` is
    the injectable PR-state source (default DEFAULT_PR_STATE_SOURCE =
    gh_pr_state_source, shelling `gh pr view`). For an already-`opened` work order
    whose SOURCE issue updated_at ADVANCED past the act-time pin, run_tick queries
    the recorded PR `ref`; a CLOSED-AND-NOT-MERGED PR makes the work order
    RE-ENTER the dispatch set (its acted-ledger entry cleared, the item
    re-dispatched). Merged / still-open / closed-but-issue-unchanged stay locked.
    Tests inject a stub so no network; the query is bounded to changed issues.

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

    # The directory each agent-state dispatch's output file is written under
    # (DESIGN §3.4.6 file-based context isolation). Created up-front so the
    # subagent's file-writing tool has a directory to write into; a pure-script
    # tick never uses it, but creating it is cheap + harmless.
    output_dir = os.path.abspath(os.path.join(runtime_dir, "dispatch-out"))
    os.makedirs(output_dir, exist_ok=True)

    # Load the central config once per tick (project-local config.json, else the
    # legacy governance.json migrated once, else the documented defaults).
    # Threaded into the runtime dict so acting adapters can consult permits/budget;
    # run_tick also reads backoff.threshold (default 5) from it for the backoff
    # gate. load_governance is a thin alias of load_config.
    gov = sg.load_config(project_dir)

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
    # it over. `is_resume` is true when the executor resumes from written outputs
    # OR when a prior PAUSE left a durable checkpoint (crash-safety re-emit).
    persisted_checkpoint = persisted_tick_checkpoint(state_path)
    is_resume = resume or bool(persisted_checkpoint)

    # The per-tick id (#112): assigned ONCE at FRESH tick start (incrementing the
    # durable per-tick counter — distinct across ticks, even on a read-only route
    # that never bumps the work `counter`) and read BACK from the durable
    # checkpoint on resume / crash-safety re-emit, so a single tick's `--step` ->
    # `--resume` (separate processes, no injected `now`) shares ONE id. It is
    # computed before the route runs so both the event emitter and the rendered
    # agent-dispatch {tick_id} slot use the same value.
    tick_id = _assign_tick_id(state_path, resume, persisted_checkpoint)

    if is_resume:
        # Reuse the persisted budget window (no fresh-start gate). Carry the
        # evaluated/rolled window forward (#123): the fresh tick's window was
        # persisted on the PAUSE early-return path, but a resume must always
        # surface a real {window_key, spent_tokens} even if the persisted value
        # is {} — so assign budget["budget_state"] back rather than re-rolling.
        prior_budget_state = persisted_budget_state(state_path)
        budget_clock = _clock_for_window(prior_budget_state.get("window_key"))
        budget = sg.evaluate_budget(gov, prior_budget_state, budget_clock)
        new_budget_state = budget["budget_state"]
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

    # Route-as-data: load (override else default) -> migrate -> resolve ->
    # validate. A bad override raises WiringError here, before any tick body runs.
    # The migrate hook self-heals stale KNOWN-port agent entries (e.g. a v0.6.0
    # REVIEW writing the retired review_verdicts slot) against the live templates
    # BEFORE resolve+validate. adapter_map_config imports run_tick, so it is
    # imported LAZILY here to avoid a module-level circular import.
    import adapter_map_config  # noqa: E402 - lazy: avoids circular import
    route, states = aw.build_loop(
        DEFAULT_ROUTE, DEFAULT_ADAPTER_MAP, runtime,
        start="GUARD", initial=_INITIAL_SLOTS,
        migrate=adapter_map_config.migrate_known_port_entries)

    # The agent-states resolved in this route ({name: AgentState}). When empty,
    # the route is pure-script and runs the UNCHANGED tick_orchestrator.run path.
    agentstates = {name: second for name, (m, second) in states.items()
                   if isinstance(second, aw.AgentState)}

    # Whether ANY agent-state in this route reads the work_items slot (i.e. an
    # agent TRIAGE) — only then does the skip-unchanged filter run and the durable
    # LAST_TRIAGED_KEY carry this tick's judged/pulled. When no work_items
    # agent-state exists, the terminal trace shows triaged=<n>/<n> (n = full PULL),
    # never a stale cross-tick last_triaged value (§3.5.3).
    has_triage_agent = any(
        wi.WORK_ITEMS_SLOT["name"] in st.manifest.reads
        for st in agentstates.values())

    # Whether the route has an ACTING agent-state the CURRENT mode actually
    # PERMITS to act (a dispatch entry carrying a truthy `effect`, e.g. an
    # IMPLEMENT agent that opens PRs, AND `sg.permits(effect, mode)` True) — the
    # gate for the §3.3.3 immediate-refire in make_exit's EXIT wrapper, threaded
    # onto the SAME runtime dict the EXIT factory closed over at build time so EXIT
    # reads it at run time. The permits check keeps the back-compat IDLE cases
    # idling: the read-and-idle spine has no acting agent at all, and the dry-run
    # inert IMPLEMENT can act on NOTHING (`permits` is False for every effect at
    # dry-run), so the refire override never fires for either — only a route that
    # can genuinely make progress this mode refires on a workable pool.
    runtime["has_acting_agent"] = any(
        sg.permits(st.entry["dispatch"][0].get("effect"), gov.get("mode", ""))
        for st in agentstates.values()
        if st.entry["dispatch"][0].get("effect"))

    # Spend metering on ALL agent-state resumes (acting OR non-acting): fold the
    # metered `spent` into the budget window (sg.record_spend) so the terminal
    # persist records it. The budget is a token ceiling over ALL model spend in
    # the loop (DESIGN §3.8.4), so a non-acting TRIAGE resume's spend is metered
    # too — otherwise the budget would undercount real loop model usage. Default
    # spent 0 (back-compatible). This is distinct from the acting-only budget
    # PRE-GATE and the acting-only acted-ledger record, which are unchanged.
    if resume and spent and persisted_checkpoint:
        new_budget_state = sg.record_spend(
            new_budget_state, budget_clock, spent)

    # The structured event log (observability §3.9.1), opened at
    # ${runtime_dir}/events.jsonl. The event `ts` reuses the tz-aware budget
    # clock (the injected `now`), so the log is deterministic; the tick_id is the
    # per-tick id (#112) assigned above — distinct per tick AND stable across a
    # single tick's `--step` -> `--resume`, so every event of one logical tick
    # carries the SAME id (it is NOT the work `counter`, which a read-only route
    # never bumps, so it no longer collapses to 0). Event emission is purely
    # additive — it writes ALONGSIDE the tick, never altering the
    # walk/signals/disposition/persistence/trace.
    event_now = _budget_clock(now)
    events = _EventEmitter(runtime_dir, event_now, tick_id)
    mode = gov.get("mode", "")
    route_src = route_source_label(project_dir)

    if agentstates:
        agent_outcome = _run_agent_tick(
            route, states, agentstates, ctx_seed=lambda: _seed_context(
                state_path, journal_path, route),
            state_path=state_path,
            resume=resume, mode=mode, output_dir=output_dir,
            gov=gov, budget_clock=budget_clock, tick_id=tick_id, events=events,
            route_src=route_src,
            escalate_sink=escalate_sink or DEFAULT_ESCALATE_SINK,
            escalate_now=event_now,
            pr_state_source=pr_state_source or DEFAULT_PR_STATE_SOURCE)
        if agent_outcome[0] is not None:
            # PAUSED or invalid_output: return the structured dict directly. The
            # executor re-invokes run_tick(resume=True) to continue after the
            # subagent has written its output file.
            #
            # Persist the durable budget window BEFORE returning (#123). An agent
            # route pauses at the first agent-state and returns here, BEFORE the
            # terminal budget-persist block below — so without this the fresh
            # tick's rolled window would never be saved (durable budget={},
            # /status win= empty). Load-modify-save ONLY BUDGET_KEY so the
            # checkpoint + read products + every other durable key are preserved;
            # the budget is a durable cross-tick fact on EVERY route.
            doc = ds.DurableState(state_path).load()
            doc[BUDGET_KEY] = new_budget_state
            ds.DurableState(state_path).save(doc)
            return agent_outcome[0]
        result = agent_outcome[1]
        ctx = agent_outcome[2]
        # The driver reached the terminal — clear the PAUSED checkpoint.
        _clear_checkpoint(state_path)
    else:
        ctx = _seed_context(state_path, journal_path, route)
        result = to.run(route, states, ctx, _VOCAB, start="GUARD")
        # Pure-script path: derive the per-state events from the RunResult after
        # the run (one state_run/signal per visited non-terminal state, in order).
        # result.path ends at the terminal (DONE/HALTED), so path[:-1] are the
        # visited non-terminal states and signals[i] is path[i]'s emitted signal.
        events.emit("tick_start", detail={"source": route_src, "mode": mode})
        for visited, sig in zip(result.path[:-1], result.signals):
            events.emit("state_run", state=visited)
            events.emit("signal", state=visited, signal=sig)

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
        tick_handoffs = (
            ctx.read(im.HANDOFFS_SLOT["name"])
            if "IMPLEMENT" in route["states"] else [])
        doc[HANDOFFS_KEY] = tick_handoffs
        # The verify-integrate close-the-loop read products (#64): VERIFY writes
        # verdicts, INTEGRATE writes integration_result. A route omitting the
        # producing stage persists that product empty (NOT a stale carry-forward),
        # symmetric with the four products above.
        doc[VERDICTS_KEY] = (
            ctx.read(vi.VERDICTS_SLOT["name"])
            if "VERIFY" in route["states"] else [])
        # review_findings (FT-C): the ADVISORY REVIEW state's per-tick read
        # product. Persisted when REVIEW is routed (the slot is seeded only then),
        # else empty (NOT a stale carry-forward). INTEGRATE no longer consumes it.
        doc[REVIEW_FINDINGS_KEY] = (
            ctx.read(vi.REVIEW_FINDINGS_SLOT["name"])
            if "REVIEW" in route["states"] else [])
        doc[INTEGRATION_RESULT_KEY] = (
            ctx.read(vi.INTEGRATION_RESULT_SLOT["name"])
            if "INTEGRATE" in route["states"] else {})
        # The durable, cross-tick budget window (NOT a #64 read product): persist
        # the evaluated/recorded budget_state so the window rollover + spend
        # accrual survive across ticks.
        doc[BUDGET_KEY] = new_budget_state
        ds.DurableState(state_path).save(doc)
        # Outbound REPORT flush (out-of-band — §3.11): file the tick's
        # discoveries (handoffs[].discovered_work + the optional `discoveries`
        # slot + REVIEW's advisory review_findings, FT-C) at the terminal, AFTER
        # the read products are persisted, on BOTH the pure-script and agent-driver
        # done paths. Trust-gated + journaled idempotent inside _flush_report. The
        # review_findings are already DiscoveredIssue-conforming records with a
        # stable dedup_key, so they file via the SAME file_discoveries path
        # (known_dedup_keys + known_open) — NOT a parallel mechanism. The tick's
        # PULLed open work_items are passed as known_open so a discovery (handoff
        # OR review finding) duplicating an already-open issue is skipped, not
        # filed as duplicate noise (dedup-vs-open, DESIGN §3.5.4).
        extra_discoveries = list(discoveries or []) + list(
            doc.get(REVIEW_FINDINGS_KEY, []))
        reported_filed, reported_skipped, reported_errored = _flush_report(
            state_path, tick_handoffs, extra_discoveries, mode, gov,
            report_sink or DEFAULT_REPORT_SINK,
            work_items=doc.get(WORK_ITEMS_KEY, []))
    else:
        # GUARD short-circuited (STOPPED/ABORTED/RESTART_NEEDED): the tick did no
        # read/PERSIST work, but the budget window is still a durable cross-tick
        # fact — persist its rollover/state so it stays current even on a halt.
        signal = "halt"
        doc = ds.DurableState(state_path).load()
        doc[BUDGET_KEY] = new_budget_state
        ds.DurableState(state_path).save(doc)
        # No route body ran on a halt: nothing to report.
        reported_filed, reported_skipped, reported_errored = 0, 0, 0

    disposition = ld.read_disposition(runtime_dir)
    work_items_count = persisted_work_items_count(state_path)
    work_orders_count = persisted_work_orders_count(state_path)
    execution_plan_count = persisted_execution_plan_count(state_path)
    handoffs_count = persisted_handoffs_count(state_path)
    # Governance surface (#69 style — always shown): the trust mode + a compact
    # budget field, plus a budget_paused indicator when the budget is exhausted.
    # Placed after the existing fields; all current fields/order are preserved.
    # `route_src` (#59) was resolved above from the SAME project_dir build_loop
    # loaded the route from, and reused for both the trace and the event log.
    gov_fields = governance_fields(gov, new_budget_state, budget)
    # The outbound REPORT surface (#69 style — always shown): reported=<filed>/
    # <skipped>. Placed after the governance fields; all current fields/order are
    # preserved. A no-discovery tick shows reported=0/0. When any discovery FAILED
    # to file (errored>0), append report_errors=<n> so a filing failure is VISIBLE
    # rather than masquerading as reported=0/0 "no discoveries".
    reported_field = f"reported={reported_filed}/{reported_skipped}"
    if reported_errored > 0:
        reported_field += f" report_errors={reported_errored}"
    # The skip-unchanged re-triage surface (§3.5.3 — always shown): triaged=
    # <judged>/<pulled>. When a work_items agent-state (TRIAGE) ran this tick, the
    # filter persisted this tick's judged/pulled under LAST_TRIAGED_KEY; read it.
    # Otherwise nothing was filtered, so judged == pulled == the full PULL count
    # (never a stale cross-tick value). Placed after the reported field; all
    # current fields/order are preserved.
    if has_triage_agent:
        last_triaged = ds.DurableState(state_path).load().get(
            LAST_TRIAGED_KEY, {})
        triaged_judged = last_triaged.get("judged", work_items_count)
        triaged_pulled = last_triaged.get("pulled", work_items_count)
    else:
        triaged_judged = triaged_pulled = work_items_count
    triaged_field = f"triaged={triaged_judged}/{triaged_pulled}"
    # The INTEGRATE surface (#69 style — always shown): merged=<n>. From the #64
    # integration_result read product ({merged: [{pr_ref, url}], skipped:
    # [{pr_ref, reason}], errors: [{pr_ref, reason}]}), surface the merge counts +
    # the merged pr_refs so a working merge is VISIBLE (the dogfood "invisible
    # merge" fix). A route with no INTEGRATE persists integration_result empty, so
    # merged=0 / merged_refs=[]. The trace shows merged=<n>; integrate_errored=<n>
    # is appended ONLY when >0 (a merge-sink failure must be visible).
    integration_result = ds.DurableState(state_path).load().get(
        INTEGRATION_RESULT_KEY, {}) or {}
    merged_entries = integration_result.get("merged", [])
    merged_refs = [e.get("pr_ref") for e in merged_entries]
    merged_count = len(merged_entries)
    integrate_skipped = len(integration_result.get("skipped", []))
    integrate_errored = len(integration_result.get("errors", []))
    merged_field = f"merged={merged_count}"
    if integrate_errored > 0:
        merged_field += f" integrate_errored={integrate_errored}"
    # The refire decision (legible idle-vs-refire): the EXIT signal already carries
    # it; surface a boolean in the tick_end detail disambiguating
    # idle-because-no-work (False) from refire-because-work-remains (True).
    refire = signal == "refire"
    # Persist the last-tick REPORT outcome so status.py can surface it without
    # re-running (load-modify-save just LAST_REPORTED_KEY, preserving all else).
    doc = ds.DurableState(state_path).load()
    doc[LAST_REPORTED_KEY] = {"filed": reported_filed,
                              "skipped": reported_skipped,
                              "errored": reported_errored}
    ds.DurableState(state_path).save(doc)
    sys.stdout.write(
        f"[tick] path={'->'.join(result.path)} work_items={work_items_count} "
        f"work_orders={work_orders_count} "
        f"execution_plan={execution_plan_count} handoffs={handoffs_count} "
        f"disposition={disposition} "
        f"signal={signal} route={route_src} {gov_fields} {reported_field} "
        f"{triaged_field} {merged_field}\n")

    # Terminal events (observability §3.9.1): the resulting disposition, then the
    # tick_end carrying the final signal + the four read-product counts, the REPORT
    # counts, the INTEGRATE merge results, and the refire decision. Emitted
    # ALONGSIDE the trace at the terminal of BOTH the pure-script and the
    # agent-driver done paths (the agent PAUSE path returned early, above).
    events.emit("disposition", signal=signal, detail={"disposition": disposition})
    events.emit("tick_end", signal=signal, detail={
        "work_items": work_items_count,
        "work_orders": work_orders_count,
        "execution_plan": execution_plan_count,
        "handoffs": handoffs_count,
        "reported_filed": reported_filed,
        "reported_skipped": reported_skipped,
        "reported_errored": reported_errored,
        "merged": merged_count,
        "integrate_skipped": integrate_skipped,
        "integrate_errored": integrate_errored,
        "merged_refs": merged_refs,
        "refire": refire})

    if return_run_result:
        return result
    return signal


# --------------------------------------------------------------------------
# JSON tick CLI (the --step / --resume executor seam).
#
# A THIN deterministic wrapper around the EXISTING run_tick(...) structured
# returns (the yield/resume seam above) — it adds NO new tick logic. The executor
# skill drives the yield/resume loop by calling --step, relaying the rendered
# prompt to the subagent (which WRITES its JSON to the dispatch's output_path),
# then calling --resume (NO file argument) — run_tick reads those output files.
# stdout is PURE JSON in those modes (the skill parses stdout); the human trace
# run_tick writes is captured into the JSON `trace` field, never leaked raw. Bare
# invocation (no --step/--resume) is UNCHANGED: it prints the human trace, so
# pure-script bash callers keep working.
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
    --resume (NO file argument): read the paused agent-state's subagent-WRITTEN
    output FILES at the checkpoint's output_paths via run_tick(resume=True), print
    the same envelope shape.

    Exit codes: done/paused -> 0; invalid_output (bad agent output OR a missing
    output file) -> 1.
    """
    parser = argparse.ArgumentParser(
        description="Run one maintainer tick (JSON --step/--resume seam).")
    parser.add_argument(
        "--step", action="store_true",
        help="run to the next pause/terminal and print a JSON envelope")
    parser.add_argument(
        "--resume", action="store_true",
        help="resume a paused tick by reading the subagent-written output files "
             "at the checkpoint's output_paths; print a JSON envelope")
    parser.add_argument(
        "--spent", type=int, default=0,
        help="tokens spent by the resumed dispatch, metered into the durable "
             "budget window (only on --resume of an acting agent-state; "
             "default 0)")
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

    # Capture run_tick's one-line human trace so it does NOT pollute stdout (the
    # skill parses stdout as pure JSON); fold it into the envelope `trace` field.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = run_tick(resume=args.resume, spent=args.spent, **paths)
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
