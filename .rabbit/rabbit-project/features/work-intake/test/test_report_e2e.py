#!/usr/bin/env python3
"""End-to-end + unit tests for the work-intake slice-3 REPORT outbound port.

REPORT is the write-side mirror of PULL: it turns `DiscoveredIssue`s into
durably-tracked tracker items via an INJECTABLE filing sink (mirroring
`gh_issue_source`). `file_discoveries` is PURE orchestration — dedup against a
known set, file the rest through the injected sink, and capture per-discovery
sink errors into `errors[]` without aborting the batch.

Determinism: the only non-deterministic edge — the live `gh issue create` call —
sits behind the injectable sink, so these tests pass a stub (or a fake
subprocess runner) with NO network (spec-rules §1: the failure is locatable to
the file boundary). `file_discoveries` performs no I/O of its own.

The shipped triager guard test asserts the v1.5.0 subagent definition no longer
rejects loop-filed items: the §3.11.5 loopback guard is enforced UPSTREAM at PULL
by exclusion (work_intake.is_loop_filed), so the triager never sees them.

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

import work_intake as wi  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a builder for DiscoveredIssues and a stub sink.
# --------------------------------------------------------------------------

def _discovery(dedup_key="am-001", title="Flaky test in foo",
               kind="bug", severity="medium", target="project"):
    return wi.DiscoveredIssue(
        title=title,
        body="Observed the failure under load.",
        kind=kind,
        severity=severity,
        target=target,
        dedup_key=dedup_key,
    )


def _recording_sink(ref_for=None):
    """A stub sink that records every call and returns a deterministic ref.

    `ref_for` maps dedup_key -> (tracker_ref, url); unmapped keys get a
    synthesized default. The injectable sink contract mirrors the production
    one: callable(discovery, repo=None) -> {tracker_ref, url}.
    """
    ref_for = ref_for or {}
    calls = []

    def sink(discovery, repo=None):
        calls.append((discovery, repo))
        if discovery.dedup_key in ref_for:
            tracker_ref, url = ref_for[discovery.dedup_key]
        else:
            tracker_ref = f"acme/widget#{len(calls)}"
            url = f"https://github.com/acme/widget/issues/{len(calls)}"
        return {"tracker_ref": tracker_ref, "url": url}

    sink.calls = calls
    return sink


# ==========================================================================
# Behaviour: DiscoveredIssue slot schema — typed, machine-first, versioned.
# ==========================================================================

def test_discovered_issue_roundtrips_through_dict():
    d = wi.DiscoveredIssue(
        title="Flaky test in foo",
        body="Observed the failure under load.",
        kind="bug",
        severity="medium",
        target="project",
        dedup_key="am-001",
    )
    as_dict = d.to_dict()
    assert wi.DiscoveredIssue.from_dict(as_dict) == d


def test_discovered_issue_dict_carries_schema_version():
    d = _discovery()
    as_dict = d.to_dict()
    assert as_dict["schema_version"] == wi.DISCOVERED_ISSUE_SCHEMA_VERSION
    assert wi.DISCOVERED_ISSUE_SCHEMA_VERSION  # non-empty


def test_discovered_issue_default_filed_by_is_loop_provenance():
    """filed_by stamps the loop's provenance, defaulting to the loop marker."""
    d = _discovery()
    assert d.filed_by == "autonomous-maintainer"
    assert d.to_dict()["filed_by"] == "autonomous-maintainer"


# ==========================================================================
# E2E Behaviour: file_discoveries files NEW discoveries through the sink and
# records each in `filed` with its dedup_key, tracker_ref, url.
# ==========================================================================

