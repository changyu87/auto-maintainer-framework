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

The only non-deterministic edge — the live `gh` call — sits behind an
INJECTABLE source (Pull(source=...)), so tests drive PULL with a stub over
fixture issues with no network (spec-rules §1: the failure is locatable to the
fetch boundary, never a flaky live call). TRIAGE / dedup / decompose / order /
work_orders are deferred to slice 2.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the tracker-read model changes
  incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a
  breaking major version). See docs/spec.md.
"""

import json
import subprocess
from dataclasses import dataclass, field
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
