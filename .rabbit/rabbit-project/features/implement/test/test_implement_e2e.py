#!/usr/bin/env python3
"""End-to-end + unit tests for the implement IMPLEMENT adapter state.

IMPLEMENT is the dry-run reference adapter (DESIGN §1.1, §2.6): it turns the
`execution_plan` PRIORITIZE produced into a list of `handoffs`, WITHOUT
performing any work. It reads `execution_plan` ONLY (deliberately NOT
`workspace`), writes `handoffs`, and emits OK | BLOCKED. It is the `dry-run`
rung of the trust ladder.

IMPLEMENT is DETERMINISTIC and INERT: a pure function of `execution_plan` — no
model, no wall-clock, no randomness, no network, no filesystem, no VCS artifact.
The same input yields byte-identical handoffs; after a tick that runs it,
`git status` is clean.

The e2e tests drive IMPLEMENT exactly as tick-orchestrator will — building a
real fsm-contracts TickContext, registering the `execution_plan` + `handoffs`
slots, seeding `execution_plan`, running the state, and committing its
StateResult through `fc.apply_result` under the manifest + signal vocabulary
(bounded-scope).

Owner: changyu87
"""

import json
import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import implement as impl  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — an execution_plan builder and a fresh ctx with both slots wired.
# --------------------------------------------------------------------------

def _plan(ordered):
    """Build a minimal execution_plan dict in the shape PRIORITIZE writes.
    IMPLEMENT consumes only `ordered`, so `status` carries sane filler."""
    return {
        "schema_version": "1.0.0",
        "ordered": list(ordered),
        "status": {oid: "pending" for oid in ordered if isinstance(oid, str)},
    }


def _fresh_ctx(execution_plan=None):
    ctx = fc.TickContext()
    # `execution_plan` is the upstream slot PRIORITIZE owns; IMPLEMENT only
    # reads it. Registered as the object slot tick-orchestrator would, with no
    # cross-feature import (IMPLEMENT consumes only the dict `ordered` field).
    ctx.register_slot("execution_plan", {"type": "object"}, version="1.0.0")
    ctx.register_slot(
        impl.HANDOFFS_SLOT["name"],
        impl.HANDOFFS_SLOT["schema"],
        version=impl.HANDOFFS_SLOT["version"],
    )
    if execution_plan is not None:
        ctx.write("execution_plan", execution_plan)
    return ctx


# ==========================================================================
# Behaviour: the Handoff slot descriptor is typed, machine-first, versioned
# and mirrors work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
# ==========================================================================

def test_handoffs_slot_descriptor_is_versioned():
    slot = impl.HANDOFFS_SLOT
    assert slot["name"] == "handoffs"
    assert slot["schema"] == {"type": "array"}
    assert slot["version"] == impl.HANDOFF_SCHEMA_VERSION
    # 1.1.0: additive `concerns` field (auto-maintainer-framework#212).
    assert impl.HANDOFF_SCHEMA_VERSION == "1.1.0"


# ==========================================================================
# Behaviour: per-state manifest is {reads: [execution_plan],
# writes: [handoffs], emits: [OK, BLOCKED]} and conforms to the fsm-contracts
# manifest shape. NOTE: reads execution_plan ONLY — NOT workspace.
# ==========================================================================

def test_implement_manifest_declares_reads_writes_emits():
    m = impl.IMPLEMENT_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("execution_plan",)
    assert "workspace" not in m.reads
    assert m.writes == ("handoffs",)
    assert set(m.emits) == {"OK", "BLOCKED"}


