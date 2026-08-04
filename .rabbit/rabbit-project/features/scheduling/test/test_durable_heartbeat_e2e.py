#!/usr/bin/env python3
"""End-to-end tests for the durable heartbeat + SessionStart auto-resume (#31).

The maintainer loop is warm-only: its heartbeat is a session-scheduled prompt
that ends with the session. To make the heartbeat DURABLE across sessions
(DESIGN §3.3.2 heartbeat bootstrap) without a platform clock, the scheduling
feature persists a durable loop-intent marker and re-arms the in-session
heartbeat from a SessionStart hook on the next session, with at-most-one-refire
dedup across sessions.

Behaviours under test (every one deterministic, no session/clock/scheduler):

  1. /start records durable loop-intent; /stop clears it. The intent is the
     durable bit that survives the session ending.
  2. should_auto_resume is the pure decision: True only when intent=running AND
     the loop is not latched STOPPED/ABORTED/RESTART_NEEDED AND this session has
     not already armed the heartbeat.
  3. At-most-one-refire dedup: a single session arms at most once (mark_resumed),
     even when SessionStart fires multiple times in one session; a NEW session
     arms again.
  4. THE DEDUP-HOLE REGRESSION (the #31 core bug): the SessionStart hook tells the
     session to run /start; /start must NOT clear the resume-dedup, so a SECOND
     SessionStart in the SAME session does NOT re-arm a duplicate heartbeat. The
     full cross-session flow is: hook-arm -> /start -> 2nd SessionStart same
     session -> assert NO 2nd arm.
  5. The SessionStart hook (session-start-resume.py) emits the re-arm
     additionalContext exactly when should_auto_resume is True, and stays silent
     otherwise — and is registered in the shipped hooks.json.
  6. A latched STOPPED / ABORTED / owed RESTART is NEVER silently auto-resumed.

scheduling CONSUMES lifecycle-dispositions UNCHANGED; heartbeat.py only reads its
disposition marker via the public API.

Owner: changyu87
"""

import json
import os
import subprocess
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

import lifecycle_dispositions as ld  # noqa: E402
import heartbeat as hb  # noqa: E402
import work_intake as wi  # noqa: E402
import run_tick as rt  # noqa: E402
import start as sa  # noqa: E402
import stop as sp  # noqa: E402


GH_JSON_FIXTURE = """[
  {"number": 7, "title": "x", "body": "", "url": "u", "state": "OPEN",
   "labels": [], "author": {"login": "o"},
   "createdAt": "2026-05-01T10:00:00Z", "updatedAt": "2026-05-02T11:30:00Z"}
]"""


def _stub_source():
    items = wi.parse_gh_issues(GH_JSON_FIXTURE)

    def source(repo=None, issue_filter=None):
        return list(items)
    return source


def _paths():
    root = tempfile.mkdtemp(prefix="durhb-")
    runtime_dir = os.path.join(root, "runtime")
    state_path = os.path.join(root, "state.json")
    journal_path = os.path.join(root, "journal.jsonl")
    return runtime_dir, state_path, journal_path


# --------------------------------------------------------------------------
# 1. Durable loop-intent: set by start, cleared by stop.
# --------------------------------------------------------------------------

def test_loop_intent_marker_roundtrips():
    rd, _, _ = _paths()
    assert hb.read_loop_intent(rd) is None
    assert not hb.loop_intent_is_running(rd)
    hb.record_loop_intent(rd)
    assert hb.read_loop_intent(rd) == hb.INTENT_RUNNING
    assert hb.loop_intent_is_running(rd)
    hb.clear_loop_intent(rd)
    assert hb.read_loop_intent(rd) is None
    assert not hb.loop_intent_is_running(rd)


def test_clear_loop_intent_is_idempotent():
    rd, _, _ = _paths()
    hb.clear_loop_intent(rd)  # no marker yet — must not raise
    hb.record_loop_intent(rd)
    hb.clear_loop_intent(rd)
    hb.clear_loop_intent(rd)  # double clear — idempotent
    assert hb.read_loop_intent(rd) is None


