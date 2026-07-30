# Changelog — safety-governance

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field, and the central-config `schema_version`
(`GOVERNANCE_SCHEMA_VERSION`).

## schema 2.9.0 — 2026-07-30 (exclude_labels in the field catalog)

- **`issue_filter.exclude_labels` promoted into `configure.py`'s
  `_field_catalog`.** The negative PULL filter (an open issue carrying ANY
  listed label is dropped) now appears as a catalog entry at the `PULL` stage,
  positioned after `issue_filter.with_title_regex` and before
  `work_own_filings`, matching the spec's PULL order. Because `--describe`,
  the guided `--setup` walk-through, and `render_config` all derive purely from
  the catalog, the knob is now surfaced by all three (previously it had a
  writer flag `--issue-exclude-labels` but was silently omitted from the
  catalog-driven views). `render_config` renders a configured `exclude_labels`
  as a comma list and an em dash when empty. No new schema and no new CLI flag
  (the `--issue-exclude-labels` writer already exists); `GOVERNANCE_SCHEMA_VERSION`
  stays 2.9.0.

## schema 2.9.0 — 2026-07-30 (stylish configure `--show`)

- **`render_config(config) -> str` — the derived human view of a loaded config.**
  A pure, deterministic formatter (mirrors `scheduling/status.py`'s
  `status_data()` / `render_status()` split) that reuses this feature's own
  `_field_catalog` for labels + loop-stage order, so the human view never drifts
  from the knob catalog. It renders a grouped, labeled, loop-stage-ordered
  (PULL → IMPLEMENT → VERIFY → GATE → SCHEDULING → SAFETY) plain-text view with a
  `schema_version` header and friendly value formatting (interval `<n> min`, a
  `null` budget ceiling `unlimited` with `window_tz`, booleans `on`/`off`,
  `null`/empty command or root fields an em dash, `include_labels` DNF readable,
  `with_title_regex` the regex or an em dash).
- **`--json` machine-first escape hatch.** `configure.py --show` (and the
  post-write config echo) now print the human render BY DEFAULT; `--json`
  re-selects the raw `json.dumps(load_config, indent=2, sort_keys=True)` for
  tooling that parses configure's output. `--describe` / `--preflight` ALWAYS
  emit their JSON catalogs and are UNAFFECTED by `--json`. Schema unchanged
  (2.9.0); this is an additive UX change.

## schema 2.9.0 — 2026-07-24

- **`issue_filter` field rename (with coexistence).** `labels` → `include_labels`
  and `title_pattern` → `with_title_regex` (the new name states the regex
  semantics); `exclude_labels` unchanged. The loader/normalizer reads the new
  names but STILL accepts the legacy `labels`/`title_pattern` as a fallback and
  canonicalizes to the new names, so an existing project `config.json` with the
  old keys keeps working (coexistence window, spec-rules §3). `DEFAULT_GOVERNANCE`,
  `load_config`, the `issue_filter` normalizer + `ValueError` messages, and
  `configure.py` (writes) all use the new names; the internal matcher object
  returned by `issue_filter(config)` keeps its consumer-facing shape (scheduling /
  work-intake untouched). `--issue-labels` / `--issue-title-pattern` flag names are
  retained for back-compat and now write the new fields.
- **Ship-as-is `heartbeat.interval_minutes` default 3 → 10.**
- `issue_filter.exclude_labels` default stays `[]` in `DEFAULT_GOVERNANCE`.

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
