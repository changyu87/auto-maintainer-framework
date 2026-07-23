#!/usr/bin/env python3
"""End-to-end conformance tests for src/clobber.py — the /auto-maintainer:clobber
control script that RESETS the loop to a clean start.

clobber DELETES only the RUNTIME-STATE artifacts under the runtime dir while
PRESERVING user config:

  Cleared:  durable-state.json, the disposition + lock.json markers,
            events.jsonl, tick-journal.jsonl, the dispatch-out/ dir, and the
            heartbeat markers (loop-intent + last-resume-session).
  Preserved (NEVER touched): config.json, route.json, adapter-map.json, and any
            *.bak / *.migrated config backups.

It resolves the runtime dir via run_tick.resolve_runtime_paths (never
duplicating path logic), is idempotent (a missing artifact is a no-op), NEVER
creates the runtime dir, requires --yes to actually delete (without it a DRY-RUN
that deletes nothing), and prints a machine-first {removed, preserved} summary.
clobber writes NOTHING except the deletions (no model, no network).

scheduling CONSUMES run_tick + lifecycle-dispositions + heartbeat UNCHANGED via
sys.path; edits live ONLY in scheduling (clobber.py).

Owner: changyu87
"""

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
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability", "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import clobber  # noqa: E402
import lifecycle_dispositions as ld  # noqa: E402
import heartbeat  # noqa: E402


# The runtime-state artifacts clobber removes and the config it preserves.
_STATE_FILES = ["durable-state.json", "disposition", "lock.json",
                "events.jsonl", "tick-journal.jsonl",
                "loop-intent", "last-resume-session"]
_CONFIG_FILES = ["config.json", "route.json", "adapter-map.json",
                 "route.json.bak", "adapter-map.json.migrated"]


