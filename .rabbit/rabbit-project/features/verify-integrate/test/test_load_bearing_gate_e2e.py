#!/usr/bin/env python3
"""Tests for the doc-surface load-bearing-token survival GATE check (issue #353).

A doc-reduction PR (housekeep-style) is guarded today only by a line-count
baseline + the advisory REVIEW gate; feature test suites do NOT assert doc prose,
so an over-deletion that drops a load-bearing token (a schema field, a
script/symbol name, an invariant statement, a cross-reference) can pass the
line-count gate and auto-merge. GATE, having already built the post-merge
integration tree, now asserts that every token a doc-touched feature DECLARES
load-bearing (in test/load_bearing_tokens.json) still appears in its post-change
doc surfaces (docs/spec.md, docs/contract.md, skills/*/SKILL.md). A dropped
declared token = a "load-bearing" gate failure (the PR is rolled out, excluded,
and NOT merged), mirroring the rabbit-housekeep load-bearing-survival test but
enforced on the loop's own auto-merge path.

The pure helpers are unit-tested directly against on-disk feature fixtures. The
GATE e2e drives the cumulative gate with a FAKE git+regression runner over a REAL
temp integration worktree (the doc surfaces the check reads live in it), scripting
the `git diff --name-only` the check consults — no real git, no PRs, no network.

Owner: changyu87
"""

import json
import os
import sys
import tempfile

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES_DIR = os.path.dirname(_FEATURE_DIR)
for _sib in ("fsm-contracts", "safety-governance", "lifecycle-dispositions"):
    _p = os.path.join(_FEATURES_DIR, _sib, "src")
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fsm_contracts as fc  # noqa: E402
import verify_integrate as vi  # noqa: E402

_DEFAULT_BRANCH = "main"
_FEATURES_ROOT = ".rabbit/rabbit-project/features"


# --------------------------------------------------------------------------
# On-disk fixture helpers: materialize a feature's doc surfaces + declared tokens
# under a root, so the pure readers + the GATE worktree check exercise real files.
# --------------------------------------------------------------------------

def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


def _make_feature(root, feature, spec="", contract="", skills=None,
                  tokens=None):
    """Create <root>/<feature>/ with the given doc surfaces + declared tokens."""
    feat = os.path.join(root, feature)
    if spec:
        _write(os.path.join(feat, "docs", "spec.md"), spec)
    if contract:
        _write(os.path.join(feat, "docs", "contract.md"), contract)
    for name, body in (skills or {}).items():
        _write(os.path.join(feat, "skills", name, "SKILL.md"), body)
    if tokens is not None:
        _write(os.path.join(feat, "test", "load_bearing_tokens.json"),
               json.dumps({"tokens": tokens}))
    return feat


# ==========================================================================
# Pure helper: _is_doc_surface
# ==========================================================================

def test_is_doc_surface_recognizes_the_declared_set():
    assert vi._is_doc_surface("docs/spec.md")
    assert vi._is_doc_surface("docs/contract.md")
    assert vi._is_doc_surface("skills/start/SKILL.md")
    assert vi._is_doc_surface("skills/deep/name/SKILL.md") is False
    # non-doc surfaces are NOT gated.
    assert vi._is_doc_surface("src/verify_integrate.py") is False
    assert vi._is_doc_surface("test/run.py") is False
    assert vi._is_doc_surface("docs/CHANGELOG.md") is False
    assert vi._is_doc_surface("skills/start/other.md") is False


# ==========================================================================
# Pure helper: declared_load_bearing_tokens
# ==========================================================================

def test_declared_tokens_absent_file_is_empty():
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(td, "f", spec="x")  # no tokens file
        assert vi.declared_load_bearing_tokens(feat) == []


def test_declared_tokens_reads_the_declared_set():
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(td, "f", spec="x",
                             tokens=["Verdict", "ci_state"])
        assert vi.declared_load_bearing_tokens(feat) == ["Verdict", "ci_state"]


def test_declared_tokens_ignores_non_string_and_empty_entries():
    with tempfile.TemporaryDirectory() as td:
        feat = os.path.join(td, "f")
        _write(os.path.join(feat, "test", "load_bearing_tokens.json"),
               json.dumps({"tokens": ["ok", "", 3, None, "two"]}))
        assert vi.declared_load_bearing_tokens(feat) == ["ok", "two"]


def test_declared_tokens_malformed_json_is_empty():
    with tempfile.TemporaryDirectory() as td:
        feat = os.path.join(td, "f")
        _write(os.path.join(feat, "test", "load_bearing_tokens.json"),
               "{not json")
        assert vi.declared_load_bearing_tokens(feat) == []


# ==========================================================================
# Pure helper: missing_load_bearing_tokens (survival against doc surfaces)
# ==========================================================================

