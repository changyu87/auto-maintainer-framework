#!/usr/bin/env python3
"""work-intake — slice 1: the GitHub-Issues PULL adapter.

The read-side first adapter: each tick fetches the repo's OPEN issues into the
`work_items` slot of the fsm-contracts blackboard. This is the first real
maintainer work, replacing the `DEMO_WORK` stub.

Public surface (slice 1):
  - WorkItem            — the typed, versioned slot schema for a tracker item.
  - WORK_ITEMS_SLOT     — the fsm-contracts slot registration descriptor.
  - PULL_MANIFEST       — the PULL state's {reads, writes, emits} manifest.
  - PULL_SIGNALS        — the closed signal set PULL may emit (OK | EMPTY).
  - parse_gh_issues()   — map a `gh issue list --json ...` payload to WorkItems.
  - gh_issue_source()   — the production source: shells the deterministic gh CLI.
  - Pull                — the PULL state; run(TickContext) -> StateResult.

Public surface (slice 2 — TRIAGE validity gate):
  - WorkOrder           — the typed, versioned, decision-carrying slot schema.
  - WORK_ORDERS_SLOT    — the fsm-contracts slot registration descriptor.
  - TRIAGE_MANIFEST     — the TRIAGE state's {reads, writes, emits} manifest.
  - TRIAGE_SIGNALS      — the closed signal set TRIAGE may emit (OK | EMPTY).
  - Triage              — the TRIAGE state; run(TickContext) -> StateResult.

The only non-deterministic edge — the live `gh` call — sits behind an
INJECTABLE source (Pull(source=...)), so tests drive PULL with a stub over
fixture issues with no network (spec-rules §1: the failure is locatable to the
fetch boundary, never a flaky live call). TRIAGE applies a pure, deterministic
validity gate; its only time-dependent edge (staleness) sits behind an
INJECTABLE reference time (Triage(now=...)). Richer TRIAGE — dedup / decompose /
order / the WHAT-generation seam — is deferred to slice 3+.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the tracker-read model changes
  incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a
  breaking major version). See docs/spec.md.
"""

import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List

# fsm-contracts is a sibling feature; the consumer registers its own slots and
# builds StateResult/StateManifest from the contract layer. Tests inject it via
# sys.path exactly as the fsm-contracts tests do, so importing by module name
# resolves the sibling src/ on the path.
# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
import os  # noqa: E402
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fsm_contracts as fc


# The versioned WorkItem schema (machine-first; bumped on a breaking change to
# the field set). Slot-schema version, distinct from the feature version.
# 1.1.0: additive `comments` field (the issue's human follow-up discussion) so
# the triager + implementer see the full thread, not just the original body.
WORK_ITEM_SCHEMA_VERSION = "1.1.0"

# Bound on how many of an issue's comments ride along in the WorkItem so a long
# thread cannot bloat the rendered triager/implementer envelope. We keep the
# MOST RECENT N (the latest human guidance usually lives at the end of a thread)
# and cap each comment's body length. `gh issue view --json comments` already
# returns comments oldest-first, so "most recent N" is the trailing slice.
MAX_COMMENTS_PER_ITEM = 20
MAX_COMMENT_BODY_CHARS = 4000


@dataclass(eq=True)
class WorkItem:
    """The typed shape of a tracker item pulled from the issue tracker.

    Fields mirror the spec's slot schema. `labels` is a list of plain strings
    (the label names). `to_dict`/`from_dict` give a machine-first, versioned
    representation suitable for writing into the `work_items` blackboard slot.
    """

    id: str
    number: int
    title: str
    body: str
    url: str
    state: str
    labels: List[str] = field(default_factory=list)
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    # The issue's human follow-up discussion, each comment a machine-first
    # `{author, created_at, body}` dict, bounded (most-recent MAX_COMMENTS_PER_ITEM,
    # each body capped). The triager + implementer render these so the most
    # current guidance (which often lives in comments, not the body) is visible.
    comments: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": WORK_ITEM_SCHEMA_VERSION,
            "id": self.id,
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "state": self.state,
            "labels": list(self.labels),
            "author": self.author,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "comments": [dict(c) for c in self.comments],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            number=d["number"],
            title=d["title"],
            body=d["body"],
            url=d["url"],
            state=d["state"],
            labels=list(d.get("labels", [])),
            author=d.get("author", ""),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            comments=[dict(c) for c in d.get("comments", [])],
        )


