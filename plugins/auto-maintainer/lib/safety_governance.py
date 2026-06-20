#!/usr/bin/env python3
"""safety-governance — the cross-cutting governance layer (DESIGN §3.8), slice 1.

A pure, deterministic decision library over a machine-first, versioned
governance config. Three decision surfaces plus one effectful halt helper:

  1. Governance config + loader — GOVERNANCE_SCHEMA_VERSION, DEFAULT_GOVERNANCE,
     load_governance(project_dir). The config is project-local at
     ${project_dir}/.auto-maintainer/governance.json (mirrors route.json,
     §3.10.2); an absent file yields the documented defaults, and a present
     file is backfilled key-by-key from the defaults. A null/absent ceiling
     means NO LIMIT for that budget dimension.

  2. Trust-ladder gate (§3.8.2, §2.3) — permits(effect_kind, mode) over the
     closed effect set {implement, open_pr, merge, file} and the closed mode
     set {dry-run, propose, gated-merge}. dry-run performs nothing; propose
     allows implement/open_pr/file but never merge; gated-merge allows all.
     An unknown mode or effect raises ValueError (closed vocabulary).

  3. Budget readiness gate (§3.8.4) — auto-resuming, NEVER a latch.
     window_key(now) is the LOCAL-tz calendar date of the injected tz-aware
     `now` (the lib never reads the wall clock). evaluate_budget(...) REPORTS
     allowance over an injected spend; record_spend(...) ADVANCES the window's
     spend. Window rollover (a new local day) resets spent_tokens — this is
     the auto-resume, with no human /start. A null ceiling never blocks.

  4. No-AskUserQuestion -> ABORTED (§3.8.3) — abort_on_would_block(...) latches
     ABORTED via lifecycle-dispositions (consumed unchanged) and emits an
     escalation through an injectable seam (the real issue-comment sink is
     §3.9.3, owned by observability; stubbed here). ABORTED is a TRUE latch —
     faults do NOT auto-resume, unlike the budget gate.

Determinism (spec Invariants): deterministic given the injected `now` + the
injected spend — no model, no network, no wall clock except through the
injected `now`, and no filesystem write of its own except the ABORTED marker,
which is delegated to lifecycle-dispositions.

Budget-accounting contract (the evaluate/record split):
  - evaluate_budget is a PURE decision. It computes the current window from
    `now`, rolls the returned budget_state over to that window (resetting spend
    on a new day), and reports {allowed, reason, budget_state}. It does NOT add
    `tick_spend` to the spend — `tick_spend` is only weighed against the
    per-tick ceiling. It does NOT mutate its input state.
  - record_spend ADVANCES spent_tokens by `tokens` within the current window,
    rolling over first when `now` falls in a new local day. The caller invokes
    record_spend on an allowed acting tick to persist the spend. It does NOT
    mutate its input state; it returns the new state to persist.

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the governance config schema reaches a
  breaking major version, or when trust-ladder / budget enforcement moves into
  a different layer than a project-local governance config consulted at tick
  entry. See docs/spec.md.
"""

import json
import os

# lifecycle-dispositions is a sibling feature consumed UNCHANGED; the test
# harness and tick-orchestrator put its src/ on sys.path, so importing by
# module name resolves the sibling. abort_on_would_block delegates the ABORTED
# marker write to it (this lib writes no marker of its own).
# packaging-config: ship-time normalization — resolve sibling libs from
# this file's own (co-located) dir so the shipped plugin is self-contained.
import os  # noqa: E402
import sys  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lifecycle_dispositions as ld


# --------------------------------------------------------------------------
# 1. Governance config schema + loader (this feature OWNS the schema).
# --------------------------------------------------------------------------

# The versioned governance config schema. Bumped on a breaking change to the
# field set; distinct from the feature version.
GOVERNANCE_SCHEMA_VERSION = "1.1.0"

# The documented defaults (spec "Governance config schema"). Trust default is
# `propose` (§2.3). Both per_tick_tokens and per_day_tokens default null (NO
# LIMIT) per an explicit user decision; a finite ceiling is opt-in via
# governance.json (§3.8.4's "a real ceiling" intent is satisfied by config, not
# by the default). window_tz `local` is the host's local timezone.
DEFAULT_GOVERNANCE = {
    "schema_version": GOVERNANCE_SCHEMA_VERSION,
    "mode": "propose",
    "budget": {
        "per_tick_tokens": None,
        "per_day_tokens": None,
        "window_tz": "local",
    },
    # The destination repo (owner/repo) for REPORT discoveries whose target is
    # `maintainer-self` (§3.11.6). Default null: with no maintainer repo set,
    # maintainer-self discoveries fall back to the project tracker. Added in
    # schema 1.1.0 (additive — optional, default null).
    "maintainer_repo": None,
}

