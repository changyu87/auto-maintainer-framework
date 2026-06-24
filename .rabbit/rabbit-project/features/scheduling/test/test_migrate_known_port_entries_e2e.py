#!/usr/bin/env python3
"""End-to-end + unit conformance tests for adapter-map self-healing migration.

ROOT-CAUSE fix for the dogfood REVIEW failure: a stale persisted
``adapter-map.json`` known-port agent entry (wired under v0.6.0) carries retired
template fields — e.g. a REVIEW entry that writes the RETIRED ``review_verdicts``
slot with an old ``output_example`` — which breaks the redesigned (FT-C/D) loop
where REVIEW writes ``review_findings``. scheduling owns ``AGENT_PORT_TEMPLATES``
+ ``_build_agent_entry``, so it owns the pure migration that heals such RETIRED
entries against the LIVE template, preserving ONLY the ``subagent_type``.

SURGICAL re-derive (#279 regression fix): an EARLIER blanket re-derive rebuilt
EVERY known-port agent entry from the template, which CLOBBERED valid
customizations — the dogfood IMPLEMENT writes the still-valid ``handoffs`` slot
but reads ``work_orders`` (NOT the template's ``execution_plan``) in a
NO-PRIORITIZE route; the blanket re-derive rewrote its ``inputs`` to
``execution_plan`` (never produced) → ``adapter_wiring`` ``WiringError`` → the
loop would not start. ``migrate_known_port_entries`` now re-derives ONLY a
RETIRED-SLOT entry (an agent entry on a known port whose ``dispatch[0].writes``
is NOT among the live templates' writes slots), and leaves a valid-writes
customization UNCHANGED.

``migrate_known_port_entries(adapter_map) -> adapter_map`` (adapter_map_config):

  - ``valid_writes`` = the set of every live template's ``writes`` slot.
  - For each (port, entry): re-derive via
    ``_build_agent_entry(port, entry's existing dispatch[0].subagent_type)`` ONLY
    when ``ad.is_agent_entry(entry)`` AND ``port in AGENT_PORT_TEMPLATES`` AND
    the entry's ``dispatch[0]['writes']`` is NOT in ``valid_writes`` (a RETIRED
    slot). Otherwise return the entry UNCHANGED.
  - Leave UNCHANGED: a valid-writes known-port entry (even with customized
    reads/task), script (string) entries, custom-port agent entries (a port NOT
    in AGENT_PORT_TEMPLATES), and non-agent entries.
  - Returns a NEW dict (never mutates the input). Idempotent.

It is wired into ``run_tick`` as the ``migrate=`` hook of the single
``aw.build_loop(...)`` call, so the heal happens on load BEFORE resolve+validate.

Behaviours exercised (every one has a test; the REVIEW heal and the IMPLEMENT
preservation additionally have an e2e ``build_loop`` test, per the E2E TEST RULE):

  A. heals a RETIRED-slot REVIEW entry: review_verdicts + an old output_example ->
     review_findings + the current example + manifest.writes == review_findings;
     subagent_type preserved.
  B. idempotent on an already-current entry, AND the input dict is not mutated.
  C. a custom-port agent entry (port not in templates) + a script string entry
     are left UNCHANGED.
  D. a VALID-writes known-port entry with a customized read (IMPLEMENT writes
     handoffs, reads work_orders) is UNCHANGED — the custom inputs/task survive.
  E. e2e: run_tick --step over a VERIFY->REVIEW route whose project-local
     adapter-map.json has a STALE REVIEW agent entry reaches the REVIEW pause
     whose dispatch writes review_findings AND whose checkpoint per-dispatch
     schema is the array slot schema ({"type":"array"}).
  F. e2e: ``build_loop(migrate=...)`` over a NO-PRIORITIZE route
     GUARD..PULL->TRIAGE->IMPLEMENT->VERIFY->REVIEW->INTEGRATE with a custom
     IMPLEMENT (reads work_orders) + stale REVIEW (review_verdicts) adapter-map
     resolves WITHOUT WiringError: IMPLEMENT is preserved (reads work_orders),
     REVIEW is healed to review_findings.

Owner: changyu87
"""

