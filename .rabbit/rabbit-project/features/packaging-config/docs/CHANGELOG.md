# Changelog — packaging-config

## 0.37.0 — re-ship merge-signal observability wave; release v0.37.0

- **Release v0.37.0 (minor — ASSET-REFRESH):** `_PLUGIN_VERSION` 0.36.0 -> 0.37.0;
  committed plugin tree regenerated so the shipped status lib + skills carry
  scheduling 0.52.0's merge-signal observability:
  - `lib/status.py` — re-normalized to carry the injectable, tolerant
    open-loop-PR probe (`DEFAULT_OPEN_PR_SOURCE`): `status_data().open_loop_prs`
    surfaces `[{number, auto_merge_enabled, merge_state}]` and `render_status`
    shows each loop PR's auto-merge posture (`pending`/`off` + `merge_state`, or
    `open loop PRs: none`), making pending-vs-blocked visible at a glance.
  - `skills/start/SKILL.md` (v0.6.0) and `skills/tick/SKILL.md` (v0.9.0) —
    re-collected from `ship/`, now carrying the merge-signal interpretation
    guidance: `merged` counts in-tick merges only; `merged=0` with
    `auto_merge_enabled>0` is pending async auto-merge, NOT blocked; real blocks
    are `integrate_errored`/`reconcile_errors`; async completions surface as
    RECONCILE `auto_merged`.
- **No `_LIBS` change** — asset-refresh only; the committed `lib/status.py`
  re-normalizes from source but `_LIBS`/`_NORMALIZED_LIBS` are unchanged. No
  shipped `default-config/` CONTENT change (schema unchanged); no shipped-agent
  change; `run_tick.py`/`work_intake.py` byte-identical to 0.36.0.
- Version-assertion test advanced to 0.37.0; an e2e test added asserting the
  committed + freshly built `lib/status.py` carries the open-loop-PR probe and the
  shipped start/tick skills carry the merge-signal interpretation guidance; the two
  skill-version assertions advanced (start 0.5.0 -> 0.6.0, tick 0.8.0 -> 0.9.0);
  `test/release_lib_baseline.json` re-anchored (version -> 0.37.0, fresh
  `lib_digest`); the housekeep doc baseline re-anchored to the grown spec
  (506 -> 521).

## 0.36.0 — re-ship operator-in-control "config is the bound" guidance; release v0.36.0

- **Release v0.36.0 (minor — ASSET-REFRESH):** `_PLUGIN_VERSION` 0.35.1 -> 0.36.0;
  committed plugin tree regenerated so the shipped skills + persona hook carry the
  "config is the bound" operator-in-control guidance:
  - `skills/start/SKILL.md` (scheduling 0.51.0, v0.5.0) and `skills/tick/SKILL.md`
    (v0.8.0) — re-collected from `ship/`, now carrying the "Limits are
    operator-owned: config is the bound" section: the loop's limits are
    operator-owned via configuration (budget / backoff / issue_filter / mode) and
    enforced by the deterministic runner; the executor invents no ad-hoc
    token/tick/issue caps nor silently narrows scope, keeps draining while a tick
    reports `refire`, and surfaces genuine anomalies to the operator.
  - `hooks/session-start-persona.py` (v0.6.0) — the shipped SessionStart persona
    now carries the same operator-in-control principle (limits are config-owned +
    runner-enforced; no ad-hoc caps; keeps working on `refire`; surfaces anomalies;
    operator bounds via `/auto-maintainer:configure` or halts via
    `/auto-maintainer:stop`), so it is reinforced every session.
- **No `_LIBS` change** — asset-refresh only; the committed `lib/` digest is
  byte-identical to 0.35.1. No shipped `default-config/` CONTENT change (schema
  unchanged); no shipped-agent change.
- Version-assertion test advanced to 0.36.0; e2e tests added asserting the shipped
  persona hook carries the operator-in-control principle (operator-owned +
  issue_filter + refire + configure/stop, no absolutist framing) and the shipped
  start/tick skills carry the operator-owned guidance; the two skill-version
  assertions advanced (start 0.4.0 -> 0.5.0, tick 0.7.0 -> 0.8.0);
  `test/release_lib_baseline.json` re-anchored (version -> 0.36.0, `lib_digest`
  unchanged); the housekeep doc baseline re-anchored to the grown spec
  (491 -> 506).

## 0.35.1 — re-ship status config-presence warning; release v0.35.1

- **Release v0.35.1 (patch — LIB-REFRESH):** `_PLUGIN_VERSION` 0.35.0 -> 0.35.1;
  committed plugin tree regenerated so the shipped `lib/status.py` carries
  scheduling 0.50.0's config-presence warning:
  - `lib/status.py` — `status_data()` gains `config_path` + `local_config_present`;
    `render_status` shows a loud `WARNING:` line naming the config path (plus the
    shipped-defaults / no-scope-filter / aggressive-auto-merge trap) when the
    project-local `config.json` is missing or empty, or a quiet `config <path>`
    confirmation when present — surfacing the wrong-anchor / unconfigured run at a
    glance.
- **No `_LIBS` change** — `status.py` already registered in `_NORMALIZED_LIBS`;
  pure lib-refresh with no build change beyond the version bump. `run_tick.py` /
  `work_intake.py` byte-identical to 0.35.0.