_GOVERNANCE_RELPATH = os.path.join(".auto-maintainer", "governance.json")


def load_governance(project_dir):
    """Load the project-local governance config, backfilled from defaults.

    Reads ${project_dir}/.auto-maintainer/governance.json when present; an
    absent file yields the documented defaults. Any missing top-level or
    budget key is filled from DEFAULT_GOVERNANCE. An explicit `null` ceiling
    in the file is PRESERVED (NO LIMIT); a finite ceiling is opt-in.
    """
    path = os.path.join(project_dir, _GOVERNANCE_RELPATH)
    if not os.path.isfile(path):
        return _copy_defaults()
    with open(path, "r") as f:
        raw = json.load(f)

    config = _copy_defaults()
    if "schema_version" in raw:
        config["schema_version"] = raw["schema_version"]
    if "mode" in raw:
        config["mode"] = raw["mode"]
    budget = raw.get("budget", {})
    # Backfill per key; an explicit key (including a `null` value) overrides the
    # default, while an absent key keeps the default.
    for key in ("per_tick_tokens", "per_day_tokens", "window_tz"):
        if key in budget:
            config["budget"][key] = budget[key]
    # maintainer_repo is a known top-level key, backfilled like the others: an
    # explicit value in the file is PRESERVED, an absent key keeps the default.
    if "maintainer_repo" in raw:
        config["maintainer_repo"] = raw["maintainer_repo"]
    return config


def _copy_defaults():
    """A deep-enough copy of DEFAULT_GOVERNANCE (nested budget dict copied) so
    callers never mutate the module-level constant."""
    d = dict(DEFAULT_GOVERNANCE)
    d["budget"] = dict(DEFAULT_GOVERNANCE["budget"])
    return d


# --------------------------------------------------------------------------
# 2. Trust-ladder gate (§3.8.2, §2.3) — permits(effect_kind, mode).
# --------------------------------------------------------------------------

# The closed effect set this gate decides over.
_EFFECTS = ("implement", "open_pr", "merge", "file")

# The trust ladder: mode -> {effect: permitted}. dry-run performs nothing
# (intent logged, not performed, incl. filing §3.11.7); propose allows
# implement + open PR + file but NEVER merge (§2.3); gated-merge allows all.
_LADDER = {
    "dry-run": {"implement": False, "open_pr": False,
                "merge": False, "file": False},
    "propose": {"implement": True, "open_pr": True,
                "merge": False, "file": True},
    "gated-merge": {"implement": True, "open_pr": True,
                    "merge": True, "file": True},
}


def permits(effect_kind, mode):
    """Whether `effect_kind` is permitted under trust-ladder `mode`.

    `mode` is one of dry-run | propose | gated-merge; `effect_kind` is one of
    implement | open_pr | merge | file. An unknown mode or effect raises
    ValueError (closed vocabulary — never silently allow/deny).
    """
    if mode not in _LADDER:
        raise ValueError(
            f"unknown mode {mode!r} (expected one of {tuple(_LADDER)})")
    if effect_kind not in _EFFECTS:
        raise ValueError(
            f"unknown effect_kind {effect_kind!r} "
            f"(expected one of {_EFFECTS})")
    return _LADDER[mode][effect_kind]


# --------------------------------------------------------------------------
# 2b. Merge guardrails (§3.8.1) — a hard backstop BELOW the trust ladder.
# --------------------------------------------------------------------------

# A PR's mergeable state is "clean" only when it is the boolean True or the
# string "MERGEABLE" (case-insensitive). CONFLICTING / UNKNOWN / None / a
# missing key are all treated as not-cleanly-mergeable (never merge a
# conflicted or not-yet-computed tree).
def _is_cleanly_mergeable(mergeable):
    if mergeable is True:
        return True
    if isinstance(mergeable, str) and mergeable.upper() == "MERGEABLE":
        return True
    return False