import contextlib
import copy
import io
import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "safety-governance", "agent-dispatch",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import agent_dispatch as ad  # noqa: E402
import verify_integrate as vi  # noqa: E402
import work_intake as wi  # noqa: E402
import durable_state as ds  # noqa: E402
import run_tick as rt  # noqa: E402
import adapter_map_config as amc  # noqa: E402


# A STALE REVIEW agent entry as v0.6.0 persisted it: it writes the RETIRED
# `review_verdicts` slot and carries an old per-finding output_example shape.
# Everything but the subagent_type is wrong for the redesigned loop.
_STALE_REVIEW = {
    "kind": "agent",
    "manifest": {"reads": ["verdicts"], "writes": ["review_verdicts"],
                 "emits": ["OK", "EMPTY"]},
    "dispatch": [
        {
            "subagent_type": "my-reviewer",
            "inputs": ["verdicts"],
            "writes": "review_verdicts",
            "cardinality": "once",
            "output_example": [{"approved": True, "severity": "none"}],
        }
    ],
    "signal": {"rule": "nonempty_else_empty"},
}


def _current_review_entry(subagent_type):
    """What _build_agent_entry produces for REVIEW from the LIVE template."""
    return amc._build_agent_entry("REVIEW", subagent_type)


# ==========================================================================
# Behaviour A — heals a RETIRED-slot REVIEW entry (writes + example + manifest),
# keeps the subagent_type.
# ==========================================================================

