#!/usr/bin/env python3
"""End-to-end tests for the optional issue_filter narrowing on PULL.

Public surface item 5: PULL optionally narrows WHICH open issues it pulls, from
the canonical normalized `issue_filter` object
`{labels: List[List[str]], title_pattern: str|None}`:

  - Labels (DNF, OR-of-ANDs) — SERVER-SIDE: one `gh issue list --label ...`
    query per AND-group (repeated `--label` is gh's native AND), unioned+deduped
    by issue number. Empty labels => the single existing all-open query.
  - Title (title_pattern) — POST-FETCH regex `search` over each fetched title,
    applied BEFORE comment enrichment. null => no-op.
  - The two compose (labels DNF AND title). Default filter is a no-op.

The live `gh` call sits behind the injectable subprocess `runner`; a fake runner
records the commands issued and returns fixture payloads so there is NO network.

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


# --------------------------------------------------------------------------
# A fake subprocess runner: records commands, serves fixture issue payloads.
# --------------------------------------------------------------------------

def _issue(number, title, labels):
    return {
        "number": number,
        "title": title,
        "body": "",
        "url": "https://github.com/acme/widget/issues/%d" % number,
        "state": "OPEN",
        "labels": [{"name": name} for name in labels],
        "author": {"login": "octocat"},
        "createdAt": "2026-05-01T10:00:00Z",
        "updatedAt": "2026-05-02T11:30:00Z",
    }


class _Result:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


class FakeRunner:
    """Serves `gh issue list` from a group->issues map and `gh issue view
    <n> --json comments` from a per-number comment map. Records every command
    so tests can assert exactly which list-queries and which comment-fetches ran.
    """

    def __init__(self, list_by_group, comments_by_number=None):
        # list_by_group: {frozenset(labels): [issue_dict, ...]}; the key ()
        # (empty frozenset) is the all-open query result.
        self._list_by_group = list_by_group
        self._comments_by_number = comments_by_number or {}
        self.list_calls = []      # list of tuple(labels) requested
        self.view_numbers = []    # issue numbers whose comments were fetched

    def __call__(self, cmd, capture_output=True, text=True, check=True):
        if cmd[:3] == ["gh", "issue", "list"]:
            labels = tuple(
                cmd[i + 1] for i, tok in enumerate(cmd) if tok == "--label")
            self.list_calls.append(labels)
            issues = self._list_by_group.get(frozenset(labels), [])
            return _Result(json.dumps(issues))
        if cmd[:3] == ["gh", "issue", "view"]:
            number = int(cmd[3])
            self.view_numbers.append(number)
            comments = self._comments_by_number.get(number, [])
            return _Result(json.dumps({"comments": comments}))
        raise AssertionError("unexpected command: %r" % (cmd,))


# ==========================================================================
# (a) Empty filter => the single existing all-open query (non-breaking).
# ==========================================================================

def test_empty_filter_runs_single_all_open_query():
    runner = FakeRunner({frozenset(): [_issue(7, "A", ["bug"]),
                                        _issue(9, "B", [])]})
    items = wi.gh_issue_source(
        issue_filter={"labels": [], "title_pattern": None}, runner=runner)

    assert [it.number for it in items] == [7, 9]
    # Exactly one list query, with NO --label.
    assert runner.list_calls == [()]


def test_none_filter_is_also_no_op():
    runner = FakeRunner({frozenset(): [_issue(7, "A", ["bug"])]})
    items = wi.gh_issue_source(issue_filter=None, runner=runner)
    assert [it.number for it in items] == [7]
    assert runner.list_calls == [()]


# ==========================================================================
# (b) Single AND-group => one query with a --label per label.
# ==========================================================================

def test_single_and_group_runs_one_query_with_label_per_label():
    runner = FakeRunner({
        frozenset({"bug", "p1"}): [_issue(7, "A", ["bug", "p1"])],
    })
    items = wi.gh_issue_source(
        issue_filter={"labels": [["bug", "p1"]], "title_pattern": None},
        runner=runner)

    assert [it.number for it in items] == [7]
    assert len(runner.list_calls) == 1
    assert set(runner.list_calls[0]) == {"bug", "p1"}
    # Only the matching issue's comments were fetched.
    assert runner.view_numbers == [7]


# ==========================================================================
# (c) Multi-group DNF => one query per group, unioned+deduped by number.
# ==========================================================================

def test_multi_group_dnf_unions_and_dedupes_by_number():
    runner = FakeRunner({
        frozenset({"bug"}): [_issue(7, "A", ["bug"]),
                             _issue(9, "Shared", ["bug", "p2"])],
        frozenset({"p2"}): [_issue(9, "Shared", ["bug", "p2"]),
                            _issue(11, "C", ["p2"])],
    })
    items = wi.gh_issue_source(
        issue_filter={"labels": [["bug"], ["p2"]], "title_pattern": None},
        runner=runner)

    # One query per AND-group.
    assert len(runner.list_calls) == 2
    # Union deduped by number, first-seen order preserved.
    assert [it.number for it in items] == [7, 9, 11]
    # Comments fetched once per surviving (deduped) issue.
    assert sorted(runner.view_numbers) == [7, 9, 11]


# ==========================================================================
# (d) title_pattern drops non-matches BEFORE comment enrichment.
# ==========================================================================

def test_title_pattern_drops_nonmatches_before_comment_fetch():
    runner = FakeRunner({
        frozenset(): [_issue(7, "Crash on empty config", ["bug"]),
                      _issue(9, "Add retry knob", [])],
    })
    items = wi.gh_issue_source(
        issue_filter={"labels": [], "title_pattern": r"[Cc]rash"},
        runner=runner)

    assert [it.number for it in items] == [7]
    # The dropped issue's comments were NEVER fetched (post-fetch filter runs
    # BEFORE the per-issue comment enrichment).
    assert runner.view_numbers == [7]


def test_title_pattern_is_a_regex_search():
    runner = FakeRunner({
        frozenset(): [_issue(7, "prefix-BUG-suffix", []),
                      _issue(9, "clean", [])],
    })
    items = wi.gh_issue_source(
        issue_filter={"labels": [], "title_pattern": r"BUG"}, runner=runner)
    assert [it.number for it in items] == [7]


# ==========================================================================
# (e) label + title compose (must clear labels DNF AND match title).
# ==========================================================================

def test_labels_and_title_compose():
    runner = FakeRunner({
        frozenset({"bug"}): [_issue(7, "Crash here", ["bug"]),
                             _issue(9, "Feature ask", ["bug"])],
    })
    items = wi.gh_issue_source(
        issue_filter={"labels": [["bug"]], "title_pattern": r"Crash"},
        runner=runner)

    # #9 clears the label group but fails the title => dropped.
    assert [it.number for it in items] == [7]
    assert runner.view_numbers == [7]


# ==========================================================================
# Pull-level: the source receives the issue_filter Pull was constructed with.
# ==========================================================================

def test_pull_threads_issue_filter_into_source():
    captured = {}

    def stub_source(repo=None, issue_filter=None):
        captured["repo"] = repo
        captured["issue_filter"] = issue_filter
        return wi.parse_gh_issues(json.dumps([_issue(7, "A", ["bug"])]))

    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])

    the_filter = {"labels": [["bug"]], "title_pattern": r"A"}
    state = wi.Pull(source=stub_source, repo="acme/widget",
                    issue_filter=the_filter)
    result = state.run(ctx)

    assert result.signal == "OK"
    assert captured["repo"] == "acme/widget"
    assert captured["issue_filter"] == the_filter


def test_pull_default_no_filter_passes_all_items_through():
    seen = {}

    def stub_source(repo=None, issue_filter=None):
        seen["issue_filter"] = issue_filter
        return wi.parse_gh_issues(
            json.dumps([_issue(7, "A", ["bug"]), _issue(9, "B", [])]))

    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"], wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"])

    state = wi.Pull(source=stub_source)
    result = state.run(ctx)

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    assert seen["issue_filter"] is None
    assert [it["number"] for it in ctx.read("work_items")] == [7, 9]
