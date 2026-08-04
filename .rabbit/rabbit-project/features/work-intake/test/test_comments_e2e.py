#!/usr/bin/env python3
"""Tests for work-intake's issue-COMMENTS enrichment (#213).

`gh issue list` returns only the original issue body — not comments — so the
triager + implementer were blind to human follow-up guidance posted as comments
(the most current guidance often lives there). This adds a bounded `comments`
field to the WorkItem (and carries it through TRIAGE onto the WorkOrder the
implementer reads). These tests cover:

  - parse_gh_issues maps a `comments` array (gh's shape) onto the WorkItem;
  - the bound (most-recent N + per-body cap) holds;
  - to_dict/from_dict roundtrip comments;
  - the comment thread survives PULL into the committed work_items slot;
  - TRIAGE carries comments onto the accepted WorkOrder;
  - gh_issue_source fetches comments per issue via the injectable runner with
    NO network (the determinism seam), and tolerates a per-issue fetch failure.

Owner: changyu87
"""

import json
import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import work_intake as wi  # noqa: E402


# A `gh issue list --json ...` payload that ALSO carries a comments array (as
# `gh issue view --json comments` returns: author object + createdAt + body).
GH_JSON_WITH_COMMENTS = """[
  {
    "number": 213,
    "title": "PULL should include issue comments",
    "body": "## Problem\\nThe loop is blind to comments.",
    "url": "https://github.com/changyu87/repo/issues/213",
    "state": "OPEN",
    "labels": [{"name": "enhancement"}],
    "author": {"login": "changyu87"},
    "createdAt": "2026-06-21T06:43:57Z",
    "updatedAt": "2026-06-21T07:00:00Z",
    "comments": [
      {"author": {"login": "alice"}, "createdAt": "2026-06-21T08:00:00Z",
       "body": "Actually fetch most-recent N."},
      {"author": {"login": "changyu87"}, "createdAt": "2026-06-21T09:00:00Z",
       "body": "Agreed, keep it bounded."}
    ]
  }
]"""


# ==========================================================================
# parse_gh_issues maps the comments array onto the WorkItem
# ==========================================================================

def test_parse_maps_comments_onto_workitem():
    items = wi.parse_gh_issues(GH_JSON_WITH_COMMENTS)
    assert len(items) == 1
    comments = items[0].comments
    assert len(comments) == 2
    assert comments[0]["author"] == "alice"
    assert comments[0]["created_at"] == "2026-06-21T08:00:00Z"
    assert comments[0]["body"] == "Actually fetch most-recent N."
    assert comments[1]["author"] == "changyu87"


def test_issue_without_comments_field_gets_empty_list():
    # `gh issue list` (no comments field) -> empty, never an error.
    payload = json.dumps([{
        "number": 1, "title": "t", "body": "b",
        "url": "https://github.com/o/r/issues/1", "state": "OPEN",
        "labels": [], "author": {"login": "x"},
        "createdAt": "2026-06-01T00:00:00Z", "updatedAt": "2026-06-01T00:00:00Z",
    }])
    items = wi.parse_gh_issues(payload)
    assert items[0].comments == []


# ==========================================================================
# Bounding: most-recent N + per-comment body cap
# ==========================================================================

def test_comments_bounded_to_most_recent_n():
    raw = [{"author": {"login": "u"}, "createdAt": f"t{i}",
            "body": f"c{i}"} for i in range(wi.MAX_COMMENTS_PER_ITEM + 5)]
    bounded = wi._normalize_comments(raw)
    assert len(bounded) == wi.MAX_COMMENTS_PER_ITEM
    # The MOST RECENT (trailing) comments are kept (gh returns oldest-first).
    assert bounded[-1]["body"] == f"c{wi.MAX_COMMENTS_PER_ITEM + 4}"
    assert bounded[0]["body"] == "c5"


def test_long_comment_body_is_capped():
    big = "x" * (wi.MAX_COMMENT_BODY_CHARS + 500)
    bounded = wi._normalize_comments(
        [{"author": {"login": "u"}, "createdAt": "t", "body": big}])
    assert len(bounded) == 1
    assert len(bounded[0]["body"]) <= wi.MAX_COMMENT_BODY_CHARS + len(
        "\n... [comment truncated]")
    assert bounded[0]["body"].endswith("[comment truncated]")


def test_normalize_tolerates_missing_author_and_body():
    bounded = wi._normalize_comments([{"createdAt": "t"}])
    assert bounded == [{"author": "", "created_at": "t", "body": ""}]


# ==========================================================================
# Schema roundtrip: comments survive to_dict/from_dict + schema version bump
# ==========================================================================

def test_workitem_comments_roundtrip():
    item = wi.parse_gh_issues(GH_JSON_WITH_COMMENTS)[0]
    d = item.to_dict()
    assert d["comments"] == item.comments
    assert wi.WorkItem.from_dict(d) == item


def test_workitem_schema_version_bumped_for_comments():
    assert wi.WORK_ITEM_SCHEMA_VERSION == "1.1.0"
    assert wi.WORK_ITEMS_SLOT["version"] == "1.1.0"


