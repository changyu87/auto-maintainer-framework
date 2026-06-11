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
import fsm_contracts as fc


# The versioned WorkItem schema (machine-first; bumped on a breaking change to
# the field set). Slot-schema version, distinct from the feature version.
WORK_ITEM_SCHEMA_VERSION = "1.0.0"


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


def parse_gh_issues(json_text):
    """Map a `gh issue list --json ...` payload string to a list of WorkItems.

    Expects gh's shape: author is an object with a `login`, labels a list of
    objects with a `name`, timestamps `createdAt`/`updatedAt` (camelCase).
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
        ))
    return items


_GH_JSON_FIELDS = (
    "number,title,body,url,state,labels,author,createdAt,updatedAt")


def gh_issue_source(repo=None):
    """Production issue source: shell the deterministic `gh` CLI for OPEN issues
    and parse its JSON into WorkItems. `gh` carries its own auth. When `repo` is
    given it is passed via `--repo`; otherwise gh resolves the repo from the
    project default / git remote."""
    cmd = ["gh", "issue", "list", "--state", "open", "--json", _GH_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return parse_gh_issues(out.stdout)


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
        writes = {"work_items": [item.to_dict() for item in items]}
        signal = "OK" if items else "EMPTY"
        return fc.StateResult(signal=signal, writes=writes)


# ==========================================================================
# Slice 2 — TRIAGE: the deterministic validity gate (work_items -> work_orders)
# ==========================================================================

# The versioned WorkOrder schema (machine-first; bumped on a breaking change to
# the field set). Distinct from both the feature version and WorkItem's version.
WORK_ORDER_SCHEMA_VERSION = "1.0.0"


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
            ))
        writes = {"work_orders": [order.to_dict() for order in accepted]}
        signal = "OK" if accepted else "EMPTY"
        return fc.StateResult(signal=signal, writes=writes)
