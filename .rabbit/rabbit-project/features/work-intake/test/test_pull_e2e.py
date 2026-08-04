#!/usr/bin/env python3
"""End-to-end + unit tests for the work-intake slice-1 GitHub-Issues PULL adapter.

Every slice-1 spec behaviour has a deterministic test here. The live `gh` call
sits behind an INJECTABLE issue source; tests pass a stub returning fixture
issues so there is NO network and the suite is fully reproducible (spec-rules
§1: the only non-deterministic edge is isolated to the fetch boundary).

The e2e tests drive PULL exactly as tick-orchestrator will — building a real
fsm-contracts TickContext, registering the `work_items` slot, running the state,
and committing its StateResult through `fc.apply_result` under the state's
manifest + signal vocabulary (the bounded-scope contract).

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Consume fsm-contracts unchanged (sibling feature), via sys.path as the
# fsm-contracts tests themselves do.
_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import work_intake as wi  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a stub issue source and a captured `gh ... --json` output string.
# --------------------------------------------------------------------------

# Two issues as the deterministic `gh` CLI emits them: author is an object with
# a `login`, labels is a list of objects with a `name`, timestamps camelCase.
GH_JSON_FIXTURE = """[
  {
    "number": 7,
    "title": "Crash on empty config",
    "body": "Steps to reproduce ...",
    "url": "https://github.com/acme/widget/issues/7",
    "state": "OPEN",
    "labels": [{"name": "bug"}, {"name": "p1"}],
    "author": {"login": "octocat"},
    "createdAt": "2026-05-01T10:00:00Z",
    "updatedAt": "2026-05-02T11:30:00Z"
  },
  {
    "number": 9,
    "title": "Add retry knob",
    "body": "",
    "url": "https://github.com/acme/widget/issues/9",
    "state": "OPEN",
    "labels": [],
    "author": {"login": "hubber"},
    "createdAt": "2026-05-03T08:00:00Z",
    "updatedAt": "2026-05-03T08:00:00Z"
  }
]"""


def _stub_source(issues):
    """Return a zero-arg-ish callable source yielding parsed WorkItems.

    The injectable source contract is: callable(repo) -> list[WorkItem].
    The stub ignores repo and returns a fixed list, so no network is touched.
    """
    def source(repo=None, issue_filter=None):
        return list(issues)
    return source


def _fixture_workitems():
    return wi.parse_gh_issues(GH_JSON_FIXTURE)


def _fresh_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"],
        wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"],
    )
    return ctx


# ==========================================================================
# Behaviour: WorkItem slot schema — typed, machine-first, versioned roundtrip
# ==========================================================================

def test_workitem_roundtrips_through_dict():
    item = wi.WorkItem(
        id="acme/widget#7",
        number=7,
        title="Crash on empty config",
        body="Steps ...",
        url="https://github.com/acme/widget/issues/7",
        state="OPEN",
        labels=["bug", "p1"],
        author="octocat",
        created_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-02T11:30:00Z",
    )
    d = item.to_dict()
    assert wi.WorkItem.from_dict(d) == item


def test_workitem_dict_carries_schema_version():
    item = _fixture_workitems()[0]
    d = item.to_dict()
    assert d["schema_version"] == wi.WORK_ITEM_SCHEMA_VERSION
    assert wi.WORK_ITEM_SCHEMA_VERSION  # non-empty


def test_workitem_labels_is_a_list_of_strings():
    item = _fixture_workitems()[0]
    assert item.labels == ["bug", "p1"]
    assert all(isinstance(label, str) for label in item.labels)


# ==========================================================================
# Behaviour: gh JSON -> WorkItem mapping (unit, against a captured fixture)
# ==========================================================================

def test_parse_gh_issues_maps_every_field():
    items = wi.parse_gh_issues(GH_JSON_FIXTURE)
    assert len(items) == 2
    first = items[0]
    assert first.number == 7
    assert first.title == "Crash on empty config"
    assert first.body == "Steps to reproduce ..."
    assert first.url == "https://github.com/acme/widget/issues/7"
    assert first.state == "OPEN"
    assert first.labels == ["bug", "p1"]          # list[name]
    assert first.author == "octocat"              # author.login
    assert first.created_at == "2026-05-01T10:00:00Z"   # createdAt
    assert first.updated_at == "2026-05-02T11:30:00Z"   # updatedAt
    assert first.id                                # a stable id is derived


def test_parse_gh_issues_handles_empty_labels_and_body():
    items = wi.parse_gh_issues(GH_JSON_FIXTURE)
    second = items[1]
    assert second.labels == []
    assert second.body == ""
    assert second.author == "hubber"


# ==========================================================================
# E2E Behaviour: PULL with a stub returning 2+ issues -> work_items + OK
# ==========================================================================

def test_pull_e2e_with_issues_writes_slot_and_emits_ok():
    ctx = _fresh_ctx()
    source = _stub_source(_fixture_workitems())
    state = wi.Pull(source=source)

    result = state.run(ctx)

    # The result is a well-formed fsm-contracts StateResult.
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    # Commit through the bounded-scope contract: manifest + vocabulary.
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    written = ctx.read("work_items")
    assert isinstance(written, list)
    assert len(written) == 2
    # Field mapping survives into the committed slot (incl. labels + author).
    assert written[0]["number"] == 7
    assert written[0]["labels"] == ["bug", "p1"]
    assert written[0]["author"] == "octocat"
    assert written[0]["schema_version"] == wi.WORK_ITEM_SCHEMA_VERSION


def test_pull_e2e_empty_issues_writes_empty_slot_and_emits_empty():
    ctx = _fresh_ctx()
    source = _stub_source([])
    state = wi.Pull(source=source)

    result = state.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    assert ctx.read("work_items") == []


# ==========================================================================
# Behaviour: per-state manifest is {reads: [], writes: [work_items],
# emits: [OK, EMPTY]} and conforms to the fsm-contracts manifest shape.
# ==========================================================================

def test_pull_manifest_declares_reads_writes_emits():
    m = wi.PULL_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ()
    assert m.writes == ("work_items",)
    assert set(m.emits) == {"OK", "EMPTY"}


def test_pull_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("EMPTY")
    assert not vocab.is_member("MAYBE")


def test_pull_rejects_signal_outside_manifest_via_apply_result():
    """apply_result enforces the bounded-scope contract: a PULL result may only
    emit OK/EMPTY and write only work_items. Sanity-check the wiring holds."""
    ctx = _fresh_ctx()
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    bad = fc.StateResult(signal="MAYBE", writes={})
    try:
        fc.apply_result(ctx, wi.PULL_MANIFEST, bad, vocab)
    except fc.ContractError:
        pass
    else:
        raise AssertionError("an undeclared signal must be rejected by apply_result")