def test_start_records_durable_loop_intent():
    """A clean /start durably records loop-intent=running so a future session
    auto-resumes the heartbeat."""
    rd, sp_, jp = _paths()
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    assert hb.loop_intent_is_running(rd)


def test_start_clear_only_records_durable_loop_intent():
    """--clear-only (the executor-model start) still arms the DURABLE heartbeat
    even though tick #1 is deferred to the executor."""
    rd, sp_, jp = _paths()

    def _exploding(repo=None):
        raise AssertionError("--clear-only must NOT run a tick")

    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_exploding, clear_only=True)
    assert hb.loop_intent_is_running(rd)


def test_start_after_stopped_re_records_intent():
    """/start after a /stop clears the STOPPED latch AND re-records the durable
    intent (a human resume re-arms the durable heartbeat)."""
    rd, sp_, jp = _paths()
    ld.write_disposition(rd, ld.Disposition.STOPPED)
    hb.clear_loop_intent(rd)
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    assert hb.loop_intent_is_running(rd)
    assert ld.read_disposition(rd) != ld.Disposition.STOPPED


def test_start_refused_on_aborted_does_not_record_intent():
    """A refused start (latched ABORTED) records NO intent — the fault must be
    investigated, never durably re-armed."""
    rd, sp_, jp = _paths()
    ld.write_disposition(rd, ld.Disposition.ABORTED)

    def _exploding(repo=None):
        raise AssertionError("must not tick on ABORTED")

    raised = False
    try:
        sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
                 source=_exploding)
    except sa.StartRefused:
        raised = True
    assert raised
    assert hb.read_loop_intent(rd) is None


def test_stop_clears_durable_loop_intent_end_to_end():
    """stop.py latches STOPPED AND clears the durable loop-intent so a future
    session does NOT auto-resume."""
    project_dir = tempfile.mkdtemp(prefix="durhb-proj-")
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = project_dir
    try:
        sa.start(source=_stub_source())  # records intent
        rd, _, _ = rt.resolve_runtime_paths()
        assert hb.loop_intent_is_running(rd)
        sp.stop()
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old
    assert ld.read_disposition(rd) == ld.Disposition.STOPPED
    assert not hb.loop_intent_is_running(rd)


# --------------------------------------------------------------------------
# 2 + 6. should_auto_resume — the pure decision; latches block auto-resume.
# --------------------------------------------------------------------------

def test_should_auto_resume_true_when_intent_running_and_idle():
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)  # default disposition IDLE
    assert hb.should_auto_resume(rd, "sess-1") is True


def test_should_auto_resume_false_without_intent():
    rd, _, _ = _paths()
    assert hb.should_auto_resume(rd, "sess-1") is False


def test_should_auto_resume_false_when_stopped_latched():
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    ld.write_disposition(rd, ld.Disposition.STOPPED)
    assert hb.should_auto_resume(rd, "sess-1") is False


def test_should_auto_resume_false_when_aborted_latched():
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    ld.write_disposition(rd, ld.Disposition.ABORTED)
    assert hb.should_auto_resume(rd, "sess-1") is False


def test_should_auto_resume_false_when_restart_owed():
    """A latched RESTART_NEEDED BLOCKS auto-resume (the safe choice). This is the
    conservative gate — it does NOT implement the §3.3.4 RESTART_NEEDED->resume
    *drive* flow (still deferred); it only guarantees a restart-owed loop is
    never silently re-armed."""
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    ld.write_disposition(rd, ld.Disposition.RESTART_NEEDED)
    assert hb.should_auto_resume(rd, "sess-1") is False


def test_should_auto_resume_true_when_running_disposition():
    """RUNNING (mid-loop) with intent still auto-resumes a fresh session."""
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    ld.write_disposition(rd, ld.Disposition.RUNNING)
    assert hb.should_auto_resume(rd, "sess-1") is True