- **Shipped `default-config/` CONTENT unchanged** — config schema unchanged; no
  shipped-agent change; only `lib/status.py` and the version stamps
  (`plugin.json` + `marketplace.json` -> 0.35.1) move.
- Version-assertion test advanced to 0.35.1; an e2e test added asserting the
  committed `lib/status.py` carries the config-presence warning
  (`local_config_present` + `config_path` + `render_status` `WARNING`);
  `test/release_lib_baseline.json` re-anchored (version -> 0.35.1, `lib_digest`
  -> the new committed-lib digest); the housekeep doc baseline re-anchored to the
  grown spec (480 -> 491).

## 0.35.0 — re-ship the operator/debug wave; release v0.35.0

- **Release v0.35.0 (minor — LIB-REFRESH):** `_PLUGIN_VERSION` 0.34.0 -> 0.35.0;
  committed plugin tree regenerated so the shipped libs carry scheduling 0.49.0's
  sources:
  - `lib/run_tick.py` — the `tick_start` event now carries a version+file
    provenance block: `plugin_version` (plus a `plugin_version=<v>` trace token),
    `lib_dir`, `runtime_dir`, and the resolved `config_path`/`route_path`/
    `adapter_map_path` with their default-vs-override source, so an operator can
    see exactly which code + config a tick ran with. Purely additive;
    `EVENT_KINDS` unchanged.
  - `lib/status.py` — a new injectable, tolerant release-check probe
    `DEFAULT_RELEASE_PROBE` shelling `gh` against the distribution repo;
    `status_data()` gains `latest_version`/`update_available`/`release_check_error`
    and `render_status` shows an update-available / up-to-date / check-errored
    line, so `/auto-maintainer:status` tells the operator when a newer release is
    available.
- **No `_LIBS` change** — both libs already registered in `_NORMALIZED_LIBS`;
  pure lib-refresh with no build change beyond the version bump.
- **Shipped `default-config/` CONTENT unchanged** — config schema unchanged; no
  shipped-agent change; only the two affected libs and the version stamps
  (`plugin.json` + `marketplace.json` -> 0.35.0) move.
- Version-assertion test advanced to 0.35.0; an e2e test added asserting the
  committed `run_tick` + `status` carry the v0.35.0 provenance + release-check
  tokens; `test/release_lib_baseline.json` re-anchored (version -> 0.35.0,
  `lib_digest` -> the new committed-lib digest); the housekeep doc baseline
  re-anchored to the grown spec (464 -> 480) so the drift, #355 monotonicity,
  and doc-size guards stay green.

## 0.34.0 — re-ship the disposition-visibility wave; release v0.34.0

- **Release v0.34.0 (minor — LIB-REFRESH):** `_PLUGIN_VERSION` 0.33.0 -> 0.34.0;
  committed plugin tree regenerated so the shipped libs + triager carry
  work-intake 0.13.0 + scheduling 0.48.0's sources:
  - `lib/work_intake.py` — new `ALREADY_DONE_MARKER` + `gh_issue_already_done_sink`:
    an on-issue disposition for an `already_done` outcome mirroring the reject
    sink, reusing `REJECTED_LABEL`, NEVER closing, idempotent on the distinct
    marker; plus the pure `is_strong_reason` guard so no weak/boilerplate
    disposition comment is ever posted.
  - `lib/run_tick.py` — enacts the `already_done` on-issue disposition trust-gated
    by `permits('file', mode)`, leaving the issue open; and gates BOTH the reject
    and `already_done` dispositions on `is_strong_reason` (a weak reason or a
    missing evidence commit means the disposition is NOT enacted and NOT recorded,
    so the item re-works next tick).
  - `agents/auto-maintainer-triager.md` — v1.5.0: mandates a concrete, specific
    reject reason.
- **No `_LIBS` change** — both libs already registered; the triager ships via
  `ship/agents`; pure lib-refresh with no build change beyond the version bump.
- **Shipped `default-config/` CONTENT unchanged** — config schema unchanged;
  only the two affected libs, the shipped triager agent, and the version stamps
  (`plugin.json` + `marketplace.json` -> 0.34.0) move.
- Version-assertion test advanced to 0.34.0; an e2e test added asserting the
  committed libs + triager carry the v0.34.0 already_done-sink + is_strong_reason
  + triager-v1.5.0 tokens; `test/release_lib_baseline.json` re-anchored
  (version -> 0.34.0, `lib_digest` -> the new committed-lib digest); the housekeep
  doc baseline re-anchored to the grown spec (446 -> 464) so the drift, #355
  monotonicity, and doc-size guards stay green.

## 0.33.0 — re-ship the loop-convergence wave; release v0.33.0

- **Release v0.33.0 (minor — LIB-REFRESH):** `_PLUGIN_VERSION` 0.32.0 -> 0.33.0;
  committed plugin tree regenerated so the shipped libs carry work-intake
  0.12.0 + implement 0.12.0 + scheduling 0.47.0's sources:
  - `lib/work_intake.py` — new pure `is_in_flight(item, in_flight_issue_refs)` +
    `Pull(in_flight_issue_refs=…)`: an UNCONDITIONAL PULL exclusion of any issue
    that already has an OPEN loop PR (left open/untouched); a consume-only set,
    no new gh plumbing.
  - `lib/implement.py` — Handoff schema 1.1.0 -> 1.2.0 adds the terminal
    `already_done` status carrying its evidence in
    `artifact.kind="already-on-main"` / `ref=<commit-sha>`; `validate_handoff`
    treats it as a verdict-free non-`opened` handoff.
  - `lib/run_tick.py` — `make_pull` computes the in-flight issue-ref set from the
    acted-ledger `opened` entries live-confirmed OPEN via `gh_pr_state_source`
    and threads it into `Pull`; an `already_done` handoff records a
    terminal-resolved `triage_memory` skip WITHOUT incrementing backoff.
  - `agents/auto-maintainer-implementer.md` — v2.12.0: report `already_done` (not
    `blocked`) when the fix is already on `main`.