# The fsm-contracts slot descriptor. `work_items` is an array slot (a list of
# WorkItem dicts); the slot version tracks the schema version.
WORK_ITEMS_SLOT = {
    "name": "work_items",
    "schema": {"type": "array"},
    "version": WORK_ITEM_SCHEMA_VERSION,
}

# Closed signal set PULL emits: OK when any items were found, else EMPTY.
PULL_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope contract): reads nothing, writes the
# work_items slot, emits OK | EMPTY.
PULL_MANIFEST = fc.StateManifest(reads=[], writes=["work_items"],
                                 emits=PULL_SIGNALS)


def _derive_id(url, number):
    """Derive a stable id `owner/repo#number` from the issue URL, falling back
    to `#number` when the URL is not the expected GitHub issues form."""
    marker = "github.com/"
    idx = url.find(marker)
    if idx != -1:
        tail = url[idx + len(marker):]
        parts = tail.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}#{number}"
    return f"#{number}"


def _normalize_comments(raw_comments):
    """Map gh's `comments` shape to the bounded machine-first WorkItem form.

    gh returns each comment as `{author: {login}, createdAt, body, ...}` in
    chronological (oldest-first) order. We keep the MOST RECENT
    MAX_COMMENTS_PER_ITEM (the trailing slice, where the latest human guidance
    lives) and cap each body at MAX_COMMENT_BODY_CHARS so a long thread cannot
    bloat the rendered envelope. Pure and deterministic.
    """
    out = []
    for comment in raw_comments or []:
        author = comment.get("author") or {}
        body = comment.get("body", "") or ""
        if len(body) > MAX_COMMENT_BODY_CHARS:
            body = body[:MAX_COMMENT_BODY_CHARS] + "\n... [comment truncated]"
        out.append({
            "author": author.get("login", ""),
            "created_at": comment.get("createdAt", ""),
            "body": body,
        })
    if len(out) > MAX_COMMENTS_PER_ITEM:
        out = out[-MAX_COMMENTS_PER_ITEM:]
    return out


def parse_gh_issues(json_text):
    """Map a `gh issue list --json ...` payload string to a list of WorkItems.

    Expects gh's shape: author is an object with a `login`, labels a list of
    objects with a `name`, timestamps `createdAt`/`updatedAt` (camelCase). When
    an issue carries a `comments` array (as `gh issue view --json comments`
    returns) it is normalized + bounded into the WorkItem's `comments` field;
    `gh issue list` does not return comments, so absent it stays empty.
    """
    raw = json.loads(json_text)
    items = []
    for issue in raw:
        author = issue.get("author") or {}
        labels = [lbl.get("name", "") for lbl in issue.get("labels") or []]
        number = issue["number"]
        url = issue.get("url", "")
        items.append(WorkItem(
            id=_derive_id(url, number),
            number=number,
            title=issue.get("title", ""),
            body=issue.get("body", "") or "",
            url=url,
            state=issue.get("state", ""),
            labels=labels,
            author=author.get("login", ""),
            created_at=issue.get("createdAt", ""),
            updated_at=issue.get("updatedAt", ""),
            comments=_normalize_comments(issue.get("comments")),
        ))
    return items


_GH_JSON_FIELDS = (
    "number,title,body,url,state,labels,author,createdAt,updatedAt")

# `gh issue list` does NOT return comments, so they are fetched per pulled issue
# via `gh issue view <n> --json comments`. Each gh comment carries author +
# createdAt + body (the fields _normalize_comments reads).
_GH_COMMENT_FIELDS = "comments"