def merge_guardrails(pr_meta, default_branch, delete_branch=None):
    """Declarative merge red-flags the host enforces before an autonomous merge.

    A pure, deterministic check over a PR's metadata (`pr_meta` is a dict
    tolerant of the keys {base, mergeable, head}). It is a hard backstop BELOW
    the trust ladder: even at gated-merge (where permits("merge", …) is True) a
    violation blocks the merge. Checks (each adds a named violation):

      - never-merge-wrong-base — pr_meta['base'] != default_branch (the loop
        only merges PRs targeting the repo's default branch).
      - never-merge-dirty — pr_meta['mergeable'] is not cleanly mergeable
        (CONFLICTING / UNKNOWN / None / missing).
      - never-delete-non-matching-branch — ONLY when `delete_branch` is supplied
        and != pr_meta['head'] (CLEANUP must bound deletion to the PR's own
        head). When `delete_branch` is None the check is skipped.

    Returns {'ok': bool, 'violations': [str]}: ok is True with an empty
    violations list only when every check passes; otherwise ok is False and
    violations names each failed check (machine-first, so INTEGRATE can record
    each reason in its `skipped` list). Pure: no I/O, does not mutate pr_meta.
    """
    violations = []

    base = pr_meta.get("base")
    if base != default_branch:
        violations.append(
            f"wrong-base: PR base {base!r} != default branch "
            f"{default_branch!r}")

    mergeable = pr_meta.get("mergeable")
    if not _is_cleanly_mergeable(mergeable):
        violations.append(
            f"dirty: PR mergeable {mergeable!r} is not cleanly mergeable")

    if delete_branch is not None:
        head = pr_meta.get("head")
        if delete_branch != head:
            violations.append(
                f"delete-non-matching-branch: delete target "
                f"{delete_branch!r} != PR head {head!r}")

    return {"ok": violations == [], "violations": violations}


# --------------------------------------------------------------------------
# 3. Budget readiness gate (§3.8.4) — auto-resuming, NOT a latch.
# --------------------------------------------------------------------------

def window_key(now):
    """The LOCAL-tz calendar-date key of the injected tz-aware `now`.

    The lib never reads the wall clock — `now` is always injected. The key is
    the ISO date string of `now`'s own (local) date, so a tz-aware instant maps
    to its local calendar day, defining the per-day budget window.
    """
    return now.date().isoformat()


def _rolled_state(budget_state, now):
    """Return a fresh budget_state advanced to `now`'s window.

    On a window rollover (the stored window_key differs from now's) spend
    resets to 0 — the auto-resume. Otherwise the spend is carried forward.
    Never mutates the input; always returns a new dict.
    """
    wk = window_key(now)
    if budget_state.get("window_key") == wk:
        return {"window_key": wk,
                "spent_tokens": budget_state.get("spent_tokens", 0)}
    return {"window_key": wk, "spent_tokens": 0}


def evaluate_budget(config, budget_state, now, tick_spend=0):
    """Report whether the tick may act under the budget ceilings.

    Pure decision (no filesystem, no latch). Returns
    {allowed, reason, budget_state} where budget_state is rolled over to
    `now`'s window (spend reset on a new local day). Order of checks:
      - per_day: finite ceiling and rolled spent_tokens >= it -> blocked,
        reason "per_day_exhausted".
      - per_tick: finite ceiling and `tick_spend` > it -> blocked, reason
        "per_tick_exceeded".
      - else allowed, reason "ok".
    A null ceiling never blocks its dimension. Does NOT add tick_spend to the
    spend (see the module-level budget-accounting contract).
    """
    rolled = _rolled_state(budget_state, now)
    budget = config["budget"]
    per_day = budget.get("per_day_tokens")
    per_tick = budget.get("per_tick_tokens")

    if per_day is not None and rolled["spent_tokens"] >= per_day:
        return {"allowed": False, "reason": "per_day_exhausted",
                "budget_state": rolled}
    if per_tick is not None and tick_spend > per_tick:
        return {"allowed": False, "reason": "per_tick_exceeded",
                "budget_state": rolled}
    return {"allowed": True, "reason": "ok", "budget_state": rolled}


def record_spend(budget_state, now, tokens):
    """Advance spent_tokens by `tokens` within `now`'s window.

    Rolls the window over first when `now` is a new local day (dropping the
    prior day's spend), then adds `tokens`. Never mutates the input; returns the
    new budget_state for the caller to persist. The caller invokes this on an
    allowed acting tick (see the module-level budget-accounting contract).
    """
    rolled = _rolled_state(budget_state, now)
    rolled["spent_tokens"] += tokens
    return rolled


# --------------------------------------------------------------------------
# 4. No-AskUserQuestion -> ABORTED helper (§3.8.3) — the only effectful fn.
# --------------------------------------------------------------------------

def abort_on_would_block(runtime_dir, reason, escalate=None):
    """Latch ABORTED instead of blocking on an interactive prompt.

    In autonomous mode an `AskUserQuestion` would block forever; this helper
    latches the ABORTED disposition via lifecycle-dispositions (consumed
    unchanged — this lib writes no marker itself) and emits an escalation
    through the `escalate` seam when provided. The real issue-comment sink is
    §3.9.3 (observability), stubbed here: `escalate` defaults to None (no-op).

    ABORTED is a TRUE latch (§1.2: faults hold until a human investigates),
    unlike the auto-resuming budget gate. Returns the latched disposition as
    the halt marker.
    """
    ld.write_disposition(runtime_dir, ld.Disposition.ABORTED)
    if escalate is not None:
        escalate(reason)
    return ld.Disposition.ABORTED