def test_file_discoveries_files_new_through_sink():
    discoveries = [_discovery(dedup_key="am-001"),
                   _discovery(dedup_key="am-002")]
    sink = _recording_sink(ref_for={
        "am-001": ("acme/widget#11", "https://github.com/acme/widget/issues/11"),
        "am-002": ("acme/widget#12", "https://github.com/acme/widget/issues/12"),
    })

    result = wi.file_discoveries(discoveries, sink=sink, known_dedup_keys=())

    assert len(sink.calls) == 2          # both were filed through the sink
    assert result.skipped_existing == []
    assert result.errors == []
    filed_keys = {f["dedup_key"] for f in result.filed}
    assert filed_keys == {"am-001", "am-002"}
    by_key = {f["dedup_key"]: f for f in result.filed}
    assert by_key["am-001"]["tracker_ref"] == "acme/widget#11"
    assert by_key["am-001"]["url"] == (
        "https://github.com/acme/widget/issues/11")


# ==========================================================================
# E2E Behaviour: a discovery whose dedup_key is already KNOWN goes to
# skipped_existing with NO sink call (idempotent re-filing is a no-op).
# ==========================================================================

def test_file_discoveries_skips_known_dedup_keys_without_calling_sink():
    discoveries = [_discovery(dedup_key="am-001"),    # known -> skipped
                   _discovery(dedup_key="am-002")]    # new -> filed
    sink = _recording_sink()

    result = wi.file_discoveries(
        discoveries, sink=sink, known_dedup_keys=("am-001",))

    # Only the NEW discovery reached the sink.
    assert len(sink.calls) == 1
    assert sink.calls[0][0].dedup_key == "am-002"
    assert result.skipped_existing == ["am-001"]
    assert [f["dedup_key"] for f in result.filed] == ["am-002"]
    assert result.errors == []


# ==========================================================================
# E2E Behaviour: a per-discovery sink error is CAUGHT into errors[] and the
# batch CONTINUES (one bad discovery never aborts the whole filing run).
# ==========================================================================

def test_file_discoveries_catches_sink_error_without_aborting_batch():
    good = _discovery(dedup_key="am-001")
    bad = _discovery(dedup_key="am-002")
    after = _discovery(dedup_key="am-003")

    def sink(discovery, repo=None):
        if discovery.dedup_key == "am-002":
            raise RuntimeError("gh: rate limited")
        return {"tracker_ref": f"acme/widget#{discovery.dedup_key}",
                "url": f"https://github.com/acme/widget/issues/{discovery.dedup_key}"}

    result = wi.file_discoveries(
        [good, bad, after], sink=sink, known_dedup_keys=())

    # The error was captured, and the discovery AFTER it was still filed.
    filed_keys = {f["dedup_key"] for f in result.filed}
    assert filed_keys == {"am-001", "am-003"}
    assert len(result.errors) == 1
    err = result.errors[0]
    assert err["dedup_key"] == "am-002"
    assert "rate limited" in err["reason"]


def test_file_discoveries_empty_input_is_empty_result():
    result = wi.file_discoveries([], sink=_recording_sink(), known_dedup_keys=())
    assert result.filed == []
    assert result.skipped_existing == []
    assert result.skipped_open == []
    assert result.errors == []


# ==========================================================================
# E2E Behaviour (dedup-vs-open, #224): a discovery whose SUBJECT matches an
# ALREADY-OPEN tracker issue is NOT filed — it goes to skipped_open with NO
# sink call (it would be duplicate noise). A genuinely-new discovery, whose
# subject matches no open issue, is still filed normally.
# ==========================================================================

def _open_item(number, title):
    """A minimal open WorkItem (the shape PULL writes / scheduling passes as
    known_open)."""
    return wi.WorkItem(
        id=f"acme/widget#{number}", number=number, title=title, body="",
        url=f"https://github.com/acme/widget/issues/{number}", state="OPEN")


def test_file_discoveries_skips_discovery_matching_open_issue():
    # The open tracker already has an issue on the SAME subject as the discovery.
    open_items = [_open_item(209, "Add a model-backed REVIEW gate before merge")]
    discovery = _discovery(
        dedup_key="am-dup",
        title="Add a model-backed REVIEW gate before merge")
    sink = _recording_sink()

    result = wi.file_discoveries(
        [discovery], sink=sink, known_dedup_keys=(), known_open=open_items)

    # It duplicates an open issue -> NOT filed, NO sink call, recorded skipped.
    assert sink.calls == []
    assert result.filed == []
    assert result.skipped_existing == []
    assert len(result.skipped_open) == 1
    assert result.skipped_open[0]["dedup_key"] == "am-dup"
    assert result.skipped_open[0]["matched"] == "acme/widget#209"
    assert result.errors == []