def gh_issue_source(repo=None, runner=subprocess.run):
    """Production issue source: shell the deterministic `gh` CLI for OPEN issues
    and parse its JSON into WorkItems. `gh` carries its own auth. When `repo` is
    given it is passed via `--repo`; otherwise gh resolves the repo from the
    project default / git remote.

    `gh issue list` does not return comments, so for each pulled issue this also
    shells `gh issue view <number> --json comments` and attaches the bounded
    human-discussion thread to the WorkItem (so the triager + implementer see
    follow-up guidance, not just the original body). A per-issue comment fetch
    that fails is tolerated (the item keeps an empty `comments`) — a flaky
    comment read must never sink the whole PULL. The subprocess `runner` is
    INJECTABLE (defaulting to subprocess.run) so tests drive it without network.
    """
    cmd = ["gh", "issue", "list", "--state", "open", "--json", _GH_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    items = parse_gh_issues(out.stdout)
    for item in items:
        item.comments = _fetch_issue_comments(item.number, repo, runner)
    return items


def _fetch_issue_comments(number, repo=None, runner=subprocess.run):
    """Fetch + bound one issue's comments via `gh issue view <n> --json comments`.

    Returns the normalized, bounded comment list; on any error (the fetch is a
    best-effort enrichment, never load-bearing for PULL) returns an empty list.
    """
    cmd = ["gh", "issue", "view", str(number), "--json", _GH_COMMENT_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    try:
        out = runner(cmd, capture_output=True, text=True, check=True)
        payload = json.loads(out.stdout)
    except Exception:  # noqa: BLE001 — locate failure to the comment fetch
        return []
    return _normalize_comments(payload.get("comments"))


class Pull:
    """The PULL state. Fetches the repo's OPEN issues via the injectable source,
    maps each to a WorkItem, writes the `work_items` slot, and emits OK if any
    were found else EMPTY.

    The source is injectable (a callable `source(repo) -> list[WorkItem]`,
    defaulting to the production gh-shelling source) so tests pass a stub over
    fixture issues — the determinism seam.
    """

    def __init__(self, source=gh_issue_source, repo=None):
        self._source = source
        self._repo = repo

    def run(self, ctx):  # noqa: ARG002 — ctx is the fsm-contracts TickContext
        items = self._source(self._repo)
        # §3.11.5 loopback guard: EXCLUDE items the loop filed itself so they
        # never enter the pipeline — they stay open for human triage. This is
        # exclusion at PULL, NOT a TRIAGE reject (a reject would route to the
        # doer's close path and CLOSE the discovery, the opposite of intent).
        items = [item for item in items if not is_loop_filed(item)]
        writes = {"work_items": [item.to_dict() for item in items]}
        signal = "OK" if items else "EMPTY"
        return fc.StateResult(signal=signal, writes=writes)


# ==========================================================================
# Slice 2 — TRIAGE: the deterministic validity gate (work_items -> work_orders)
# ==========================================================================

# The versioned WorkOrder schema (machine-first; bumped on a breaking change to
# the field set). Distinct from both the feature version and WorkItem's version.
# 1.1.0: additive `comments` field, carried from the source WorkItem so the
# implementer (which reads work_orders, not work_items) sees the human thread.
WORK_ORDER_SCHEMA_VERSION = "1.1.0"


@dataclass(eq=True)
class WorkOrder:
    """A validated, decision-carrying item produced by TRIAGE from a WorkItem.

    `decision` is "accepted" or "rejected"; `reason` records why a rejected item
    was gated out (empty for accepted). `work_item_id` links back to the source
    WorkItem.id. `to_dict`/`from_dict` give a machine-first, versioned
    representation suitable for the `work_orders` blackboard slot.
    """

    id: str
    work_item_id: str
    title: str
    body: str
    url: str
    decision: str
    reason: str
    labels: List[str] = field(default_factory=list)
    created_at: str = ""
    # The source issue's human follow-up discussion, carried from the WorkItem
    # so the implementer (which reads work_orders) sees the full thread.
    comments: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": WORK_ORDER_SCHEMA_VERSION,
            "id": self.id,
            "work_item_id": self.work_item_id,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "labels": list(self.labels),
            "decision": self.decision,
            "reason": self.reason,
            "created_at": self.created_at,
            "comments": [dict(c) for c in self.comments],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            id=d["id"],
            work_item_id=d["work_item_id"],
            title=d["title"],
            body=d["body"],
            url=d["url"],
            labels=list(d.get("labels", [])),
            decision=d["decision"],
            reason=d.get("reason", ""),
            created_at=d.get("created_at", ""),
            comments=[dict(c) for c in d.get("comments", [])],
        )


# The fsm-contracts slot descriptor. `work_orders` is an array slot (a list of
# WorkOrder dicts); the slot version tracks the schema version.
WORK_ORDERS_SLOT = {
    "name": "work_orders",
    "schema": {"type": "array"},
    "version": WORK_ORDER_SCHEMA_VERSION,
}

# Closed signal set TRIAGE emits: OK when any item was accepted, else EMPTY.
TRIAGE_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope contract): reads work_items, writes
# work_orders, emits OK | EMPTY.
TRIAGE_MANIFEST = fc.StateManifest(reads=["work_items"], writes=["work_orders"],
                                   emits=TRIAGE_SIGNALS)

