"""E2E + unit tests for stale/incompatible tick-checkpoint discard-and-fresh.

ROOT-CAUSE fix for the third dogfood crash: a durable ``tick_checkpoint``
paused at an agent-state under an OLDER wiring carries a ``pending.writes`` slot
that the current (migrated, seeded) wiring NO LONGER registers — e.g. a v0.7.0
checkpoint paused at REVIEW with ``pending.writes='review_verdicts'``, a slot the
loop redesign (FT-C/D) retired in favour of ``review_findings``. The adapter-map
migration self-heals the live CONFIG, but the persisted CHECKPOINT bypasses it:

  * ``--step`` re-emits the stale dispatch from the checkpoint (the source of
    truth), and
  * ``--resume`` applies the subagent output to the unregistered ``review_verdicts``
    slot -> ``fc.ContractError`` CRASHES run_tick (a Python traceback, not a
    graceful status).

The runner must treat a checkpoint whose pending writes slot is absent from the
current freshly-seeded wiring as STALE: ``_clear_checkpoint`` it and drive a
FRESH tick (re-walk from GUARD, re-pause at the agent-state with the CURRENT
migrated writes slot). This applies on BOTH ``--step`` and ``--resume``.

Behaviours exercised (each has a test):

  A. ``_checkpoint_compatible(empty, ctx)`` is True.
  B. ``_checkpoint_compatible(all-writes-registered, ctx)`` is True.
  C. ``_checkpoint_compatible(retired-slot writes, ctx)`` is False.
  D. e2e ``--step`` over a healed REVIEW route with a STALE
     ``pending.writes='review_verdicts'`` checkpoint: the stale checkpoint is
     DISCARDED and a FRESH tick pauses writing ``review_findings`` (no re-emit of
     the stale dispatch, no crash); ``TICK_CHECKPOINT_KEY`` after the discard
     carries the FRESH pause (its pending writes = ``review_findings``).
  E. e2e ``--resume`` with the SAME stale checkpoint does NOT raise ContractError
     — it discards + drives a fresh tick (a graceful paused/done envelope).
  F. regression: a COMPATIBLE checkpoint (pending.writes registered) is still
     re-emitted byte-identically on ``--step`` (no discard).
  G. regression: a COMPATIBLE checkpoint is still RESUMED on ``--resume`` (the
     subagent output is applied, the tick advances) — no discard.

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

import verify_integrate as vi  # noqa: E402
import work_intake as wi  # noqa: E402
import durable_state as ds  # noqa: E402
import fsm_contracts as fc  # noqa: E402
import run_tick as rt  # noqa: E402
import adapter_map_config as amc  # noqa: E402


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
# PERSIST->EXIT. VERIFY is the default script make_verify; REVIEW is an agent
# state (current healthy entry). The read path is enough to pause REVIEW.
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


def _setup_review_project():
    """A project whose route reaches an agent REVIEW and whose adapter-map wires
    a CURRENT (healthy) REVIEW agent entry writing review_findings."""
    project_dir = tempfile.mkdtemp(prefix="sched-ckpt-")
    cfg = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "route.json"), "w") as f:
        json.dump(_REVIEW_ROUTE, f)
    amap = dict(rt.DEFAULT_ADAPTER_MAP)
    amap["REVIEW"] = amc._build_agent_entry("REVIEW", "my-reviewer")
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


def _reach_review_pause(project_dir, cfg, state_path, journal_path):
    """Run --step to reach the REVIEW pause; return the paused envelope."""
    argv = ["--step", "--runtime-dir", cfg, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, out
    env = json.loads(out)
    assert env["status"] == "paused" and env["state"] == "REVIEW", env
    return env


def _make_stale_checkpoint(state_path):
    """Take the durable checkpoint a clean REVIEW pause wrote (writes =
    review_findings) and MUTATE it to simulate a pre-upgrade checkpoint paused at
    REVIEW writing the RETIRED ``review_verdicts`` slot, persisting it back."""
    doc = ds.DurableState(state_path).load()
    ckpt = copy.deepcopy(doc[rt.TICK_CHECKPOINT_KEY])
    assert ckpt["pending"]["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], ckpt
    # Retire the writes slot to the slot the redesign removed.
    ckpt["pending"]["writes"] = "review_verdicts"
    doc[rt.TICK_CHECKPOINT_KEY] = ckpt
    ds.DurableState(state_path).save(doc)
    # Write a VALID subagent output file at each pending dispatch's output_path,
    # so a --resume would reach ctx.write('review_verdicts', ...) (the crash)
    # rather than short-circuiting on a missing-output-file invalid_output.
    for d in ckpt["pending"]["dispatches"]:
        op = d["output_path"]
        os.makedirs(os.path.dirname(op), exist_ok=True)
        with open(op, "w") as f:
            json.dump([], f)
    return ckpt


# ==========================================================================
# Behaviours A/B/C — the pure predicate _checkpoint_compatible.
# ==========================================================================

def _seed_review_ctx(state_path, journal_path):
    return rt._seed_context(state_path, journal_path, _REVIEW_ROUTE)


def test_checkpoint_compatible_empty_is_true():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    ds.DurableState(state_path).save({"counter": 0})
    ctx = _seed_review_ctx(state_path, journal_path)
    assert rt._checkpoint_compatible({}, ctx) is True


def test_checkpoint_compatible_all_registered_is_true():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    ds.DurableState(state_path).save({"counter": 0})
    ctx = _seed_review_ctx(state_path, journal_path)
    # review_findings IS registered for a REVIEW route (seeded empty).
    assert vi.REVIEW_FINDINGS_SLOT["name"] in ctx.registered_slots()
    ckpt = {"pending": {"state": "REVIEW",
                        "writes": vi.REVIEW_FINDINGS_SLOT["name"]}}
    assert rt._checkpoint_compatible(ckpt, ctx) is True


def test_checkpoint_compatible_retired_slot_is_false():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    ds.DurableState(state_path).save({"counter": 0})
    ctx = _seed_review_ctx(state_path, journal_path)
    # review_verdicts is NOT registered (the redesign retired it).
    assert "review_verdicts" not in ctx.registered_slots()
    ckpt = {"pending": {"state": "REVIEW", "writes": "review_verdicts"}}
    assert rt._checkpoint_compatible(ckpt, ctx) is False


# ==========================================================================
# Behaviour D — --step over a STALE checkpoint discards it + drives a FRESH tick
# pausing with the CURRENT review_findings writes slot (no crash, no stale re-emit).
# ==========================================================================

def test_step_discards_stale_checkpoint_and_pauses_fresh_review_findings():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    _reach_review_pause(project_dir, cfg, state_path, journal_path)
    _make_stale_checkpoint(state_path)

    # --step again. Without the discard guard the runner would re-emit the stale
    # checkpoint (pending.writes='review_verdicts'); with it the stale checkpoint
    # is dropped and a FRESH tick pauses writing review_findings.
    argv = ["--step", "--runtime-dir", cfg, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, out
    env = json.loads(out)
    assert env["status"] == "paused", env
    assert env["state"] == "REVIEW", env
    dispatch = env["dispatches"][0]
    # The FRESH pause writes review_findings (NOT the retired review_verdicts).
    assert dispatch["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], dispatch

    # The persisted checkpoint is the FRESH one — its pending writes is the
    # current slot, never the discarded review_verdicts.
    doc = ds.DurableState(state_path).load()
    ckpt = doc[rt.TICK_CHECKPOINT_KEY]
    assert ckpt["pending"]["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], ckpt


# ==========================================================================
# Behaviour E — --resume with the SAME stale checkpoint does NOT raise
# ContractError; it discards + drives a fresh tick.
# ==========================================================================

def test_resume_with_stale_checkpoint_discards_no_contract_error():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    _reach_review_pause(project_dir, cfg, state_path, journal_path)
    _make_stale_checkpoint(state_path)

    # --resume. Without the guard this applies the subagent output to the
    # unregistered review_verdicts slot -> fc.ContractError crash. With it the
    # stale checkpoint is discarded and a fresh tick runs (graceful envelope).
    argv = ["--resume", "--runtime-dir", cfg, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    raised = None
    with _stub_pull_source():
        try:
            code, out = _run_main(argv)
        except fc.ContractError as exc:  # the bug being fixed
            raised = exc
    assert raised is None, f"resume raised ContractError on stale checkpoint: {raised}"
    assert code == 0, out
    env = json.loads(out)
    # The fresh re-walk pauses at the healed agent REVIEW writing review_findings.
    assert env["status"] == "paused", env
    assert env["state"] == "REVIEW", env
    assert env["dispatches"][0]["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], env

    doc = ds.DurableState(state_path).load()
    ckpt = doc[rt.TICK_CHECKPOINT_KEY]
    assert ckpt["pending"]["writes"] == vi.REVIEW_FINDINGS_SLOT["name"], ckpt


# ==========================================================================
# Behaviour F — a COMPATIBLE checkpoint is still re-emitted on --step (no discard,
# no behaviour change for the normal crash-safety path).
# ==========================================================================

def test_step_with_compatible_checkpoint_re_emits_unchanged():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    first = _reach_review_pause(project_dir, cfg, state_path, journal_path)
    # The checkpoint left by the first pause is COMPATIBLE (writes=review_findings).

    # --step again WITHOUT mutating the checkpoint: crash-safety re-emit, the
    # SAME paused dispatch (byte-identical output_path), checkpoint untouched.
    argv = ["--step", "--runtime-dir", cfg, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, out
    env = json.loads(out)
    assert env["status"] == "paused" and env["state"] == "REVIEW", env
    # Byte-identical re-emit: same writes slot AND same output_path as the first.
    assert env["dispatches"][0]["writes"] == \
        first["dispatches"][0]["writes"], env
    assert env["dispatches"][0]["output_path"] == \
        first["dispatches"][0]["output_path"], env


# ==========================================================================
# Behaviour G — a COMPATIBLE checkpoint is still RESUMED on --resume (the subagent
# output is applied, the tick advances past REVIEW). No discard.
# ==========================================================================

def test_resume_with_compatible_checkpoint_applies_output_unchanged():
    project_dir, cfg, state_path, journal_path = _setup_review_project()
    paused = _reach_review_pause(project_dir, cfg, state_path, journal_path)
    # Write the subagent output file for the REVIEW dispatch (an EMPTY advisory
    # review_findings list — signal EMPTY -> PERSIST).
    out_path = paused["dispatches"][0]["output_path"]
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump([], f)

    argv = ["--resume", "--runtime-dir", cfg, "--state", state_path,
            "--journal", journal_path, "--project-dir", project_dir]
    with _stub_pull_source():
        code, out = _run_main(argv)
    assert code == 0, out
    env = json.loads(out)
    # The compatible resume applied the output and reached the terminal (done) —
    # no discard, no re-pause at REVIEW.
    assert env["status"] == "done", env

    # The checkpoint is cleared at the terminal (normal resume completion).
    doc = ds.DurableState(state_path).load()
    assert rt.TICK_CHECKPOINT_KEY not in doc, doc
