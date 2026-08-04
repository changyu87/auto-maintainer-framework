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

Reject disposition (deterministic, at TRIAGE):
  - REJECTED_LABEL / REJECT_MARKER — the fixed label + comment-marker literals.
  - reject_dispositions() — pure selector of the disposition payload for every
    decision="rejected" WorkOrder.
  - gh_issue_reject_sink() — injectable tracker sink: ensure label + one marked
    comment carrying the reason + add-label; NEVER closes; idempotent no-op when
    already labeled.

already_done disposition (on-issue, visible — mirrors reject; NOT a reject):
  - ALREADY_DONE_MARKER — the fixed comment marker, DISTINCT from REJECT_MARKER,
    so an already-done disposition is machine-distinguishable from a reject even
    though the two SHARE the label (REJECTED_LABEL).
  - gh_issue_already_done_sink() — injectable tracker sink mirroring the reject
    sink: ensure the shared label + one ALREADY_DONE_MARKER comment carrying the
    reason + add-label; NEVER closes; idempotent no-op when the marker is already
    present (keyed off the marker, not the shared label).
  - is_strong_reason() — pure guard both dispositions consult before enacting: a
    reason is strong iff >= 40 chars stripped AND not reflexive-deferral
    boilerplate.

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

# Park guard (Phase 2 convergence, §3.11 park guard bullet). verify-integrate's
# INTEGRATE posts the FIXED gate-fail marker below on the issue for each failed
# merge attempt; once an issue's (bounded) comments carry >= PARK_THRESHOLD such
# markers PULL UNCONDITIONALLY EXCLUDES (parks) it so the loop stops re-working
# it and CONVERGES to idle. The issue stays OPEN with its gate-fail comments for
# a human to resolve on the tracker.
PARK_THRESHOLD = 5

# The exact gate-fail marker. SOURCE OF TRUTH: verify_integrate.GATE_FAIL_MARKER
# ('<!-- auto-maintainer:gate-fail -->'). Hardcoded here (work-intake imports
# only fsm_contracts, not verify_integrate) — keep this identical to the
# verify-integrate constant; the park e2e test pins the exact string so drift
# fails loudly.
GATE_FAIL_MARKER = "<!-- auto-maintainer:gate-fail -->"