# The staleness window: an item whose `updated_at` is older than this many days
# relative to the reference time is gated out as stale. Hardcoded for slice 2;
# configuration is deferred (#17-style).
STALE_WINDOW_DAYS = 365


def _parse_iso(ts):
    """Parse a tracker ISO-8601 timestamp (e.g. `2026-05-02T11:30:00Z`) to an
    aware datetime, returning None when it cannot be parsed."""
    if not ts:
        return None
    text = ts.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class Triage:
    """The TRIAGE state. Reads the `work_items` slot, applies a deterministic
    validity gate to each WorkItem, maps each ACCEPTED item 1:1 to a
    WorkOrder(decision="accepted"), writes the `work_orders` slot, and emits OK
    if any were accepted else EMPTY.

    The validity gate (pure rules, no network, no AI):
      - well-formed: a non-empty title  (else rejected "malformed: no title");
      - in-scope:    state == "open"    (else rejected "not open");
      - not-stale:   updated_at within STALE_WINDOW_DAYS of the reference time
                     (else rejected "stale: ...").

    Staleness is the only time-dependent edge; it keys off an INJECTABLE
    reference time (`now`, defaulting to the current UTC time) so tests pin
    staleness deterministically (spec-rules §1).
    """

    def __init__(self, now=None):
        self._now = now if now is not None else datetime.now(timezone.utc)

    def classify(self, item):
        """Apply the validity gate to one WorkItem, returning (decision, reason)
        where decision is "accepted" or "rejected". Pure and deterministic."""
        if not (item.title and item.title.strip()):
            return ("rejected", "malformed: no title")
        if (item.state or "").lower() != "open":
            return ("rejected", "not open")
        updated = _parse_iso(item.updated_at)
        if updated is None:
            return ("rejected", "stale: missing or unparseable updated_at")
        if self._now - updated > timedelta(days=STALE_WINDOW_DAYS):
            return ("rejected", f"stale: not updated within "
                                f"{STALE_WINDOW_DAYS} days")
        return ("accepted", "")

    def run(self, ctx):
        raw = ctx.read("work_items") or []
        items = [WorkItem.from_dict(d) for d in raw]
        accepted = []
        for item in items:
            decision, _reason = self.classify(item)
            if decision != "accepted":
                continue
            accepted.append(WorkOrder(
                id=f"wo-{item.id}",
                work_item_id=item.id,
                title=item.title,
                body=item.body,
                url=item.url,
                labels=list(item.labels),
                decision="accepted",
                reason="",
                created_at=item.created_at,
                comments=[dict(c) for c in item.comments],
            ))
        writes = {"work_orders": [order.to_dict() for order in accepted]}
        signal = "OK" if accepted else "EMPTY"
        return fc.StateResult(signal=signal, writes=writes)


