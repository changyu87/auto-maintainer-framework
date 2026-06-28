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
  - CrossCuttingRisk    — the typed, versioned cross-cutting-risk slot schema.
  - CROSS_CUTTING_RISK_SLOT — the fsm-contracts slot registration descriptor.
  - normalize_cross_cutting_risk() — pure normalizer/validator for the batch
    annotation (DESIGN §3.5.9; risk only on >=2 distinct features + a reason).
  - target_features_for() — pure detection of a WorkItem's blast-radius target
    feature(s) from authoritative signals (prefixed labels, a Component: body
    line, a conventional title prefix); TRIAGE stamps the result onto the
    WorkOrder's `target_feature` field so PRIORITIZE reads an authoritative
    field instead of re-scraping (issue #258).

The only non-deterministic edge — the live `gh` call — sits behind an
INJECTABLE source (Pull(source=...)), so tests drive PULL with a stub over
fixture issues with no network (spec-rules §1: the failure is locatable to the
fetch boundary, never a flaky live call). TRIAGE applies a pure, deterministic
validity gate; its only time-dependent edge (staleness) sits behind an
INJECTABLE reference time (Triage(now=...)). Richer TRIAGE — dedup / decompose /
order / the WHAT-generation seam — is deferred to slice 3+.

Version: 0.5.0
Owner: changyu87
Deprecation criterion: Superseded when the tracker-read model changes
  incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a
  breaking major version). See docs/spec.md.