def test_missing_tokens_none_when_all_survive_across_surfaces():
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="the Verdict schema has ci_state",
            contract="cross-ref: fsm-contracts",
            skills={"start": "the start SKILL mentions permits"},
            tokens=["Verdict", "ci_state", "fsm-contracts", "permits"])
        assert vi.missing_load_bearing_tokens(feat) == []


def test_missing_tokens_reports_dropped_token_in_declared_order():
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="the Verdict schema",  # ci_state DROPPED
            tokens=["Verdict", "ci_state", "reasons"])
        # ci_state and reasons are absent, in declared order.
        assert vi.missing_load_bearing_tokens(feat) == ["ci_state", "reasons"]


def test_missing_tokens_empty_when_no_tokens_declared():
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(td, "f", spec="anything")  # no declaration
        assert vi.missing_load_bearing_tokens(feat) == []


def test_missing_tokens_dropped_token_not_saved_by_incidental_substring():
    # `Verdict` was DROPPED, but `ReviewVerdict` remains: a raw substring test
    # would falsely count it as surviving. Word-boundary matching catches it.
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="the ReviewVerdict schema explains findings",
            tokens=["Verdict"])
        assert vi.missing_load_bearing_tokens(feat) == ["Verdict"]


def test_missing_tokens_standalone_token_still_survives():
    # The same token DOES survive when it appears as a standalone word.
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="the Verdict schema and the ReviewVerdict schema",
            tokens=["Verdict"])
        assert vi.missing_load_bearing_tokens(feat) == []


def test_missing_tokens_short_numeric_section_id_not_saved_by_substring():
    # A bare section id like `3.7` must not be judged surviving on `13.7`.
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="see DESIGN 13.7 for the async model",
            tokens=["3.7"])
        assert vi.missing_load_bearing_tokens(feat) == ["3.7"]


def test_missing_tokens_hyphenated_and_path_tokens_survive():
    # Tokens with internal/edge punctuation (cross-refs, paths) still match
    # their real occurrences under word-boundary semantics.
    with tempfile.TemporaryDirectory() as td:
        feat = _make_feature(
            td, "f",
            spec="cross-ref: fsm-contracts; reads docs/spec.md",
            tokens=["fsm-contracts", "docs/spec.md"])
        assert vi.missing_load_bearing_tokens(feat) == []


# ==========================================================================
# Pure helper: features_with_changed_doc_surfaces
# ==========================================================================

def test_changed_doc_surfaces_maps_only_doc_paths_to_features():
    changed = [
        f"{_FEATURES_ROOT}/alpha/docs/spec.md",       # doc -> alpha
        f"{_FEATURES_ROOT}/beta/skills/x/SKILL.md",   # doc -> beta
        f"{_FEATURES_ROOT}/alpha/src/code.py",        # non-doc -> ignored
        "README.md",                                  # not under root -> ignored
    ]
    feats = vi.features_with_changed_doc_surfaces(changed, _FEATURES_ROOT)
    assert feats == [f"{_FEATURES_ROOT}/alpha", f"{_FEATURES_ROOT}/beta"]


def test_changed_doc_surfaces_empty_for_non_doc_pr():
    changed = [
        f"{_FEATURES_ROOT}/alpha/src/code.py",
        f"{_FEATURES_ROOT}/alpha/test/run.py",
    ]
    assert vi.features_with_changed_doc_surfaces(changed, _FEATURES_ROOT) == []


def test_changed_doc_surfaces_empty_when_no_root():
    changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
    assert vi.features_with_changed_doc_surfaces(changed, None) == []


# ==========================================================================
# GATE e2e — a fake git+regression runner over a REAL integration worktree.
# ==========================================================================

class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class _FakeGitWorktree:
    """A fake git+regression runner backed by a REAL on-disk worktree fixture.

    The doc-survival check reads real files under the worktree, so this fake
    materializes the worktree's feature doc surfaces up front and answers
    `git diff --name-only` with the scripted changed paths. Merge/regression are
    scripted to succeed (so the ONLY thing that can fail the gate here is the
    token check). Records commands so tests assert roll-back on a token drop."""

    def __init__(self, worktree, changed_paths, regression_command="pytest"):
        self._worktree = worktree
        self._changed = list(changed_paths)
        self._regression_command = regression_command
        self.commands = []

    def __call__(self, cmd, **kwargs):
        is_git = isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git"
        self.commands.append(list(cmd) if is_git else [cmd])
        if not is_git:
            return _FakeCompleted(returncode=0, stdout="ok")
        sub = self._git_sub(cmd)
        if sub == "rev-parse":
            return _FakeCompleted(returncode=0, stdout="deadbeef\n")
        if sub == "diff":
            return _FakeCompleted(returncode=0,
                                  stdout="\n".join(self._changed) + "\n")
        return _FakeCompleted(returncode=0)

    @staticmethod
    def _git_sub(cmd):
        i = 1
        while i < len(cmd) and cmd[i] == "-C":
            i += 2
        return cmd[i] if i < len(cmd) else ""