# ==========================================================================
# Slice 3 — REPORT: the outbound filing port (discoveries -> tracker items).
# ==========================================================================
#
# The write-side mirror of PULL. work-intake owns the inbound tracker I/O, so it
# also owns the outbound port: the DiscoveredIssue/ReportResult schemas, the
# default GitHub filing sink, and the pure file_discoveries orchestrator. REPORT
# is out-of-band — NOT a routed tick state; scheduling.run_tick flushes
# discoveries through it after the route runs (that wiring + the journaled
# idempotency + the trust-ladder GATE live in scheduling, NOT here).

# The versioned DiscoveredIssue schema (machine-first; bumped on a breaking
# change to the field set). Distinct from the feature + other slot versions.
DISCOVERED_ISSUE_SCHEMA_VERSION = "1.0.0"

# The provenance stamp, as ONE source of truth shared by the stamper
# (gh_issue_file_sink, which WRITES it) and the recognizer (is_loop_filed,
# which READS it):
#   - LOOP_FILED_LABEL is the label stamped on every loop filing;
#   - AM_DEDUP_MARKER_PREFIX is the prefix of the body marker, whose full text
#     is exactly `<!-- am-dedup:<dedup_key> -->`.
# Both must agree so a later PULL can recognize and EXCLUDE the loop's own
# filings (§3.11.5).
LOOP_FILED_LABEL = "filed-by:autonomous-maintainer"
AM_DEDUP_MARKER_PREFIX = "<!-- am-dedup:"

# The filing sink stamps this same label; keep the historical name as an alias
# so the stamper and recognizer share one constant.
FILED_BY_LABEL = LOOP_FILED_LABEL


def _am_dedup_marker(dedup_key):
    return f"{AM_DEDUP_MARKER_PREFIX}{dedup_key} -->"


def is_loop_filed(item):
    """Return True when `item` was filed by the maintainer loop itself.

    A loop filing carries the provenance stamp gh_issue_file_sink writes: the
    LOOP_FILED_LABEL label OR the AM_DEDUP_MARKER_PREFIX body marker. Either
    stamp alone is sufficient. `item` may be a WorkItem or a dict (the
    machine-first WorkItem form); pure and deterministic.

    PULL uses this to EXCLUDE loop-filed items so they never enter the pipeline
    — they stay open for human triage (this is exclusion, never a reject/close).
    """
    if isinstance(item, dict):
        labels = item.get("labels") or []
        body = item.get("body") or ""
    else:
        labels = getattr(item, "labels", None) or []
        body = getattr(item, "body", None) or ""
    if LOOP_FILED_LABEL in labels:
        return True
    return AM_DEDUP_MARKER_PREFIX in body


@dataclass(eq=True)
class DiscoveredIssue:
    """A discovery the maintainer wants durably tracked, filed through REPORT.

    `dedup_key` is a stable caller-supplied key making filing idempotent;
    `target` selects the destination tracker (`project` | `maintainer-self`);
    `filed_by` stamps the loop's provenance. `to_dict`/`from_dict` give a
    machine-first, versioned representation.
    """

    title: str
    body: str
    kind: str
    severity: str
    target: str
    dedup_key: str
    filed_by: str = "autonomous-maintainer"

    def to_dict(self):
        return {
            "schema_version": DISCOVERED_ISSUE_SCHEMA_VERSION,
            "title": self.title,
            "body": self.body,
            "kind": self.kind,
            "severity": self.severity,
            "target": self.target,
            "dedup_key": self.dedup_key,
            "filed_by": self.filed_by,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            title=d["title"],
            body=d["body"],
            kind=d["kind"],
            severity=d["severity"],
            target=d["target"],
            dedup_key=d["dedup_key"],
            filed_by=d.get("filed_by", "autonomous-maintainer"),
        )


