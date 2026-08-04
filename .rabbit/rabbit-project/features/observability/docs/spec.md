---
feature: observability
version: 0.1.0
owner: changyu87
deprecation_criterion: Superseded when the event-log schema or escalation contract reaches a breaking major version, or when surfacing moves to a different sink than a local JSONL log + tracker issue-comment.
---

# observability

## Purpose

The loop's **surfacing layer** (DESIGN §3.9): a machine-first record of *what the
loop did*, plus the channel to *escalate* to a human. **Slice 1** ships two
things:

- **Structured event log** (§3.9.1) — an append-only, versioned, tail-able
  JSONL log: the source of truth for "what did the loop do", readable by other
  sessions.
- **Escalation channel** (§3.9.3) — post an issue-comment on a *triggering*
  issue, via an injectable sink (default `gh`), so the loop can ask a human to
  look. This is the real sink that un-stubs `safety-governance`'s
  `abort_on_would_block` escalation (§3.8.3).

## Event log (this feature owns the schema)

Machine-first, versioned; append-only at
`${CLAUDE_PROJECT_DIR}/.auto-maintainer/events.jsonl` (one JSON object per line).

```json
{
  "schema_version": "1.0.0",
  "seq": 7,
  "ts": "<ISO-8601 or null>",
  "tick_id": "<id>",
  "kind": "<closed-vocab>",
  "state": "<state name | null>",
  "signal": "<signal | null>",
  "detail": { }
}
```

- `kind` is from a **closed vocabulary** (v1): `tick_start`, `state_run`,
  `signal`, `pause`, `dispatch`, `resume`, `disposition`, `tick_end`,
  `escalation`. Unknown kind raises.
- `seq` is a per-log monotonic integer (line order); the writer assigns it.
- `ts` comes from an **injectable clock** (`now`); tests pin it or pass `None`.
  Deterministic states never read the wall clock — the event writer is
  orchestration-layer (like the budget window), so it may stamp time via the
  injected `now`.

## Public surface (deterministic given inputs)

- `EVENT_SCHEMA_VERSION`, `EVENT_KINDS` (the closed set).
- `EventLog(path)`:
  - `append(kind, tick_id, *, state=None, signal=None, detail=None, now=None)`
    — validate `kind`, assign the next `seq`, stamp `ts` from `now`, append one
    JSON line. Raises on unknown `kind`.
  - `read()` / `tail(n)` — parse the JSONL back to a list of event dicts (for
    status views / other sessions).
- Escalation:
  - `escalate(target_ref, message, *, sink=None, now=None)` — post an
    issue-comment to `target_ref` (e.g. `owner/repo#N`) via `sink` (an injectable
    callable; default = a `gh issue comment` runner). The message is
    provenance-stamped (`filed_by: autonomous-maintainer`). Returns a structured
    result `{target_ref, ok, detail}`. Tests inject a stub sink (no network).

## Invariants

- Event log is **append-only** and machine-first (JSONL); a human-readable view
  is derived by a tool, never authored alongside (Machine-First §1).
- Deterministic given injected `now` + injected escalation `sink`: no implicit
  wall clock, no network in tests (the live `gh` sink is the injectable seam,
  mirroring work-intake's PULL source).
- Closed `kind` vocabulary; unknown raises.
- Escalation comments on the **triggering issue** only; it never creates new
  tracked items (that is REPORT / `outbound-report`).
- Bounded scope: owns the event-log schema/writer + the escalation channel;
  it does not walk the route, dispatch, or decide control flow.

## Deferred (NOT in this slice)

- **REPORT** port + `DiscoveredIssue`/`ReportResult` + new-item filing
  (§1.3, §3.11) → `outbound-report`.
- **Escalation idempotency/dedup** (avoid repeat comments) — a later refinement
  (§3.11.4-style dedup is REPORT's; v1 escalation posts a comment).
- **Richer sinks** (Slack/email/webhook, §3.9.4) → v2.
- **SessionStart banner/persona** (§3.9.2) already ships via packaging's
  session-start hook; not re-done here.