def _verdict(number=1):
    return vi.Verdict(
        pr_ref=f"acme/widget#{number}",
        url=f"https://github.com/acme/widget/pull/{number}",
        ok=True, ci_state="passing", mergeable=True, base=_DEFAULT_BRANCH,
    ).to_dict()


def _fresh_ctx():
    ctx = fc.TickContext()
    ctx.register_slot(vi.VERDICTS_SLOT["name"], vi.VERDICTS_SLOT["schema"],
                      version=vi.VERDICTS_SLOT["version"])
    ctx.register_slot(vi.GATE_RESULTS_SLOT["name"],
                      vi.GATE_RESULTS_SLOT["schema"],
                      version=vi.GATE_RESULTS_SLOT["version"])
    return ctx


def _run_gate(gate, ctx):
    result = gate.run(ctx)
    vocab = fc.SignalVocabulary(vi.GATE_SIGNALS)
    assert fc.validate_state_result(result).passed is True
    fc.apply_result(ctx, vi.GATE_MANIFEST, result, vocab)
    return ctx.read("gate_results")


def _is_git_sub(cmd, sub):
    if not cmd or cmd[0] != "git":
        return False
    i = 1
    while i < len(cmd) and cmd[i] == "-C":
        i += 2
    return i < len(cmd) and cmd[i] == sub


def test_gate_e2e_doc_pr_dropping_token_is_blocked_load_bearing():
    """Acceptance #1: a doc-reduction PR that removes a declared load-bearing
    token is BLOCKED (passed=False, reason='load-bearing') and rolled back — not
    merged."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        # post-merge worktree: the feature's spec DROPPED the 'ci_state' token.
        _make_feature(root, "alpha", spec="the Verdict schema",
                      tokens=["Verdict", "ci_state"])
        changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
        fake = _FakeGitWorktree(wt, changed)
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=_FEATURES_ROOT)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is False
    assert results[0]["reason"] == "load-bearing"
    assert "ci_state" in results[0]["failure_summary"]
    # the drop rolled the merge back (git reset --hard to the pre-merge sha).
    assert any(_is_git_sub(c, "reset") and "--hard" in c for c in fake.commands)
    # the regression is NOT run when the token check fails (only git commands ran).
    assert not any(c and c[0] != "git" for c in fake.commands)


def test_gate_e2e_doc_pr_preserving_tokens_passes():
    """Acceptance #2: a reduction that preserves all declared tokens PASSES."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        _make_feature(root, "alpha",
                      spec="the Verdict schema has ci_state and reasons",
                      tokens=["Verdict", "ci_state", "reasons"])
        changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
        fake = _FakeGitWorktree(wt, changed)
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=_FEATURES_ROOT)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is True
    assert results[0]["reason"] is None
    # tokens survived -> the regression DID run (one non-git invocation).
    assert any(c and c[0] != "git" for c in fake.commands)


def test_gate_e2e_non_doc_pr_is_unaffected():
    """Acceptance #3: a PR that touches NO doc surface is not gated on tokens —
    even a feature that declares tokens is unaffected when its docs are untouched.
    """
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        # the feature declares tokens, but the spec here is MISSING the token —
        # yet the PR changed only source, so the token check must NOT fire.
        _make_feature(root, "alpha", spec="no tokens here at all",
                      tokens=["Verdict", "ci_state"])
        changed = [f"{_FEATURES_ROOT}/alpha/src/verify_integrate.py"]
        fake = _FakeGitWorktree(wt, changed)
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=_FEATURES_ROOT)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is True
    assert results[0]["reason"] is None


def test_gate_e2e_doc_pr_feature_without_declaration_is_unaffected():
    """A doc PR to a feature that has NOT declared any tokens is unaffected (the
    check is opt-in)."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        _make_feature(root, "alpha", spec="slimmed docs")  # no token declaration
        changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
        fake = _FakeGitWorktree(wt, changed)
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=_FEATURES_ROOT)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is True


def test_gate_e2e_features_root_none_skips_token_check():
    """With no repo-relative features_root wired, the token check is skipped (the
    regression gate is unaffected) — a conservative no-op."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        _make_feature(root, "alpha", spec="dropped", tokens=["Verdict"])
        changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
        fake = _FakeGitWorktree(wt, changed)
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=None)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is True
    # no `git diff` was consulted (the check short-circuited on None root).
    assert not any(_is_git_sub(c, "diff") for c in fake.commands)
