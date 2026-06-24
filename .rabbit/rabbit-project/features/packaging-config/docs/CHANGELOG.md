# Changelog — packaging-config

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
