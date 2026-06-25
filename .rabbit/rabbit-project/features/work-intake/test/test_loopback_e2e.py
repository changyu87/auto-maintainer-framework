#!/usr/bin/env python3
"""End-to-end + unit tests for the §3.11.5 loopback / provenance guard.

The maintainer does NOT auto-work its own filings: items the loop filed itself
carry the provenance stamp `gh_issue_file_sink` writes — the
`filed-by:autonomous-maintainer` label AND the `<!-- am-dedup:<key> -->` body
marker. Enforcement is a deterministic PULL-side EXCLUSION (not a TRIAGE reject,
which would route to the doer's close path and CLOSE the discovery). `PULL`
drops any work_item for which `work_intake.is_loop_filed(item)` is true, so
loop-filed items never enter the pipeline and stay open for human triage.

These tests are fully deterministic — no network. They cover:
  1. is_loop_filed recognizes a label-stamped item (True), an am-dedup-body
     item (True), and a normal item (False); tolerant of a WorkItem or a dict.
  2. The stamper (gh_issue_file_sink) and the recognizer (is_loop_filed) agree
     on ONE source of truth: a discovery filed through the real sink assembly,
     re-read as a WorkItem, is recognized as loop-filed (round-trip).
  3. PULL with a stub source mixing normal + loop-filed items EXCLUDES the
     loop-filed ones from work_items (assert survivors + OK/EMPTY).

Owner: changyu87
"""

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


def _normal_item(number=7):
    return wi.WorkItem(
        id=f"acme/widget#{number}",
        number=number,
        title="Crash on empty config",
        body="Steps to reproduce ...",
        url=f"https://github.com/acme/widget/issues/{number}",
        state="OPEN",
        labels=["bug", "p1"],
        author="octocat",
        created_at="2026-05-01T10:00:00Z",
        updated_at="2026-05-02T11:30:00Z",
    )


def _label_stamped_item(number=21):
    item = _normal_item(number)
    item.labels = ["bug", wi.LOOP_FILED_LABEL]
    return item


def _body_marked_item(number=22):
    item = _normal_item(number)
    item.labels = ["enhancement"]
    item.body = f"Found a flaky test.\n\n{wi._am_dedup_marker('am-007')}"
    return item


def _stub_source(items):
    def source(repo=None):
        return list(items)
    return source


def _fresh_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(
        wi.WORK_ITEMS_SLOT["name"],
        wi.WORK_ITEMS_SLOT["schema"],
        version=wi.WORK_ITEMS_SLOT["version"],
    )
    return ctx


# ==========================================================================
# Behaviour: the provenance constants are ONE shared source of truth — the
# stamper (gh_issue_file_sink) writes exactly the label + marker the
# recognizer (is_loop_filed) reads.
# ==========================================================================

def test_loop_filed_label_constant_is_the_provenance_label():
    assert wi.LOOP_FILED_LABEL == "filed-by:autonomous-maintainer"
    # The sink stamps THIS constant (no second literal).
    assert wi.FILED_BY_LABEL == wi.LOOP_FILED_LABEL


def test_am_dedup_marker_prefix_constant_matches_emitted_marker():
    marker = wi._am_dedup_marker("am-123")
    assert marker.startswith(wi.AM_DEDUP_MARKER_PREFIX)
    assert wi.AM_DEDUP_MARKER_PREFIX == "<!-- am-dedup:"


# ==========================================================================
# Behaviour: is_loop_filed — True for a label-stamped item, True for an
# am-dedup-body item, False for a normal item; tolerant of WorkItem or dict.
# ==========================================================================

def test_is_loop_filed_true_for_label_stamped_item():
    assert wi.is_loop_filed(_label_stamped_item()) is True
    assert wi.is_loop_filed(_label_stamped_item().to_dict()) is True


def test_is_loop_filed_true_for_am_dedup_body_item():
    assert wi.is_loop_filed(_body_marked_item()) is True
    assert wi.is_loop_filed(_body_marked_item().to_dict()) is True


def test_is_loop_filed_false_for_normal_item():
    assert wi.is_loop_filed(_normal_item()) is False
    assert wi.is_loop_filed(_normal_item().to_dict()) is False


# ==========================================================================
# Behaviour (round-trip): the stamper and recognizer agree. A discovery filed
# through the real gh_issue_file_sink assembly produces a label + body marker
# that, re-read as a WorkItem, is_loop_filed recognizes as loop-filed.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def test_stamper_and_recognizer_agree_round_trip():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted("https://github.com/acme/widget/issues/55\n")

    discovery = wi.DiscoveredIssue(
        title="Flaky test in foo",
        body="Observed the failure under load.",
        kind="bug",
        severity="medium",
        target="project",
        dedup_key="am-roundtrip",
    )
    wi.gh_issue_file_sink(discovery, repo="acme/widget", runner=fake_runner)

    cmd = captured["cmd"]
    filed_label = cmd[cmd.index("--label") + 1]
    filed_body = cmd[cmd.index("--body") + 1]

    # Reconstruct the WorkItem a later PULL would build from the filed issue.
    refiled = wi.WorkItem(
        id="acme/widget#55", number=55, title=discovery.title,
        body=filed_body, url="https://github.com/acme/widget/issues/55",
        state="OPEN", labels=[filed_label], author="autonomous-maintainer",
        created_at="2026-05-01T10:00:00Z", updated_at="2026-05-01T10:00:00Z",
    )
    # The recognizer agrees on BOTH stamps the sink wrote.
    assert wi.is_loop_filed(refiled) is True
    # And each stamp alone is sufficient.
    label_only = wi.WorkItem.from_dict(refiled.to_dict())
    label_only.body = "no marker here"
    assert wi.is_loop_filed(label_only) is True
    body_only = wi.WorkItem.from_dict(refiled.to_dict())
    body_only.labels = ["bug"]
    assert wi.is_loop_filed(body_only) is True