# --------------------------------------------------------------------------
# 3. At-most-one-refire dedup across sessions.
# --------------------------------------------------------------------------

def test_dedup_arms_at_most_once_per_session():
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    # First SessionStart of session-1 arms; mark it.
    assert hb.should_auto_resume(rd, "sess-1") is True
    hb.mark_resumed(rd, "sess-1")
    # A second SessionStart in the SAME session does NOT re-arm.
    assert hb.should_auto_resume(rd, "sess-1") is False


def test_dedup_allows_a_new_session_to_arm():
    rd, _, _ = _paths()
    hb.record_loop_intent(rd)
    hb.mark_resumed(rd, "sess-1")
    assert hb.should_auto_resume(rd, "sess-1") is False
    # A DIFFERENT session arms again (the loop resumes in each new session).
    assert hb.should_auto_resume(rd, "sess-2") is True


# --------------------------------------------------------------------------
# 4. THE DEDUP-HOLE REGRESSION (the #31 core bug).
# --------------------------------------------------------------------------

def test_start_does_not_clear_resume_dedup():
    """/start must NOT clear the cross-session resume-dedup.

    The SessionStart hook is what asks the session to run /start; if /start
    cleared the dedup, a SECOND SessionStart in the same session would re-arm a
    DUPLICATE heartbeat. So record_loop_intent leaves the dedup exactly as the
    hook left it."""
    rd, sp_, jp = _paths()
    hb.record_loop_intent(rd)
    hb.mark_resumed(rd, "sess-1")
    assert hb.read_resume_dedup(rd) == "sess-1"
    # /start re-records the intent but must leave the dedup stamp untouched.
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    assert hb.read_resume_dedup(rd) == "sess-1"


def test_hook_arm_then_start_then_2nd_sessionstart_no_duplicate_arm():
    """The full #31 core-bug flow: hook arms (1st SessionStart) -> the session
    runs /start (per the hook's instruction) -> a 2nd SessionStart in the SAME
    session must NOT re-arm.

    SessionStart fires on startup / resume / /clear / compact, so a single
    session can see several SessionStart events. The dedup must hold across
    /start: the 2nd SessionStart returns False (no duplicate heartbeat)."""
    rd, sp_, jp = _paths()
    hb.record_loop_intent(rd)
    sess = "sess-A"
    # 1st SessionStart of this session arms (the hook decides True + stamps).
    assert hb.should_auto_resume(rd, sess) is True
    hb.mark_resumed(rd, sess)
    # The hook told the session to run /start; the session does so.
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    # A 2nd SessionStart in the SAME session must NOT re-arm a duplicate.
    assert hb.should_auto_resume(rd, sess) is False


def test_new_session_still_arms_after_start_in_prior_session():
    """The dedup is per-session: even though /start ran in sess-A (and did not
    clear the dedup), a genuinely NEW session sess-B still arms once."""
    rd, sp_, jp = _paths()
    hb.record_loop_intent(rd)
    hb.mark_resumed(rd, "sess-A")
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    assert hb.should_auto_resume(rd, "sess-A") is False
    assert hb.should_auto_resume(rd, "sess-B") is True


def test_stop_then_start_same_session_can_rearm():
    """/stop clears the dedup (ends the arming epoch), so a /start in the SAME
    session — a human resume without a restart — can be re-armed by the next
    SessionStart instead of being silenced by the stale stamp."""
    rd, sp_, jp = _paths()
    hb.record_loop_intent(rd)
    sess = "sess-A"
    hb.mark_resumed(rd, sess)
    assert hb.should_auto_resume(rd, sess) is False
    # Stop clears intent AND the dedup.
    hb.clear_loop_intent(rd)
    assert hb.read_resume_dedup(rd) is None
    # A human resume in the same session: /start re-records intent (dedup stays
    # cleared because stop cleared it), so the next SessionStart re-arms.
    sa.start(runtime_dir=rd, state_path=sp_, journal_path=jp,
             source=_stub_source())
    assert hb.should_auto_resume(rd, sess) is True


