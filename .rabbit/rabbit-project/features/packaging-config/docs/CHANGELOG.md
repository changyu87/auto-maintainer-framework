# Changelog — packaging-config

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