def test_file_discoveries_files_new_discovery_not_matching_any_open_issue():
    # A genuinely-new subject: no open issue is about it -> filed normally.
    open_items = [_open_item(209, "Add a model-backed REVIEW gate before merge")]
    discovery = _discovery(
        dedup_key="am-new", title="Document the packaging build steps")
    sink = _recording_sink()

    result = wi.file_discoveries(
        [discovery], sink=sink, known_dedup_keys=(), known_open=open_items)

    assert len(sink.calls) == 1
    assert [f["dedup_key"] for f in result.filed] == ["am-new"]
    assert result.skipped_open == []
    assert result.errors == []


def test_file_discoveries_dedup_key_precedes_open_match():
    # A discovery that is BOTH already-filed (known_dedup_keys) AND matches an
    # open issue is recorded as skipped_existing (ledger idempotency takes
    # precedence), never double-counted in skipped_open.
    open_items = [_open_item(209, "Add a REVIEW gate before merge")]
    discovery = _discovery(
        dedup_key="am-known", title="Add a REVIEW gate before merge")
    sink = _recording_sink()

    result = wi.file_discoveries(
        [discovery], sink=sink, known_dedup_keys=("am-known",),
        known_open=open_items)

    assert sink.calls == []
    assert result.skipped_existing == ["am-known"]
    assert result.skipped_open == []


def test_file_discoveries_default_known_open_files_everything():
    # known_open defaults to empty: behaviour is unchanged when no open set is
    # supplied (back-compat with existing callers).
    discovery = _discovery(dedup_key="am-x", title="Anything at all here")
    sink = _recording_sink()
    result = wi.file_discoveries([discovery], sink=sink, known_dedup_keys=())
    assert len(sink.calls) == 1
    assert [f["dedup_key"] for f in result.filed] == ["am-x"]
    assert result.skipped_open == []


def test_match_open_issue_overlap_threshold():
    # The deterministic title-overlap heuristic: near-identical titles match;
    # an unrelated title does not. Stopwords + casing are normalized away.
    disc = _discovery(title="Serialize same-feature work orders")
    same = [_open_item(214, "Serialize the same-feature work-orders")]
    other = [_open_item(99, "Completely different unrelated subject line")]
    assert wi._match_open_issue(disc, same) == "acme/widget#214"
    assert wi._match_open_issue(disc, other) is None
    # An empty open set never matches.
    assert wi._match_open_issue(disc, []) is None


# ==========================================================================
# E2E Behaviour: gh_issue_file_sink assembles the `gh issue create` command
# with the provenance label, the am-dedup body marker, and title/body; and
# parses the created issue URL into a tracker_ref. Driven by an INJECTED fake
# subprocess runner — NO network.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, stdout):
        self.stdout = stdout
        self.returncode = 0


def test_gh_issue_file_sink_assembles_command_and_parses_ref():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return _FakeCompleted(
            "https://github.com/acme/widget/issues/42\n")

    discovery = wi.DiscoveredIssue(
        title="Flaky test in foo",
        body="Observed the failure under load.",
        kind="bug",
        severity="medium",
        target="project",
        dedup_key="am-xyz",
    )

    out = wi.gh_issue_file_sink(
        discovery, repo="acme/widget", runner=fake_runner)

    cmd = captured["cmd"]
    assert cmd[:3] == ["gh", "issue", "create"]
    # --repo carries the destination chosen by the caller.
    assert "--repo" in cmd
    assert cmd[cmd.index("--repo") + 1] == "acme/widget"
    # Title is passed verbatim.
    assert "--title" in cmd
    assert cmd[cmd.index("--title") + 1] == "Flaky test in foo"
    # The provenance label is stamped.
    assert "--label" in cmd
    assert cmd[cmd.index("--label") + 1] == "filed-by:autonomous-maintainer"
    # The body carries the original text AND the am-dedup marker EXACTLY.
    body = cmd[cmd.index("--body") + 1]
    assert "Observed the failure under load." in body
    assert "<!-- am-dedup:am-xyz -->" in body

    # The created URL is parsed into a tracker_ref + url.
    assert out["url"] == "https://github.com/acme/widget/issues/42"
    assert out["tracker_ref"] == "acme/widget#42"


