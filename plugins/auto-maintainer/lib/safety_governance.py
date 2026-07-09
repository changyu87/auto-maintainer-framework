#!/usr/bin/env python3
"""safety-governance — the cross-cutting governance layer (DESIGN §3.8), slice 1.

A pure, deterministic decision library over a machine-first, versioned CENTRAL
config (config.json). Decision surfaces plus one effectful halt helper:

  1. Central config + loader — GOVERNANCE_SCHEMA_VERSION (2.5.0),
     DEFAULT_GOVERNANCE, load_config(project_dir), work_own_filings(config),
     regression_command(config). The
     config is project-local at
     ${project_dir}/.auto-maintainer/config.json (the single central userConfig,
     §3.10.1; mirrors route.json, §3.10.2); an absent file yields the documented
     defaults, and a present file is backfilled key-by-key from the defaults. A
     null/absent per_day ceiling means NO LIMIT (the budget gate is a no-op).
     A legacy governance.json is MIGRATED once (see load_config). load_governance
     is a thin alias delegating to load_config during the coexistence window.

  2. Maintainer-self REPORT destination — MAINTAINER_REPO, a FIXED module
     constant (§3.11.6), NOT a config field. maintainer-self discoveries (the
     loop's OWN defects, the dogfood case) route there ALWAYS — never the project
     tracker, no fallback. run_tick._repo_for_target imports this constant.

  3. Trust-ladder gate (§3.8.2, §2.3) — permits(effect_kind, mode) over the
     closed effect set {implement, open_pr, merge, file} and the closed mode
     set {dry-run, propose, auto-merge}. dry-run performs nothing; propose
     allows implement/open_pr/file but never merge; auto-merge allows all.
     An unknown mode or effect raises ValueError (closed vocabulary). The legacy
     mode name `gated-merge` is tolerated and mapped to `auto-merge` on load (see
     _overlay / _normalize_mode).

  4. Merge guardrails (§3.8.1) — merge_guardrails(pr_meta, default_branch,
     delete_branch) a pure declarative backstop BELOW the trust ladder.

  5. Budget readiness gate (§3.8.4) — auto-resuming, NEVER a latch. A PER-DAY
     token ceiling only (the per-tick ceiling is REMOVED). window_key(now) is the
     LOCAL-tz calendar date of the injected tz-aware `now` (the lib never reads
     the wall clock). evaluate_budget(...) REPORTS allowance; record_spend(...)
     ADVANCES the window's spend. Window rollover (a new local day) resets
     spent_tokens — the auto-resume, no human /start. A null ceiling never blocks.

  6. No-AskUserQuestion -> ABORTED (§3.8.3) — abort_on_would_block(...) latches
     ABORTED via lifecycle-dispositions (consumed unchanged) and emits an
     escalation through an injectable seam. ABORTED is a TRUE latch — faults do
     NOT auto-resume, unlike the budget gate.

Determinism (spec Invariants): deterministic given the injected `now` + the
injected spend — no model, no network, no wall clock except through the injected
`now`, and no filesystem write except the durable config (migration) and the
ABORTED marker, which is delegated to lifecycle-dispositions.

Budget-accounting contract (the evaluate/record split):
  - evaluate_budget is a PURE decision. It computes the current window from
    `now`, rolls the returned budget_state over to that window (resetting spend
    on a new day), and reports {allowed, reason, budget_state}. It does NOT
    mutate its input state. The optional tick_spend argument is TOLERATED for
    backward compatibility but ignored (the per-tick ceiling is removed).
  - record_spend ADVANCES spent_tokens by `tokens` within the current window,
    rolling over first when `now` falls in a new local day. The caller invokes
    record_spend on an allowed acting tick to persist the spend. It does NOT
    mutate its input state; it returns the new state to persist.

Version: 0.2.0
Owner: changyu87
Deprecation criterion: Superseded when trust-ladder / budget enforcement moves
  into a different layer than a project-local central config (config.json)
  consulted at tick entry, or when the config schema reaches its next breaking
  major (3.0.0). See docs/spec.md.
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
# 1. Central config schema + loader (this feature OWNS the schema).
# --------------------------------------------------------------------------

# The versioned central-config schema. Bumped on a breaking change to the field
# set; distinct from the feature version. 2.0.0: config.json rename, per-tick
# ceiling + maintainer_repo removed, heartbeat + backoff knobs added. 2.1.0:
# trust mode `gated-merge` renamed to `auto-merge` (the legacy name is tolerated
# on load and mapped forward, a non-breaking coexistence migration). 2.2.0:
# additive `work_own_filings` knob (default true) — the loop works its own
# filings by default with a manual opt-out (§3.11.5); an absent key backfills
# True, so the bump is backward compatible. 2.4.0: the `self_deploy` knob is
# REMOVED — the self_deploy ACTION was removed in #324 (the auto-maintainer is
# NOT self-deployable), so the knob gates nothing. A config.json still carrying a
# stale `self_deploy` key is TOLERATED (dropped, never surfaced), so the bump is
# backward compatible. 2.5.0: additive `regression_command` knob (default null =
# NO gate) — the GATE full-regression shell command read by verify-integrate; an
# absent key backfills null (GATE is a no-op PASS), so the bump is backward
# compatible.
GOVERNANCE_SCHEMA_VERSION = "2.5.0"

# The maintainer-self REPORT destination — a FIXED constant (§3.11.6), NOT a
# config field. The loop's OWN defects route here ALWAYS, never the project
# tracker, no fallback. run_tick._repo_for_target imports this. Revisit only if
# the upstream home repo moves or per-install self-tracking is reintroduced.
MAINTAINER_REPO = "changyu87/auto-maintainer-framework"

# The documented defaults (spec "Central config schema"). Trust default is
# `propose` (§2.3). per_day_tokens defaults null (NO LIMIT) per an explicit user
# decision; a finite ceiling is opt-in. window_tz `local` is the host's local
# timezone. heartbeat.interval_minutes (tick cadence, §3.3.2) defaults 3;
# backoff.threshold (consecutive-blocked count K, §3.8.5) defaults 5.
# features_root (the MAINTAINED project's features directory, §3.7.6) defaults
# null (UNCONFIGURED): VERIFY's cross-feature complement then conservatively
# gates a cross-cutting-flagged tick. It is config-driven because the maintained
# repo's layout is not fixed; an explicit path opts the complement run in.
# work_own_filings (§3.11.5) defaults True: the loop works its OWN filings by
# default, with a manual opt-out (work_own_filings: false). The owner flipped the
# previously-deferred "explicitly opted in" provision to default-on opt-out. Owned
# here; work-intake PULL consumes it to apply the loopback exclusion and scheduling
# threads it from the loaded config into PULL (separate cycles).
# regression_command (§3.7, verify-integrate GATE) defaults null (NO gate): the
# GATE state runs this full-regression shell command against each REVIEW-passed
# PR (exit 0 = pass), but a null command makes GATE a no-op PASS, so an
# unconfigured project merges exactly as before (non-breaking opt-in). Owned here;
# read by verify-integrate through load_config.
DEFAULT_GOVERNANCE = {
    "schema_version": GOVERNANCE_SCHEMA_VERSION,
    "mode": "propose",
    "features_root": None,
    "work_own_filings": True,
    "regression_command": None,
    "budget": {
        "per_day_tokens": None,
        "window_tz": "local",
    },
    "heartbeat": {
        "interval_minutes": 3,
    },
    "backoff": {
        "threshold": 5,
    },
}

_CONFIG_RELPATH = os.path.join(".auto-maintainer", "config.json")
_LEGACY_RELPATH = os.path.join(".auto-maintainer", "governance.json")

# The shipped default-config dir (#337): the aggressive operational default
# (mode auto-merge, …) ships as the plugin's `default-config/config.json`,
# refreshed every release by packaging-config. In the installed plugin this file
# is `<plugin_root>/lib/safety_governance.py`, so the dir is the sibling of lib/,
# i.e. `dirname(dirname(__file__))/default-config`, mirroring scheduling's
# resolver. In the source tree that sibling is ABSENT, so _shipped_default
# returns None and the embedded conservative DEFAULT_GOVERNANCE constant is used.
# Tests point this at a temp fixture dir. There is NO seed-once copy: the shipped
# default is read FRESH when no project-local config exists, so a release that
# changes it reaches an existing install automatically (the #337 staleness fix);
# a project-local .auto-maintainer/config.json override still WINS.
DEFAULT_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "default-config")


def _shipped_default():
    """Read + parse the shipped `<DEFAULT_CONFIG_DIR>/config.json` FRESH (#337).

    Returns the parsed dict when the file is present and valid JSON, else None
    (the dir/file is absent — the source-tree / no-plugin safety fallback — or
    the file is unparsable). A None result tells load_config to fall back to the
    embedded conservative DEFAULT_GOVERNANCE constant. It NEVER copies the file
    (no seed-once copy).
    """
    path = os.path.join(DEFAULT_CONFIG_DIR, "config.json")
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _copy_defaults():
    """A deep-enough copy of DEFAULT_GOVERNANCE (nested dicts copied) so callers
    never mutate the module-level constant."""
    d = dict(DEFAULT_GOVERNANCE)
    d["budget"] = dict(DEFAULT_GOVERNANCE["budget"])
    d["heartbeat"] = dict(DEFAULT_GOVERNANCE["heartbeat"])
    d["backoff"] = dict(DEFAULT_GOVERNANCE["backoff"])
    return d


# The legacy trust-mode name -> its current name. A config (or CLI request) still
# carrying the pre-2.1.0 `gated-merge` is TOLERATED and mapped forward to
# `auto-merge` (the rename is a non-breaking coexistence migration, not an error).
_MODE_ALIASES = {"gated-merge": "auto-merge"}


def _normalize_mode(mode):
    """Map a legacy trust-mode name forward to its current name.

    Returns `mode` unchanged when it is not a known legacy alias (so unknown
    modes still flow through to the closed-vocabulary check in permits()).
    """
    return _MODE_ALIASES.get(mode, mode)


def _overlay(raw):
    """Backfill `raw` onto a fresh defaults copy, returning the merged config.

    Only KNOWN keys are surfaced — the removed per_tick_tokens (under budget) and
    a removed top-level maintainer_repo are silently dropped (tolerated, ignored).
    An explicit key (including a `null` value) overrides the default; an absent
    key keeps the default. A legacy `mode: "gated-merge"` is tolerated and mapped
    forward to `auto-merge` (the rename coexistence migration). An explicit
    top-level `features_root` (the maintained project's features directory, used
    by VERIFY's cross-feature complement, §3.7.6) is surfaced; absent keeps the
    null default (UNCONFIGURED -> the complement conservatively gates). An
    explicit top-level `work_own_filings` (the loopback toggle, §3.11.5) is
    surfaced; absent keeps the default True (the loop works its own filings). An
    explicit top-level `regression_command` (the GATE full-regression shell
    command read by verify-integrate, §3.7) is surfaced, including an explicit
    null; absent keeps the default None (NO gate -> GATE is a no-op PASS). A
    stale top-level `self_deploy` key (the removed self-deployment gate, #324) is
    silently dropped (tolerated, ignored — the self_deploy ACTION was removed, so
    the knob gates nothing).
    """
    config = _copy_defaults()
    if "mode" in raw:
        config["mode"] = _normalize_mode(raw["mode"])
    if "features_root" in raw:
        config["features_root"] = raw["features_root"]
    if "work_own_filings" in raw:
        config["work_own_filings"] = raw["work_own_filings"]
    if "regression_command" in raw:
        config["regression_command"] = raw["regression_command"]
    budget = raw.get("budget", {})
    for key in ("per_day_tokens", "window_tz"):
        if key in budget:
            config["budget"][key] = budget[key]
    heartbeat = raw.get("heartbeat", {})
    if "interval_minutes" in heartbeat:
        config["heartbeat"]["interval_minutes"] = heartbeat["interval_minutes"]
    backoff = raw.get("backoff", {})
    if "threshold" in backoff:
        config["backoff"]["threshold"] = backoff["threshold"]
    return config


def load_config(project_dir):
    """Load the project-local central config, backfilled from defaults.

    Resolution order:
      1. ${project_dir}/.auto-maintainer/config.json present -> read + backfill.
      2. config.json absent but legacy governance.json present -> MIGRATE ONCE:
         map the surviving fields (mode, budget.per_day_tokens, budget.window_tz),
         DROP per_tick_tokens + maintainer_repo, backfill heartbeat/backoff,
         WRITE config.json, and rename the legacy file to
         governance.json.migrated (non-destructive). Returns the migrated config.
      3. Neither present -> the DEFAULT resolution (#337, no file written):
         read the shipped default-config/config.json (sibling of lib/) FRESH via
         _shipped_default() when present — backfilled + validated through _overlay
         like any config (the aggressive operational default, mode auto-merge, …).
         When that shipped file is absent/unparsable (the source tree / no plugin)
         the conservative embedded DEFAULT_GOVERNANCE constant is the fallback.
         There is NO seed-once copy: a release that changes the shipped config
         reaches an existing install automatically, while a project-local
         .auto-maintainer/config.json override still WINS (steps 1-2 above).

    The removed per_tick_tokens / maintainer_repo keys, if still present in a
    config.json, are TOLERATED and dropped (never surfaced on the loaded config).
    """
    config_path = os.path.join(project_dir, _CONFIG_RELPATH)
    if os.path.isfile(config_path):
        with open(config_path, "r") as f:
            raw = json.load(f)
        return _overlay(raw)

    legacy_path = os.path.join(project_dir, _LEGACY_RELPATH)
    if os.path.isfile(legacy_path):
        with open(legacy_path, "r") as f:
            legacy = json.load(f)
        # Migrate: _overlay already maps the surviving fields and drops the
        # removed ones. Persist the migrated config, then rename the legacy file.
        config = _overlay(legacy)
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2, sort_keys=True)
            f.write("\n")
        os.rename(legacy_path, legacy_path + ".migrated")
        return config

    # No project-local config and no legacy governance.json: read the shipped
    # default-config/config.json FRESH (#337) when present, backfilled + validated
    # like any config; else the conservative embedded DEFAULT_GOVERNANCE.
    shipped = _shipped_default()
    if shipped is not None:
        return _overlay(shipped)
    return _copy_defaults()


def load_governance(project_dir):
    """Thin alias delegating to load_config during the coexistence window.

    Consumers should migrate to load_config; this name is honored until they do.
    """
    return load_config(project_dir)


def work_own_filings(config):
    """Whether the loop works its OWN filings (the loopback toggle, §3.11.5).

    A pure config read: returns the loaded config's `work_own_filings`, defaulting
    to True when the key is absent (the default-on opt-out — an existing config
    without the key opts IN). work-intake PULL consumes this to conditionally
    apply the loopback exclusion; scheduling threads it from the loaded config.
    """
    return config.get("work_own_filings", True)


def regression_command(config):
    """The GATE full-regression shell command (§3.7), or None for NO gate.

    A pure config read: returns the loaded config's `regression_command`,
    defaulting to None when the key is absent (NO gate -> GATE is a no-op PASS,
    non-breaking). verify-integrate's GATE consumes this to run the command
    against each REVIEW-passed PR (exit 0 = pass); a None command skips the gate.
    """
    return config.get("regression_command")


# --------------------------------------------------------------------------
# 2. Trust-ladder gate (§3.8.2, §2.3) — permits(effect_kind, mode).
# --------------------------------------------------------------------------

# The closed effect set this gate decides over.
_EFFECTS = ("implement", "open_pr", "merge", "file")

# The trust ladder: mode -> {effect: permitted}. dry-run performs nothing
# (intent logged, not performed, incl. filing §3.11.7); propose allows
# implement + open PR + file but NEVER merge (§2.3); auto-merge allows all.
_LADDER = {
    "dry-run": {"implement": False, "open_pr": False,
                "merge": False, "file": False},
    "propose": {"implement": True, "open_pr": True,
                "merge": False, "file": True},
    "auto-merge": {"implement": True, "open_pr": True,
                   "merge": True, "file": True},
}


def permits(effect_kind, mode):
    """Whether `effect_kind` is permitted under trust-ladder `mode`.

    `mode` is one of dry-run | propose | auto-merge; `effect_kind` is one of
    implement | open_pr | merge | file. The legacy mode name `gated-merge` is
    tolerated and mapped to `auto-merge`. An unknown mode or effect raises
    ValueError (closed vocabulary — never silently allow/deny).
    """
    mode = _normalize_mode(mode)
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
    the trust ladder: even at auto-merge (where permits("merge", …) is True) a
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
    """Report whether the tick may act under the per-day budget ceiling.

    Pure decision (no filesystem, no latch). Returns
    {allowed, reason, budget_state} where budget_state is rolled over to
    `now`'s window (spend reset on a new local day). A finite per_day ceiling
    with rolled spent_tokens >= it blocks with reason "per_day_exhausted"; a
    null per_day ceiling never blocks (reason "ok"). `tick_spend` is TOLERATED
    for backward compatibility but ignored — the per-tick ceiling is REMOVED.
    Does NOT mutate its input state.
    """
    del tick_spend  # the per-tick ceiling is removed; argument tolerated, ignored
    rolled = _rolled_state(budget_state, now)
    per_day = config["budget"].get("per_day_tokens")

    if per_day is not None and rolled["spent_tokens"] >= per_day:
        return {"allowed": False, "reason": "per_day_exhausted",
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
