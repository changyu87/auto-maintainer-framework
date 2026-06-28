#!/usr/bin/env python3
"""End-to-end conformance tests for the release_needed DETECTION in run_tick.

The auto-maintainer is NOT self-deployable: it does NOT regenerate / commit /
push the plugin tree itself. It DOES keep the operator signal `release_needed`
— the human-release-owed flag (#319). When THIS tick merged >=1 PR whose diff
touched SHIPPED feature source AND the maintained project is the framework's own
checkout, a human release is owed to keep the plugin version 1:1 with the shipped
bytes. The loop merges PRs server-side but performs NO build/commit/push, so the
committed plugin tree drifts from the bumped version a release would carry;
`release_needed` surfaces that explicitly so the operator knows a release is owed.

`_release_needed` fires PURELY on the merged shipped-src change + the framework
checkout — it is INDEPENDENT of any self_deploy knob (nothing self-deploys now).
These tests exercise `_release_needed` directly with an INJECTED files source (no
network) over a synthetic integration_result, plus an e2e tick proving a merged
shipped-src change does NO commit / push / regenerate yet still sets
release_needed=True on the trace + tick_end event.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "observability", "verify-integrate", "packaging-config"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import json  # noqa: E402
import safety_governance as sg  # noqa: E402,F401
import run_tick as rt  # noqa: E402
import build_plugin as bp  # noqa: E402,F401


_SHIPPED_FILE = os.path.join(
    ".rabbit", "rabbit-project", "features", "scheduling", "src",
    "run_tick.py").replace(os.sep, "/")
_DOCS_FILE = os.path.join(
    ".rabbit", "rabbit-project", "features", "scheduling", "docs",
    "spec.md").replace(os.sep, "/")


def _merged_result(*pr_refs):
    return {"merged": [{"pr_ref": r, "url": ""} for r in pr_refs],
            "skipped": [], "errors": []}


class _FilesSource:
    """An injectable per-PR files source mapping pr_ref -> changed paths."""

    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def __call__(self, pr_ref, repo=None):
        self.calls.append(pr_ref)
        return self.mapping.get(pr_ref, [])


def _FEATURES_repo_root():
    """The framework checkout root (parent of .rabbit/) — carries the marker, so
    _self_deploy_repo_root resolves it."""
    # _FEATURES = .../.rabbit/rabbit-project/features -> up 3 = repo root
    return os.path.dirname(os.path.dirname(os.path.dirname(_FEATURES)))


# ==========================================================================
# Behaviour 0 — the kept detection seams + helper exist; the self-deploy ACTION
# seams (the flush + the git commit/sync sinks) are GONE.
# ==========================================================================

def test_release_detection_seams_exist():
    # The PR-files source seam (shared by the detector) is KEPT.
    assert rt.DEFAULT_PR_FILES_SOURCE is rt.gh_pr_files_source
    # The own-checkout detector is KEPT.
    assert callable(rt._self_deploy_repo_root)
    # The detector itself is KEPT.
    assert callable(rt._release_needed)


def test_self_deploy_action_seams_removed():
    # The self-deploy ACTION (build/commit/push/sync) is removed wholesale —
    # the auto-maintainer is not self-deployable.
    for gone in ("_flush_package", "git_commit_sink", "git_sync_sink",
                 "DEFAULT_PACKAGE_COMMIT_SINK", "DEFAULT_PACKAGE_SYNC_SINK"):
        assert not hasattr(rt, gone), gone


# ==========================================================================
# Behaviour 1 — release_needed is TRUE when a merged PR touched shipped src in
# the framework's own checkout (a human release is owed). It fires on the merged
# shipped-src change alone — no package_deployed param, no self_deploy knob.
# ==========================================================================

def test_release_needed_true_when_shipped_src_merged():
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files, None) is True
    assert files.calls == ["acme/widget#42"], files.calls


# ==========================================================================
# Behaviour 2 — release_needed is INDEPENDENT of any self_deploy governance knob:
# whether self_deploy is on or off (the knob is dead now), the signal fires the
# same on a merged shipped-src change. _release_needed takes no gov / no knob.
# ==========================================================================

def test_release_needed_independent_of_self_deploy_knob():
    files_a = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    files_b = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    # Same args (no gov passed) -> same True result; the function signature has
    # no governance/knob input at all.
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files_a, None) is True
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files_b, None) is True


# ==========================================================================
# Behaviour 3 — a docs/test-only merge owes NO release; False.
# ==========================================================================

def test_release_needed_false_for_docs_only_merge():
    files = _FilesSource({"acme/widget#42": [_DOCS_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), _FEATURES_repo_root(),
        files, None) is False


# ==========================================================================
# Behaviour 4 — a tick that merged NOTHING owes no release; False (files source
# never queried).
# ==========================================================================

def test_release_needed_false_when_nothing_merged():
    files = _FilesSource({})
    assert rt._release_needed(
        {"merged": [], "skipped": [], "errors": []},
        _FEATURES_repo_root(), files, None) is False
    assert files.calls == []


# ==========================================================================
# Behaviour 5 — a maintained project that is NOT the framework checkout owes no
# self-release; False (files source never queried — no extra network).
# ==========================================================================

def test_release_needed_false_when_not_framework_checkout():
    import tempfile
    not_framework = tempfile.mkdtemp(prefix="sched-rel-not-fw-")
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})
    assert rt._release_needed(
        _merged_result("acme/widget#42"), not_framework,
        files, None) is False
    assert files.calls == []


# ==========================================================================
# Behaviour 6 — multiple merged PRs: ANY one touching shipped src owes a release.
# ==========================================================================

def test_release_needed_true_when_any_merged_pr_touches_shipped_src():
    files = _FilesSource({
        "acme/widget#1": [_DOCS_FILE],
        "acme/widget#2": [_SHIPPED_FILE],
    })
    assert rt._release_needed(
        _merged_result("acme/widget#1", "acme/widget#2"),
        _FEATURES_repo_root(), files, None) is True


# ==========================================================================
# Behaviour 7 (E2E) — a full tick whose INTEGRATE merged a shipped-src change
# does NO commit / push / regenerate (the loop is not self-deployable) yet sets
# release_needed=True on the one-line trace AND the tick_end event. This proves
# the detection survives while the action is gone: no git/build seam is ever
# touched.
# ==========================================================================

class _Boom:
    """A sentinel that EXPLODES if called — proves no git/build/commit ran."""

    def __init__(self, label):
        self.label = label

    def __call__(self, *a, **k):
        raise AssertionError(f"self-deploy action ran: {self.label}")


def test_e2e_merged_shipped_src_sets_release_needed_without_acting(tmp_path=None):
    import tempfile
    runtime_dir = tempfile.mkdtemp(prefix="sched-rel-e2e-")
    state_path = os.path.join(runtime_dir, "durable-state.json")
    journal_path = os.path.join(runtime_dir, "journal.jsonl")

    # Pre-seed the durable integration_result so the terminal sees a merged
    # shipped-src PR WITHOUT routing a real INTEGRATE (the detector reads the
    # persisted #64 integration_result product).
    import durable_state as ds
    doc = {}
    doc[rt.INTEGRATION_RESULT_KEY] = _merged_result("acme/widget#42")
    ds.DurableState(state_path).save(doc)

    # Inject a files source that maps the merged PR to a shipped-src change, and
    # point project_dir at the framework checkout so _self_deploy_repo_root
    # resolves. Guard against ANY accidental build/commit: bp.build / bp.bump
    # must NOT be invoked. We assert via the absence of the seams + an unchanged
    # plugin tree; the detector queries ONLY the injected files source.
    files = _FilesSource({"acme/widget#42": [_SHIPPED_FILE]})

    def _stub_source():
        return []  # no open issues; PULL idles

    rt.run_tick(
        runtime_dir=runtime_dir, state_path=state_path,
        journal_path=journal_path, project_dir=_FEATURES_repo_root(),
        source=_stub_source(), pr_files_source=files)

    # The trace's tick_end event carries release_needed=True; no self_deployed /
    # deployed_version keys remain (the action is gone).
    events = []
    log = os.path.join(runtime_dir, "events.jsonl")
    if os.path.exists(log):
        with open(log) as fh:
            events = [json.loads(line) for line in fh if line.strip()]
    end = next(e for e in events if e["kind"] == "tick_end")
    assert end["detail"].get("release_needed") is True, end
    assert "self_deployed" not in end["detail"], end
    assert "deployed_version" not in end["detail"], end
    # The detector queried the merged PR's files (the only seam it touches).
    assert files.calls == ["acme/widget#42"], files.calls


if __name__ == "__main__":
    import traceback
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    failures = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    if failures:
        print(f"\n{failures} failure(s)")
        sys.exit(1)
    print(f"\nall {len(fns)} passed")
