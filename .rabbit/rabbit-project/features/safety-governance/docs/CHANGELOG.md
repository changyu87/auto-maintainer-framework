# Changelog — safety-governance

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field, and the central-config `schema_version`
(`GOVERNANCE_SCHEMA_VERSION`).

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