def test_gh_issue_file_sink_omits_repo_flag_when_unset():
    captured = {}

    def fake_runner(cmd, **kwargs):
        captured["cmd"] = cmd
        return _FakeCompleted("https://github.com/acme/widget/issues/7\n")

    wi.gh_issue_file_sink(_discovery(dedup_key="k"), runner=fake_runner)
    assert "--repo" not in captured["cmd"]


# ==========================================================================
# E2E Behaviour (live-found bug): `gh issue create --label <L>` FAILS if label
# L is absent in the repo. The sink therefore first ENSURES the provenance
# label exists via `gh label create` (idempotent, check=False so an
# "already exists" non-zero exit is TOLERATED, never raised) BEFORE the
# `gh issue create`. Both gh calls honor --repo and the injectable runner.
# ==========================================================================

def test_gh_issue_file_sink_ensures_label_before_issue_create():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:3] == ["gh", "label", "create"]:
            return _FakeCompleted("")
        return _FakeCompleted("https://github.com/acme/widget/issues/42\n")

    out = wi.gh_issue_file_sink(
        _discovery(dedup_key="am-lbl"), repo="acme/widget", runner=fake_runner)

    # First gh call is the idempotent label-ensure, BEFORE the issue create.
    label_cmd, label_kwargs = calls[0]
    assert label_cmd[:3] == ["gh", "label", "create"]
    assert label_cmd[3] == "filed-by:autonomous-maintainer"
    assert "--description" in label_cmd
    # check=False so an "already exists" non-zero exit is tolerated.
    assert label_kwargs.get("check") is False
    # --repo carries through to the label-ensure.
    assert "--repo" in label_cmd
    assert label_cmd[label_cmd.index("--repo") + 1] == "acme/widget"

    # Second gh call is the issue create (check=True), carrying --repo too.
    create_cmd, create_kwargs = calls[1]
    assert create_cmd[:3] == ["gh", "issue", "create"]
    assert create_kwargs.get("check") is True
    assert create_cmd[create_cmd.index("--repo") + 1] == "acme/widget"

    assert out["tracker_ref"] == "acme/widget#42"


def test_gh_issue_file_sink_tolerates_nonzero_label_create_exit():
    """An "already exists" label-create exits non-zero; check=False means the
    runner does NOT raise, so the issue create still runs and returns a ref."""
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "label", "create"]:
            # Emulate `gh label create` on a pre-existing label: returns
            # non-zero. With check=False subprocess.run does NOT raise; the
            # fake mirrors that by simply returning a non-zero result.
            r = _FakeCompleted("label already exists")
            r.returncode = 1
            return r
        return _FakeCompleted("https://github.com/acme/widget/issues/8\n")

    out = wi.gh_issue_file_sink(
        _discovery(dedup_key="am-exists"), repo="acme/widget",
        runner=fake_runner)

    # The non-zero label-create did NOT abort filing: issue create still ran.
    assert calls[0][:3] == ["gh", "label", "create"]
    assert calls[1][:3] == ["gh", "issue", "create"]
    assert out["tracker_ref"] == "acme/widget#8"
    assert out["url"] == "https://github.com/acme/widget/issues/8"