- **No `_LIBS` change** — all three libs already registered; pure lib-refresh
  with no build change beyond the version bump.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; only the three affected libs, the shipped implementer
  agent, and the version stamps (`plugin.json` + `marketplace.json` -> 0.33.0)
  move.
- Version-assertion test advanced to 0.33.0; an e2e test added asserting the
  committed libs + agent carry the v0.33.0 in-flight-PULL-exclusion +
  already_done (schema 1.2.0) + in-flight-wiring tokens;
  `test/release_lib_baseline.json` re-anchored (version -> 0.33.0, lib_digest ->
  the new committed-lib digest); the housekeep doc baseline re-anchored to the
  grown spec (427 -> 446) so the drift, #355 monotonicity, and doc-size guards
  stay green.

## 0.32.0 — re-ship the audit-wave RECONCILE fixes; release v0.32.0

- **Release v0.32.0 (minor — LIB-REFRESH):** `_PLUGIN_VERSION` 0.31.0 -> 0.32.0;
  committed plugin tree regenerated so the shipped libs carry verify-integrate
  0.12.0 + scheduling 0.46.0 + implement 0.11.1's sources:
  - `lib/verify_integrate.py` — all disposable-worktree git ops in
    `reconcile_rebase_worktree` + the GATE helper run
    `-c core.hooksPath=/dev/null`, so the target repo's `post-checkout` hook can
    no longer wedge tier-1 conflict recovery; `gh_closing_issue_ref` resolves the
    closing-issue number via `gh api graphql`
    (`closingIssuesReferences(first:1)`) with a `Closes #N` body-parse fallback,
    instead of the unsupported `closingIssuesReferences` `--json` field.
  - `lib/run_tick.py` — `_reconcile_ledger_seed` derives a canonical
    `owner/repo#N` issue_ref by stripping the `-wo` suffix, so the RECONCILE
    seed's `gh issue view` no longer fails every tick.
  - `lib/open_pr.py` — the implementer worktree add runs hooks-free
    (`-c core.hooksPath=/dev/null`), mirroring the reconcile/GATE hooks-off fix.
- **No `_LIBS` change** — pure lib-refresh with no build change beyond the
  version bump.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; only the three affected libs and the version stamps
  (`plugin.json` + `marketplace.json` -> 0.32.0) move.
- Version-assertion test advanced to 0.32.0; an e2e test added asserting the
  committed libs carry the v0.32.0 hooks-free-worktree + gh-graphql + `-wo`
  issue_ref tokens; `test/release_lib_baseline.json` re-anchored (version ->
  0.32.0, lib_digest -> the new committed-lib digest); the housekeep doc baseline
  re-anchored to the grown spec (415 -> 427) so the drift, #355 monotonicity, and
  doc-size guards stay green.

## 0.31.0 — re-ship RECONCILE worktree-robustness + recovery observability; release v0.31.0

- **Release v0.31.0 (minor — LIB-REFRESH):** `_PLUGIN_VERSION` 0.30.0 -> 0.31.0;
  committed plugin tree regenerated so the shipped libs carry verify-integrate
  0.11.2 + scheduling 0.45.0's sources:
  - `lib/verify_integrate.py` — `reconcile_rebase_worktree` now uses a UNIQUE
    per-invocation `mkdtemp` worktree + `git worktree prune` before add, so a
    tick killed mid-rebase can never wedge later tier-1 conflict recovery on a
    leftover worktree.
  - `lib/run_tick.py` — `tick_end` surfaces the RECONCILE recovery outcome
    (`rebased`/`relanded`/`reconcile_errors` counts + refs, with `>0`-only trace
    tokens), so a silently-erroring RECONCILE is never invisible.
- **No `_LIBS` change** — `open_pr.py` was already registered in v0.30.0; this is
  a pure lib-refresh with no build change beyond the version bump.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; only the two affected libs and the version stamps
  (`plugin.json` + `marketplace.json` -> 0.31.0) move.
- Version-assertion test advanced to 0.31.0; an e2e test added asserting the
  committed libs carry the v0.31.0 reconcile-worktree + observability tokens;
  `test/release_lib_baseline.json` re-anchored (version -> 0.31.0, lib_digest ->
  the new committed-lib digest); the housekeep doc baseline re-anchored to the
  grown spec (403 -> 415) so the drift, #355 monotonicity, and doc-size guards
  stay green.

## 0.30.0 — re-ship the loop-convergence fixes + register open_pr lib; release v0.30.0

