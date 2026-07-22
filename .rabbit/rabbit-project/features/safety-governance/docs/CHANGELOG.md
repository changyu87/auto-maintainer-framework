# Changelog — safety-governance

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field, and the central-config `schema_version`
(`GOVERNANCE_SCHEMA_VERSION`).

## schema 2.7.0 — 2026-07-22

- **Additive `issue_filter` knob (default no-filter).** An optional filter
  narrowing WHICH open GitHub issues the PULL stage (work-intake) pulls, an
  object `{"labels": <DNF>, "title_pattern": <regex-string-or-null>}`.
  - `labels` is a **disjunctive-normal-form (OR-of-ANDs)** matcher in canonical
    `List[List[str]]` form; `title_pattern` is a post-fetch title regex or
    `null`. Default `{"labels": [], "title_pattern": null}` = NO filter (pull
    all open issues), so the bump is non-breaking, opt-in.
  - `DEFAULT_GOVERNANCE['issue_filter']` is the no-filter object; `_overlay` /
    `load_config` backfill it when the key is absent and preserve an explicit
    override.
  - New pure accessor `issue_filter(config)` normalizes + validates: absent /
    `null` / `[]` => no-filter; a flat string list is sugar for one AND-group;
    a `List[List[str]]` is validated as-is. Raises `ValueError` (never a silent
    write) on a non-string label, an empty-string label, an empty inner group,
    or a `title_pattern` that is neither `null` nor a compilable regex. Consumed
    by `work-intake` PULL, threaded by `scheduling`/`tick-orchestrator`
    (separate cycles).
- **Schema bump `2.6.0` -> `2.7.0`** (`GOVERNANCE_SCHEMA_VERSION`): additive and
  backward compatible — an existing `config.json` without the key loads with the
  no-filter default.

## schema 2.4.0 — 2026-06-28

- **Removed the dead `self_deploy` knob.** The `self_deploy` ACTION was removed
  in #324 (the auto-maintainer is NOT self-deployable), so the knob no longer
  gates anything.
  - `DEFAULT_GOVERNANCE` no longer carries `self_deploy`; `_overlay` no longer
    backfills it; the `self_deploy(config)` accessor is removed.
  - A `config.json` still carrying a stale `self_deploy` key is TOLERATED — the
    key is dropped (ignored), never surfaced on the loaded config.
- **Schema bump `2.3.0` -> `2.4.0`** (`GOVERNANCE_SCHEMA_VERSION`): additive and
  backward compatible — an existing `config.json` (with or without the stale
  `self_deploy` key) loads unchanged.

## schema 2.2.0 — 2026-06-21

- **Additive `work_own_filings` knob (default `true`).** The loop works its OWN
  filings by default, with a manual **opt-OUT** (`work_own_filings: false`).
  §3.11.5 originally deferred this to "explicitly opted in" to prevent
  self-amplification; the owner has flipped it to default-on opt-out.
  - `DEFAULT_GOVERNANCE['work_own_filings']` is `True`; `_overlay` / `load_config`
    backfill `True` when the key is absent and preserve an explicit `false`.
  - New pure accessor `work_own_filings(config) -> bool` (default `True` when
    absent), consumed by `work-intake` PULL and threaded by `scheduling`
    (separate cycles).
- **Schema bump `2.1.0` -> `2.2.0`** (`GOVERNANCE_SCHEMA_VERSION`): additive and
  backward compatible — an existing `config.json` without the key loads with
  `work_own_filings=True` (the new default).

## schema 2.1.0 — 2026-06-21

- **Trust mode `gated-merge` renamed to `auto-merge`.** "gated" read as
  *blocked/held back* — the opposite of what the rung does (it is the rung that
  ALLOWS merging). The merge guardrails (§3.8.1) are the separate hard backstop
  that actually gates a merge, so the mode name need not carry "gated". The
  closed mode vocabulary is now `dry-run` / `propose` / `auto-merge`.
  - `_LADDER` key, the `permits()` closed mode set, and the documented config
    schema all use `auto-merge`.
  - `configure.py` `--mode` accepts `auto-merge`; the `--describe` catalog and
    the `/auto-maintainer:configure` skill advertise `auto-merge`.
- **Coexistence migration (non-breaking).** A config (or `--mode` request) still
  carrying the legacy `mode: "gated-merge"` is TOLERATED and mapped forward to
  `auto-merge` on load — it never errors. `permits()` normalizes the legacy name
  too, so a stale caller decides identically. `configure.py` persists the
  canonical `auto-merge`.
- **Schema bump `2.0.0` -> `2.1.0`** (`GOVERNANCE_SCHEMA_VERSION`): a
  backward-compatible field-value rename with a forward-mapping alias.
