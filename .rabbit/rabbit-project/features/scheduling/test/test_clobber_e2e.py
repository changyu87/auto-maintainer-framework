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

UX (the clobber-preview slice): the user-facing `--yes` flag is REPLACED. The
script's surface is now PREVIEW (default, no flag — deletes NOTHING) vs `--apply`
(the internal apply flag that actually deletes). Both modes emit a MACHINE-FIRST
structured payload the SKILL renders as a table:

  {mode: 'preview'|'applied',
   artifacts: [{name, path, exists, action: 'would-remove'|'removed'|'absent'}],
   preserved: [names],
   loop_intent_present: bool}

The verbatim-`yes` confirmation gate is a SKILL-owned conversational
confirmation, NOT a CLI flag; the SKILL maps a user `--no-dry-run` request to
invoking `clobber.py --apply` directly (no gate).

It resolves the runtime dir via run_tick.resolve_runtime_paths (never
duplicating path logic), is idempotent (a missing artifact is a no-op / action
'absent'), NEVER creates the runtime dir, and prints the machine-first payload.
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


def _run(runtime_dir, project_dir, apply):
    argv = ["--runtime-dir", runtime_dir, "--project-dir", project_dir]
    if apply:
        argv.append("--apply")
    return clobber.main(argv)


def _summary_from_run(runtime_dir, root, apply):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _run(runtime_dir, root, apply=apply)
    assert rc == 0, rc
    out = buf.getvalue()
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


def _actions_by_name(summary):
    return {a["name"]: a["action"] for a in summary["artifacts"]}


# ==========================================================================
# Behaviour 1 — --apply removes every runtime-state artifact and PRESERVES config.
# ==========================================================================

