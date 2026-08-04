#!/usr/bin/env python3
"""E2E: the RELEASE-hygiene guards SKIP under RABBIT_GATE (the per-PR gate).

Spec invariant (docs/spec.md, "Release-hygiene guards SKIP under RABBIT_GATE"):
the guards that build/normalize a FRESH artifact from current src and assert it
equals the COMMITTED tree/lib/baseline — the build-drift guard, the per-file
committed-vs-fresh-normalization checks, and the baseline-digest guard — are
RELEASE invariants, not per-PR correctness. When RABBIT_GATE is set (the
verify-integrate GATE runs scripts/gate-regression.sh with it exported), they
SKIP (early-return, pass); with it unset they run in full. This keeps the per-PR
GATE from false-failing every src-changing PR on expected pre-release drift.

These tests exercise the SAME guard functions the suite ships, INDUCING a
guaranteed fresh-vs-committed drift (a committed tree pointed at an empty root),
then proving:
  - with RABBIT_GATE set   -> the guard early-returns (no failure, drift ignored);
  - with RABBIT_GATE unset -> the guard enforces (the induced drift FAILS).

Owner: changyu87
"""

import importlib.util
import os
import tempfile

_TEST_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_test_module(basename):
    path = os.path.join(_TEST_DIR, basename)
    name = os.path.splitext(basename)[0] + "_probe"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _gate_env:
    """Context manager: set/clear RABBIT_GATE, restoring the prior value."""

    def __init__(self, value):
        self._value = value

    def __enter__(self):
        self._prior = os.environ.get("RABBIT_GATE")
        if self._value is None:
            os.environ.pop("RABBIT_GATE", None)
        else:
            os.environ["RABBIT_GATE"] = self._value
        return self

    def __exit__(self, *exc):
        if self._prior is None:
            os.environ.pop("RABBIT_GATE", None)
        else:
            os.environ["RABBIT_GATE"] = self._prior
        return False


def _raised(fn):
    """Call fn(); return True iff it raised any exception."""
    try:
        fn()
    except Exception:
        return True
    return False


# ---------------------------------------------------------------------------
# The helper each suite uses to decide the skip is env-driven: True iff
# RABBIT_GATE is set, False otherwise. Both test modules expose the same helper.
# ---------------------------------------------------------------------------
def test_skip_helper_is_rabbit_gate_driven():
    for basename in (
        "test_build_plugin.py",
        "test_release_lib_version_monotonicity.py",
    ):
        mod = _load_test_module(basename)
        assert hasattr(mod, "_skip_under_gate"), \
            f"{basename} must expose the _skip_under_gate helper"
        with _gate_env("1"):
            assert mod._skip_under_gate() is True, \
                f"{basename} _skip_under_gate must be True with RABBIT_GATE set"
        with _gate_env(None):
            assert mod._skip_under_gate() is False, \
                f"{basename} _skip_under_gate must be False without RABBIT_GATE"


# ---------------------------------------------------------------------------
# The BUILD-DRIFT guard: with a guaranteed committed-vs-fresh drift induced (the
# committed tree pointed at an EMPTY temp root, so a fresh build differs), the
# guard FAILS with RABBIT_GATE unset but SKIPS (passes) with it set.
# ---------------------------------------------------------------------------
def test_build_drift_guard_skips_under_gate_enforces_without():
    mod = _load_test_module("test_build_plugin.py")
    empty_root = tempfile.mkdtemp(prefix="pkgcfg-emptyrepo-")
    # a committed tree that cannot match any fresh build (it does not exist).
    prior_root = mod._REPO_ROOT
    mod._REPO_ROOT = empty_root
    try:
        # RABBIT_GATE unset: the guard runs and the induced drift FAILS.
        with _gate_env(None):
            assert _raised(
                mod.test_committed_plugin_tree_matches_fresh_build,
            ), "build-drift guard must ENFORCE (fail on drift) without RABBIT_GATE"
        # RABBIT_GATE set: the guard early-returns; the drift is ignored.
        with _gate_env("1"):
            assert not _raised(
                mod.test_committed_plugin_tree_matches_fresh_build,
            ), "build-drift guard must SKIP (pass) under RABBIT_GATE"
    finally:
        mod._REPO_ROOT = prior_root
        os.rmdir(empty_root)


# ---------------------------------------------------------------------------
# A per-file committed-vs-fresh-normalization guard (adapter_map_config #290):
# same proof — enforces without RABBIT_GATE, skips with it. Induce drift by
# pointing the committed-lib read at an EMPTY root (the committed file is absent,
# so the guard's committed-read/assert cannot pass).
# ---------------------------------------------------------------------------
def test_per_file_normalization_guard_skips_under_gate_enforces_without():
    mod = _load_test_module("test_build_plugin.py")
    empty_root = tempfile.mkdtemp(prefix="pkgcfg-emptyrepo-")
    prior_root = mod._REPO_ROOT
    mod._REPO_ROOT = empty_root
    try:
        with _gate_env(None):
            assert _raised(
                mod.test_committed_adapter_map_config_carries_290_review_always_ok,
            ), "per-file normalization guard must ENFORCE without RABBIT_GATE"
        with _gate_env("1"):
            assert not _raised(
                mod.test_committed_adapter_map_config_carries_290_review_always_ok,
            ), "per-file normalization guard must SKIP under RABBIT_GATE"
    finally:
        mod._REPO_ROOT = prior_root
        os.rmdir(empty_root)


# ---------------------------------------------------------------------------
# The baseline-digest guard (fresh-build lib digest == committed lib digest):
# same proof. Point the committed lib at an EMPTY root so the fresh build's lib
# digest cannot match — enforces without RABBIT_GATE, skips with it.
# ---------------------------------------------------------------------------
def test_baseline_digest_guard_skips_under_gate_enforces_without():
    mod = _load_test_module("test_release_lib_version_monotonicity.py")
    empty_lib = tempfile.mkdtemp(prefix="pkgcfg-emptylib-")
    prior_lib = mod._COMMITTED_LIB
    mod._COMMITTED_LIB = empty_lib
    try:
        with _gate_env(None):
            assert _raised(
                mod.test_baseline_digest_covers_the_fresh_build_lib,
            ), "baseline-digest guard must ENFORCE without RABBIT_GATE"
        with _gate_env("1"):
            assert not _raised(
                mod.test_baseline_digest_covers_the_fresh_build_lib,
            ), "baseline-digest guard must SKIP under RABBIT_GATE"
    finally:
        mod._COMMITTED_LIB = prior_lib
        os.rmdir(empty_lib)


# ---------------------------------------------------------------------------
# Correctness/logic guards are NOT env-gated: the committed-lib-vs-baseline
# monotonicity guard compares committed bytes to the recorded baseline (no fresh
# build), so it must run and pass REGARDLESS of RABBIT_GATE. Proves the skip is
# scoped to the fresh-vs-committed release guards only.
# ---------------------------------------------------------------------------
def test_monotonicity_guard_runs_regardless_of_gate():
    mod = _load_test_module("test_release_lib_version_monotonicity.py")
    for value in ("1", None):
        with _gate_env(value):
            # The real repo is release-clean, so this passes either way; the
            # point is it does NOT early-return on RABBIT_GATE (it is not
            # a fresh-vs-committed guard).
            mod.test_shipped_lib_change_requires_version_bump()
