#!/usr/bin/env python3
"""observability — the loop's surfacing layer (DESIGN §3.9), slice 1.

Two surfaces, both deterministic given their injected seams:

  1. Structured event log (§3.9.1) — an append-only, versioned, tail-able JSONL
     log: the machine-first source of truth for "what did the loop do". The
     writer assigns a per-log monotonic `seq` (the current line count of the
     file), stamps `ts` from an INJECTED clock (`now`), validates `kind`
     against the closed vocabulary, and appends one JSON object per line.
     read()/tail(n) parse the JSONL back to event dicts.

  2. Escalation channel (§3.9.3) — escalate(target_ref, message, ...) posts an
     issue-comment on the TRIGGERING issue via an INJECTABLE sink (default: the
     live `gh issue comment` runner). The posted body is provenance-stamped
     (`filed_by: autonomous-maintainer`). Escalation never creates a new tracked
     item (that is REPORT / outbound-report) and never raises on sink failure —
     it returns {ok:False, detail:...} so a failed escalation cannot crash the
     loop.

Determinism (spec Invariants): the lib never reads the wall clock implicitly
(`ts` comes only from the injected `now`); the live `gh` sink is the injectable
seam, never invoked from tests; it imports no route/dispatch/Agent mechanism
and decides no control flow.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the event-log schema or escalation
  contract reaches a breaking major version, or when surfacing moves to a
  different sink than a local JSONL log + tracker issue-comment. See
  docs/spec.md.
"""

import json
import os

# The versioned event-log schema (machine-first; bumped on a breaking change to
# the field set). Distinct from the feature version.
EVENT_SCHEMA_VERSION = "1.0.0"

# Closed `kind` vocabulary (v1). An append with a kind outside this set raises.
EVENT_KINDS = {
    "tick_start", "state_run", "signal", "pause", "dispatch",
    "resume", "disposition", "tick_end", "escalation",
}

# The provenance line stamped onto every escalation body so a human reading the
# triggering issue knows the comment came from the autonomous maintainer.
_PROVENANCE_LINE = "filed_by: autonomous-maintainer"


class EventLog:
    """An append-only JSONL event log at `path`. The writer assigns a per-log
    monotonic `seq` (the current line count) so ordering survives across
    processes; `read`/`tail` round-trip what `append` wrote."""

    def __init__(self, path):
        self.path = path

    def _line_count(self):
        """The current number of lines in the log (0 when absent). This IS the
        next `seq` — reading it per-append keeps seq monotonic across separate
        EventLog instances / processes on the same path."""
        if not os.path.exists(self.path):
            return 0
        with open(self.path, "r") as fh:
            return sum(1 for _ in fh)

    def append(self, kind, tick_id, *, state=None, signal=None, detail=None,
               now=None):
        """Validate `kind`, assign the next `seq`, stamp `ts` from `now`, and
        append one JSON object per line. Creates the file/dir if missing.
        Raises ValueError on an unknown `kind`."""
        if kind not in EVENT_KINDS:
            raise ValueError(f"unknown event kind: {kind!r}")

        ts = now.isoformat() if now is not None else None
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "seq": self._line_count(),
            "ts": ts,
            "tick_id": tick_id,
            "kind": kind,
            "state": state,
            "signal": signal,
            "detail": detail,
        }

        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(self.path, "a") as fh:
            fh.write(json.dumps(event) + "\n")
        return event

    def read(self):
        """Parse the JSONL log back to a list of event dicts. Empty list when
        the file is absent."""
        if not os.path.exists(self.path):
            return []
        events = []
        with open(self.path, "r") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events

    def tail(self, n):
        """The last `n` events, in order."""
        return self.read()[-n:]


def gh_comment_sink(target_ref, body):
    """The DEFAULT live escalation sink: post an issue-comment via the `gh` CLI.

    `target_ref` is `owner/repo#N`. Shells
    `gh issue comment <N> --repo <owner/repo> --body <body>`. This is the
    injectable seam; tests pass a stub sink instead and never invoke this.
    `subprocess` is imported lazily so importing this module never shells `gh`.
    """
    import subprocess

    repo, _, num = target_ref.partition("#")
    out = subprocess.run(
        ["gh", "issue", "comment", num, "--repo", repo, "--body", body],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def escalate(target_ref, message, *, sink=None, now=None):
    """Post an issue-comment on the triggering issue `target_ref` via `sink`.

    The body is provenance-stamped with `filed_by: autonomous-maintainer`. On
    sink success returns {target_ref, ok:True, detail:<sink return>}; on sink
    exception returns {target_ref, ok:False, detail:<reason>} WITHOUT raising —
    a failed escalation must never crash the loop. When `now` is provided its
    ISO timestamp is included in the body; the lib never reads the wall clock
    itself.
    """
    if sink is None:
        sink = gh_comment_sink

    lines = [message, "", _PROVENANCE_LINE]
    if now is not None:
        lines.append(f"filed_at: {now.isoformat()}")
    body = "\n".join(lines)

    try:
        detail = sink(target_ref, body)
        return {"target_ref": target_ref, "ok": True, "detail": detail}
    except Exception as exc:
        return {"target_ref": target_ref, "ok": False, "detail": str(exc)}