"""

import json
import re
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

    def __init__(self, source=gh_issue_source, repo=None, work_own_filings=True):
        self._source = source
        self._repo = repo
        self._work_own_filings = work_own_filings

    def run(self, ctx):  # noqa: ARG002 — ctx is the fsm-contracts TickContext
        items = self._source(self._repo)
        # §3.11.5 loopback guard: the exclusion is CONDITIONAL on work_own_filings
        # (default True = the loop works its own filings, so INCLUDE loop-filed
        # items). Only under the opt-out (work_own_filings=False) does PULL drop
        # items the loop filed itself so they stay open for human triage. This is
        # exclusion at PULL, NOT a TRIAGE reject (a reject would route to the
        # doer's close path and CLOSE the discovery, the opposite of intent).
        if not self._work_own_filings:
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
# 1.2.0: additive `target_feature` field (issue #258) — the order's blast-radius
# target feature(s), computed by TRIAGE from authoritative signals so PRIORITIZE
# reads an authoritative field instead of re-scraping labels/body/title.
WORK_ORDER_SCHEMA_VERSION = "1.2.0"


# --------------------------------------------------------------------------
# Target-feature (blast-radius) detection (issue #258). TRIAGE — the WorkOrder
# producer — owns this computation and stamps the result onto each order's
# `target_feature` field, so PRIORITIZE reads a single authoritative field
# instead of re-deriving the feature from labels/body/title at PRIORITIZE time.
# The signals are AUTHORITATIVE only (the same set #214/#257 proved correct for
# same-feature serialization): prefixed labels, a Component:/Feature: body line,
# and a conventional title prefix — never generic labels nor a bare
# conventional-commit type. An order with no provable feature gets an EMPTY list.
# --------------------------------------------------------------------------

# The label-prefix convention that authoritatively declares a target feature:
# `feature:<name>` (the maintainer's own filing convention, e.g.
# `feature:scheduling`) or `component:<name>`. ONLY prefixed labels name a
# feature — generic labels (`bug`, `enhancement`, `filed-by:...`, `priority:*`)
# are NOT feature keys.
_FEATURE_LABEL_PREFIXES = ("feature:", "component:")

# The `Component:`/`Feature:` line convention in a free-form issue body, e.g.
# "Component: scheduling" or "Scope: ... Component: scheduling, prioritize".
# Captures the remainder of the line for splitting into one or more features.
_COMPONENT_LINE_RE = re.compile(
    r"^\s*(?:component|feature)s?\s*:\s*(.+)$",
    re.IGNORECASE | re.MULTILINE,
)

# Connectors that separate multiple features in a multi-feature blast radius.
# Deliberately EXCLUDES the word "and" — an "and" split can mis-cut a feature
# name (e.g. "command-and-control") and is unsafe to infer a shared radius from
# (issue #214 guidance item 3); a real multi-feature radius uses punctuation.
_FEATURE_SPLIT_RE = re.compile(r"[+,&/]")

# The conventional title-prefix convention that names a target feature for
# LABEL-LESS issues (issue #257), matching `name: ...` (the bare prefix, e.g.
# `scheduling: ...`) and `type(scope): ...` (a conventional-commit header, e.g.
# `feat(scheduling): ...` / `fix(work-intake): ...`). The optional `(scope)` is
# captured separately so a scoped header yields the scope as the feature; a bare
# prefix yields the name.
_TITLE_PREFIX_RE = re.compile(r"^\s*([^\s():]+)\s*(?:\(([^)]+)\))?\s*:")

# Bare conventional-commit TYPES used without a scope (e.g. `fix: x`,
# `docs: y`). These are NOT feature names — grouping on a bare type would
# over-serialize unrelated work orders (the #216 regression), so a `name:`
# prefix whose name is a bare type claims NO feature. A `type(scope):` header is
# unaffected: the scope (not the type) is the feature key.
_CONVENTIONAL_COMMIT_TYPES = frozenset({
    "feat", "fix", "docs", "chore", "refactor",
    "test", "perf", "build", "ci", "style",
})


def _normalize_feature(token):
    """Normalize a raw feature token to a comparison key: trimmed, lower-cased.
    Returns None for an empty/whitespace token so it never claims a feature."""
    key = token.strip().lower()
    return key or None


def _title_feature(title):
    """Derive the target feature from a conventional title prefix, or None.

    Recognizes `name: ...` (take `name`, e.g. `scheduling: ...`) and
    `type(scope): ...` (take `scope`, e.g. `feat(scheduling): ...`). A bare
    conventional-commit type used WITHOUT a scope (`fix: x`, `docs: y`) names NO
    feature, so unrelated `fix:`/`docs:` orders are not falsely grouped (the
    #216 over-serialization regression); a scoped header is unaffected because
    the scope, not the type, is the key.
    """
    match = _TITLE_PREFIX_RE.match(title or "")
    if not match:
        return None
    name, scope = match.group(1), match.group(2)
    if scope is not None:
        # `type(scope):` — the scope is the feature, whatever the type.
        return _normalize_feature(scope)
    # `name:` — a bare conventional-commit type names no feature.
    if name.strip().lower() in _CONVENTIONAL_COMMIT_TYPES:
        return None
    return _normalize_feature(name)


def target_features_for(labels=None, body="", title=""):
    """The SORTED list of target features a WorkOrder's blast radius touches,
    derived from AUTHORITATIVE signals only (issue #258):

    1. `feature:<name>` / `component:<name>` prefixed labels (the filing
       convention) — generic labels are ignored.
    2. a `Component:`/`Feature:` line in the body, split on +,&/, into one or
       more feature names (never on the word "and").
    3. a conventional title prefix — `name:` or `type(scope):` — which covers
       LABEL-LESS issues (issue #257); a bare conventional-commit type
       (`fix:`, `docs:`) names no feature.

    Returns an EMPTY list when no feature is provable. The result is SORTED so
    the stamped `target_feature` field is deterministic (byte-identical for the
    same inputs). Pure and deterministic.
    """
    features = set()

    for label in labels or []:
        if not isinstance(label, str):
            continue
        low = label.lower()
        for prefix in _FEATURE_LABEL_PREFIXES:
            if low.startswith(prefix):
                name = _normalize_feature(label[len(prefix):])
                if name:
                    features.add(name)
                break

    for match in _COMPONENT_LINE_RE.finditer(body or ""):
        for token in _FEATURE_SPLIT_RE.split(match.group(1)):
            name = _normalize_feature(token)
            if name:
                features.add(name)

    title_feature = _title_feature(title)
    if title_feature:
        features.add(title_feature)

    return sorted(features)


@dataclass(eq=True)
class WorkOrder:
    """A validated, decision-carrying item produced by TRIAGE from a WorkItem.

    `decision` is "accepted" or "rejected"; `reason` records why a rejected item
    was gated out (empty for accepted). `work_item_id` links back to the source
    WorkItem.id. `target_feature` is the order's blast-radius target feature(s),
    a list of normalized feature keys TRIAGE computes from authoritative signals
    (issue #258), so PRIORITIZE reads an authoritative field instead of
    re-scraping. `to_dict`/`from_dict` give a machine-first, versioned
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
    # The order's blast-radius target feature(s) (issue #258), a SORTED list of
    # normalized feature keys TRIAGE computes from authoritative signals
    # (prefixed labels, a Component: body line, a conventional title prefix).
    # Empty when no feature is provable. PRIORITIZE reads this authoritative
    # field for same-feature serialization instead of re-scraping.
    target_feature: List[str] = field(default_factory=list)

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
            "target_feature": list(self.target_feature),
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
            target_feature=list(d.get("target_feature", [])),
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
# work_orders AND cross_cutting_risk (DESIGN §3.5.9 — TRIAGE always writes the
# risk slot so VERIFY can always read it), emits OK | EMPTY.
TRIAGE_MANIFEST = fc.StateManifest(
    reads=["work_items"],
    writes=["work_orders", "cross_cutting_risk"],
    emits=TRIAGE_SIGNALS)


# --------------------------------------------------------------------------
# Cross-cutting-risk slot (DESIGN §3.5.9). TRIAGE is the only state with the
# whole-batch view, so it flags when accepted work orders' blast radii may
# overlap across DIFFERENT features and writes this machine-first slot for
# VERIFY (§3.7.6) to act on. Same-feature overlap is handled by serialization
# (§3.8.6); this flag is the residual semantic cross-feature case.
# --------------------------------------------------------------------------

# The versioned CrossCuttingRisk schema (machine-first; bumped on a breaking
# field-set change). Distinct from the feature + other slot versions.
CROSS_CUTTING_RISK_SCHEMA_VERSION = "1.0.0"

# risk=true requires at least this many DISTINCT affected features (a single
# feature's overlap is serialization's job, not this cross-feature flag).
_CROSS_CUTTING_MIN_FEATURES = 2


@dataclass(eq=True)
class CrossCuttingRisk:
    """The whole-batch cross-cutting-risk verdict TRIAGE writes each tick.

    `risk` is True only when accepted work orders' blast radii may overlap
    across DIFFERENT features; `features` names the affected features and
    `reason` the specific overlap. The default value is no-risk (risk=False,
    empty features, empty reason). `to_dict`/`from_dict` give a machine-first,
    versioned representation suitable for the `cross_cutting_risk` slot.
    """

    risk: bool
    features: List[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self):
        return {
            "schema_version": CROSS_CUTTING_RISK_SCHEMA_VERSION,
            "risk": bool(self.risk),
            "features": list(self.features),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            risk=bool(d["risk"]),
            features=list(d.get("features", [])),
            reason=d.get("reason", ""),
        )


# The fsm-contracts slot descriptor. `cross_cutting_risk` is an object slot (a
# single CrossCuttingRisk dict); the slot version tracks the schema version.
CROSS_CUTTING_RISK_SLOT = {
    "name": "cross_cutting_risk",
    "schema": {"type": "object"},
    "version": CROSS_CUTTING_RISK_SCHEMA_VERSION,
}


def normalize_cross_cutting_risk(annotation):
    """Fold a batch-level cross-cutting annotation into a normalized
    CrossCuttingRisk. Pure and deterministic.

    `annotation` is None (no risk) or a mapping `{features: [str], reason: str}`.
    The verdict `risk` is True ONLY when the annotation names at least
    _CROSS_CUTTING_MIN_FEATURES DISTINCT features AND carries a non-empty
    (non-whitespace) reason; a single-feature, empty, or whitespace-only
    annotation normalizes to no-risk. Malformed input — a non-mapping, a
    non-list `features`, a non-string feature entry, or a non-string `reason` —
    is REJECTED (ValueError/TypeError), so a bad annotation is locatable at this
    boundary rather than silently swallowed (spec-rules §1).
    """
    if annotation is None:
        return CrossCuttingRisk(risk=False, features=[], reason="")
    if not isinstance(annotation, dict):
        raise TypeError(
            "cross-cutting annotation must be a mapping or None, got "
            f"{type(annotation).__name__}")
    features = annotation.get("features", [])
    reason = annotation.get("reason", "")
    if not isinstance(features, list):
        raise TypeError("annotation 'features' must be a list")
    if not all(isinstance(f, str) for f in features):
        raise TypeError("annotation 'features' entries must be strings")
    if not isinstance(reason, str):
        raise TypeError("annotation 'reason' must be a string")
    distinct = []
    for f in features:
        if f and f not in distinct:
            distinct.append(f)
    risk = len(distinct) >= _CROSS_CUTTING_MIN_FEATURES and bool(reason.strip())
    return CrossCuttingRisk(risk=risk, features=distinct, reason=reason)

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
    WorkOrder(decision="accepted"), writes the `work_orders` slot AND ALWAYS the
    `cross_cutting_risk` slot (DESIGN §3.5.9 — a default no-risk verdict when no
    batch annotation is supplied), and emits OK if any were accepted else EMPTY.

    The validity gate (pure rules, no network, no AI):
      - well-formed: a non-empty title  (else rejected "malformed: no title");
      - in-scope:    state == "open"    (else rejected "not open");
      - not-stale:   updated_at within STALE_WINDOW_DAYS of the reference time
                     (else rejected "stale: ...").

    Staleness is the only time-dependent edge; it keys off an INJECTABLE
    reference time (`now`, defaulting to the current UTC time) so tests pin
    staleness deterministically (spec-rules §1).
    """

    def __init__(self, now=None, cross_cutting_annotation=None):
        self._now = now if now is not None else datetime.now(timezone.utc)
        # The batch-level cross-cutting annotation (DESIGN §3.5.9), normalized
        # once at construction so a malformed annotation is rejected at the
        # boundary rather than at run time. None -> a default no-risk verdict.
        self._cross_cutting = normalize_cross_cutting_risk(
            cross_cutting_annotation)

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
                # Stamp the blast-radius target feature(s) from authoritative
                # signals (issue #258) so PRIORITIZE reads this field instead of
                # re-scraping labels/body/title at PRIORITIZE time.
                target_feature=target_features_for(
                    labels=item.labels, body=item.body, title=item.title),
            ))
        # ALWAYS write the cross_cutting_risk slot (DESIGN §3.5.9) — a default
        # no-risk verdict when no annotation — so VERIFY (§3.7.6) can always
        # read it regardless of this tick's batch.
        writes = {
            "work_orders": [order.to_dict() for order in accepted],
            "cross_cutting_risk": self._cross_cutting.to_dict(),
        }
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
      - skipped_open:     [{dedup_key, matched}] — discoveries whose subject
                          matches an ALREADY-OPEN tracker issue (dedup-vs-open):
                          NOT filed (duplicate noise), no sink call. `matched`
                          is the id/number of the open issue it duplicates.
      - errors:           [{dedup_key, reason}] — per-discovery sink failures.
    """

    filed: List[dict] = field(default_factory=list)
    skipped_existing: List[str] = field(default_factory=list)
    skipped_open: List[dict] = field(default_factory=list)
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


# Dedup-vs-open: the minimum normalized-title token overlap (Jaccard) above
# which a discovery is judged to duplicate an already-open issue. Tuned high
# enough that only near-identical subjects match (the #222/#223-vs-#209/#210
# duplicate class), low enough to tolerate trivial wording drift. The v1 match
# is this deterministic heuristic; a model-judged "is this already tracked?"
# check is the deferred robust v2 (see docs/spec.md).
OPEN_DUP_TITLE_OVERLAP = 0.7

# Tokens stripped before comparing titles: pure noise for subject matching.
_OPEN_DUP_STOPWORDS = frozenset({
    "a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "is",
    "are", "be", "vs", "via", "with", "when", "that", "this", "it",
})


def _normalize_title_tokens(title):
    """Lowercase a title and split it into a set of meaningful word tokens
    (alphanumerics, stopwords dropped). Pure and deterministic; the unit of the
    dedup-vs-open title-overlap heuristic."""
    out = set()
    word = []
    for ch in (title or "").lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.add("".join(word))
            word = []
    if word:
        out.add("".join(word))
    return {w for w in out if w and w not in _OPEN_DUP_STOPWORDS}


def _match_open_issue(discovery, open_items):
    """Return the id/number of an OPEN tracker item whose subject matches this
    discovery's title (dedup-vs-open), or None when none matches.

    v1 heuristic (deterministic): the title token sets overlap (Jaccard) at or
    above OPEN_DUP_TITLE_OVERLAP. `open_items` are WorkItem objects or their
    machine-first dicts. Pure; performs no I/O. A robust model-judged v2 is
    deferred (docs/spec.md)."""
    disc_tokens = _normalize_title_tokens(discovery.title)
    if not disc_tokens:
        return None
    best_ref = None
    best_score = 0.0
    for item in open_items or []:
        if isinstance(item, dict):
            title = item.get("title", "")
            ref = item.get("id") or item.get("number")
        else:
            title = getattr(item, "title", "")
            ref = getattr(item, "id", None) or getattr(item, "number", None)
        item_tokens = _normalize_title_tokens(title)
        if not item_tokens:
            continue
        overlap = disc_tokens & item_tokens
        union = disc_tokens | item_tokens
        score = len(overlap) / len(union)
        if score >= OPEN_DUP_TITLE_OVERLAP and score > best_score:
            best_score = score
            best_ref = ref
    return best_ref


def file_discoveries(discoveries, sink=gh_issue_file_sink, known_dedup_keys=(),
                     known_open=()):
    """Pure orchestration over a batch of DiscoveredIssues.

    For each discovery, in order:
      - if its `dedup_key` is in `known_dedup_keys` it is recorded in
        `skipped_existing` with NO sink call (idempotent re-filing is a no-op);
      - else if its subject matches an ALREADY-OPEN issue in `known_open`
        (dedup-vs-open) it is recorded in `skipped_open` with NO sink call —
        the loop must not file a duplicate of an issue already in the tracker
        (DESIGN §3.5.4, REPORT side);
      - otherwise the injected `sink` is invoked and its {tracker_ref, url}
        recorded in `filed`. A sink exception is caught and recorded in `errors`
        (filing one bad discovery never aborts the batch).

    `known_open` are the tick's PULLed open tracker items (WorkItem objects or
    their machine-first dicts); matching uses the deterministic title-overlap
    heuristic (_match_open_issue). Deterministic given the injected sink + known
    set + open set; performs no I/O of its own. The trust-ladder GATE is NOT
    here — it lives in scheduling.run_tick, which only calls this when filing is
    permitted.
    """
    known = set(known_dedup_keys)
    open_items = list(known_open)
    result = ReportResult()
    for discovery in discoveries:
        if discovery.dedup_key in known:
            result.skipped_existing.append(discovery.dedup_key)
            continue
        matched = _match_open_issue(discovery, open_items)
        if matched is not None:
            result.skipped_open.append({
                "dedup_key": discovery.dedup_key,
                "matched": matched,
            })
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
