# Changelog — packaging-config

## 0.7.11 — version-integrity restore release: deploy #310

- Bump `_PLUGIN_VERSION` 0.7.10 -> 0.7.11 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the loop's #310 (dormant
  self-deploy capability, default-OFF) reaches the marketplace under a PROPER
  version. #310 regenerated the committed lib mirrors in-PR but did NOT bump the
  plugin version, so main's committed tree carried new bytes under the SAME
  0.7.10 as the released v0.7.10 — a version-integrity break, and a
  `/plugin marketplace update` keyed on the version would not even fetch it. No
  build LOGIC change beyond the version bump. self_deploy stays OFF by default
  (dormant); this release does NOT enable it.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_11_and_consistent` (assert 0.7.11 in
  `plugin.json` + `marketplace.json`); add
  `test_shipped_default_keeps_self_deploy_off` +
  `test_committed_default_keeps_self_deploy_off` asserting the shipped/committed
  `lib/safety_governance.py` defaults `self_deploy` to False and the
  `default-config/config.json` does not enable it; advance the housekeep doc
  baseline (486 -> 503) for the appended per-release section.

## 0.7.10 — empty-skip + build-drift-fix release: deploy #307

- Bump `_PLUGIN_VERSION` 0.7.9 -> 0.7.10 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so #307 (deterministic
  empty-skip for idle ticks, for #306) plus the loop's #302/#303 reach the
  installed plugin. The auto-maintainer loop merged these as src without a build
  step, drifting the committed tree so 7 build-drift guards failed; this release
  regenerates the committed tree and resolves the drift. The #307 fix makes a
  NON-ACTING `once` agent-state whose signal rule yields the empty-signal on
  empty input skip via `_empty_skip_result`, so the loop stops dispatching the
  triager on an empty pool. No build LOGIC change beyond the version bump.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_10_and_consistent` (assert 0.7.10 in
  `plugin.json` + `marketplace.json`); add
  `test_shipped_run_tick_carries_307_empty_skip` +
  `test_committed_run_tick_carries_307_empty_skip` asserting the
  shipped/committed `run_tick` carries `_empty_skip_result`; advance the
  housekeep doc baseline (471 -> 486) for the appended per-release section.

## 0.7.9 — file-referenced dispatch prompts release: deploy #304

- Bump `_PLUGIN_VERSION` 0.7.8 -> 0.7.9 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so #304 (file-referenced
  dispatch prompts) reaches the installed plugin: scheduling's `run_tick` now
  writes each agent-state dispatch's rendered invocation envelope to a
  `prompt_path` file and hands the executor only the path (dropping the inline
  prompt body), and the shipped tick skill (v0.6.0) documents the
  file-referenced dispatch protocol (point each subagent at the runner-named
  `prompt_path` file, no inline prompt). No build LOGIC change beyond the
  version bump.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_9_and_consistent`; update the shipped tick-skill
  test to `test_shipped_tick_skill_is_v0_6_0_file_referenced_dispatch` (was the
  v0.5.0 form) asserting frontmatter `version: 0.6.0` + `prompt_path`; add
  `test_shipped_run_tick_carries_304_file_referenced_dispatch` +
  `test_committed_run_tick_carries_304_file_referenced_dispatch` asserting the
  shipped/committed `run_tick` carries `prompt_path` and the tick skill
  documents the file-referenced dispatch; advance the housekeep doc baseline
  (455 -> 471) for the appended per-release spec section.

## 0.7.8 — work_own_filings default-on opt-out release: deploy #297/#298/#299

- Bump `_PLUGIN_VERSION` 0.7.7 -> 0.7.8 (`plugin.json` + `marketplace.json`).
- Surface the §3.11.5 `work_own_filings` opt-out in the shipped
  `default-config/config.json`: bump its `schema_version` 2.1.0 -> 2.2.0
  (matching safety-governance's `GOVERNANCE_SCHEMA_VERSION`) and add a top-level
  `"work_own_filings": true` (the default-on policy, so users see the knob to opt
  out). The existing keys (mode/features_root/budget/heartbeat/backoff) are
  unchanged.
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the opt-out reaches the
  installed plugin across three sibling features shipped as-is:
  safety-governance's default-true accessor (#297:
  `safety_governance.work_own_filings(config)` defaults `True`), work-intake's
  PULL honoring the flag (#298: `work_intake.Pull(work_own_filings=…)`), and
  scheduling's `make_pull` threading it from the loaded config (#299). No build
  LOGIC change beyond the version bump + the default-config asset edit.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_8_and_consistent`; add
  `test_default_config_surfaces_work_own_filings_at_schema_2_2_0` +
  `test_committed_default_config_surfaces_work_own_filings` (the
  shipped/committed seed `config.json` carries `work_own_filings: true` at schema
  2.2.0 with the pre-existing keys unchanged) and
  `test_committed_libs_carry_work_own_filings_opt_out` +
  `test_shipped_libs_carry_work_own_filings_opt_out` (the committed/shipped
  `lib/{safety_governance,work_intake,run_tick}.py` carry the opt-out and match a
  fresh normalization of their sources; work-intake's `Pull` honors the flag at
  runtime from `lib/` alone); advance the housekeep doc baseline (433 -> 455) for
  the appended per-release spec section.