def test_migrate_heals_stale_review_entry():
    amap = {"REVIEW": copy.deepcopy(_STALE_REVIEW)}
    out = amc.migrate_known_port_entries(amap)

    healed = out["REVIEW"]
    dispatch = healed["dispatch"][0]
    # subagent_type preserved.
    assert dispatch["subagent_type"] == "my-reviewer", dispatch
    # writes re-derived to the LIVE review_findings slot.
    assert dispatch["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], dispatch
    assert healed["manifest"]["writes"] == [vi.REVIEW_FINDINGS_SLOT["name"]], \
        healed["manifest"]
    # output_example re-derived from the live template (NOT the stale shape).
    assert dispatch["output_example"] != _STALE_REVIEW["dispatch"][0][
        "output_example"], dispatch
    assert dispatch["output_example"] == amc.AGENT_PORT_TEMPLATES["REVIEW"][
        "output_example"], dispatch
    # The healed entry is byte-identical to a freshly built one (only the
    # subagent_type carried over).
    assert healed == _current_review_entry("my-reviewer"), healed


def test_migrate_heals_inputs_signal_manifest_from_live_template():
    amap = {"REVIEW": copy.deepcopy(_STALE_REVIEW)}
    healed = amc.migrate_known_port_entries(amap)["REVIEW"]
    dispatch = healed["dispatch"][0]
    # inputs/manifest.reads re-derived from the live REVIEW template reads.
    assert dispatch["inputs"] == list(vi.REVIEW_MANIFEST.reads), dispatch
    assert healed["manifest"]["reads"] == list(vi.REVIEW_MANIFEST.reads), healed
    # signal re-derived from the live template rule.
    assert healed["signal"]["rule"] == \
        amc.AGENT_PORT_TEMPLATES["REVIEW"]["signal_rule"], healed
    # REVIEW is non-acting (no effect/isolation in the live template).
    assert "effect" not in dispatch, dispatch
    assert "isolation" not in dispatch, dispatch


# ==========================================================================
# Behaviour B — idempotent on a current entry, AND no input mutation.
# ==========================================================================

def test_migrate_idempotent_on_current_entry():
    current = _current_review_entry("my-reviewer")
    amap = {"REVIEW": copy.deepcopy(current)}
    once = amc.migrate_known_port_entries(amap)
    twice = amc.migrate_known_port_entries(once)
    assert once["REVIEW"] == current, once
    assert twice["REVIEW"] == current, twice


def test_migrate_returns_new_dict_no_input_mutation():
    amap = {"REVIEW": copy.deepcopy(_STALE_REVIEW)}
    before = copy.deepcopy(amap)
    out = amc.migrate_known_port_entries(amap)
    # A NEW dict object is returned.
    assert out is not amap
    # The input dict (and its nested stale entry) is untouched.
    assert amap == before, amap
    assert amap["REVIEW"] is not out["REVIEW"]


# ==========================================================================
# Behaviour C — script string + custom-port agent entries unchanged.
# ==========================================================================

def test_migrate_leaves_script_and_custom_port_entries_unchanged():
    custom_agent = {
        "kind": "agent",
        "manifest": {"reads": ["my_slot"], "writes": ["my_slot"],
                     "emits": ["OK", "EMPTY"]},
        "dispatch": [
            {
                "subagent_type": "custom-doer",
                "inputs": ["my_slot"],
                "writes": "my_slot",
                "cardinality": "once",
                "output_example": [{"x": 1}],
            }
        ],
        "signal": {"rule": "nonempty_else_empty"},
    }
    amap = {
        "GUARD": "run_tick:make_guard",          # script string entry
        "PULL": "run_tick:make_pull",            # script string entry
        "CUSTOM_PORT": copy.deepcopy(custom_agent),  # agent, port NOT in templates
        "REVIEW": copy.deepcopy(_STALE_REVIEW),  # agent, KNOWN port -> healed
    }
    out = amc.migrate_known_port_entries(amap)

    # Script strings untouched.
    assert out["GUARD"] == "run_tick:make_guard", out
    assert out["PULL"] == "run_tick:make_pull", out
    # Custom-port agent entry untouched (port not in AGENT_PORT_TEMPLATES).
    assert out["CUSTOM_PORT"] == custom_agent, out
    # The known port WAS healed (sanity — the migration did run).
    assert out["REVIEW"]["dispatch"][0]["writes"] == \
        vi.REVIEW_FINDINGS_SLOT["name"], out


# ==========================================================================
# Behaviour D — a VALID-writes known-port entry with a customized read is
# UNCHANGED (the #279 regression fix). The dogfood IMPLEMENT writes the still-
# valid `handoffs` slot but reads `work_orders` (NOT the template's
# `execution_plan`); the surgical migration must NOT clobber it.
# ==========================================================================

# The dogfood IMPLEMENT entry: writes the VALID `handoffs` slot, reads
# `work_orders` (custom, not the template's `execution_plan`), carries a custom
# `task` + `description`. A blanket re-derive would rewrite inputs to
# `execution_plan`; the surgical migration must leave it byte-identical.
_CUSTOM_IMPLEMENT = {
    "kind": "agent",
    "manifest": {"reads": ["work_orders"], "writes": ["handoffs"],
                 "emits": ["OK", "BLOCKED"]},
    "dispatch": [
        {
            "subagent_type": "auto-maintainer:auto-maintainer-implementer",
            "inputs": ["work_orders"],
            "writes": "handoffs",
            "cardinality": {"per_item": "work_orders"},
            "effect": "implement",
            "isolation": "worktree",
            "description": "auto-maintainer implement",
            "task": "Enact this ONE work order's triage decision.",
            "output_example": {
                "schema_version": "1.0.0",
                "work_order_id": "wo-owner/repo#1",
                "status": "opened",
                "artifact": {"kind": "pr", "ref": "https://x/pull/1"},
                "discovered_work": [],
                "blocked_reason": None,
            },
        }
    ],
    "signal": {"rule": "blocked_if_any"},
}


def test_migrate_leaves_valid_writes_custom_read_entry_unchanged():
    amap = {"IMPLEMENT": copy.deepcopy(_CUSTOM_IMPLEMENT)}
    out = amc.migrate_known_port_entries(amap)
    healed = out["IMPLEMENT"]
    # writes is a VALID live-template slot (handoffs), so the entry is NOT
    # re-derived — the custom `work_orders` read survives verbatim.
    assert healed == _CUSTOM_IMPLEMENT, healed
    dispatch = healed["dispatch"][0]
    assert dispatch["inputs"] == ["work_orders"], dispatch
    assert dispatch["writes"] == "handoffs", dispatch
    # The custom task/description are preserved (a blanket re-derive would drop
    # them).
    assert dispatch["task"] == "Enact this ONE work order's triage decision."
    assert dispatch["description"] == "auto-maintainer implement"


def test_migrate_mixed_map_heals_retired_preserves_valid():
    """A single map with BOTH a stale REVIEW (retired writes -> healed) and a
    custom IMPLEMENT (valid writes -> preserved) does each correctly."""
    amap = {
        "IMPLEMENT": copy.deepcopy(_CUSTOM_IMPLEMENT),
        "REVIEW": copy.deepcopy(_STALE_REVIEW),
    }
    out = amc.migrate_known_port_entries(amap)
    # IMPLEMENT preserved.
    assert out["IMPLEMENT"] == _CUSTOM_IMPLEMENT, out["IMPLEMENT"]
    # REVIEW healed.
    assert out["REVIEW"]["dispatch"][0]["writes"] == \
        vi.REVIEW_FINDINGS_SLOT["name"], out["REVIEW"]


# ==========================================================================
# Behaviour E — e2e: run_tick --step over a VERIFY->REVIEW route whose
# project-local adapter-map.json has a STALE REVIEW agent entry reaches the
# REVIEW pause writing review_findings; the checkpoint per-dispatch schema is
# the array slot schema.
# ==========================================================================

_GH_JSON = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  }
]"""


@contextlib.contextmanager
def _stub_pull_source():
    saved = rt.DEFAULT_PULL_SOURCE
    items = wi.parse_gh_issues(_GH_JSON)

    def source(repo=None):
        return list(items)
    rt.DEFAULT_PULL_SOURCE = source
    try:
        yield
    finally:
        rt.DEFAULT_PULL_SOURCE = saved


# A minimal route reaching an AGENT REVIEW: GUARD->DRAIN->PULL->VERIFY->REVIEW->
# PERSIST->EXIT. VERIFY is the default script make_verify; REVIEW is the stale
# agent entry. (No IMPLEMENT/INTEGRATE — the read path is enough to pause REVIEW.)
_REVIEW_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "VERIFY", "REVIEW", "PERSIST", "EXIT",
               "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "VERIFY"},
        {"state": "PULL", "signal": "EMPTY", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "REVIEW"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "REVIEW"},
        {"state": "REVIEW", "signal": "OK", "next": "PERSIST"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def _setup_stale_review_project():
    project_dir = tempfile.mkdtemp(prefix="sched-migrate-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_REVIEW_ROUTE, f)
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["REVIEW"] = copy.deepcopy(_STALE_REVIEW)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)
    state_path = os.path.join(cfg, "durable-state.json")
    journal_path = os.path.join(cfg, "tick-journal.jsonl")
    return project_dir, cfg, state_path, journal_path


def _run_main(argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = rt.main(argv)
    return code, buf.getvalue()


def test_e2e_step_over_stale_review_reaches_review_pause_writing_review_findings():
    project_dir, runtime_dir, state_path, journal_path = \
        _setup_stale_review_project()
    argv = ["--step", "--runtime-dir", runtime_dir, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)

    # Without the migrate hook the stale entry's review_verdicts writes would
    # crash the load/resolve (no review_verdicts slot/schema); with the hook the
    # entry self-heals and the tick PAUSES at the healed agent REVIEW.
    assert code == 0, out
    envelope = json.loads(out)
    assert envelope["status"] == "paused", envelope
    assert envelope["state"] == "REVIEW", envelope
    dispatch = envelope["dispatches"][0]
    # The healed dispatch writes review_findings (NOT the retired review_verdicts).
    assert dispatch["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], dispatch
    assert dispatch["subagent_type"] == "my-reviewer", dispatch

    # The durable checkpoint's per-dispatch schema is the array slot schema —
    # proving the healed `writes` flowed through to the resume-validation schema.
    doc = ds.DurableState(state_path).load()
    checkpoint = doc[rt.TICK_CHECKPOINT_KEY]
    cp_dispatch = checkpoint["pending"]["dispatches"][0]
    assert cp_dispatch["schema"] == vi.REVIEW_FINDINGS_SLOT["schema"], checkpoint


# ==========================================================================
# Behaviour F — e2e: build_loop(migrate=...) over the dogfood-shaped NO-PRIORITIZE
# route GUARD..PULL->TRIAGE->IMPLEMENT->VERIFY->REVIEW->INTEGRATE with a custom
# IMPLEMENT (reads work_orders) + stale REVIEW (review_verdicts) resolves WITHOUT
# WiringError: IMPLEMENT is PRESERVED (still reads work_orders), REVIEW is HEALED.
# The OLD blanket migration would rewrite IMPLEMENT to read execution_plan (never
# produced — no PRIORITIZE) and raise WiringError; the surgical migration must not.
# ==========================================================================

import adapter_wiring as aw  # noqa: E402

_NO_PRIORITIZE_ROUTE = {
    "schema_version": "1.0.0",
    "states": ["GUARD", "DRAIN", "PULL", "TRIAGE", "IMPLEMENT", "VERIFY",
               "REVIEW", "INTEGRATE", "PERSIST", "EXIT", "DONE", "HALTED"],
    "edges": [
        {"state": "GUARD", "signal": "OK", "next": "DRAIN"},
        {"state": "GUARD", "signal": "HALT_REQUESTED", "next": "HALTED"},
        {"state": "GUARD", "signal": "RESTART_REQUIRED", "next": "HALTED"},
        {"state": "DRAIN", "signal": "OK", "next": "PULL"},
        {"state": "PULL", "signal": "OK", "next": "TRIAGE"},
        {"state": "PULL", "signal": "EMPTY", "next": "TRIAGE"},
        {"state": "TRIAGE", "signal": "OK", "next": "IMPLEMENT"},
        {"state": "TRIAGE", "signal": "EMPTY", "next": "VERIFY"},
        {"state": "IMPLEMENT", "signal": "OK", "next": "VERIFY"},
        {"state": "IMPLEMENT", "signal": "BLOCKED", "next": "VERIFY"},
        {"state": "VERIFY", "signal": "OK", "next": "REVIEW"},
        {"state": "VERIFY", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "REVIEW", "signal": "OK", "next": "INTEGRATE"},
        {"state": "REVIEW", "signal": "EMPTY", "next": "PERSIST"},
        {"state": "INTEGRATE", "signal": "OK", "next": "PERSIST"},
        {"state": "PERSIST", "signal": "OK", "next": "EXIT"},
        {"state": "EXIT", "signal": "refire", "next": "DONE"},
        {"state": "EXIT", "signal": "idle", "next": "DONE"},
        {"state": "EXIT", "signal": "break", "next": "DONE"},
        {"state": "EXIT", "signal": "halt", "next": "DONE"},
    ],
    "terminal": ["DONE", "HALTED"],
}


def _setup_dogfood_project():
    project_dir = tempfile.mkdtemp(prefix="sched-dogfood-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_NO_PRIORITIZE_ROUTE, f)
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["TRIAGE"] = amc._build_agent_entry("TRIAGE", "my-triager")
    amap["IMPLEMENT"] = copy.deepcopy(_CUSTOM_IMPLEMENT)
    amap["REVIEW"] = copy.deepcopy(_STALE_REVIEW)
    with open(os.path.join(cfg, "adapter-map.json"), "w") as f:
        json.dump(amap, f)
    return project_dir, cfg


def test_e2e_build_loop_preserves_implement_heals_review_no_wiring_error():
    project_dir, cfg = _setup_dogfood_project()
    runtime = {
        "project_dir": project_dir,
        "runtime_dir": cfg,
        "source": None,
        "now": None,
        "governance": {"mode": "auto-merge"},
    }
    # The surgical migration resolves WITHOUT WiringError; the OLD blanket
    # re-derive would rewrite IMPLEMENT to read execution_plan (never produced)
    # and raise here.
    route, states = aw.build_loop(
        _NO_PRIORITIZE_ROUTE, rt.DEFAULT_ADAPTER_MAP, runtime,
        "GUARD", rt._INITIAL_SLOTS,
        migrate=amc.migrate_known_port_entries)

    # IMPLEMENT PRESERVED — its manifest still READS work_orders (not the
    # template's execution_plan).
    implement_manifest = states["IMPLEMENT"][0]
    assert "work_orders" in list(implement_manifest.reads), implement_manifest.reads
    assert "execution_plan" not in list(implement_manifest.reads), \
        implement_manifest.reads
    # REVIEW HEALED — its manifest WRITES review_findings (not review_verdicts).
    review_manifest = states["REVIEW"][0]
    assert vi.REVIEW_FINDINGS_SLOT["name"] in list(review_manifest.writes), \
        review_manifest.writes
    assert "review_verdicts" not in list(review_manifest.writes), \
        review_manifest.writes