def test_implement_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(impl.IMPLEMENT_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("BLOCKED")
    assert not vocab.is_member("MAYBE")


# ==========================================================================
# E2E Behaviour: a plan with N ordered ids -> N handoffs, one per id, in plan
# order; each status="planned", artifact={"kind":"none","ref":null},
# discovered_work=[], blocked_reason=null; signal OK.
# ==========================================================================

def test_implement_e2e_n_ordered_yields_n_planned_handoffs_in_order():
    ctx = _fresh_ctx(_plan(["wo-3", "wo-1", "wo-2"]))

    result = impl.run(ctx)

    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(impl.IMPLEMENT_SIGNALS)
    fc.apply_result(ctx, impl.IMPLEMENT_MANIFEST, result, vocab)

    handoffs = ctx.read("handoffs")
    assert isinstance(handoffs, list)
    # One handoff per ordered entry, in plan order.
    assert [h["work_order_id"] for h in handoffs] == ["wo-3", "wo-1", "wo-2"]
    for h in handoffs:
        assert h["schema_version"] == impl.HANDOFF_SCHEMA_VERSION
        assert h["status"] == "planned"
        assert h["artifact"] == {"kind": "none", "ref": None}
        assert h["discovered_work"] == []
        assert h["concerns"] == []
        assert h["blocked_reason"] is None


# ==========================================================================
# E2E Behaviour: empty plan (ordered=[]) -> handoffs=[], signal OK.
# ==========================================================================

def test_implement_e2e_empty_plan_yields_no_handoffs_ok():
    ctx = _fresh_ctx(_plan([]))

    result = impl.run(ctx)
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(impl.IMPLEMENT_SIGNALS)
    fc.apply_result(ctx, impl.IMPLEMENT_MANIFEST, result, vocab)

    assert ctx.read("handoffs") == []


# ==========================================================================
# E2E Behaviour: a malformed entry (empty/missing id) -> a BLOCKED handoff for
# that entry with blocked_reason set, and the state signal becomes BLOCKED.
# ==========================================================================

def test_implement_e2e_malformed_empty_id_yields_blocked():
    ctx = _fresh_ctx(_plan(["wo-1", "", "wo-3"]))

    result = impl.run(ctx)
    assert result.signal == "BLOCKED"

    vocab = fc.SignalVocabulary(impl.IMPLEMENT_SIGNALS)
    fc.apply_result(ctx, impl.IMPLEMENT_MANIFEST, result, vocab)

    handoffs = ctx.read("handoffs")
    # One handoff per entry, still in plan order (no budget cap, whole plan).
    assert len(handoffs) == 3
    assert handoffs[0]["work_order_id"] == "wo-1"
    assert handoffs[0]["status"] == "planned"
    assert handoffs[0]["blocked_reason"] is None

    # The malformed entry yields a BLOCKED handoff with blocked_reason set.
    bad = handoffs[1]
    assert bad["status"] == "blocked"
    assert bad["blocked_reason"]
    assert isinstance(bad["blocked_reason"], str)
    assert bad["artifact"] == {"kind": "none", "ref": None}
    assert bad["discovered_work"] == []
    assert bad["concerns"] == []

    assert handoffs[2]["work_order_id"] == "wo-3"
    assert handoffs[2]["status"] == "planned"


def test_implement_e2e_malformed_missing_id_yields_blocked():
    # An entry that is a dict missing an `id`, or None, is malformed too.
    plan = {"schema_version": "1.0.0", "ordered": [{"no_id": True}],
            "status": {}}
    ctx = _fresh_ctx(plan)

    result = impl.run(ctx)
    assert result.signal == "BLOCKED"

    vocab = fc.SignalVocabulary(impl.IMPLEMENT_SIGNALS)
    fc.apply_result(ctx, impl.IMPLEMENT_MANIFEST, result, vocab)

    handoffs = ctx.read("handoffs")
    assert len(handoffs) == 1
    assert handoffs[0]["status"] == "blocked"
    assert handoffs[0]["blocked_reason"]


# ==========================================================================
# Behaviour: NO budget cap — every ordered entry produces a handoff, even for
# a large plan.
# ==========================================================================

def test_implement_no_budget_cap_processes_whole_plan():
    ordered = [f"wo-{i}" for i in range(50)]
    ctx = _fresh_ctx(_plan(ordered))

    result = impl.run(ctx)
    assert result.signal == "OK"

    handoffs = result.writes["handoffs"]
    assert len(handoffs) == 50
    assert [h["work_order_id"] for h in handoffs] == ordered


# ==========================================================================
# Behaviour: determinism — same execution_plan twice yields byte-identical
# handoffs (json.dumps equal).
# ==========================================================================

def test_implement_is_deterministic_byte_identical():
    plan = _plan(["wo-3", "wo-1", "wo-2"])

    ctx_a = _fresh_ctx(plan)
    ctx_b = _fresh_ctx(plan)

    handoffs_a = impl.run(ctx_a).writes["handoffs"]
    handoffs_b = impl.run(ctx_b).writes["handoffs"]

    assert json.dumps(handoffs_a, sort_keys=True) == json.dumps(
        handoffs_b, sort_keys=True)


# ==========================================================================
# Behaviour (INERTness): running the adapter performs no filesystem/VCS effect.
# The module must not import subprocess/git/gh; the run returns only slot writes.
# ==========================================================================

def test_implement_module_imports_no_vcs_or_subprocess():
    src_path = os.path.join(_SRC, "implement.py")
    with open(src_path, "r") as f:
        source = f.read()
    # The dry-run adapter is inert: it never shells out or touches VCS.
    for forbidden in ("import subprocess", "import os", "import random",
                      "import socket", "import urllib", "import requests",
                      "from subprocess", "import git", "import time"):
        assert forbidden not in source, f"inert adapter must not use {forbidden}"


def test_implement_run_returns_only_slot_writes():
    ctx = _fresh_ctx(_plan(["wo-1", "wo-2"]))
    result = impl.run(ctx)
    # The only effect is the StateResult's writes — nothing escapes to the world.
    assert set(result.writes.keys()) == {"handoffs"}
    assert result.journal == []


# ==========================================================================
# Behaviour: the Handoff schema surface is exactly the seven declared keys
# (the v1.1.0 `concerns` field is part of the surface).
# ==========================================================================

def test_handoff_surface_is_exactly_the_declared_keys():
    ctx = _fresh_ctx(_plan(["wo-1"]))
    handoffs = impl.run(ctx).writes["handoffs"]
    assert set(handoffs[0].keys()) == {
        "schema_version", "work_order_id", "status", "artifact",
        "discovered_work", "concerns", "blocked_reason",
    }


# ==========================================================================
# Behaviour: every handoff carries a `concerns` list (the v1.1.0 self-flagged
# doubts field, auto-maintainer-framework#212), mirroring `discovered_work`. The
# dry-run reference adapter self-flags nothing, so it is always an empty list on
# both planned and blocked handoffs.
# ==========================================================================

def test_handoff_concerns_is_always_an_empty_list_for_dry_run():
    ctx = _fresh_ctx(_plan(["wo-1", "", "wo-3"]))
    handoffs = impl.run(ctx).writes["handoffs"]
    assert len(handoffs) == 3
    for h in handoffs:
        assert "concerns" in h
        assert h["concerns"] == []
        assert isinstance(h["concerns"], list)
    # Holds across both statuses the dry-run rung emits.
    assert handoffs[0]["status"] == "planned"
    assert handoffs[1]["status"] == "blocked"


# ==========================================================================
# Behaviour: the factory follows the adapter-wiring convention —
# factory(runtime) -> (StateManifest, run_callable). The returned callable IS
# the state's run; the returned manifest IS IMPLEMENT_MANIFEST.
# ==========================================================================

def test_factory_returns_manifest_and_run_callable():
    manifest, run = impl.factory({})
    assert manifest is impl.IMPLEMENT_MANIFEST
    assert callable(run)

    ctx = _fresh_ctx(_plan(["wo-1", "wo-2"]))
    result = run(ctx)
    assert result.signal == "OK"
    assert [h["work_order_id"] for h in result.writes["handoffs"]] == [
        "wo-1", "wo-2"]