@dataclass(eq=True)
class ReportResult:
    """The outcome of a filing batch (machine-first).

      - filed:            [{dedup_key, tracker_ref, url}] — newly filed items.
      - skipped_existing: [dedup_key] — keys already known (a no-op re-file).
      - errors:           [{dedup_key, reason}] — per-discovery sink failures.
    """

    filed: List[dict] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)


def gh_issue_file_sink(discovery, repo=None, runner=subprocess.run):
    """Production filing sink: shell `gh issue create` for one DiscoveredIssue
    and return {tracker_ref, url}. `gh` carries its own auth.

    The provenance label `filed-by:autonomous-maintainer` is stamped, and a
    `<!-- am-dedup:<dedup_key> -->` marker is appended to the body so a later
    PULL/TRIAGE (and a dedup re-scan) can recognize the filing. When `repo` is
    given it is passed via `--repo` (the caller chooses it from
    discovery.target); otherwise gh resolves the repo from the project default.

    The subprocess `runner` is INJECTABLE (defaulting to subprocess.run) so
    tests pass a fake — no network, the failure locatable to the file boundary.
    """
    body = discovery.body
    marker = _am_dedup_marker(discovery.dedup_key)
    body = f"{body}\n\n{marker}" if body else marker
    # `gh issue create --label <L>` FAILS if label L is absent in the repo, and
    # a fresh repo has no provenance label — so every filing errored (silently,
    # since file_discoveries catches sink errors). ENSURE the label exists first
    # via an idempotent `gh label create`. check=False: an "already exists"
    # non-zero exit is TOLERATED, never raised (the label simply already exists).
    label_cmd = ["gh", "label", "create", FILED_BY_LABEL,
                 "--description", "filed by the autonomous maintainer"]
    if repo:
        label_cmd += ["--repo", repo]
    runner(label_cmd, capture_output=True, text=True, check=False)
    cmd = ["gh", "issue", "create",
           "--title", discovery.title,
           "--body", body,
           "--label", FILED_BY_LABEL]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    url = out.stdout.strip().splitlines()[-1].strip()
    return {"tracker_ref": _derive_ref(url), "url": url}


def _derive_ref(url):
    """Derive a stable `owner/repo#number` ref from a created-issue URL,
    falling back to the raw URL when it is not the expected GitHub form."""
    marker = "github.com/"
    idx = url.find(marker)
    if idx != -1:
        tail = url[idx + len(marker):]
        parts = tail.strip("/").split("/")
        # .../<owner>/<repo>/issues/<number>
        if len(parts) >= 4 and parts[2] == "issues":
            return f"{parts[0]}/{parts[1]}#{parts[3]}"
    return url


def file_discoveries(discoveries, sink=gh_issue_file_sink, known_dedup_keys=()):
    """Pure orchestration over a batch of DiscoveredIssues.

    For each discovery: if its `dedup_key` is in `known_dedup_keys` it is
    recorded in `skipped_existing` with NO sink call (idempotent re-filing is a
    no-op); otherwise the injected `sink` is invoked and its {tracker_ref, url}
    recorded in `filed`. A sink exception is caught and recorded in `errors`
    (filing one bad discovery never aborts the batch).

    Deterministic given the injected sink + known set; performs no I/O of its
    own. The trust-ladder GATE is NOT here — it lives in scheduling.run_tick,
    which only calls this when filing is permitted.
    """
    known = set(known_dedup_keys)
    result = ReportResult()
    for discovery in discoveries:
        if discovery.dedup_key in known:
            result.skipped_existing.append(discovery.dedup_key)
            continue
        try:
            ref = sink(discovery)
        except Exception as exc:  # noqa: BLE001 — locate failure to the sink
            result.errors.append({
                "dedup_key": discovery.dedup_key,
                "reason": str(exc),
            })
            continue
        result.filed.append({
            "dedup_key": discovery.dedup_key,
            "tracker_ref": ref["tracker_ref"],
            "url": ref["url"],
        })
    return result
