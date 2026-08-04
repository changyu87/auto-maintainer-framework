#!/usr/bin/env python3
"""End-to-end + unit tests for the observability surfacing layer (DESIGN §3.9).

Two surfaces under test:

  1. Structured event log (§3.9.1) — an append-only, versioned, tail-able JSONL
     log. The e2e tests drive EventLog exactly as the loop will: append a series
     of events under an injected `now`, then read/tail them back and assert the
     round-trip carries the right schema_version, monotonic seq, and the
     kind/state/signal/detail payload. seq monotonicity is asserted to survive
     across two EventLog instances on the same path (proving cross-process line
     ordering). Unknown `kind` raises ValueError (closed vocabulary).

  2. Escalation channel (§3.9.3) — escalate() posts an issue-comment on the
     triggering issue via an INJECTED stub sink (never the live gh). The posted
     body carries the message and the `filed_by: autonomous-maintainer`
     provenance stamp; success returns ok:True; a sink that raises returns
     ok:False without raising (escalation failure must not crash the loop).

Determinism (spec Invariants): the lib never reads the wall clock implicitly
(ts comes only from the injected `now`), never touches the network in tests
(the sink is injected), and never imports a route/dispatch mechanism.

Owner: changyu87
"""

import json
import os
import sys
from datetime import datetime, timezone

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

import observability as obs  # noqa: E402


# A fixed tz-aware instant injected wherever a clock is needed.
_NOW = datetime(2026, 6, 10, 12, 30, 0, tzinfo=timezone.utc)
_NOW_ISO = _NOW.isoformat()


def _tmp_log(tmpname="events.jsonl"):
    """A unique log path under the test dir; cleaned by the caller. Uses a
    fresh subdir so EventLog must create the dir itself (spec: create dir if
    missing)."""
    import tempfile
    d = tempfile.mkdtemp(prefix="obs-test-")
    return os.path.join(d, "sub", tmpname)


# ==========================================================================
# Behaviour: the public surface exposes the closed vocab + schema version.
# ==========================================================================

def test_event_schema_version_and_kinds_are_closed():
    assert obs.EVENT_SCHEMA_VERSION == "1.0.0"
    assert obs.EVENT_KINDS == {
        "tick_start", "state_run", "signal", "pause", "dispatch",
        "resume", "disposition", "tick_end", "escalation",
    }


# ==========================================================================
# E2E Behaviour: append then read round-trips events with the right
# schema_version, monotonic seq (0,1,2...), kind/state/signal/detail, and the
# ts equal to the injected now's ISO string.
# ==========================================================================

def test_e2e_append_then_read_roundtrips_with_monotonic_seq():
    path = _tmp_log()
    log = obs.EventLog(path)

    log.append("tick_start", "tick-1", now=_NOW)
    log.append("state_run", "tick-1", state="PULL", now=_NOW)
    log.append("signal", "tick-1", state="PULL", signal="OK",
               detail={"count": 3}, now=_NOW)

    events = log.read()
    assert len(events) == 3

    assert [e["seq"] for e in events] == [0, 1, 2]
    assert all(e["schema_version"] == "1.0.0" for e in events)
    assert all(e["tick_id"] == "tick-1" for e in events)
    assert all(e["ts"] == _NOW_ISO for e in events)

    assert events[0]["kind"] == "tick_start"
    assert events[0]["state"] is None
    assert events[0]["signal"] is None

    assert events[1]["kind"] == "state_run"
    assert events[1]["state"] == "PULL"

    assert events[2]["kind"] == "signal"
    assert events[2]["signal"] == "OK"
    assert events[2]["detail"] == {"count": 3}


# ==========================================================================
# E2E Behaviour: when now=None the ts field is null (the lib NEVER calls the
# wall clock itself).
# ==========================================================================

def test_e2e_append_with_no_now_stamps_null_ts():
    path = _tmp_log()
    log = obs.EventLog(path)
    log.append("tick_end", "tick-9")  # now omitted -> None
    events = log.read()
    assert len(events) == 1
    assert events[0]["ts"] is None
    assert events[0]["kind"] == "tick_end"


# ==========================================================================
# Behaviour: unknown kind raises ValueError (closed vocabulary).
# ==========================================================================