- **Release v0.30.0 (minor — loop-convergence fixes + a new implementer lib
  reach installs):** `_PLUGIN_VERSION` 0.29.0 -> 0.30.0; committed plugin tree
  regenerated so the shipped libs carry verify-integrate 0.11.1 + scheduling
  0.44.0 + implement 0.11.0's sources: `lib/verify_integrate.py` — RECONCILE
  `_pr_number` tolerates a URL-form `pr_ref` so tier-1 conflict-recovery never
  crashes; `lib/run_tick.py` — RECONCILE seed uses canonical `owner/repo#N` refs
  + a durable prior-verdicts snapshot that survives the fresh-tick reset, so the
  (B) race-breaker actually recovers a CONFLICTING loop PR.
- **Build change — register `open_pr.py` in `_LIBS`:** `build_plugin._LIBS`
  gains `open_pr.py` (`implement/src/open_pr.py`), a pure-stdlib
  (argparse/subprocess/sys) byte-for-byte lib exactly like `test_gate.py`, so
  the shipped `agents/auto-maintainer-implementer.md` (v2.11.0) can invoke
  `${CLAUDE_PLUGIN_ROOT}/lib/open_pr.py` (its deterministic worktree-setup +
  explicit `--base <default>` PR-open that fixes wrong-base stacking). Without
  this registration the deployed agent's call would fail. The implementer agent
  md re-collects via the `ship/` convention.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; only the affected libs, the new `lib/open_pr.py`, the
  re-collected agent md, and the version stamps (`plugin.json` +
  `marketplace.json` -> 0.30.0) move.
- Version-assertion test advanced to 0.30.0; ship assertions added for
  `lib/open_pr.py` (byte-identical to `implement/src/open_pr.py`, no `.rabbit`
  leak); `test/release_lib_baseline.json` re-anchored (version -> 0.30.0,
  `lib_digest` -> the new committed-lib digest incl. `open_pr.py`); the
  housekeep doc baseline re-anchored to the grown spec (387 -> 403) so the
  drift, #355 monotonicity, and doc-size guards stay green.

## 0.29.0 — re-ship the auto-merge-completion observability; release v0.29.0

- **Release v0.29.0 (minor — auto-merge-completion observability reaches
  installs):** `_PLUGIN_VERSION` 0.28.0 -> 0.29.0; committed plugin tree
  regenerated so the shipped libs carry verify-integrate 0.11.0 + scheduling
  0.43.0's sources: `lib/verify_integrate.py` — RECONCILE now records every
  merged `acted_ledger` PR in `reconcile_result.auto_merged`, surfacing an
  auto-merge GitHub completed asynchronously between ticks; `lib/run_tick.py` —
  `tick_end` surfaces `auto_merged` + `auto_merged_refs`, and
  `_persist_reconcile_outcome` stamps those ledger entries terminal
  `outcome="merged"` so the completion reports exactly once.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; this is a lib-refresh release, not a config-content
  change. Only the affected libs and the version stamps (`plugin.json` +
  `marketplace.json` -> 0.29.0) move.
- Version-assertion test advanced to 0.29.0; `test/release_lib_baseline.json`
  re-anchored (version -> 0.29.0, `lib_digest` -> the new committed-lib digest
  since the shipped lib bytes changed) so the drift and #355 monotonicity guards
  stay green.

## 0.28.0 — re-ship the RECONCILE prior-verdict race-breaker; release v0.28.0

- **Release v0.28.0 (minor — RECONCILE race-breaker reaches installs):**
  `_PLUGIN_VERSION` 0.27.0 -> 0.28.0; committed plugin tree regenerated so the
  shipped libs carry verify-integrate 0.10.0 + scheduling 0.42.0's sources:
  `lib/verify_integrate.py` — RECONCILE's (B) ladder now recovers a loop PR
  whose LIVE mergeability is still `UNKNOWN` at tick-top by consulting the
  previous tick's confirmed-CONFLICTING VERIFY verdict, so a genuinely
  conflicting loop PR no longer lingers; `lib/run_tick.py` — `make_reconcile`
  seeds the new `prior_verdicts` slot from the durable persisted verdicts;
  `lib/status.py` — the heartbeat fallback now sources `DEFAULT_GOVERNANCE` = 10.
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; this is a lib-refresh release, not a config-content
  change. Only the affected libs and the version stamps (`plugin.json` +
  `marketplace.json` -> 0.28.0) move.
- Version-assertion test advanced to 0.28.0; `test/release_lib_baseline.json`
  re-anchored (version -> 0.28.0, `lib_digest` -> the new committed-lib digest
  since the shipped lib bytes changed); `test/housekeep_doc_baseline.json`
  re-anchored (spec.md 364 -> 376 for the v0.28.0 release note) so the drift,
  #355 monotonicity, and doc-no-growth guards stay green.

## 0.27.0 — re-ship the stylish-configure lib; release v0.27.0

- **Release v0.27.0 (minor — new configure UX reaches installs):**
  `_PLUGIN_VERSION` 0.26.0 -> 0.27.0; committed plugin tree regenerated so the
  shipped `lib/configure.py` carries safety-governance 0.17.0's changes:
  `/auto-maintainer:configure --show` (and the post-write echo) now print a
  human-readable, loop-stage-ordered `render_config` view **by default**, a new
  `--json` flag preserves the raw machine JSON, and
  `issue_filter.exclude_labels` is promoted into the `_field_catalog` (so it
  surfaces in `--describe`/`--setup`/the render).