def test_workorder_comments_roundtrip_and_version():
    order = wi.WorkOrder(
        id="wo-x#1", work_item_id="x#1", title="t", body="b",
        url="u", decision="accepted", reason="",
        comments=[{"author": "a", "created_at": "t", "body": "hi"}])
    d = order.to_dict()
    assert d["comments"] == order.comments
    assert wi.WorkOrder.from_dict(d) == order
    # The additive target_feature field (issue #258) bumped the WorkOrder schema
    # to 1.2.0; comments are still carried unchanged.
    assert wi.WORK_ORDER_SCHEMA_VERSION == "1.2.0"
    assert wi.WORK_ORDERS_SLOT["version"] == "1.2.0"


# ==========================================================================
# PULL E2E: the comment thread survives into the committed work_items slot
# ==========================================================================

def test_pull_commits_comments_into_slot():
    ctx = fc.TickContext()
    ctx.register_slot(wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
                      version=wi.WORK_ITEMS_SLOT["version"])
    items = wi.parse_gh_issues(GH_JSON_WITH_COMMENTS)

    def source(repo=None, issue_filter=None):
        return list(items)

    result = wi.Pull(source=source).run(ctx)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result,
                    fc.SignalVocabulary(wi.PULL_SIGNALS))
    written = ctx.read("work_items")
    assert written[0]["comments"][1]["body"] == "Agreed, keep it bounded."


# ==========================================================================
# TRIAGE carries comments onto the accepted WorkOrder (implementer reads these)
# ==========================================================================

def test_triage_carries_comments_onto_workorder():
    from datetime import datetime, timezone
    ctx = fc.TickContext()
    ctx.register_slot(wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
                      version=wi.WORK_ITEMS_SLOT["version"])
    ctx.register_slot(wi.WORK_ORDERS_SLOT["name"], wi.WORK_ORDERS_SLOT["schema"],
                      version=wi.WORK_ORDERS_SLOT["version"])
    ctx.register_slot(wi.CROSS_CUTTING_RISK_SLOT["name"],
                      wi.CROSS_CUTTING_RISK_SLOT["schema"],
                      version=wi.CROSS_CUTTING_RISK_SLOT["version"])
    items = wi.parse_gh_issues(GH_JSON_WITH_COMMENTS)
    ctx.write("work_items", [it.to_dict() for it in items])

    state = wi.Triage(now=datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc))
    result = state.run(ctx)
    fc.apply_result(ctx, wi.TRIAGE_MANIFEST, result,
                    fc.SignalVocabulary(wi.TRIAGE_SIGNALS))
    orders = ctx.read("work_orders")
    assert len(orders) == 1
    assert len(orders[0]["comments"]) == 2
    assert orders[0]["comments"][0]["author"] == "alice"


# ==========================================================================
# gh_issue_source fetches comments per issue via the injectable runner (no net)
# ==========================================================================

class _FakeProc:
    def __init__(self, stdout):
        self.stdout = stdout


def test_gh_issue_source_fetches_comments_per_issue():
    list_payload = json.dumps([{
        "number": 7, "title": "t", "body": "b",
        "url": "https://github.com/o/r/issues/7", "state": "OPEN",
        "labels": [], "author": {"login": "x"},
        "createdAt": "2026-06-01T00:00:00Z", "updatedAt": "2026-06-01T00:00:00Z",
    }])
    comment_payload = json.dumps({"comments": [
        {"author": {"login": "bob"}, "createdAt": "2026-06-02T00:00:00Z",
         "body": "follow-up guidance"}]})
    calls = []

    def runner(cmd, capture_output=True, text=True, check=True):
        calls.append(cmd)
        if cmd[1] == "issue" and cmd[2] == "list":
            return _FakeProc(list_payload)
        if cmd[1] == "issue" and cmd[2] == "view":
            assert "7" in cmd  # views the right issue number
            return _FakeProc(comment_payload)
        raise AssertionError(f"unexpected gh call: {cmd}")

    items = wi.gh_issue_source(repo="o/r", runner=runner)
    assert len(items) == 1
    assert items[0].comments == [
        {"author": "bob", "created_at": "2026-06-02T00:00:00Z",
         "body": "follow-up guidance"}]
    # exactly one list + one per-issue view
    assert sum(1 for c in calls if c[2] == "list") == 1
    assert sum(1 for c in calls if c[2] == "view") == 1


def test_gh_issue_source_tolerates_comment_fetch_failure():
    list_payload = json.dumps([{
        "number": 7, "title": "t", "body": "b",
        "url": "https://github.com/o/r/issues/7", "state": "OPEN",
        "labels": [], "author": {"login": "x"},
        "createdAt": "2026-06-01T00:00:00Z", "updatedAt": "2026-06-01T00:00:00Z",
    }])

    def runner(cmd, capture_output=True, text=True, check=True):
        if cmd[2] == "list":
            return _FakeProc(list_payload)
        raise RuntimeError("comment fetch blew up")

    # A failing per-issue comment fetch must NOT sink PULL; the item just has
    # an empty comments list.
    items = wi.gh_issue_source(repo="o/r", runner=runner)
    assert len(items) == 1
    assert items[0].comments == []