# ==========================================================================
# E2E Behaviour: PULL EXCLUDES loop-filed items. A stub source mixing normal
# and loop-filed items writes ONLY the surviving normal items to work_items,
# and emits OK because survivors remain.
# ==========================================================================

def test_pull_excludes_loop_filed_items_keeps_survivors():
    # work_own_filings=False is the §3.11.5 opt-out: PULL EXCLUDES loop-filed
    # items. The default (work_own_filings=True) now INCLUDES them, so this
    # exclusion test pins the opt-out explicitly.
    ctx = _fresh_ctx()
    items = [
        _normal_item(7),
        _label_stamped_item(21),
        _body_marked_item(22),
        _normal_item(9),
    ]
    state = wi.Pull(source=_stub_source(items), work_own_filings=False)

    result = state.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    written = ctx.read("work_items")
    survivors = {w["number"] for w in written}
    # Only the two NORMAL items survive; both loop-filed items are excluded.
    assert survivors == {7, 9}
    assert all(not wi.is_loop_filed(w) for w in written)


def test_pull_all_loop_filed_emits_empty():
    # Opt-out (work_own_filings=False): an only-loop-filed batch is EMPTY.
    ctx = _fresh_ctx()
    items = [_label_stamped_item(21), _body_marked_item(22)]
    state = wi.Pull(source=_stub_source(items), work_own_filings=False)

    result = state.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)
    assert ctx.read("work_items") == []


# ==========================================================================
# E2E Behaviour: default-ON policy (work_own_filings=True). The owner flipped
# §3.11.5 so the loop works its own filings unless opted out. PULL with the
# DEFAULT flag (and an explicit True) INCLUDES loop-filed items; the exclusion
# only applies under the explicit opt-out (work_own_filings=False).
# ==========================================================================

def test_pull_default_includes_loop_filed_items():
    # No work_own_filings arg -> the new default (True) -> include loop-filed.
    ctx = _fresh_ctx()
    items = [
        _normal_item(7),
        _label_stamped_item(21),
        _body_marked_item(22),
        _normal_item(9),
    ]
    state = wi.Pull(source=_stub_source(items))  # default work_own_filings

    result = state.run(ctx)
    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    written = ctx.read("work_items")
    numbers = {w["number"] for w in written}
    # ALL four items survive — the loop-filed ones are NOT excluded by default.
    assert numbers == {7, 9, 21, 22}
    # The loop-filed items are present in the slot.
    assert any(wi.is_loop_filed(w) for w in written)


def test_pull_work_own_filings_true_includes_loop_filed_items():
    # Explicit True behaves exactly like the default: include loop-filed.
    ctx = _fresh_ctx()
    items = [
        _normal_item(7),
        _label_stamped_item(21),
        _body_marked_item(22),
        _normal_item(9),
    ]
    state = wi.Pull(source=_stub_source(items), work_own_filings=True)

    result = state.run(ctx)
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx, wi.PULL_MANIFEST, result, vocab)

    written = ctx.read("work_items")
    assert {w["number"] for w in written} == {7, 9, 21, 22}


def test_pull_mixed_batch_keeps_non_loop_items_either_way():
    # The non-loop items survive regardless of the flag; only the loop-filed
    # items' inclusion depends on it.
    items = [_normal_item(7), _label_stamped_item(21), _normal_item(9)]

    ctx_incl = _fresh_ctx()
    wi_pull_incl = wi.Pull(source=_stub_source(items), work_own_filings=True)
    res_incl = wi_pull_incl.run(ctx_incl)
    vocab = fc.SignalVocabulary(wi.PULL_SIGNALS)
    fc.apply_result(ctx_incl, wi.PULL_MANIFEST, res_incl, vocab)
    incl = {w["number"] for w in ctx_incl.read("work_items")}

    ctx_excl = _fresh_ctx()
    wi_pull_excl = wi.Pull(source=_stub_source(items), work_own_filings=False)
    res_excl = wi_pull_excl.run(ctx_excl)
    fc.apply_result(ctx_excl, wi.PULL_MANIFEST, res_excl, vocab)
    excl = {w["number"] for w in ctx_excl.read("work_items")}

    # Non-loop items {7, 9} present in BOTH; the loop-filed 21 only when included.
    assert {7, 9}.issubset(incl)
    assert {7, 9}.issubset(excl)
    assert 21 in incl
    assert 21 not in excl


def test_pull_only_loop_filed_signal_depends_on_flag():
    # An only-loop-filed batch yields OK when work_own_filings=True (the items
    # survive) and EMPTY when False (they are all excluded).
    items = [_label_stamped_item(21), _body_marked_item(22)]

    ctx_true = _fresh_ctx()
    res_true = wi.Pull(source=_stub_source(items), work_own_filings=True).run(ctx_true)
    assert res_true.signal == "OK"

    ctx_false = _fresh_ctx()
    res_false = wi.Pull(source=_stub_source(items), work_own_filings=False).run(ctx_false)
    assert res_false.signal == "EMPTY"