- **Shipped `default-config/` CONTENT unchanged** — config still schema 2.9.0
  with the same values; this is a lib-refresh release, not a config-content
  change. Only `lib/configure.py`, the version stamps (`plugin.json` +
  `marketplace.json` -> 0.27.0) move.
- Version-assertion test advanced to 0.27.0; `test/release_lib_baseline.json`
  re-anchored (version -> 0.27.0, `lib_digest` -> the new committed-lib digest
  since the shipped lib bytes changed); `test/housekeep_doc_baseline.json`
  re-anchored (spec.md 353 -> 364 for the v0.27.0 release note) so the drift,
  #355 monotonicity, and doc-no-growth guards stay green.

## 0.26.0 — shipped default-config schema 2.9.0 + neutral defaults; release v0.26.0

- **Shipped `default-config/config.json` updated to config schema 2.9.0.**
  `issue_filter` renamed to the neutral pull-all default
  `{"include_labels": [], "with_title_regex": null, "exclude_labels": []}` — the
  ship-as-is `exclude_labels` is now `[]` (no longer seeds `auto-maintainer-rejected`;
  a project re-adds it via `/configure`). `heartbeat.interval_minutes` ships at 10
  (was 3). Aligns with safety-governance 0.15.0.
- **Release v0.26.0 (minor — ship-as-is default behavior changed):** `_PLUGIN_VERSION`
  0.25.4 -> 0.26.0; committed plugin tree regenerated (plugin.json + marketplace.json
  0.26.0; the shipped default-config asset carries the new fields/defaults);
  version-assertion test + release_lib_baseline re-anchored.

## 0.25.4 — regen release: ship the verify-integrate REVIEW-scope narrowing