def _gate_fail_pr_ref(body):
    """Extract the failed `pr_ref` from a gate-fail marker comment body, or None.

    The marker comment body is (verify_integrate.gate_fail_comment_body): the
    FIXED marker line, a human sentence, a blank line, then a compact
    json.dumps({pr_ref, reason, failure_summary}) block, then a trailing newline.
    We locate the first `{` after the marker and json.loads the balanced object,
    returning its `pr_ref`. Returns None when the payload is absent, unparseable,
    or carries no `pr_ref` — the caller then treats that marker as one distinct
    attempt (keyed by position) so a malformed marker never silently defeats the
    guard. Pure and deterministic."""
    marker_idx = body.find(GATE_FAIL_MARKER)
    if marker_idx == -1:
        return None
    brace_idx = body.find("{", marker_idx)
    if brace_idx == -1:
        return None
    # The payload is a single balanced JSON object; take from the first `{` to
    # its matching `}`. Scan for the balanced close so trailing text (the
    # comment's trailing newline, or any prose) cannot break the parse.
    depth = 0
    end = -1
    for i in range(brace_idx, len(body)):
        ch = body[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None
    try:
        payload = json.loads(body[brace_idx:end])
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    pr_ref = payload.get("pr_ref")
    return pr_ref if isinstance(pr_ref, str) and pr_ref else None


def is_parked(item):
    """Return True when `item` has failed to merge too many times and must be
    PARKED (excluded from PULL) so the loop converges instead of looping forever.

    Counts DISTINCT failed PRs — NOT raw GATE_FAIL_MARKER occurrences — across
    the item's (bounded) comment bodies; parked when the count is >=
    PARK_THRESHOLD. Each real retry is a distinct PR (the implementer supersedes
    its prior open PR before opening a new one), so for each comment carrying the
    marker we parse the comment's JSON payload and collect its `pr_ref`. A marker
    whose payload is absent/unparseable (or carries no `pr_ref`) counts as one
    distinct attempt keyed by its comment index, so malformed markers never
    silently defeat the guard. This makes park a true RETRY counter rather than a
    tick-age timer: INTEGRATE re-posts a gate-fail marker every tick the SAME
    unchanged PR is re-gated, so counting raw markers would park an item after
    PARK_THRESHOLD *ticks* regardless of how many times it was actually retried.

    `item` may be a WorkItem or a dict (the machine-first WorkItem form). Pure
    and deterministic.

    PULL uses this to EXCLUDE parked items UNCONDITIONALLY (independent of
    work_own_filings) — they never become work_items / work_orders and stay open
    with their gate-fail comments for a human to resolve. This is exclusion at
    PULL, NOT a TRIAGE reject (a reject would route to the doer's close path and
    CLOSE the issue).
    """
    if isinstance(item, dict):
        comments = item.get("comments") or []
    else:
        comments = getattr(item, "comments", None) or []
    attempts = set()
    for index, comment in enumerate(comments):
        body = comment.get("body") or "" if isinstance(comment, dict) else ""
        if GATE_FAIL_MARKER not in body:
            continue
        pr_ref = _gate_fail_pr_ref(body)
        if pr_ref is not None:
            attempts.add(pr_ref)
        else:
            # Malformed marker: count as one distinct attempt keyed by position
            # so it still contributes to the guard exactly once.
            attempts.add(("__unparsed__", index))
        if len(attempts) >= PARK_THRESHOLD:
            return True
    return len(attempts) >= PARK_THRESHOLD


def _item_issue_ref(item):
    """Derive an item's issue ref `owner/repo#number` from its url + number.

    Reuses `_derive_id` (the same derivation that stamps WorkItem.id), so the
    ref is byte-identical to the item's id. `item` may be a WorkItem or a dict.
    """
    if isinstance(item, dict):
        url = item.get("url") or ""
        number = item.get("number")
    else:
        url = getattr(item, "url", "") or ""
        number = getattr(item, "number", None)
    return _derive_id(url, number)


def _normalize_issue_ref(ref):
    """Normalize a caller-supplied ref to the canonical `owner/repo#number` form
    so membership is tolerant of a full issue URL vs the short form. A full
    github.com issue URL is folded via `_derive_ref`; a non-URL ref is returned
    as-is (already the short form). Pure and deterministic."""
    if isinstance(ref, str) and "github.com/" in ref:
        return _derive_ref(ref)
    return ref


def is_in_flight(item, in_flight_issue_refs):
    """Return True when `item`'s issue already has an OPEN loop PR addressing it,
    i.e. its issue ref (`owner/repo#number`, derived from the item's url/number)
    is a member of the injected `in_flight_issue_refs` set.

    Pure and deterministic — NO I/O. work-intake CONSUMES the set (computed by
    scheduling from EXISTING verify-integrate open-PR / closing-issue seams
    and/or the acted-ledger `opened` entries); it adds no gh plumbing here. Set
    entries may be the short `owner/repo#number` form OR a full issue URL — both
    are normalized before comparison. An empty/None set is always False.

    PULL uses this to UNCONDITIONALLY EXCLUDE in-flight items so the loop does
    not re-triage / re-implement work already in flight (which would re-open
    duplicate/superseding PRs); the excluded issue is LEFT OPEN and untouched —
    its open PR's own lifecycle resolves it. Like the loopback and park guards
    this is a PULL exclusion, NOT a TRIAGE reject.
    """
    if not in_flight_issue_refs:
        return False
    normalized = {_normalize_issue_ref(r) for r in in_flight_issue_refs}
    return _item_issue_ref(item) in normalized


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


def _run_issue_list(repo, label_group, runner):
    """Run one `gh issue list --state open` query. A non-empty `label_group`
    (an AND-group) adds a `--label` flag per label — repeated `--label` is gh's
    native AND. Returns the parsed WorkItems for that single query."""
    cmd = ["gh", "issue", "list", "--state", "open", "--json", _GH_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    for label in label_group:
        cmd += ["--label", label]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return parse_gh_issues(out.stdout)


def gh_issue_source(repo=None, runner=subprocess.run, issue_filter=None):
    """Production issue source: shell the deterministic `gh` CLI for OPEN issues
    and parse its JSON into WorkItems. `gh` carries its own auth. When `repo` is
    given it is passed via `--repo`; otherwise gh resolves the repo from the
    project default / git remote.

    `issue_filter` is the already-normalized `{labels: List[List[str]],
    title_pattern: str|None}` object (owned + normalized by safety-governance,
    threaded in by scheduling — work-intake does NOT read config.json). It
    narrows WHICH open issues are pulled:
      - Labels (DNF, OR-of-ANDs) — SERVER-SIDE. For a non-empty `labels`, one
        `gh issue list --label ...` query runs per AND-group (repeated `--label`
        is gh's native AND), and the results are UNIONED deduped by issue number
        (gh cannot OR labels in one query). Empty `labels` runs the single
        all-open query. Server-side filtering also cuts the per-issue comment
        fetches to only matching issues.
      - Title (`title_pattern`) — POST-FETCH. gh has no title query, so a
        non-null `title_pattern` is applied as a regex `search` over each
        fetched title, dropping non-matches BEFORE comment enrichment.
      - Exclude labels (`exclude_labels`) — POST-FETCH NEGATIVE term. gh's
        per-AND-group union query cannot express negation, so a non-empty
        `exclude_labels` (a flat OR of forbidden labels) drops any fetched issue
        carrying ANY listed label, BEFORE comment enrichment. Empty is a no-op;
        this keeps a disposed reject (REJECTED_LABEL) out of PULL.
    The narrowings COMPOSE; the default filter (empty labels + null pattern +
    empty exclude_labels) is a no-op that pulls every open issue exactly as
    before.

    `gh issue list` does not return comments, so for each SURVIVING issue this
    also shells `gh issue view <number> --json comments` and attaches the
    bounded human-discussion thread to the WorkItem (so the triager +
    implementer see follow-up guidance, not just the original body). A per-issue
    comment fetch that fails is tolerated (the item keeps an empty `comments`) —
    a flaky comment read must never sink the whole PULL. The subprocess `runner`
    is INJECTABLE (defaulting to subprocess.run) so tests drive it without
    network.
    """
    issue_filter = issue_filter or {}
    label_groups = issue_filter.get("labels") or []
    title_pattern = issue_filter.get("title_pattern")
    exclude_labels = issue_filter.get("exclude_labels") or []

    # Labels (DNF) — server-side: one query per AND-group, unioned+deduped by
    # number (first-seen order preserved). Empty labels => single all-open query.
    if label_groups:
        items = []
        seen = set()
        for group in label_groups:
            for item in _run_issue_list(repo, group, runner):
                if item.number not in seen:
                    seen.add(item.number)
                    items.append(item)
    else:
        items = _run_issue_list(repo, [], runner)

    # Title — post-fetch regex, applied BEFORE comment enrichment so dropped
    # issues never incur a comment fetch.
    if title_pattern is not None:
        pattern = re.compile(title_pattern)
        items = [item for item in items if pattern.search(item.title)]

    # Exclude labels — post-fetch NEGATIVE term: gh's per-AND-group union query
    # cannot express negation, so a non-empty exclude_labels (a flat OR of
    # forbidden labels, normalized by safety-governance) drops any fetched issue
    # carrying ANY listed label, applied BEFORE comment enrichment so dropped
    # issues never incur a comment fetch. Empty exclude_labels is a no-op. This is
    # how a disposed reject (REJECTED_LABEL) is kept out of PULL.
    if exclude_labels:
        forbidden = set(exclude_labels)
        items = [item for item in items
                 if not (forbidden & set(item.labels))]

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

    The source is injectable (a callable
    `source(repo, issue_filter=None) -> list[WorkItem]`, defaulting to the
    production gh-shelling source) so tests pass a stub over fixture issues — the
    determinism seam.

    `issue_filter` is the already-normalized `{labels, title_pattern}` object
    (owned + normalized by safety-governance, threaded in by scheduling); it is
    the single filter point, passed straight through to the source. work-intake
    does NOT read config.json. The default `None` pulls every open issue.

    `in_flight_issue_refs` is the set of issue refs (owner/repo#N) that already
    have an OPEN loop PR, threaded in by scheduling (work-intake CONSUMES it —
    it adds no gh plumbing). PULL UNCONDITIONALLY EXCLUDES any item whose ref is
    in the set (the in-flight guard) so the loop does not re-work an issue an
    open PR is already addressing. The default (empty) is a no-op.
    """

    def __init__(self, source=gh_issue_source, repo=None, work_own_filings=True,
                 issue_filter=None, in_flight_issue_refs=None):
        self._source = source
        self._repo = repo
        self._work_own_filings = work_own_filings
        self._issue_filter = issue_filter
        # The set of issue refs (owner/repo#N) that already have an OPEN loop PR,
        # threaded in by scheduling (work-intake CONSUMES it, adds no gh
        # plumbing). Default empty => the in-flight exclusion is a no-op.
        self._in_flight_issue_refs = frozenset(in_flight_issue_refs or ())

    def run(self, ctx):  # noqa: ARG002 — ctx is the fsm-contracts TickContext
        # The source is the single filter point (issue_filter narrows what it
        # returns); park/loopback exclusions below remain after the source call.
        items = self._source(self._repo, issue_filter=self._issue_filter)
        # Park guard (Phase 2 convergence): UNCONDITIONALLY exclude parked items
        # — an issue whose comments carry >= PARK_THRESHOLD gate-fail markers has
        # failed too many times, so the loop stops re-working it and converges to
        # idle. Unlike the loopback guard below this is NOT gated on any config
        # knob; like it, it is a PULL exclusion (the issue stays open), NOT a
        # TRIAGE reject (which would close it).
        items = [item for item in items if not is_parked(item)]
        # §3.11.5 loopback guard: the exclusion is CONDITIONAL on work_own_filings
        # (default True = the loop works its own filings, so INCLUDE loop-filed
        # items). Only under the opt-out (work_own_filings=False) does PULL drop
        # items the loop filed itself so they stay open for human triage. This is
        # exclusion at PULL, NOT a TRIAGE reject (a reject would route to the
        # doer's close path and CLOSE the discovery, the opposite of intent).
        if not self._work_own_filings:
            items = [item for item in items if not is_loop_filed(item)]
        # In-flight guard (convergence): UNCONDITIONALLY exclude any item whose
        # issue already has an OPEN loop PR (its ref is in the injected set), so
        # the loop does not re-triage/re-implement work already in flight. The
        # excluded issue is LEFT OPEN and untouched — its open PR resolves it.
        # Like the park/loopback guards this is a PULL exclusion, NOT a reject.
        if self._in_flight_issue_refs:
            items = [item for item in items
                     if not is_in_flight(item, self._in_flight_issue_refs)]
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


def _ensure_label(label, repo, runner,
                  description="filed by the autonomous maintainer"):
    """ENSURE a label exists via an idempotent `gh label create`. check=False:
    an "already exists" non-zero exit is TOLERATED, never raised (the label
    simply already exists). Honors `--repo` and the injectable runner."""
    label_cmd = ["gh", "label", "create", label,
                 "--description", description]
    if repo:
        label_cmd += ["--repo", repo]
    runner(label_cmd, capture_output=True, text=True, check=False)


def gh_issue_file_sink(discovery, repo=None, apply_labels=None,
                       runner=subprocess.run):
    """Production filing sink: shell `gh issue create` for one DiscoveredIssue
    and return {tracker_ref, url}. `gh` carries its own auth.

    The provenance label `filed-by:autonomous-maintainer` is stamped, and a
    `<!-- am-dedup:<dedup_key> -->` marker is appended to the body so a later
    PULL/TRIAGE (and a dedup re-scan) can recognize the filing. When `repo` is
    given it is passed via `--repo` (the caller chooses it from
    discovery.target); otherwise gh resolves the repo from the project default.

    `apply_labels` are the PULL-visibility labels (the labels that make a filed
    issue match the active `issue_filter` — safety-governance's
    `issue_filter_apply_labels`) so the loop can RE-PULL work it filed for
    itself. In addition to the provenance label, each apply label is ENSURED to
    exist first (`gh label create <L>`, idempotent, same tolerate pattern) then
    added to the `gh issue create --label` set. `apply_labels` None/[] leaves
    behaviour unchanged (only the provenance label). The caller passes these
    ONLY for `project`-target filings.

    The subprocess `runner` is INJECTABLE (defaulting to subprocess.run) so
    tests pass a fake — no network, the failure locatable to the file boundary.
    """
    body = discovery.body
    marker = _am_dedup_marker(discovery.dedup_key)
    body = f"{body}\n\n{marker}" if body else marker
    # The full label set: the provenance label plus each PULL-visibility
    # apply label (skipping empties). `gh issue create --label <L>` FAILS if L
    # is absent in the repo, so EACH label is ENSURED first via an idempotent
    # `gh label create` (a fresh repo has none of them) before the issue create.
    labels = [FILED_BY_LABEL] + [l for l in (apply_labels or []) if l]
    for label in labels:
        _ensure_label(label, repo, runner)
    cmd = ["gh", "issue", "create",
           "--title", discovery.title,
           "--body", body]
    for label in labels:
        cmd += ["--label", label]
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
                     known_open=(), apply_labels=None):
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

    `apply_labels` (the active `issue_filter`'s PULL-visibility labels, from
    scheduling) is forwarded to the sink ONLY for `project`-target discoveries
    (so a project filing is re-pullable); a `maintainer-self` discovery is filed
    with `apply_labels=[]` (the fixed MAINTAINER_REPO has its own/no filter).
    `None`/`[]` ⇒ the sink is invoked exactly as before (every filing keeps just
    the provenance label — unchanged), so a sink taking only
    `(discovery, repo=None)` still works.
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
            if apply_labels:
                # Forward PULL-visibility labels to the sink, but ONLY for
                # project-target filings; maintainer-self gets [].
                per_target = (list(apply_labels)
                              if discovery.target == "project" else [])
                ref = sink(discovery, apply_labels=per_target)
            else:
                # None/[] ⇒ unchanged: invoke the sink exactly as before.
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


# ==========================================================================
# Reject disposition (deterministic, at TRIAGE — NOT at IMPLEMENT).
# ==========================================================================
#
# A SEMANTICALLY-rejected issue (the AI triager's decision="rejected" + reason)
# is disposed of DETERMINISTICALLY at TRIAGE-time — COMMENTED and LABELED, never
# CLOSED by default — so a human can see why and the loop stops re-pulling it.
# work-intake owns tracker labels + tracker I/O, so it owns these primitives; the
# ENACTMENT (calling them per reject) + the triage_memory recording are
# scheduling's. Only a SEMANTIC reject is disposed here; STRUCTURAL exclusions
# (the deterministic gate, loop-filed, parked) stay open and UN-labeled.

# The fixed label a disposed reject carries. safety-governance / packaging-config
# reference this SAME literal to exclude it from PULL (the issue_filter negative
# exclude_labels term). A human removing the label re-admits the issue.
REJECTED_LABEL = "auto-maintainer-rejected"

# The FIXED machine marker prefixing the one reject-disposition comment, so the
# comment (and its reason) is machine-recognizable and de-duplicable.
REJECT_MARKER = "<!-- auto-maintainer:rejected -->"

# The FIXED machine marker prefixing the one already_done-disposition comment.
# DISTINCT from REJECT_MARKER so an already-done disposition is
# machine-distinguishable from a reject in the comment thread even though the two
# SHARE the label (REJECTED_LABEL). Owned here (work-intake owns tracker markers).
ALREADY_DONE_MARKER = "<!-- auto-maintainer:already-done -->"

# The minimum stripped length a disposition `reason` must reach to be "strong"
# enough to post (the strong-reason guard, below).
_MIN_REASON_CHARS = 40

# Reflexive-deferral boilerplate phrases a disposition reason must NOT be composed
# solely of. Mirrors rabbit-issue's item-status.py rejected-phrase concept: a
# reason whose stripped, case-folded text IS one of these (or consists only of
# them + separators) is REJECTED as weak — it does not tell a human WHY.
_WEAK_REASON_PHRASES = frozenset({
    "will look into", "todo", "deferred", "not sure", "later",
    "n/a", "no reason", "as discussed", "see above", "wontfix",
})


def is_strong_reason(reason):
    """Return True when `reason` is substantive enough to post on a disposition
    comment (both the reject and already_done dispositions consult this before
    enacting). A reason is strong iff it is at least _MIN_REASON_CHARS characters
    (stripped) AND is not composed solely of reflexive-deferral boilerplate.

    Pure and deterministic; no I/O. A non-string is never strong. The gating
    (bounce-and-re-enter on a weak reason) lives in scheduling; this predicate
    only judges the reason.
    """
    if not isinstance(reason, str):
        return False
    stripped = reason.strip()
    if len(stripped) < _MIN_REASON_CHARS:
        return False
    folded = stripped.casefold()
    if folded in _WEAK_REASON_PHRASES:
        return False
    # "Composed solely of boilerplate": strip every boilerplate phrase and the
    # separators between them; if nothing substantive remains, it is weak.
    residue = folded
    for phrase in _WEAK_REASON_PHRASES:
        residue = residue.replace(phrase, " ")
    residue = re.sub(r"[\s,.;:/&+\-]+", "", residue)
    if not residue:
        return False
    return True


def _order_field(order, name, default=""):
    """Read a field from a WorkOrder object OR its machine-first dict form."""
    if isinstance(order, dict):
        return order.get(name, default)
    return getattr(order, name, default)


def reject_dispositions(work_orders):
    """Pure selector: return the disposition payload for every decision="rejected"
    order as a list of {work_item_id, issue_ref, reason}. Accepted orders are
    dropped (PRIORITIZE forwards only accepted to IMPLEMENT anyway). `issue_ref`
    is a gh-actionable reference — the order's issue `url`, falling back to its
    `work_item_id` when no url is present. Accepts WorkOrder objects OR their
    machine-first dicts (scheduling reads the work_orders slot, which is dicts).
    Deterministic; performs no I/O."""
    out = []
    for order in work_orders:
        if _order_field(order, "decision") != "rejected":
            continue
        work_item_id = _order_field(order, "work_item_id")
        issue_ref = _order_field(order, "url") or work_item_id
        out.append({
            "work_item_id": work_item_id,
            "issue_ref": issue_ref,
            "reason": _order_field(order, "reason"),
        })
    return out


def gh_issue_reject_sink(issue_ref, repo=None, reason="", label=REJECTED_LABEL,
                         runner=subprocess.run):
    """Enact the reject disposition on ONE issue (mirrors gh_issue_file_sink).

    It ENSURES the label exists (idempotent `gh label create`), posts ONE comment
    carrying the `reason` behind the FIXED REJECT_MARKER, and applies the label
    (`gh issue edit <ref> --add-label`). It NEVER closes the issue.

    Idempotent: it first reads the issue's current labels (`gh issue view <ref>
    --json labels`); if the item ALREADY carries `label` it is a NO-OP (no
    duplicate comment, no re-edit). `issue_ref` is any gh-actionable reference
    (issue number or URL). When `repo` is given it is passed via `--repo`. The
    subprocess `runner` is INJECTABLE (defaulting to subprocess.run) so tests
    pass a fake — no network, the failure locatable to the sink boundary.
    """
    view_cmd = ["gh", "issue", "view", str(issue_ref), "--json", "labels"]
    if repo:
        view_cmd += ["--repo", repo]
    out = runner(view_cmd, capture_output=True, text=True, check=True)
    existing = {lbl.get("name", "")
                for lbl in (json.loads(out.stdout).get("labels") or [])}
    if label in existing:
        # Already disposed — idempotent no-op (no duplicate comment / re-edit).
        return

    # ENSURE the label exists first (`gh issue edit --add-label` fails on a
    # missing label in a fresh repo), tolerating a pre-existing label's non-zero
    # exit (check=False), then COMMENT the reason behind the marker, then LABEL.
    _ensure_label(label, repo, runner,
                  description="disposed as rejected by the autonomous maintainer")
    comment_cmd = ["gh", "issue", "comment", str(issue_ref),
                   "--body", f"{REJECT_MARKER}\n{reason}"]
    if repo:
        comment_cmd += ["--repo", repo]
    runner(comment_cmd, capture_output=True, text=True, check=True)
    edit_cmd = ["gh", "issue", "edit", str(issue_ref), "--add-label", label]
    if repo:
        edit_cmd += ["--repo", repo]
    runner(edit_cmd, capture_output=True, text=True, check=True)


def gh_issue_already_done_sink(issue_ref, repo=None, reason="",
                               label=REJECTED_LABEL, runner=subprocess.run):
    """Enact the already_done disposition on ONE issue (MIRRORS
    gh_issue_reject_sink). Used when the IMPLEMENT doer reports the requested
    change is ALREADY PRESENT ON `main`: it makes that outcome VISIBLE on the
    tracker so a human sees the loop resolved it, exactly parallel to a reject —
    but it is NOT a reject (the work was real and is done, not invalid).

    It ENSURES the (shared) label exists (idempotent `gh label create`), posts
    ONE comment carrying the `reason` (the on-`main` evidence + a short note that
    the change is already present) behind the FIXED ALREADY_DONE_MARKER, and
    applies the label (`gh issue edit <ref> --add-label`). It NEVER closes the
    issue.

    Idempotent: it first reads the issue's current comments (`gh issue view
    <ref> --json comments`); if any comment already carries ALREADY_DONE_MARKER
    it is a NO-OP (no duplicate comment, no re-edit). Idempotency keys off the
    MARKER, NOT the label — the label is SHARED with reject, so a prior reject's
    label must not suppress an already_done comment (and vice versa). `issue_ref`
    is any gh-actionable reference (issue number or URL). When `repo` is given it
    is passed via `--repo`. The subprocess `runner` is INJECTABLE (defaulting to
    subprocess.run) so tests pass a fake — no network, the failure locatable to
    the sink boundary.
    """
    view_cmd = ["gh", "issue", "view", str(issue_ref), "--json", "comments"]
    if repo:
        view_cmd += ["--repo", repo]
    out = runner(view_cmd, capture_output=True, text=True, check=True)
    comments = json.loads(out.stdout).get("comments") or []
    for comment in comments:
        if ALREADY_DONE_MARKER in (comment.get("body") or ""):
            # Already disposed — idempotent no-op (no duplicate comment/re-edit).
            return

    # ENSURE the (shared) label exists first (`gh issue edit --add-label` fails
    # on a missing label in a fresh repo), tolerating a pre-existing label's
    # non-zero exit (check=False), then COMMENT the reason behind the marker,
    # then LABEL.
    _ensure_label(
        label, repo, runner,
        description="disposed by the autonomous maintainer")
    comment_cmd = ["gh", "issue", "comment", str(issue_ref),
                   "--body", f"{ALREADY_DONE_MARKER}\n{reason}"]
    if repo:
        comment_cmd += ["--repo", repo]
    runner(comment_cmd, capture_output=True, text=True, check=True)
    edit_cmd = ["gh", "issue", "edit", str(issue_ref), "--add-label", label]
    if repo:
        edit_cmd += ["--repo", repo]
    runner(edit_cmd, capture_output=True, text=True, check=True)