def test_clobber_apply_removes_state_preserves_config():
    root = tempfile.mkdtemp(prefix="sched-clobber-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    rc = _run(runtime_dir, root, apply=True)
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
# Behaviour 2 — the payload is machine-first {mode, artifacts, preserved,
# loop_intent_present}; on --apply mode=='applied' and each present artifact's
# action is 'removed'; config is NEVER an artifact.
# ==========================================================================

def test_clobber_apply_payload_shape():
    root = tempfile.mkdtemp(prefix="sched-clobber-sum-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    summary = _summary_from_run(runtime_dir, root, apply=True)
    assert summary["mode"] == "applied", summary
    assert "artifacts" in summary and "preserved" in summary, summary
    assert "loop_intent_present" in summary, summary
    actions = _actions_by_name(summary)
    # Every present runtime-state artifact is marked removed.
    assert actions["durable-state.json"] == "removed", summary
    assert actions["dispatch-out"] == "removed", summary
    for name in _STATE_FILES:
        assert actions[name] == "removed", (name, summary)
    # Each artifact record carries name/path/exists/action.
    for a in summary["artifacts"]:
        assert set(a) >= {"name", "path", "exists", "action"}, a
        assert os.path.isabs(a["path"]), a
    # Config is NEVER an artifact (the delete set); it is in preserved.
    artifact_names = set(actions)
    for cfg in _CONFIG_FILES:
        assert cfg not in artifact_names, (cfg, summary)
        assert cfg in summary["preserved"], (cfg, summary)


# ==========================================================================
# Behaviour 3 — DEFAULT (no flag) is a PREVIEW: it deletes NOTHING (rc 0),
# mode=='preview', and each present artifact's action is 'would-remove'.
# ==========================================================================

def test_clobber_preview_deletes_nothing():
    root = tempfile.mkdtemp(prefix="sched-clobber-dry-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    summary = _summary_from_run(runtime_dir, root, apply=False)
    # Nothing was deleted (preview).
    for name in _STATE_FILES:
        assert os.path.exists(os.path.join(runtime_dir, name)), name
    assert os.path.exists(os.path.join(runtime_dir, "dispatch-out")), \
        "preview deleted dispatch-out"
    # The payload marks the mode and the would-remove actions.
    assert summary["mode"] == "preview", summary
    actions = _actions_by_name(summary)
    assert actions["durable-state.json"] == "would-remove", summary
    assert actions["dispatch-out"] == "would-remove", summary


# ==========================================================================
# Behaviour 4 — idempotent: a second --apply is a no-op (every artifact
# 'absent', rc 0), config still intact.
# ==========================================================================

def test_clobber_apply_is_idempotent():
    root = tempfile.mkdtemp(prefix="sched-clobber-idem-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    assert _run(runtime_dir, root, apply=True) == 0
    summary = _summary_from_run(runtime_dir, root, apply=True)
    # Nothing left to remove — every runtime-state artifact reads 'absent'.
    actions = _actions_by_name(summary)
    for name in _STATE_FILES:
        assert actions[name] == "absent", (name, summary)
    assert actions["dispatch-out"] == "absent", summary
    # Config is still there.
    for name in _CONFIG_FILES:
        assert os.path.exists(os.path.join(runtime_dir, name)), name


# ==========================================================================
# Behaviour 5 — clobber NEVER creates the runtime dir: an absent dir is a
# nothing-to-do no-op (rc 0), the dir stays absent, and the payload still
# carries the mode + all-absent artifacts.
# ==========================================================================

def test_clobber_never_creates_runtime_dir():
    root = tempfile.mkdtemp(prefix="sched-clobber-absent-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    # runtime_dir does NOT exist.
    assert not os.path.exists(runtime_dir)
    summary = _summary_from_run(runtime_dir, root, apply=True)
    # Still absent — clobber never created it.
    assert not os.path.exists(runtime_dir), "clobber created the runtime dir"
    assert summary["mode"] == "applied", summary
    actions = _actions_by_name(summary)
    for name in _STATE_FILES:
        assert actions[name] == "absent", (name, summary)


# ==========================================================================
# Behaviour 6 — the user-facing --yes flag is GONE. clobber.py rejects it (the
# apply is driven by the internal --apply flag the SKILL invokes).
# ==========================================================================

def test_clobber_rejects_retired_yes_flag():
    import io
    import contextlib
    root = tempfile.mkdtemp(prefix="sched-clobber-noyes-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    _populate(runtime_dir)
    raised = False
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err):
            clobber.main(["--yes", "--runtime-dir", runtime_dir,
                          "--project-dir", root])
    except SystemExit as e:
        raised = True
        assert e.code != 0, e.code
    assert raised, "clobber.py must reject the retired --yes flag"
    # And --yes must NOT have deleted anything.
    assert os.path.exists(os.path.join(runtime_dir, "durable-state.json"))


# ==========================================================================
# Behaviour 7 — clobber reuses the canonical marker-name constants (it does not
# hardcode names it can import), and a realistically-populated runtime is fully
# reset by --apply.
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
    API + a loop-intent marker via heartbeat) is fully reset by --apply."""
    root = tempfile.mkdtemp(prefix="sched-clobber-real-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    ld.write_disposition(runtime_dir, ld.Disposition.STOPPED)
    heartbeat.record_loop_intent(runtime_dir)
    _touch(os.path.join(runtime_dir, "config.json"))
    assert ld.read_disposition(runtime_dir) == ld.Disposition.STOPPED
    assert heartbeat.loop_intent_is_running(runtime_dir)
    rc = _run(runtime_dir, root, apply=True)
    assert rc == 0, rc
    # The disposition marker is gone -> reads default IDLE; intent cleared.
    assert ld.read_disposition(runtime_dir) == ld.Disposition.IDLE
    assert not heartbeat.loop_intent_is_running(runtime_dir)
    # Config preserved.
    assert os.path.exists(os.path.join(runtime_dir, "config.json"))


# ==========================================================================
# Behaviour 8 — the machine-first payload carries a loop_intent_present bool,
# read from heartbeat's durable loop-intent marker BEFORE deletion. The clobber
# SKILL keys its '/stop first' recommendation on this flag, NOT on disposition.
# ==========================================================================

def test_summary_carries_loop_intent_present_true_with_marker():
    """A /start-armed runtime (durable loop-intent marker present) reports
    loop_intent_present == True, on both the preview and the apply paths."""
    root = tempfile.mkdtemp(prefix="sched-clobber-lip-true-")
    runtime_dir = os.path.join(root, ".auto-maintainer")
    os.makedirs(runtime_dir, exist_ok=True)
    heartbeat.record_loop_intent(runtime_dir)
    assert heartbeat.loop_intent_is_running(runtime_dir)
    # Preview: flag present and True, deletes nothing.
    summary = _summary_from_run(runtime_dir, root, apply=False)
    assert summary["loop_intent_present"] is True, summary
    assert heartbeat.loop_intent_is_running(runtime_dir), "preview deleted intent"
    # Apply: flag read BEFORE deletion, so still reported True even though the
    # marker is removed by this same run.
    summary = _summary_from_run(runtime_dir, root, apply=True)
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
    summary = _summary_from_run(runtime_dir, root, apply=True)
    assert summary["loop_intent_present"] is False, summary


# ==========================================================================
# Behaviour 9 — the SHIPPED /auto-maintainer:clobber SKILL is real, well-formed
# (frontmatter + lifecycle metadata) and owned by the rabbit-workflow team.
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


# ==========================================================================
# Behaviour 10 — the SKILL implements the dry-run-preview-then-verbatim-`yes`
# UX by default, and a --no-dry-run immediate-delete path. The retired --yes
# flag is GONE from the SKILL; the internal --apply flag drives the delete.
# ==========================================================================

def test_clobber_skill_default_is_preview_then_verbatim_yes():
    body = _body(_SHIP_SKILL)
    low = body.lower()
    # The default path is a dry-run PREVIEW rendered as a table.
    assert "preview" in low, "SKILL must describe the default preview"
    assert "table" in low, "SKILL must render the payload as a table"
    # The gate is a conversational VERBATIM `yes`, owned by the SKILL.
    assert "verbatim" in low, "SKILL must require a verbatim confirmation"
    assert "`yes`" in body or "'yes'" in body or '"yes"' in body, \
        "SKILL must ask the user to type the verbatim word yes"
    assert "confirm" in low, "no confirmation surfaced"


def test_clobber_skill_has_no_dry_run_immediate_path():
    body = _body(_SHIP_SKILL)
    assert "--no-dry-run" in body, (
        "SKILL must document the --no-dry-run immediate-delete path")


def test_clobber_skill_is_script_backed_via_apply_flag():
    body = _body(_SHIP_SKILL)
    # Script-backed: invokes clobber.py, and the delete uses the --apply flag.
    assert "clobber.py" in body, "does not invoke the backing script"
    assert "--apply" in body, "does not pass --apply on the delete path"
    # The retired user-facing --yes flag must be gone.
    assert "--yes" not in body, "SKILL still references the retired --yes flag"


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
    assert "/stop" in body or "/auto-maintainer:stop" in body, \
        "does not remind to /stop first"
    assert "if the loop may be running" not in body.lower(), (
        "the vague disposition-agnostic 'if the loop may be running' wording "
        "must be removed in favor of the loop_intent_present flag")


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
