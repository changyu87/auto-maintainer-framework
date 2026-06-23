# Changelog — packaging-config

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