# --------------------------------------------------------------------------
# 5. The SessionStart hook — decision wiring + dedup, registered in hooks.json.
# --------------------------------------------------------------------------

def _hook_path():
    return os.path.join(_FEATURE_DIR, "ship", "hooks",
                        "session-start-resume.py")


def test_resume_hook_is_shipped():
    assert os.path.isfile(_hook_path()), _hook_path()


def test_resume_hook_emits_context_when_intent_running():
    """The hook emits the re-arm additionalContext when loop-intent is running
    and the loop is not latched, and stamps the dedup so it arms once."""
    project_dir = tempfile.mkdtemp(prefix="durhb-hook-")
    rd = os.path.join(project_dir, ".auto-maintainer")
    hb.record_loop_intent(rd)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    payload = json.dumps({"hookEventName": "SessionStart",
                          "session_id": "sess-A"})
    proc = subprocess.run([sys.executable, _hook_path()],
                          input=payload, capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    out = json.loads(proc.stdout)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "/auto-maintainer:start" in ctx, ctx
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    # The dedup was stamped for this session.
    assert hb.read_resume_dedup(rd) == "sess-A"


def test_resume_hook_silent_without_intent():
    """No loop-intent -> the hook emits nothing (the loop was stopped/never
    started, so do not auto-resume)."""
    project_dir = tempfile.mkdtemp(prefix="durhb-hook-")
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    payload = json.dumps({"hookEventName": "SessionStart",
                          "session_id": "sess-A"})
    proc = subprocess.run([sys.executable, _hook_path()],
                          input=payload, capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "", proc.stdout


def test_resume_hook_silent_when_stopped_latched():
    """A latched STOPPED is never silently auto-resumed even with intent set."""
    project_dir = tempfile.mkdtemp(prefix="durhb-hook-")
    rd = os.path.join(project_dir, ".auto-maintainer")
    hb.record_loop_intent(rd)
    ld.write_disposition(rd, ld.Disposition.STOPPED)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    payload = json.dumps({"session_id": "sess-A"})
    proc = subprocess.run([sys.executable, _hook_path()],
                          input=payload, capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)
    assert proc.stdout.strip() == "", proc.stdout


def test_resume_hook_dedups_within_a_session():
    """Running the hook twice for the same session arms only once (the second
    invocation is silent)."""
    project_dir = tempfile.mkdtemp(prefix="durhb-hook-")
    rd = os.path.join(project_dir, ".auto-maintainer")
    hb.record_loop_intent(rd)
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    payload = json.dumps({"session_id": "sess-A"})
    first = subprocess.run([sys.executable, _hook_path()], input=payload,
                           capture_output=True, text=True, env=env)
    second = subprocess.run([sys.executable, _hook_path()], input=payload,
                            capture_output=True, text=True, env=env)
    assert first.stdout.strip() != "", first.stdout
    assert second.stdout.strip() == "", second.stdout


def test_resume_hook_never_crashes_session_on_bad_stdin():
    """A hook must never break the session: malformed/empty stdin exits 0."""
    project_dir = tempfile.mkdtemp(prefix="durhb-hook-")
    env = dict(os.environ)
    env["CLAUDE_PROJECT_DIR"] = project_dir
    proc = subprocess.run([sys.executable, _hook_path()],
                          input="not json", capture_output=True, text=True,
                          env=env)
    assert proc.returncode == 0, (proc.returncode, proc.stderr)


def test_resume_hook_registered_in_shipped_hooks_json():
    """The shipped hooks.json (packaging-config plugin_assets) registers the
    resume hook as a SessionStart command so it actually runs."""
    hooks_json = os.path.join(
        _FEATURES, "packaging-config", "src", "plugin_assets", "hooks",
        "hooks.json")
    data = json.load(open(hooks_json))
    commands = [
        h["command"]
        for entry in data["hooks"]["SessionStart"]
        for h in entry["hooks"]
    ]
    assert any("session-start-resume.py" in c for c in commands), commands
