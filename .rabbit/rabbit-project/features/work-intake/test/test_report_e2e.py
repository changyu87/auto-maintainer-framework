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

The shipped triager guard test asserts the loopback reject criterion (§3.11.5)
is present in the deployed v1.1.0 subagent definition.

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
    assert result.errors == []


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
# Behaviour: the shipped triager .md is v1.1.0 and carries the loopback reject
# criterion (§3.11.5) — it rejects items stamped filed-by:autonomous-maintainer.
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


def test_shipped_triager_is_v1_1_0():
    with open(_SHIP_AGENT) as f:
        fm, _body = _split_frontmatter(f.read())
    assert _frontmatter_value(fm, "version") == "1.1.0"


def test_shipped_triager_body_has_loopback_reject_instruction():
    with open(_SHIP_AGENT) as f:
        _fm, body = _split_frontmatter(f.read())
    lowered = body.lower()
    # The loopback guard rejects items stamped by the loop itself.
    assert "filed-by:autonomous-maintainer" in body, (
        "triager must reject items carrying the loop provenance label")
    assert "am-dedup" in lowered, (
        "triager must recognize the am-dedup body marker as loop provenance")
    assert "loop-filed" in lowered or "loop filed" in lowered, (
        "triager must name the loopback policy in its reject criterion")