def test_gh_issue_file_sink_label_ensure_omits_repo_when_unset():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["gh", "label", "create"]:
            return _FakeCompleted("")
        return _FakeCompleted("https://github.com/acme/widget/issues/3\n")

    wi.gh_issue_file_sink(_discovery(dedup_key="k"), runner=fake_runner)

    assert calls[0][:3] == ["gh", "label", "create"]
    assert "--repo" not in calls[0]
    assert "--repo" not in calls[1]


# ==========================================================================
# E2E Behaviour (provenance, end to end): a discovery filed through the real
# sink assembly carries BOTH the provenance label and the am-dedup marker, and
# file_discoveries records the parsed tracker_ref it returns.
# ==========================================================================

def test_file_discoveries_through_real_sink_assembly_stamps_provenance():
    seen = {}

    def fake_runner(cmd, **kwargs):
        seen["cmd"] = cmd
        return _FakeCompleted("https://github.com/acme/widget/issues/99\n")

    def sink(discovery, repo=None):
        return wi.gh_issue_file_sink(discovery, repo=repo, runner=fake_runner)

    result = wi.file_discoveries(
        [_discovery(dedup_key="am-prov")], sink=sink, known_dedup_keys=())

    cmd = seen["cmd"]
    assert cmd[cmd.index("--label") + 1] == "filed-by:autonomous-maintainer"
    assert "<!-- am-dedup:am-prov -->" in cmd[cmd.index("--body") + 1]
    assert result.filed[0]["tracker_ref"] == "acme/widget#99"


# ==========================================================================
# E2E Behaviour (apply_labels — PULL-visibility stamping): the sink stamps each
# apply_label alongside the provenance label on `gh issue create`, ENSURING each
# exists first via an idempotent `gh label create`. apply_labels None/[] leaves
# behaviour unchanged (only the provenance label). Driven by an INJECTED fake
# runner — NO network.
# ==========================================================================

def _label_flags(cmd):
    """The values of every `--label` flag in a gh argv."""
    return [cmd[i + 1] for i, v in enumerate(cmd) if v == "--label"]


def test_gh_issue_file_sink_stamps_apply_labels_and_ensures_them():
    calls = []

    def fake_runner(cmd, **kwargs):
        calls.append((cmd, kwargs))
        if cmd[:3] == ["gh", "label", "create"]:
            return _FakeCompleted("")
        return _FakeCompleted("https://github.com/acme/widget/issues/55\n")

    out = wi.gh_issue_file_sink(
        _discovery(dedup_key="am-al"), repo="acme/widget",
        apply_labels=["dci-team marketplace"], runner=fake_runner)

    # The issue create carries BOTH the provenance label AND the apply label.
    create_cmd = next(c for c, _ in calls if c[:3] == ["gh", "issue", "create"])
    flags = _label_flags(create_cmd)
    assert "filed-by:autonomous-maintainer" in flags
    assert "dci-team marketplace" in flags

    # Each label was ENSURED first via an idempotent `gh label create` (the
    # apply label AND the provenance label), check=False so a pre-existing
    # label's non-zero exit is tolerated.
    label_creates = [(c, k) for c, k in calls if c[:3] == ["gh", "label", "create"]]
    ensured = [c[3] for c, _ in label_creates]
    assert "filed-by:autonomous-maintainer" in ensured
    assert "dci-team marketplace" in ensured
    for _c, k in label_creates:
        assert k.get("check") is False

    assert out["tracker_ref"] == "acme/widget#55"