def _touch(path, content="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)


def _populate(runtime_dir):
    """Create a full runtime dir: every state artifact + config + dispatch-out."""
    os.makedirs(runtime_dir, exist_ok=True)
    for name in _STATE_FILES:
        _touch(os.path.join(runtime_dir, name))
    for name in _CONFIG_FILES:
        _touch(os.path.join(runtime_dir, name))
    _touch(os.path.join(runtime_dir, "dispatch-out", "TRIAGE-0-0.json"))


def _run(runtime_dir, project_dir, yes):
    argv = ["--runtime-dir", runtime_dir, "--project-dir", project_dir]
    if yes:
        argv.append("--yes")
    return clobber.main(argv)


# ==========================================================================
# Behaviour 1 — --yes removes every runtime-state artifact and PRESERVES config.
# ==========================================================================

def test_clobber_removes_state_preserves_config():
    root = tempfile.mkdtemp(prefix="sched-clobber-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    rc = _run(runtime_dir, root, yes=True)
    assert rc == 0, rc
    # Every runtime-state artifact is gone.
    for name in _STATE_FILES:
        assert not os.path.exists(os.path.join(runtime_dir, name)), name
    assert not os.path.exists(os.path.join(runtime_dir, "dispatch-out")), \
        "dispatch-out dir not removed"
    # Every config file is preserved.
    for name in _CONFIG_FILES:
        assert os.path.exists(os.path.join(runtime_dir, name)), name
    # The runtime dir itself still exists (clobber empties, never removes it).
    assert os.path.isdir(runtime_dir)


# ==========================================================================
# Behaviour 2 — the summary is a machine-first {removed, preserved} JSON.
# ==========================================================================

def test_clobber_prints_machine_first_summary():
    import io
    import contextlib
    root = tempfile.mkdtemp(prefix="sched-clobber-sum-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(runtime_dir, root, yes=True)
    assert rc == 0, rc
    out = buf.getvalue()
    # The machine-first JSON object is embedded in stdout.
    start = out.index("{")
    end = out.rindex("}") + 1
    summary = json.loads(out[start:end])
    assert "removed" in summary and "preserved" in summary, summary
    # Every removed entry names a real runtime-state artifact; config never in it.
    removed_names = {os.path.basename(p) for p in summary["removed"]}
    assert "durable-state.json" in removed_names, summary
    assert "dispatch-out" in removed_names, summary
    for cfg in _CONFIG_FILES:
        assert cfg not in removed_names, (cfg, summary)
    preserved_names = {os.path.basename(p) for p in summary["preserved"]}
    assert "config.json" in preserved_names, summary
    assert "route.json.bak" in preserved_names, summary


# ==========================================================================
# Behaviour 3 — idempotent: a second run is a no-op (removes nothing, rc 0).
# ==========================================================================

def test_clobber_is_idempotent():
    root = tempfile.mkdtemp(prefix="sched-clobber-idem-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    assert _run(runtime_dir, root, yes=True) == 0
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(runtime_dir, root, yes=True)
    assert rc == 0, rc
    out = buf.getvalue()
    summary = json.loads(out[out.index("{"):out.rindex("}") + 1])
    # Nothing left to remove.
    assert summary["removed"] == [], summary
    # Config is still there.
    for name in _CONFIG_FILES:
        assert os.path.exists(os.path.join(runtime_dir, name)), name


# ==========================================================================
# Behaviour 4 — without --yes it is a DRY-RUN: it deletes NOTHING (rc 0) and
# reports what it WOULD remove.
# ==========================================================================

def test_clobber_dry_run_deletes_nothing():
    root = tempfile.mkdtemp(prefix="sched-clobber-dry-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(runtime_dir, root, yes=False)
    assert rc == 0, rc
    # Nothing was deleted (dry-run).
    for name in _STATE_FILES:
        assert os.path.exists(os.path.join(runtime_dir, name)), name
    assert os.path.exists(os.path.join(runtime_dir, "dispatch-out")), \
        "dry-run deleted dispatch-out"
    # But the summary reports what WOULD be removed.
    out = buf.getvalue()
    summary = json.loads(out[out.index("{"):out.rindex("}") + 1])
    removed_names = {os.path.basename(p) for p in summary["removed"]}
    assert "durable-state.json" in removed_names, summary
    assert "dispatch-out" in removed_names, summary


# ==========================================================================
# Behaviour 5 — clobber NEVER creates the runtime dir: an absent dir is a
# nothing-to-do no-op (rc 0), and the dir stays absent.
# ==========================================================================

def test_clobber_never_creates_runtime_dir():
    root = tempfile.mkdtemp(prefix="sched-clobber-absent-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    # runtime_dir does NOT exist.
    assert not os.path.exists(runtime_dir)
    rc = _run(runtime_dir, root, yes=True)
    assert rc == 0, rc
    # Still absent — clobber never created it.
    assert not os.path.exists(runtime_dir), "clobber created the runtime dir"


# ==========================================================================
# Behaviour 6 — clobber reuses the canonical marker-name constants (it does not
# hardcode names it can import), and resolves the runtime dir via
# run_tick.resolve_runtime_paths.
# ==========================================================================

def test_clobber_uses_canonical_marker_names():
    # The disposition + lock markers come from lifecycle-dispositions.
    assert ld._DISPOSITION_MARKER == "disposition"
    assert ld._LOCK_MARKER == "lock.json"
    # The heartbeat markers come from heartbeat.
    assert heartbeat._INTENT_MARKER == "loop-intent"
    assert heartbeat._RESUME_DEDUP_MARKER == "last-resume-session"


def test_clobber_written_disposition_and_intent_are_cleared():
    """A realistically-populated runtime (disposition marker via the lifecycle
    API + a loop-intent marker via heartbeat) is fully reset by clobber."""
    root = tempfile.mkdtemp(prefix="sched-clobber-real-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    heartbeat.record_loop_intent(runtime_dir)
    _touch(os.path.join(runtime_dir, "config.json"))
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED
    assert heartbeat.loop_intent_is_running(runtime_dir)
    rc = _run(runtime_dir, root, yes=True)
    assert rc == 0, rc
    # The disposition marker is gone -> reads default IDLE; intent cleared.
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE
    assert not heartbeat.loop_intent_is_running(runtime_dir)
    # Config preserved.
    assert os.path.exists(os.path.join(runtime_dir, "config.json"))


# ==========================================================================
# Behaviour 7 — the SHIPPED /auto-maintainer:clobber SKILL is real, well-formed
# (frontmatter + lifecycle metadata), is CONFIRMATION-guarded (destructive
# reset; reminds to /stop first), invokes the backing lib/clobber.py --yes
# (script-backed, no runtime-placeholder bash), and dispatches NO subagent.
# ==========================================================================

_SHIP_SKILL = os.path.join(_FEATURE_DIR, "ship", "skills", "clobber", "SKILL.md")
_REQUIRED_META = ("name", "description", "version", "owner",
                  "deprecation_criterion")


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _parse_frontmatter(path):
    text = _read(path)
    assert text.startswith("---\n"), ("missing frontmatter open", path)
    body = text[4:]
    end = body.index("\n---\n")
    fields = {}
    for line in body[:end].splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _body(path):
    text = _read(path)
    body = text[4:]
    end = body.index("\n---\n")
    return body[end + 5:]


def test_clobber_skill_exists_with_metadata():
    assert os.path.isfile(_SHIP_SKILL), _SHIP_SKILL
    fm = _parse_frontmatter(_SHIP_SKILL)
    for key in _REQUIRED_META:
        assert fm.get(key), (key, fm)
    assert fm["name"] == "clobber", fm
    # Repo-shipped feature -> the owner MUST be the rabbit-workflow team.
    assert fm["owner"] == "rabbit-workflow team", fm


def test_clobber_skill_is_confirmation_guarded_and_script_backed():
    body = _body(_SHIP_SKILL).lower()
    # Confirmation-guarded (destructive reset) + reminds to /stop first.
    assert "confirm" in body, "no confirmation surfaced"
    assert "/stop" in body or "/auto-maintainer:stop" in body, \
        "does not remind to /stop first"
    # Script-backed: invokes lib/clobber.py --yes.
    assert "clobber.py" in body, "does not invoke the backing script"
    assert "--yes" in body, "does not pass --yes on the confirmed path"


def test_clobber_skill_dispatches_no_subagent():
    body = _body(_SHIP_SKILL)
    assert "Agent(" not in body, "clobber must dispatch no subagent"
    assert "subagent_type" not in body, "clobber must dispatch no subagent"


def test_clobber_skill_keys_stop_advice_on_loop_intent_not_disposition():
    """The SKILL keys its /stop-first recommendation on the loop_intent_present
    flag surfaced by clobber.py, NOT on disposition. It must reference the flag
    AND must NOT carry the retired vague 'if the loop may be running' wording."""
    body = _body(_SHIP_SKILL)
    assert "loop_intent_present" in body, (
        "SKILL must key /stop-first advice on the loop_intent_present flag")
    assert "if the loop may be running" not in body.lower(), (
        "the vague disposition-agnostic 'if the loop may be running' wording "
        "must be removed in favor of the loop_intent_present flag")


# ==========================================================================
# Behaviour 8 — the machine-first summary carries a loop_intent_present bool,
# read from heartbeat's durable loop-intent marker BEFORE deletion. The clobber
# SKILL keys its '/stop first' recommendation on this flag, NOT on disposition.
# ==========================================================================

def _summary_from_run(runtime_dir, root, yes):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(runtime_dir, root, yes=yes)
    assert rc == 0, rc
    out = buf.getvalue()
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


def test_summary_carries_loop_intent_present_true_with_marker():
    """A /start-armed runtime (durable loop-intent marker present) reports
    loop_intent_present == True, on both the dry-run and the apply paths."""
    root = tempfile.mkdtemp(prefix="sched-clobber-lip-true-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    heartbeat.record_loop_intent(runtime_dir)
    assert heartbeat.loop_intent_is_running(runtime_dir)
    # Dry-run: flag present and True, deletes nothing.
    summary = _summary_from_run(runtime_dir, root, yes=False)
    assert "loop_intent_present" in summary, summary
    assert summary["loop_intent_present"] is True, summary
    assert heartbeat.loop_intent_is_running(runtime_dir), "dry-run deleted intent"
    # Apply: flag read BEFORE deletion, so still reported True even though the
    # marker is removed by this same run.
    summary = _summary_from_run(runtime_dir, root, yes=True)
    assert summary["loop_intent_present"] is True, summary
    assert not heartbeat.loop_intent_is_running(runtime_dir), "intent not cleared"


def test_summary_loop_intent_present_false_for_tick_only_session():
    """A /tick-only / paused-tick runtime — disposition==RUNNING and a tick
    checkpoint present, but NO durable loop-intent marker (never /start-ed) —
    reports loop_intent_present == False. disposition==RUNNING must NOT flip the
    flag; only the loop-intent marker does."""
    root = tempfile.mkdtemp(prefix="sched-clobber-lip-tick-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    # Simulate a tick mid-flight: RUNNING disposition + a durable checkpoint,
    # but the human never /start-ed the loop (no loop-intent marker).
    ld.write_disposition(runtime_dir, ld.Disposition.RUNNING)
    _touch(os.path.join(runtime_dir, "durable-state.json"))
    assert ld.read_disposition(runtime_dir) == ld.Disposition.RUNNING
    assert not heartbeat.loop_intent_is_running(runtime_dir)
    summary = _summary_from_run(runtime_dir, root, yes=True)
    assert summary["loop_intent_present"] is False, summary


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\nall {len(fns)} passed")