## 0.7.7 — merge-sink-url + pool-based-refire release: deploy #294/#295

- Bump `_PLUGIN_VERSION` 0.7.6 -> 0.7.7 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so two merged fixes reach
  the installed plugin: the verify-integrate merge sink now records the merged
  PR url (#294: `lib/verify_integrate.py` carries `_pr_url` for traceability),
  and scheduling's pool-based immediate-refire + INTEGRATE/refire observability
  (#295: `lib/run_tick.py`'s `_work_remains` is a triage-memory-aware POOL
  predicate computing its candidate pool via `_filter_triage_work_items` — the
  same §3.5.3 skip-filter TRIAGE applies — and the `tick_end` detail surfaces
  `merged_refs`). No build LOGIC change beyond the version bump.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_7_and_consistent`; add
  `test_committed_verify_integrate_carries_294_merged_pr_url` +
  `test_shipped_verify_integrate_carries_merged_pr_url` (the committed/shipped
  `lib/verify_integrate.py` carries `_pr_url` and matches a fresh normalization
  of the source) and
  `test_committed_run_tick_carries_295_pool_refire_and_merged_refs` +
  `test_shipped_run_tick_carries_pool_refire_and_merged_refs` (the
  committed/shipped `lib/run_tick.py` carries `_filter_triage_work_items` +
  `merged_refs`); advance the housekeep doc baseline (416 -> 433) for the
  appended per-release spec section.

## 0.7.6 — immediate-refire enhancement release: deploy #292

- Bump `_PLUGIN_VERSION` 0.7.5 -> 0.7.6 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged
  immediate-refire enhancement (#292) reaches the installed plugin: scheduling's
  `run_tick` EXIT anchor is wrapped with the `_work_remains` predicate (over the
  remaining work + the durable backoff ledger), so a completed tick that still
  has actionable work signals `refire` and the loop runs the next tick
  immediately instead of waiting for the heartbeat; the shipped tick skill
  (v0.5.0) documents the refire loop. No build LOGIC change beyond the version
  bump.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_6_and_consistent`, add
  `test_committed_run_tick_carries_292_immediate_refire` (committed
  `lib/run_tick.py` carries `_work_remains`, matches a fresh normalization of
  the source, and the committed tick skill documents the refire loop) and
  `test_shipped_run_tick_carries_immediate_refire` (the freshly built tree ships
  the predicate + refire-documenting skill); update the stale tick-skill version
  test to 0.5.0; advance the housekeep doc baseline (401 -> 416) for the
  appended per-release spec section.

## 0.7.5 — advisory-REVIEW merge-fix release: deploy #290

- Bump `_PLUGIN_VERSION` 0.7.4 -> 0.7.5 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged
  advisory-REVIEW fix (#290) reaches the installed plugin: the REVIEW
  adapter-map template's signal is now `always_ok`, so a clean (zero-finding)
  review emits OK and ALWAYS continues to INTEGRATE instead of EMPTY-branching
  past the merge. No build LOGIC change beyond the version bump.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_5_and_consistent`, add
  `test_committed_adapter_map_config_carries_290_review_always_ok` asserting the
  shipped `lib/adapter_map_config.py` carries the REVIEW `always_ok` signal_rule
  and matches a fresh normalization of the source, and advance the housekeep doc
  baseline (388 -> 401) for the appended per-release spec section.

## 0.7.4 — per-item dispatch-description fix release: deploy #288

- Bump `_PLUGIN_VERSION` 0.7.3 -> 0.7.4 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged per-item
  dispatch-description fix (#288) reaches the installed plugin: `run_tick`'s
  `_dispatch_description` now branches on cardinality (`if "item" in env:`) so a
  per_item fan-out ALWAYS names the item ref (distinct parallel subagents)
  instead of letting an explicit `dispatch_entry['description']` win verbatim.
  The drifted `lib/run_tick.py` is re-normalized from the updated source; the two
  version strings also change.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_4_and_consistent`, add
  `test_committed_run_tick_carries_288_per_item_dispatch_desc` and
  `test_shipped_run_tick_carries_per_item_dispatch_desc` asserting the shipped
  lib carries the per-item dispatch-description branch and matches a fresh
  normalization of the source, and advance the housekeep doc baseline
  (375 -> 388) for the appended per-release spec section.

## 0.7.3 — stale-checkpoint discard fix release: deploy #285

- Bump `_PLUGIN_VERSION` 0.7.2 -> 0.7.3 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged
  stale-checkpoint discard fix (#285) reaches the installed plugin: `run_tick`
  now discards a persisted PAUSED checkpoint that is incompatible with the
  current route/context (gated by the `_checkpoint_compatible` guard) instead of
  resuming against a stale checkpoint. The drifted `lib/run_tick.py` is
  re-normalized from the updated source; the two version strings also change.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_3_and_consistent`, add
  `test_committed_run_tick_carries_285_checkpoint_compat_guard` and
  `test_shipped_run_tick_carries_checkpoint_compat_guard` asserting the shipped
  lib carries the `_checkpoint_compatible` guard and matches a fresh
  normalization of the source, and advance the housekeep doc baseline
  (362 -> 375) for the appended per-release spec section.

## 0.7.2 — surgical adapter-map migration fix release: deploy #283

- Bump `_PLUGIN_VERSION` 0.7.1 -> 0.7.2 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged surgical
  adapter-map migration fix (#283) reaches the installed plugin: the scheduling
  adapter-map migration now only heals retired-writes entries and preserves
  valid custom wiring (gated by the `valid_writes` set derived from the
  agent-port templates). Only the two version strings change in the committed
  tree; `lib/adapter_map_config.py` already matched the merged source.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_2_and_consistent`, add
  `test_committed_adapter_map_config_carries_283_surgical_migration` asserting
  the shipped lib carries the `valid_writes` gate and matches a fresh
  normalization of the source, and advance the housekeep doc baseline
  (349 -> 362) for the appended per-release spec section.

## 0.7.1 — dogfood-fix release: deploy PRs #277-#280

- Bump `_PLUGIN_VERSION` 0.7.0 -> 0.7.1 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src so the merged dogfood fixes
  reach the installed plugin: the agent-dispatch empty-schema guard (#277), the
  adapter-wiring `build_loop` `migrate` hook (#278), the scheduling adapter-map
  known-port auto-migration (#279), and the issue/PR-named dispatch descriptions
  (#280). The drifted `lib/run_tick.py` is re-normalized from the updated source.
- Tests: rename the version test to `test_version_bumped_to_0_7_1_and_consistent`,
  add `test_committed_libs_carry_277_278_279_dogfood_fixes` asserting the shipped
  libs carry the three merged fixes, and advance the housekeep doc baseline
  (333 -> 349) for the appended per-release spec section.

## 0.7.0 — loop-redesign final release: ship test_gate.py

- Add implement's `test_gate.py` (the IMPLEMENT doer's deterministic test gate)
  to `_LIBS` in `build_plugin.py`. It imports only stdlib and no sibling lib, so
  it is byte-copied (PURE), not normalized, landing at
  `plugins/auto-maintainer/lib/test_gate.py` byte-identical to its
  `implement/src/test_gate.py` source.
- Bump `_PLUGIN_VERSION` 0.6.0 -> 0.7.0 (`plugin.json` + `marketplace.json`).
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src.
- Tests: rename the version test to `test_version_bumped_to_0_7_0_and_consistent`
  and add e2e coverage that the fresh build ships `lib/test_gate.py`
  byte-identical to source with no source-tree leak.