def test_gh_issue_file_sink_apply_labels_none_or_empty_unchanged():
    for apply_labels in (None, []):
        calls = []

        def fake_runner(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["gh", "label", "create"]:
                return _FakeCompleted("")
            return _FakeCompleted("https://github.com/acme/widget/issues/1\n")

        wi.gh_issue_file_sink(
            _discovery(dedup_key="k"), repo="acme/widget",
            apply_labels=apply_labels, runner=fake_runner)

        # Only the provenance label is stamped, and only it is ensured.
        create_cmd = next(c for c in calls if c[:3] == ["gh", "issue", "create"])
        assert _label_flags(create_cmd) == ["filed-by:autonomous-maintainer"]
        label_creates = [c for c in calls if c[:3] == ["gh", "label", "create"]]
        assert [c[3] for c in label_creates] == ["filed-by:autonomous-maintainer"]


# ==========================================================================
# E2E Behaviour: file_discoveries forwards apply_labels to the sink ONLY for
# project-target discoveries; a maintainer-self discovery is filed with
# apply_labels=[] (the fixed MAINTAINER_REPO has its own/no filter).
# ==========================================================================

def _recording_apply_labels_sink():
    """A stub sink recording the apply_labels it was invoked with per call."""
    calls = []

    def sink(discovery, apply_labels=None):
        calls.append((discovery, apply_labels))
        n = len(calls)
        return {"tracker_ref": f"acme/widget#{n}",
                "url": f"https://github.com/acme/widget/issues/{n}"}

    sink.calls = calls
    return sink


def test_file_discoveries_forwards_apply_labels_for_project_only():
    proj = _discovery(dedup_key="am-proj", target="project",
                      title="Project target discovery here")
    selfd = _discovery(dedup_key="am-self", target="maintainer-self",
                       title="Maintainer self discovery here")
    sink = _recording_apply_labels_sink()

    result = wi.file_discoveries(
        [proj, selfd], sink=sink, known_dedup_keys=(),
        apply_labels=["dci-team marketplace"])

    by_key = {d.dedup_key: al for d, al in sink.calls}
    # project-target -> gets the apply labels; maintainer-self -> [].
    assert by_key["am-proj"] == ["dci-team marketplace"]
    assert by_key["am-self"] == []
    assert {f["dedup_key"] for f in result.filed} == {"am-proj", "am-self"}


def test_file_discoveries_default_apply_labels_unchanged():
    # apply_labels defaults to None: the sink is invoked exactly as before
    # (a stub taking only `(discovery, repo=None)` must still work).
    discovery = _discovery(dedup_key="am-x", title="Anything at all here")
    sink = _recording_sink()
    result = wi.file_discoveries([discovery], sink=sink, known_dedup_keys=())
    assert len(sink.calls) == 1
    assert [f["dedup_key"] for f in result.filed] == ["am-x"]


# ==========================================================================
# Behaviour: the shipped triager .md is v1.5.0 and carries NO loopback reject
# criterion — the §3.11.5 guard is enforced upstream at PULL by exclusion, so
# the triager never sees loop-filed items.
# ==========================================================================

_SHIP_AGENT = os.path.join(
    _FEATURE_DIR, "ship", "agents", "auto-maintainer-triager.md")


def _split_frontmatter(text):
    assert text.startswith("---\n"), "file must open with a --- frontmatter fence"
    rest = text[len("---\n"):]
    end = rest.index("\n---\n")
    return rest[:end], rest[end + len("\n---\n"):]


def _frontmatter_value(fm_text, key):
    for line in fm_text.splitlines():
        if line.startswith(key + ":"):
            return line[len(key) + 1:].strip()
    raise KeyError(key)


def test_shipped_triager_is_v1_5_0():
    with open(_SHIP_AGENT) as f:
        fm, _body = _split_frontmatter(f.read())
    assert _frontmatter_value(fm, "version") == "1.5.0"


def test_shipped_triager_body_has_no_loopback_reject_instruction():
    with open(_SHIP_AGENT) as f:
        _fm, body = _split_frontmatter(f.read())
    lowered = body.lower()
    # The triager no longer rejects loop-filed items: the §3.11.5 guard is
    # enforced UPSTREAM at PULL by exclusion, so the triager never sees them.
    # It must NOT carry a reject-the-provenance-label instruction.
    assert "reject it with a" not in lowered, (
        "v1.2.0 triager must not reject loop-filed items; PULL excludes them")
    # Instead it notes the upstream exclusion (loop-filed items never reach it).
    assert "loop" in lowered and "pull" in lowered, (
        "triager must note loop-filed items are excluded upstream at PULL")
    assert "excluded" in lowered, (
        "triager must say loop-filed items are excluded upstream")
