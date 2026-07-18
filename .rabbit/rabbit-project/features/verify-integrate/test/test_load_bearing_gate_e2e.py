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
import safety_governance as sg  # noqa: E402
import verify_integrate as vi  # noqa: E402

_DEFAULT_BRANCH = "main"
_FEATURES_ROOT = ".rabbit/rabbit-project/features"


def _gate_of(manifest_and_run):
    """The Gate instance behind a make_gate() -> (manifest, gate.run) result."""
    _, run = manifest_and_run
    return run.__self__


def _write_config(project_dir, cfg):
    conf_dir = os.path.join(project_dir, ".auto-maintainer")
    os.makedirs(conf_dir, exist_ok=True)
    with open(os.path.join(conf_dir, "config.json"), "w") as f:
        json.dump(cfg, f)


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
    `git diff --name-only` with the scripted changed paths. It also answers
    `git show <pre_sha>:<feat>/test/load_bearing_tokens.json` with the BASE
    (pre-merge) declaration text — GATE anchors the declared set to base (issue
    #392), so the fake serves `base_tokens[feat_rel]` for that revision rather
    than the worktree's (post-merge) copy. Merge/regression are scripted to
    succeed (so the ONLY thing that can fail the gate here is the token check).
    Records commands so tests assert roll-back on a token drop."""

    def __init__(self, worktree, changed_paths, regression_command="pytest",
                 base_tokens=None):
        self._worktree = worktree
        self._changed = list(changed_paths)
        self._regression_command = regression_command
        # base_tokens maps a feature-rel path (e.g. "<root>/alpha") to the token
        # list declared at the pre-merge base; absent ⇒ no declaration at base.
        self._base_tokens = dict(base_tokens or {})
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
        if sub == "show":
            return self._show(cmd)
        return _FakeCompleted(returncode=0)

    def _show(self, cmd):
        """Answer `git show <rev>:<path>` from base_tokens: nonzero (as real git
        does for an absent path) when no base declaration is scripted for the
        feature the path belongs to."""
        spec = cmd[-1]  # "<rev>:<feat_rel>/test/load_bearing_tokens.json"
        blob_path = spec.split(":", 1)[-1]
        suffix = "/test/load_bearing_tokens.json"
        feat_rel = (blob_path[:-len(suffix)] if blob_path.endswith(suffix)
                    else blob_path)
        if feat_rel not in self._base_tokens:
            return _FakeCompleted(returncode=128, stderr="path does not exist")
        body = json.dumps({"tokens": self._base_tokens[feat_rel]})
        return _FakeCompleted(returncode=0, stdout=body)

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
        fake = _FakeGitWorktree(
            wt, changed,
            base_tokens={f"{_FEATURES_ROOT}/alpha": ["Verdict", "ci_state"]})
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
        fake = _FakeGitWorktree(
            wt, changed,
            base_tokens={f"{_FEATURES_ROOT}/alpha":
                         ["Verdict", "ci_state", "reasons"]})
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


# ==========================================================================
# make_gate wiring (issue #381 + #391): the doc check is LIVE on the production
# path only when a REPO-RELATIVE root is wired via the DEDICATED
# `doc_check_features_root` key. That key is fully DECOUPLED from `features_root`
# (VERIFY's complement on-disk locator, which may be absolute): setting
# `features_root` alone must never turn this gate on or off (issue #391).
# ==========================================================================

def test_make_gate_wires_doc_check_features_root_key():
    """make_gate reads the dedicated `doc_check_features_root` key so the doc gate
    is live on the auto-merge path (issue #381)."""
    with tempfile.TemporaryDirectory() as pd:
        _write_config(pd, {"regression_command": "pytest",
                           "doc_check_features_root": _FEATURES_ROOT})
        gate = _gate_of(vi.make_gate({"project_dir": pd, "repo": "acme/widget",
                                      "default_branch": _DEFAULT_BRANCH}))
    assert gate._features_root == _FEATURES_ROOT


def test_make_gate_ignores_features_root_uses_only_doc_check_key():
    """Decoupling (issue #391): `features_root` (VERIFY's complement locator) never
    drives the doc gate; only the dedicated `doc_check_features_root` key does."""
    with tempfile.TemporaryDirectory() as pd:
        _write_config(pd, {"regression_command": "pytest",
                           "features_root": "/abs/on/disk/features",
                           "doc_check_features_root": _FEATURES_ROOT})
        gate = _gate_of(vi.make_gate({"project_dir": pd, "repo": "acme/widget",
                                      "default_branch": _DEFAULT_BRANCH}))
    assert gate._features_root == _FEATURES_ROOT


def test_make_gate_relative_features_root_alone_leaves_doc_check_off():
    """Decoupling (issue #391): a RELATIVE `features_root` with NO dedicated key
    must NOT silently turn the doc gate on — the keys no longer share semantics."""
    with tempfile.TemporaryDirectory() as pd:
        _write_config(pd, {"regression_command": "pytest",
                           "features_root": _FEATURES_ROOT})
        gate = _gate_of(vi.make_gate({"project_dir": pd, "repo": "acme/widget",
                                      "default_branch": _DEFAULT_BRANCH}))
    assert gate._features_root is None


def test_make_gate_absolute_doc_check_root_leaves_doc_check_off():
    """An absolute `doc_check_features_root` cannot match repo-relative diff paths,
    so the doc check stays off (conservative no-op)."""
    with tempfile.TemporaryDirectory() as pd:
        _write_config(pd, {"regression_command": "pytest",
                           "doc_check_features_root": "/abs/on/disk/features"})
        gate = _gate_of(vi.make_gate({"project_dir": pd, "repo": "acme/widget",
                                      "default_branch": _DEFAULT_BRANCH}))
    assert gate._features_root is None


def test_make_gate_no_root_configured_leaves_doc_check_off():
    """Neither key set -> the doc check is off (conservative no-op)."""
    with tempfile.TemporaryDirectory() as pd:
        _write_config(pd, {"regression_command": "pytest"})
        gate = _gate_of(vi.make_gate({"project_dir": pd, "repo": "acme/widget",
                                      "default_branch": _DEFAULT_BRANCH}))
    assert gate._features_root is None


# ==========================================================================
# issue #392 — the declared set is anchored to the pre-merge base, so a PR
# cannot bypass the gate by dropping a token AND its declaration together.
# ==========================================================================

def test_gate_e2e_dropping_token_and_its_declaration_is_still_blocked():
    """issue #392: a PR that drops a load-bearing token from the spec AND removes
    that token's entry from load_bearing_tokens.json in the SAME PR must STILL be
    blocked. The post-merge worktree declares nothing (the entry is gone), but the
    BASE (pre-merge) declaration still names the token, so GATE — anchoring the
    declared set to base — catches the drop (passed=False, reason='load-bearing')
    and rolls the merge back."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        # post-merge worktree: BOTH the token is dropped from the spec AND the
        # declaration is weakened to drop that token's entry.
        _make_feature(root, "alpha", spec="the Verdict schema",
                      tokens=["Verdict"])  # ci_state entry ALSO removed
        changed = [
            f"{_FEATURES_ROOT}/alpha/docs/spec.md",
            f"{_FEATURES_ROOT}/alpha/test/load_bearing_tokens.json",
        ]
        # base still declares ci_state — anchoring to base defeats the bypass.
        fake = _FakeGitWorktree(
            wt, changed,
            base_tokens={f"{_FEATURES_ROOT}/alpha": ["Verdict", "ci_state"]})
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
    # the drop rolled the merge back and the regression never ran.
    assert any(_is_git_sub(c, "reset") and "--hard" in c for c in fake.commands)
    assert not any(c and c[0] != "git" for c in fake.commands)


def test_gate_e2e_adding_a_declaration_in_the_pr_is_not_gated():
    """The declared set is the BASE set, so a token a PR ADDS to its own
    load_bearing_tokens.json (absent at base) is NOT retroactively gated in this
    PR — only the base's declared tokens must survive. Here base declared no
    tokens for the feature, so even a doc PR whose post-merge declaration lists a
    token missing from the docs passes (that token was not load-bearing at base)."""
    with tempfile.TemporaryDirectory() as wt:
        root = os.path.join(wt, _FEATURES_ROOT)
        # post-merge declares a token the docs do NOT contain, but base declared
        # nothing (base_tokens omits the feature) -> not gated on it.
        _make_feature(root, "alpha", spec="slimmed docs",
                      tokens=["ci_state"])
        changed = [f"{_FEATURES_ROOT}/alpha/docs/spec.md"]
        fake = _FakeGitWorktree(wt, changed)  # no base declaration
        gate = vi.Gate(regression_command="pytest", runner=fake,
                       repo="acme/widget", default_branch=_DEFAULT_BRANCH,
                       issue_resolver=lambda pr_ref, repo=None: None,
                       worktree_dir=wt, features_root=_FEATURES_ROOT)
        ctx = _fresh_ctx()
        ctx.write("verdicts", [_verdict(1)])
        results = _run_gate(gate, ctx)

    assert results[0]["passed"] is True
    assert results[0]["reason"] is None


# ==========================================================================
# Pure helpers: _parse_declared_tokens + missing_load_bearing_tokens(tokens=)
# ==========================================================================

def test_parse_declared_tokens_shares_the_declaration_parse():
    assert vi._parse_declared_tokens('{"tokens": ["a", "", 3, "b"]}') == \
        ["a", "b"]
    assert vi._parse_declared_tokens("") == []
    assert vi._parse_declared_tokens("{not json") == []
    assert vi._parse_declared_tokens('{"other": 1}') == []


def test_missing_tokens_explicit_set_overrides_on_disk_declaration():
    """When an explicit `tokens` set is passed (as GATE passes the base set), it
    is checked against the doc surfaces regardless of the feature's own on-disk
    declaration — the on-disk copy cannot weaken the check."""
    with tempfile.TemporaryDirectory() as td:
        # the feature's OWN declaration is empty, but the base set still names
        # ci_state, which the docs dropped -> reported missing.
        feat = _make_feature(td, "f", spec="the Verdict schema", tokens=[])
        assert vi.missing_load_bearing_tokens(
            feat, tokens=["Verdict", "ci_state"]) == ["ci_state"]