def test_append_unknown_kind_raises_valueerror():
    path = _tmp_log()
    log = obs.EventLog(path)
    raised = False
    try:
        log.append("not_a_kind", "tick-1", now=_NOW)
    except ValueError:
        raised = True
    assert raised is True
    # The bad append wrote nothing.
    assert log.read() == []


# ==========================================================================
# E2E Behaviour: seq survives across two EventLog instances on the same path —
# monotonic continues (proves cross-process ordering via line count).
# ==========================================================================

def test_e2e_seq_monotonic_across_instances_same_path():
    path = _tmp_log()

    log_a = obs.EventLog(path)
    log_a.append("tick_start", "tick-1", now=_NOW)
    log_a.append("state_run", "tick-1", state="PULL", now=_NOW)

    # A fresh instance on the SAME path must continue the seq, not reset it.
    log_b = obs.EventLog(path)
    log_b.append("tick_end", "tick-1", now=_NOW)

    events = obs.EventLog(path).read()
    assert [e["seq"] for e in events] == [0, 1, 2]
    assert events[2]["kind"] == "tick_end"


# ==========================================================================
# E2E Behaviour: tail(n) returns the last n events, in order.
# ==========================================================================

def test_e2e_tail_returns_last_n_in_order():
    path = _tmp_log()
    log = obs.EventLog(path)
    for i in range(5):
        log.append("state_run", "tick-1", state=f"S{i}", now=_NOW)

    last2 = log.tail(2)
    assert [e["state"] for e in last2] == ["S3", "S4"]
    assert [e["seq"] for e in last2] == [3, 4]

    # tail larger than the log returns everything.
    allev = log.tail(99)
    assert len(allev) == 5


# ==========================================================================
# Behaviour: read() on an absent file returns an empty list.
# ==========================================================================

def test_read_absent_file_returns_empty_list():
    path = _tmp_log()
    log = obs.EventLog(path)
    assert log.read() == []
    assert log.tail(3) == []


# ==========================================================================
# E2E Behaviour: escalate with a stub sink posts an issue-comment on the
# triggering ref; the sink receives target_ref + a body carrying the message
# AND the `filed_by: autonomous-maintainer` provenance line; returns ok:True.
# ==========================================================================

def test_e2e_escalate_with_stub_sink_provenance_and_ok():
    received = {}

    def stub_sink(target_ref, body):
        received["target_ref"] = target_ref
        received["body"] = body
        return "https://github.com/acme/widget/issues/52#comment-1"

    result = obs.escalate(
        "acme/widget#52", "budget exceeded — please /start",
        sink=stub_sink, now=_NOW)

    assert result["target_ref"] == "acme/widget#52"
    assert result["ok"] is True
    assert result["detail"] == "https://github.com/acme/widget/issues/52#comment-1"

    # The sink saw the triggering ref and a provenance-stamped body.
    assert received["target_ref"] == "acme/widget#52"
    assert "budget exceeded — please /start" in received["body"]
    assert "filed_by: autonomous-maintainer" in received["body"]


# ==========================================================================
# E2E Behaviour: escalate when the stub sink raises -> returns
# {ok:False, detail:...}, does NOT raise (escalation failure never crashes
# the loop).
# ==========================================================================

def test_e2e_escalate_sink_failure_returns_not_ok_does_not_raise():
    def failing_sink(target_ref, body):
        raise RuntimeError("gh exploded")

    result = obs.escalate(
        "acme/widget#7", "look at this", sink=failing_sink, now=_NOW)

    assert result["target_ref"] == "acme/widget#7"
    assert result["ok"] is False
    assert "gh exploded" in result["detail"]


# ==========================================================================
# Invariant: the module does not pull in a route/dispatch/Agent mechanism, and
# does not run gh at import time (the gh sink is the default seam, only invoked
# when used). Tests always run on the injected stub.
# ==========================================================================

def test_module_imports_no_dispatch_or_route_mechanism():
    src = os.path.join(_SRC, "observability.py")
    with open(src, "r") as fh:
        text = fh.read()
    # No control-flow / dispatch imports.
    for forbidden in ("import route", "import dispatch", "import scheduling",
                      "Agent("):
        assert forbidden not in text, f"unexpected reference: {forbidden}"
    # The default gh sink exists as a named seam.
    assert hasattr(obs, "gh_comment_sink")
    assert callable(obs.gh_comment_sink)