- Operator release step for the plugin: bump `_PLUGIN_VERSION` 0.25.3 -> 0.25.4
  (the single source of truth; #355 monotonicity requires strictly-greater) and
  regenerate the committed `plugins/auto-maintainer/` tree so the shipped
  `agents/auto-maintainer-reviewer.md` carries the merged `verify-integrate`
  0.12.3 change: the REVIEW reviewer is scoped to the PR's own diff — it no
  longer files merge-conflict/collision findings.
- Only the version stamps (`plugin.json` + `marketplace.json` -> 0.25.4) and the
  naturally-changed `agents/auto-maintainer-reviewer.md` bytes move; the shipped
  `lib/` is byte-identical (its digest is unchanged), so every other shipped
  byte is unchanged.
- Re-anchor `test/release_lib_baseline.json` (version -> 0.25.4; `lib_digest`
  unchanged since `lib/` did not change) so the build-drift and #355
  monotonicity guards stay green.
- No spec surface change: `docs/spec.md` is byte-identical; the release version
  is the `build_plugin.py` `_PLUGIN_VERSION` constant.
- Invariant: version 0.25.4 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak.

## 0.25.3 — regen release: ship the verify-integrate transient-UNKNOWN hotfix

- Operator release step for the plugin: bump `_PLUGIN_VERSION` 0.25.2 -> 0.25.3
  (the single source of truth; #355 monotonicity requires strictly-greater) and
  regenerate the committed `plugins/auto-maintainer/` tree so the shipped
  `lib/verify_integrate.py` carries the merged `verify-integrate` 0.12.2 hotfix:
  RECONCILE's `gh_pr_state_source` no longer polls a transient
  `mergeable=UNKNOWN`.
- Only the version stamps (`plugin.json` + `marketplace.json` -> 0.25.3) and the
  naturally-changed `lib/verify_integrate.py` bytes move; every other shipped
  byte is unchanged.
- Re-anchor `test/release_lib_baseline.json` (version + lib_digest) so the
  build-drift and #355 monotonicity guards stay green.
- No spec surface change: `docs/spec.md` is byte-identical; the release version
  is the `build_plugin.py` `_PLUGIN_VERSION` constant.
- Invariant: version 0.25.3 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak.

## 0.25.2 — regen release: ship the scheduling /status heartbeat display

- Operator release step for the plugin: bump `_PLUGIN_VERSION` 0.25.1 -> 0.25.2
  (the single source of truth; #355 monotonicity requires strictly-greater) and
  regenerate the committed `plugins/auto-maintainer/` tree so the shipped
  `lib/status.py` carries the merged `scheduling` 0.41.0 change:
  `/auto-maintainer:status` now shows the configured heartbeat interval.
- Only the version stamps (`plugin.json` + `marketplace.json` -> 0.25.2) and the
  naturally-changed `lib/status.py` bytes move; every other shipped byte is
  unchanged.
- Re-anchor `test/release_lib_baseline.json` (version + lib_digest) so the
  build-drift and #355 monotonicity guards stay green.
- No spec surface change: `docs/spec.md` is byte-identical; the release version
  is the `build_plugin.py` `_PLUGIN_VERSION` constant.
- Invariant: version 0.25.2 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak.

## 0.25.1 — regen release: ship the verify-integrate dedup hotfix

- Operator release step for the plugin: bump `_PLUGIN_VERSION` 0.25.0 -> 0.25.1
  (the single source of truth; #355 monotonicity requires strictly-greater) and
  regenerate the committed `plugins/auto-maintainer/` tree so the shipped
  `lib/verify_integrate.py` carries the merged `verify-integrate` 0.12.1 hotfix:
  the RECONCILE dedup no longer requests the invalid `closingIssuesReferences`
  field on `gh pr list`, and is fault-isolated so a dedup error can no longer
  crash the tick on stock `gh`.
- Only the version stamps (`plugin.json` + `marketplace.json` -> 0.25.1) and the
  naturally-changed `lib/verify_integrate.py` bytes move; every other shipped
  byte is unchanged.
- Re-anchor `test/release_lib_baseline.json` (version + lib_digest) so the
  build-drift and #355 monotonicity guards stay green.
- No spec surface change: `docs/spec.md` is byte-identical; the release version
  is the `build_plugin.py` `_PLUGIN_VERSION` constant.
- Invariant: version 0.25.1 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak.

## 0.25.0 — regen release: ship the RECONCILE/auto-merge/dedup fixes

- Operator release step for the plugin: bump `_PLUGIN_VERSION` 0.24.0 -> 0.25.0
  (the single source of truth; #355 monotonicity requires strictly-greater) and
  regenerate the committed `plugins/auto-maintainer/` tree so the shipped libs
  carry the merged `verify-integrate` 0.12.0 + `scheduling` 0.40.0 Wave-1/Wave-2
  fixes.
- Only the version stamps (`plugin.json` + `marketplace.json` -> 0.25.0) and the
  naturally-changed `lib/verify_integrate.py` + `lib/run_tick.py` bytes move;
  every other shipped byte is unchanged.
- Re-anchor `test/release_lib_baseline.json` (version + lib_digest) so the
  build-drift and #355 monotonicity guards stay green.
- No spec surface change: `docs/spec.md` is byte-identical; the release version
  is the `build_plugin.py` `_PLUGIN_VERSION` constant.
- Invariant: version 0.25.0 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak.

## 0.11.0 — convergence hardening for the GATE loop

- Regen release: cleans the build-drift left by two merged src PRs after the
  0.10.1 cut — the implementer agent supersede-on-retry (v2.8.0) and the
  work_intake Phase 2 park guard — neither of which regenerated the committed
  plugin tree.
- Ships supersede-on-retry: the implementer closes a prior open same-issue
  auto-maintainer PR before opening its replacement, so stale superseded PRs
  never linger to conflict or generate un-executable close-work.
- Ships the Phase 2 park guard: PULL excludes an issue after >=5 gate-fail
  markers, so a repeatedly-failing issue parks and the loop converges to idle
  instead of looping or escalating.
- Together they make the loop never stop or escalate.
- Bump `_PLUGIN_VERSION` 0.10.1 -> 0.11.0 (`plugin.json` + `marketplace.json`),
  a minor for the shipped convergence behavior.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src so
  the shipped implementer agent + `work_intake` lib reach installs release-clean;
  re-anchor `test/release_lib_baseline.json` (version + lib_digest) for the #355
  monotonicity guard.
- Invariant: version 0.11.0 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak; the shipped route resolves through `adapter_wiring.build_loop`
  with NO WiringError.

## 0.10.0 — regen release: clean post-merge drift + ship 3 features

- Ships three merged src features whose PRs changed src WITHOUT regenerating the
  committed plugin tree, leaving main with build-drift:
  - #353 — `verify_integrate` load-bearing-token doc-surface gate (#379).
  - #376 — `configure.py --regression-command` knob (#382).
  - #357 — `safety_governance` field-level 3-way merge for overridden config
    (#393).
- Ships the GATE-regression fix: the release-hygiene guards (build-drift guard,
  per-file committed-vs-fresh-normalization checks, baseline-digest guard) SKIP
  under `RABBIT_GATE`, so the per-PR cumulative GATE no longer false-fails every
  src-changing PR on expected pre-release drift; with `RABBIT_GATE` unset (a
  local run or a release cut) they run in full.
- Bump `_PLUGIN_VERSION` 0.9.0 -> 0.10.0 (`plugin.json` + `marketplace.json`), a
  minor for the shipped feature set.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src so
  the shipped lib (`verify_integrate`, `configure`, `safety_governance`) reaches
  installs release-clean; re-anchor `test/release_lib_baseline.json` (version +
  lib_digest) for the #355 monotonicity guard.
- Invariant: version 0.10.0 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak; the shipped route resolves through `adapter_wiring.build_loop`
  with NO WiringError.

## 0.9.0 — V2 cumulative GATE

- Adds the GATE state to the shipped default pipeline
  (`REVIEW -> GATE -> INTEGRATE`): a self-contained cumulative regression gate
  that runs the configured `regression_command` against each REVIEW-passed PR on
  top of prior gate-passed PRs, closing the semantic-conflict merge gap without
  external CI. Reverted the GitHub-CI-runs-tests approach.
- `default-config/route.json` inserts `GATE` into states after `REVIEW`, changes
  the `REVIEW OK` edge next `INTEGRATE -> GATE`, and adds `GATE OK -> INTEGRATE`;
  `REVIEW EMPTY -> PERSIST` and `INTEGRATE OK -> CLEANUP` unchanged.
- `default-config/adapter-map.json` wires `GATE -> run_tick:make_gate` (a script
  adapter mirroring VERIFY/INTEGRATE/CLEANUP). The shipped `config.json` leaves
  `regression_command` absent (= null = no-op gate PASS: a safe generic default;
  a repo gates by setting `regression_command` in its `.auto-maintainer/config.json`).
- Bump `_PLUGIN_VERSION` 0.8.2 -> 0.9.0 (`plugin.json` + `marketplace.json`), a
  V2 minor for the new GATE behaviour.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src so
  the shipped lib (`verify_integrate` GATE + `run_tick` `make_gate`) reaches
  installs; re-anchor `test/release_lib_baseline.json` (version + lib_digest) for
  the #355 monotonicity guard.
- Invariant: the shipped route+adapter-map resolve through
  `adapter_wiring.build_loop` with NO WiringError (GATE reads `verdicts`, writes
  `gate_results`; INTEGRATE reads `gate_results`); version 0.9.0 is consistent
  across `plugin.json` + `marketplace.json`; the committed tree matches a fresh
  build with no source-tree leak.

## 0.8.2 — regen release: green a red main (build-drift from #368)

- Bump `_PLUGIN_VERSION` 0.8.1 -> 0.8.2 (`plugin.json` + `marketplace.json`).
- Ships the #356 fresh-tick ephemeral-read-product reset in the plugin
  `lib/run_tick.py`: PR #368 changed scheduling's `run_tick.py` source (the #356
  fix) WITHOUT regenerating the committed plugin tree, so the committed shipped
  bytes drifted from the current source and the build-drift guard
  (committed == fresh build) failed on main.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src so
  the shipped `lib/run_tick.py` carries the #356 change under the 0.8.2 version;
  re-anchor `test/release_lib_baseline.json` (version + lib_digest) for the #355
  monotonicity guard.
- No behavior change beyond the shipped #356 fix.
- Invariant: version 0.8.2 is consistent across `plugin.json` +
  `marketplace.json`; the committed tree matches a fresh build with no
  source-tree leak; the #355 guard is re-anchored to the new committed-lib
  digest.

## 0.8.1 — release-clean the post-0.8.0 committed-tree change (#342 default_src)

- Bump `_PLUGIN_VERSION` 0.8.0 -> 0.8.1 (`plugin.json` + `marketplace.json`).
- Ships the #342 `default_src` tick-trace observability token in the plugin
  `lib/run_tick.py`: the tick trace line now emits `default_src=...`,
  distinguishing a shipped-default route/adapter-map from a user override. This
  token landed in the committed lib post-0.8.0 (via #350) but WITHOUT a version
  bump, so the shipped bytes drifted from the 0.8.0 tag while still reading
  version 0.8.0. v0.8.1 corrects that same-version content drift by cutting a
  clean release for the already-committed change.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src so
  the shipped bytes carry the #342 token under the 0.8.1 version.
- No behavior change beyond the shipped `default_src` observability token.
- Invariant: version 0.8.1 is consistent across `plugin.json` +
  `marketplace.json`; the shipped `lib/run_tick.py` carries the `default_src`
  token; the committed tree matches a fresh build with no source-tree leak.

## 0.8.0 — config resolution (#337) + shipped adapter-map guard-compliant (#335)

- Bump `_PLUGIN_VERSION` 0.7.13 -> 0.8.0 (`plugin.json` + `marketplace.json`).
- Shipped `default-config/adapter-map.json` drops IMPLEMENT harness isolation
  (#335): the v0.7.13 IMPLEMENT entry carried `isolation: "worktree"`, which the
  adapter-wiring load-time guard now REJECTS (harness isolation moves the acting
  subagent's cwd off the main workspace, losing its file-based handoff; the
  implementer self-isolates via its own worktree). The shipped IMPLEMENT entry is
  regenerated from `adapter_map_config._build_agent_entry('IMPLEMENT', …)`, which
  now emits NO harness isolation.
- The runtime READS `default-config/*.json` fresh; no more seed-once copy (#337):
  the shipped `default-config/{config,route,adapter-map}.json` remain the
  aggressive operational default and are still built into the tree by `_copy_tree`
  of `plugin_assets/`, but the runtime now reads them FRESH as the default on
  every start (scheduling for route/adapter-map, safety-governance for config),
  instead of `start.py` copying them ONCE into `.auto-maintainer/`. A release that
  changes a shipped default therefore reaches existing installs automatically; a
  user override in `.auto-maintainer/<file>` still wins.
- Regenerate the committed `plugins/auto-maintainer/` tree from current src.
- Invariant: the built tree still contains
  `default-config/{config,route,adapter-map}.json`, the shipped adapter-map
  resolves through `build_loop` with no WiringError, version 0.8.0 is consistent
  across `plugin.json` + `marketplace.json`, and the committed tree matches a
  fresh build with no source-tree leak.

## 0.7.13 — wire PRIORITIZE into the default pipeline (V1 audit fix)

- Bump `_PLUGIN_VERSION` 0.7.12 -> 0.7.13 (`plugin.json` + `marketplace.json`).
- `src/plugin_assets/default-config/route.json`: add the `PRIORITIZE` state and
  edges `TRIAGE OK -> PRIORITIZE`, `PRIORITIZE OK -> IMPLEMENT`, `PRIORITIZE EMPTY
  -> VERIFY` (keeping `TRIAGE EMPTY -> VERIFY`, `IMPLEMENT OK/BLOCKED -> VERIFY`,
  all else unchanged). The acting route is now `PULL -> TRIAGE -> PRIORITIZE ->
  IMPLEMENT -> VERIFY -> REVIEW -> INTEGRATE`.
- `src/plugin_assets/default-config/adapter-map.json`: wire `PRIORITIZE` to the
  script adapter `run_tick:make_prioritize`, and REPLACE the IMPLEMENT agent entry
  with the template-correct `_build_agent_entry('IMPLEMENT',
  'auto-maintainer:auto-maintainer-implementer')` form — per_item
  `execution_plan.ordered`, `inputs: [execution_plan]`, worktree isolation,
  `effect: implement`, `signal.rule: blocked_if_any`, writes `handoffs`. Without
  PRIORITIZE the same-feature serialization gate never ran; the prior IMPLEMENT
  entry read `work_orders` and bypassed the prioritized ordering.
- Regenerate the committed `plugins/auto-maintainer/` tree + `marketplace.json`
  from current src so the shipped bytes carry the wired default-config.
- New e2e test `test_default_pipeline_wires_prioritize_and_build_loop_resolves`:
  the shipped default-config route + adapter-map resolve through
  `adapter_wiring.build_loop` (from the plugin's own `lib/`) with NO WiringError;
  PRIORITIZE resolves as a script, IMPLEMENT is per_item `execution_plan.ordered`
  + worktree, and the acting route is `PULL->TRIAGE->PRIORITIZE->IMPLEMENT->
  VERIFY->REVIEW->INTEGRATE`. The version test is renamed to
  `test_version_bumped_to_0_7_13_and_consistent`.

## 0.7.12 — full self-deploy removal release: deploy #324/#325

- Bump `_PLUGIN_VERSION` 0.7.11 -> 0.7.12 (`plugin.json` + `marketplace.json`).
- Remove the dead self-deploy build helpers from `build_plugin.py`:
  `bump_version`, `package_commit_paths`, `_read_version`, `_bump_patch`,
  `_VERSION_RE`, and `_VERSION_ASSIGN` (now-orphaned `import re` removed too).
  With the self-deploy ACTION removed (scheduling #324) and the `self_deploy`
  knob removed (safety-governance #325), these had zero callers; the disk
  version-read served only the removed same-process `bump_version` rewrite.
- Revert `build()` to stamp `plugin.json` + `marketplace.json` from the
  in-memory `_PLUGIN_VERSION` constant directly (the #311/#314 disk-read is
  gone). The plugin is NOT self-deployable; releases are operator-cut by editing
  `_PLUGIN_VERSION`.
- KEEP `touches_shipped_src` + `SELF_DEPLOY_MARKER` unchanged — scheduling's
  `release_needed` operator signal still uses them.
- Regenerate the committed `plugins/auto-maintainer/` tree and
  `.claude-plugin/marketplace.json` from current src (the post-removal
  scheduling + safety-governance libs), so the shipped `lib/run_tick.py` carries
  no `_flush_package` / `git_commit_sink` and `lib/safety_governance.py` carries
  no `self_deploy` knob, while `release_needed` + `touches_shipped_src` remain.
- Tests: rename the version test to
  `test_version_bumped_to_0_7_12_and_consistent` (assert 0.7.12 in
  `plugin.json` + `marketplace.json`); replace the dormant-self-deploy unit
  tests with `test_dead_self_deploy_build_helpers_removed`,
  `test_release_detection_helpers_kept`, and
  `test_build_stamps_version_from_in_memory_constant`; replace the
  shipped/committed `*_keeps_self_deploy_off` e2e tests with
  `test_{shipped,committed}_tree_has_no_self_deploy_action_or_knob` (the action
  and knob are GONE; `release_needed` + `touches_shipped_src` remain); advance
  the housekeep doc baseline for the appended per-release section.

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

## 0.6.0 — release rebuild: deploy merged src fixes (#263/#264)

- Bump `_PLUGIN_VERSION` 0.5.0 -> 0.6.0 (`plugin.json` + `marketplace.json`).
- The committed `plugins/auto-maintainer/` tree had drifted from src: merged
  fixes — #255 model-review evidence gate
  (`verify_integrate.review_evidence_valid` / `batch_is_untrustworthy`), #252
  prioritize serialization, #259/#260/#261 — never reached the installed plugin.
  This release REBUILDS the committed tree from CURRENT src (no feature-src LOGIC
  change) and regenerates `.claude-plugin/marketplace.json`.
- Add a build-drift guard test asserting the committed tree == a fresh build.

## 0.5.0 — aggressive plug-and-play default (#211)

- Bump `_PLUGIN_VERSION` -> 0.5.0 (`plugin.json` + `marketplace.json`).
- Ship the `default-config/` seed assets — `config.json` (`mode: auto-merge`,
  unbounded budget, heartbeat 3, backoff 5), `route.json` (the full acting route
  incl. the REVIEW gate), and `adapter-map.json` (TRIAGE/IMPLEMENT/REVIEW wired
  to their agents) — authored under `src/plugin_assets/default-config/` and
  copied verbatim into `plugins/auto-maintainer/default-config/` by the existing
  `_copy_tree` of `plugin_assets/`. scheduling's `start.py` seeds these into a
  fresh install's `.auto-maintainer/` (idempotent).
- Invariant: the built tree contains
  `default-config/{config.json,route.json,adapter-map.json}` with
  `mode=auto-merge` + the REVIEW route; the version test asserts 0.5.0.
