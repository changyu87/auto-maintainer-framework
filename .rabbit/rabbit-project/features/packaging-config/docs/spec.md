---
feature: packaging-config
version: 0.7.16
owner: changyu87
deprecation_criterion: Superseded when the framework adopts a different distribution channel than a self-hosted Claude Code plugin marketplace, or when later slices fold this into a full configure/run UX feature.
---

# packaging-config

## Purpose

Package the auto-maintainer-framework as a **clean, installable Claude Code
plugin** — containing **zero `.rabbit/` development infrastructure** — and serve
it from a self-hosted **plugin marketplace** so a user can install and run it via
the GitHub marketplace flow in a fresh session.

This is **slice 1**: the *packaging + install pipeline* (a "walking skeleton" for
distribution). The maintainer loop's real adapters do not exist yet; this slice
proves the artifact ships clean and installs, with one genuine v1 component (the
SessionStart persona/banner) so the install does something real.

> Design references: DESIGN.md §0 (form factor: Claude Code plugin via
> marketplace), §3.10.4 (plugin.json + marketplace layout + install UX),
> §3.9.2 / §3.10.3 (SessionStart banner + dispatcher-persona injection).
> Official Claude Code docs: code.claude.com/docs/en/plugins,
> /en/plugin-marketplaces.

## Core principle — clean ship

`.rabbit/` is the rabbit-workflow **development** tool, explicitly "not part of
the framework source" (see repo `.gitignore`). The product a user installs is the
auto-maintainer plugin ALONE. Because the framework source is currently developed
*inside* `.rabbit/rabbit-project/features/*/src/`, the only way to ship clean is
an **assembly step** that copies the shippable pieces OUT into a plugin tree and
leaves all rabbit dev infra behind. **No artifact under the shipped plugin tree
may contain `.rabbit/`.**

## Paths governed

Greenfield. The feature's own code (assembly script + tests) lives under
`.rabbit/rabbit-project/features/packaging-config/`. Its *outputs* live at the
repo root (`.claude-plugin/marketplace.json`) and `plugins/auto-maintainer/`.

## Public surface (what this slice produces)

1. **Assembly script** (`src/build_plugin.py` or equivalent) — deterministic,
   idempotent. Builds the clean plugin tree at `plugins/auto-maintainer/` from
   the framework sources, EXCLUDING `.rabbit/` and all dev infra. Re-running it
   on an unchanged source produces a byte-identical tree.

2. **`plugins/auto-maintainer/.claude-plugin/plugin.json`** — the shipped plugin
   manifest:
   - `name`: `auto-maintainer`
   - `version`: explicit semver (controlled updates, NOT per-commit SHA)
   - `description`, `author: { name: "changyu87" }`

3. **`.claude-plugin/marketplace.json`** (repo root) — the catalog:
   - `name`: `auto-maintainer`
   - `owner: { name: "changyu87" }`
   - `plugins`: one entry — `{ name: "auto-maintainer", source:
     "./plugins/auto-maintainer", description, version }`

4. **SessionStart persona/banner hook** — `plugins/auto-maintainer/hooks/hooks.json`
   wiring a SessionStart handler that injects the dispatcher persona/banner
   (DESIGN §3.9.2/§3.10.3). A genuine v1 component, not a throwaway — it proves
   the plugin loads and is the seed of the "CLAUDE.md substitute".

5. **`/auto-maintainer:status` skill** — `plugins/auto-maintainer/skills/status/SKILL.md`,
   a minimal status reporter (reports "no loop configured yet" at this slice).
   Gives a user-invocable proof-of-load.

6. **Core libs copied in** — `fsm_contracts` and `tick_orchestrator` placed under
   the plugin tree (e.g. `plugins/auto-maintainer/lib/`) as the loop's future
   internals. Copied by the assembly step from their feature `src/` dirs.

## Layout the build produces

```
auto-maintainer-framework/
├── .claude-plugin/marketplace.json          # catalog (repo root)
└── plugins/auto-maintainer/                  # CLEAN plugin (committed; no .rabbit/)
    ├── .claude-plugin/plugin.json
    ├── hooks/hooks.json                       # SessionStart persona/banner
    ├── skills/status/SKILL.md                 # /auto-maintainer:status
    ├── default-config/{config,route,adapter-map}.json  # operational default, read fresh at start
    └── lib/{fsm_contracts,tick_orchestrator}.py
```

The built `plugins/auto-maintainer/` tree is **committed** (so a GitHub clone
includes it — required by the install flow below).

**Shipped default-config wiring for RECONCILE + a neutral issue_filter default (Wave-2 consumers).**
The shipped `default-config/` data files wire the RECONCILE state out of the box
and carry a neutral (pull-all) issue_filter default:
- `route.json` — the shipped acting route inserts `RECONCILE` between `DRAIN` and
  `PULL` (`… GUARD → DRAIN → RECONCILE → PULL → TRIAGE → …`): the `DRAIN → PULL`
  edge is repointed to `DRAIN → RECONCILE`, with a new `RECONCILE → PULL` (`OK`)
  edge, and `RECONCILE` added to the `states` list. Routing stays pure data — no
  state names another (fsm-contracts).
- `adapter-map.json` — adds `"RECONCILE": "run_tick:make_reconcile"`.
- `config.json` — the shipped `issue_filter` is the **neutral ship-as-is default**
  `{"include_labels": [], "with_title_regex": null, "exclude_labels": []}` (schema
  2.9.0 field names, aligned with safety-governance's normalizer): empty
  include/exclude + null title-regex = **pull-all**, no filtering out of the box. A
  project narrows PULL (e.g. re-adds `auto-maintainer-rejected` to `exclude_labels`,
  or sets `include_labels`) via `/configure`. `heartbeat.interval_minutes` ships at
  **10** (the ship-as-is cadence). The config `schema_version` is **2.9.0**, in
  step with safety-governance (the loader also accepts the legacy
  `labels`/`title_pattern` keys during the coexistence window).

## Slice 2 — ship the loop core (the `ship/` collection convention)

Slice 1 hardcoded what to ship. Slice 2 makes the assembly **extensible** so the
real loop reaches the installed plugin:

- **`ship/` collection** — `build_plugin.py` scans `rabbit-project/features/*/ship/`
  and copies each feature's `ship/` contents into `plugins/auto-maintainer/` at
  the plugin root (`skills/`, `hooks/`, …). Each feature owns the components it
  ships; the assembly just collects them.
- **Loop-core libs** — copy `durable_state.py`, `lifecycle_dispositions.py`, and
  `scheduling`'s `run_tick.py` into `plugins/auto-maintainer/lib/` (alongside the
  existing `fsm_contracts.py` and `tick_orchestrator.py`), so the shipped
  `/auto-maintainer:start` can run a real tick self-contained.
- **Shipped control skills** — `scheduling`'s `ship/skills/{start,stop,status}`
  land as `plugins/auto-maintainer/skills/{start,stop,status}`. The slice-1
  packaging-config status STUB is REMOVED (scheduling now owns a script-backed
  `status`, #29/#30); packaging-config still ships only the SessionStart persona
  hook from its own assets. As new control skills are added by their owning
  feature (e.g. `scheduling`'s `clobber` reset skill), `build_plugin` MUST
  register each so it ships end-to-end: (1) its companion lib in the lib map
  (a sibling-importing control script is NORMALIZED like `status.py`; e.g.
  `clobber.py` from `scheduling/src/`), and (2) a curated one-line entry in
  `_COMMAND_DESCRIPTIONS` (a shipped skill with no curated description FAILS the
  build, keeping the README command table complete). The shipped-control-lib +
  control-skill count/enumeration tests are advanced in lockstep.
- **Version bump** — `plugin.json` + `marketplace.json` `version` → `0.2.0`
  (slice 2); bump the **patch** on each re-ship of the plugin tree (e.g. `0.2.1`
  after the scheduling #24 skill-path fix re-ship).

Added invariants (TDD targets): the built tree contains the start/stop skills and
all five loop libs; still **no `.rabbit` leak**; still idempotent; the shipped
`/auto-maintainer:start`'s `run_tick` import path resolves entirely within the
plugin (self-contained — Claude copies only the plugin dir).

## Slice — ship the configure lib (v0.2.19)

The IMPLEMENT doer's arsenal reaches the installed plugin. The new ship artifacts
— the `auto-maintainer-implementer` subagent (`implement`'s `ship/agents/`), the
`/auto-maintainer:configure` skill (`safety-governance`'s `ship/skills/`), and the
reworked tick executor skill v0.3.0 (`scheduling`'s `ship/skills/tick/`) — are
collected automatically by the existing `ship/` convention, with NO build change.
The one build change is a new **normalized lib**:

- **`configure.py`** (safety-governance's governance-config writer) is added to
  `_NORMALIZED_LIBS` so it lands at `plugins/auto-maintainer/lib/configure.py`.
  It imports its sibling `safety_governance` (the reader/decider), so it gets the
  plain self-path bootstrap (it already imports `os`/`sys` at top) inserted before
  its first sibling import (`import safety_governance as sg`) — the same
  normalization every sibling-importing lib receives, making the shipped copy
  resolve `safety_governance` from the co-located `lib/` alone. The
  `/auto-maintainer:configure` skill invokes it at `${CLAUDE_PLUGIN_ROOT}/lib/configure.py`.
- **Version bump** — `plugin.json` + `marketplace.json` `version` → `0.2.19`.

**Plugin patch v0.2.20** — re-ship to carry the scheduling `run_tick.py` fix that
absolutizes the agent-dispatch `output_dir` (auto-maintainer-framework#143), so a
worktree-isolated acting subagent writes its handoff to the shared main-workspace
`dispatch-out/` and the doer no longer re-dispatches / duplicates an act. No build
change beyond `_PLUGIN_VERSION → 0.2.20`; `lib/run_tick.py` is re-normalized from
the fixed source by the existing assembly.

**Plugin patch v0.2.21** — re-ship to carry the implementer subagent v2.0.0
(auto-maintainer-framework#143 follow-up): the doer self-manages its own git
worktree instead of relying on Claude Code's `isolation: worktree` (which
sandboxed its handoff write). No build change beyond `_PLUGIN_VERSION → 0.2.21`;
`agents/auto-maintainer-implementer.md` is re-collected from the updated
`ship/agents/` source by the existing assembly.

**Plugin patch v0.2.22** — re-ship to carry the scheduling `run_tick.py` change
that meters spend on ALL agent-state resumes (TRIAGE spend now counts toward the
budget window, not just the acting doer's). No build change beyond
`_PLUGIN_VERSION → 0.2.22`; `lib/run_tick.py` re-normalizes from the updated
source by the existing assembly.

**Plugin patch v0.2.23** — re-ship the REPORT outbound port (§3.11): the updated
`lib/work_intake.py` (DiscoveredIssue/ReportResult + `gh_issue_file_sink` +
`file_discoveries` + `is_loop_filed` + PULL loop-filed exclusion), `lib/run_tick.py`
(the out-of-band REPORT flush + report-ledger), and the triager `agents/`
subagent at v1.2.0. No build change beyond `_PLUGIN_VERSION → 0.2.23`; all three
re-collect/normalize from their updated sources by the existing assembly.

**Plugin patch v0.2.24** — ship the new `verify-integrate` lib (§3.7
VERIFY/INTEGRATE/CLEANUP). `lib/verify_integrate.py` is added to
`_NORMALIZED_LIBS` — it imports its siblings `fsm_contracts` (first import,
the bootstrap anchor) and `safety_governance`, and does NOT import `os`/`sys`
at module top, so it takes the `_SELF_PATH_BOOTSTRAP_WITH_IMPORTS` variant (the
same as `prioritize`/`implement`/`work_intake`). `lib/run_tick.py` re-normalizes
to carry the make_verify/make_integrate/make_cleanup wiring, and the implementer
`agents/` subagent re-collects at v2.1.0 (PR-label stamp). Version
`_PLUGIN_VERSION → 0.2.24`.

**Plugin patch v0.2.25** — re-ship the v1 polish pack. `lib/run_tick.py`
re-normalizes to carry backoff (§3.8.5: bounded-retry → escalate → defer + the
acted-ledger blocked-leak fix) and skip-unchanged re-triage (§3.5.3 triage
memory); `lib/safety_governance.py` + `lib/configure.py` re-copy/normalize with
`maintainer_repo` (§3.11.6 maintainer-self routing). No build change beyond
`_PLUGIN_VERSION → 0.2.25`; all re-collect from their updated sources.

**Plugin patch v0.2.26** — re-ship the hardened tick executor skill
(`skills/tick/SKILL.md` v0.4.0: the `--step` once / `--resume` after every
dispatch / `--step` again only on `invalid_output` protocol clarification, from
the REPORT live-demo finding). Collected automatically by the `ship/` convention;
no build change beyond `_PLUGIN_VERSION → 0.2.26`.

**Plugin patch v0.2.27** — re-ship the REPORT silent-failure fix found in the
live demo: `lib/work_intake.py` (`gh_issue_file_sink` now ensures the
`filed-by:autonomous-maintainer` label exists before `gh issue create`, so filing
no longer errors on a missing label) and `lib/run_tick.py` (the REPORT flush now
surfaces `report_errors=<n>`, so a sink failure is never silent). Both
re-normalize/re-copy from their updated sources; no build change beyond
`_PLUGIN_VERSION → 0.2.27`.

**Plugin patch v0.2.28** — re-ship the skipped-state terminal-crash fix:
`lib/run_tick.py` now seeds schema-valid empty defaults for the producible
read-product slots, so a tick that skips a producing state via a signal branch
(`VERIFY EMPTY → PERSIST`, `TRIAGE EMPTY → …`) persists the product empty instead
of crashing with a `ContractError`. Re-normalizes from the updated source; no
build change beyond `_PLUGIN_VERSION → 0.2.28`.

**Plugin minor v0.3.0 — configurables overhaul (the user-facing config surface).**
Ships the central-config + wiring CLIs:
- `lib/safety_governance.py` + `lib/configure.py` re-normalize from source (config
  schema 2.0.0: `config.json` replaces `governance.json`, per-tick budget removed,
  fixed `MAINTAINER_REPO`, `heartbeat`/`backoff` knobs; `configure.py --describe` /
  `--interval-minutes` / `--backoff-threshold`).
- TWO new libs join `_NORMALIZED_LIBS`: `lib/route_config.py` + `lib/adapter_map_config.py`
  (the wiring-config editors), each with the self-path import bootstrap so they
  resolve their sibling libs (`adapter_wiring`, `agent_dispatch`, `run_tick`,
  `work_intake`, `implement`, …) from `lib/` alone.
- The new ship skills `skills/route/`, `skills/adapter-map/`, plus the updated
  `skills/configure/` (`--setup`) and `skills/start/` (config-driven interval) are
  auto-collected by the ship convention.
- **Version bump** `_PLUGIN_VERSION → 0.3.0` — a MINOR (new CLIs + the breaking
  config schema 2.0.0), superseding the un-logged `0.2.29` (the implementer
  `discovered_work.body` fix, PR #192). The stale `test_version_bumped_to_0_2_28`
  assertion is updated to the current version.

Added invariants (TDD targets): the built tree contains `lib/configure.py`,
`lib/route_config.py`, and `lib/adapter_map_config.py` (each with the self-path
bootstrap resolving siblings from `lib/`), `skills/{configure,route,adapter-map}/SKILL.md`,
and `agents/auto-maintainer-implementer.md`; the plugin version is `0.3.0`; still
**no `.rabbit` leak**; still idempotent.

## Slice — ship a plugin-internal README.md (#16)

Per the Claude Code plugin docs best practice ("Add a README.md with
installation and usage instructions"), the **shipped, installed** plugin now
carries its own `README.md` at the plugin root, not only the repo-root README
(added in PR #15). A user inspecting the installed plugin (or the plugin cache)
finds usage docs there too.

- **Generated, not static** — `build_plugin.py` renders
  `plugins/auto-maintainer/README.md` into the clean tree. The **Commands**
  section is DERIVED from the shipped `skills/` dir (one row per
  `skills/<name>/` = `/auto-maintainer:<name>`), so it can never omit a shipped
  command; a skill with no curated description in `_COMMAND_DESCRIPTIONS` FAILS
  the build, keeping the docs complete as commands are added.
- **Accurate status** — the README states the plugin is a **v1-complete working
  autonomous maintainer** (full pull→triage→implement→verify→integrate→report
  loop, live-proven). It does NOT describe an early "packaging skeleton" (stale
  wording superseding the prior attempt, PR #200).
- **Content** — what the plugin is, the install steps (incl. `/reload-plugins`),
  a usage walk-through, the full Commands table (all seven shipped commands:
  `start`, `stop`, `status`, `tick`, `configure`, `route`, `adapter-map`), and
  the plugin layout. The `configure` row sets mode + per-day budget + heartbeat
  interval + backoff threshold in the central `config.json`.
- **Clean-ship** — the README is an asset, so the no-`.rabbit`-leak invariant
  applies to it like every other shipped file.

Added invariants (TDD targets): the built tree contains `README.md` that names
the plugin, documents `/reload-plugins`, lists every shipped slash command, and
states the v1-complete status (no "skeleton" wording); the build fails when a
shipped skill has no curated README description; still **no `.rabbit` leak**;
still idempotent.

## Distribution & test flow — GitHub only

Testing/distribution uses the **GitHub marketplace flow exclusively** — never
`claude --plugin-dir` and never a local-path marketplace. In a fresh session:

```
/plugin marketplace add changyu87/auto-maintainer-framework
/plugin install auto-maintainer@auto-maintainer
```

The relative `./plugins/auto-maintainer` source resolves because the marketplace
is added via git. Updates: `/plugin marketplace update`.

## Invariants / acceptance criteria (TDD targets)

- **No dev infra leaks:** the built `plugins/auto-maintainer/` tree contains no
  path matching `.rabbit` (directory or file) — the headline test.
- **Manifest placement:** `plugin.json` is at `plugins/auto-maintainer/.claude-plugin/`;
  component dirs (`hooks/`, `skills/`, `lib/`) are at the plugin ROOT, never
  inside `.claude-plugin/`.
- **Marketplace validity:** `.claude-plugin/marketplace.json` has required
  `name`, `owner`, `plugins[]`; the single entry's `name`/`source` resolve to the
  built tree.
- **Schema-valid plugin:** `claude plugin validate plugins/auto-maintainer`
  passes (run as a check where the CLI is available; otherwise a structural
  equivalent).
- **Idempotent build:** running the assembly script twice yields an identical
  tree (no nondeterministic ordering/timestamps in shipped files).
- **Self-contained:** the plugin references no files outside its own directory
  (Claude copies only the plugin dir into its cache).
- **Version monotonicity on content change (#355):** a change to the committed
  `plugins/auto-maintainer/lib` bytes relative to the last released version MUST
  advance `_PLUGIN_VERSION` — a same-version content change is
  marketplace-invisible (`/plugin marketplace update` serves by version).
  Anchored by `test/release_lib_baseline.json` (`{version, lib_digest}`): if the
  current committed-lib digest differs from the baseline digest, `_PLUGIN_VERSION`
  must differ from the baseline `version`. Docs-only / test-only changes leave the
  lib digest identical and are unaffected. Complements the build-drift guard
  (committed == fresh build).
- **Release-hygiene guards SKIP under `RABBIT_GATE` (per-PR gate context).** The
  guards that build/normalize a FRESH artifact from current src and assert it
  equals the COMMITTED tree/lib/baseline — the build-drift guard, the per-file
  committed-vs-fresh-normalization checks, and the baseline-digest guard — are
  **release** invariants, not per-PR correctness: a src-only PR legitimately
  drifts from the committed tree until a release regenerates it. So when the
  environment variable `RABBIT_GATE` is set (the verify-integrate GATE runs the
  regression with it exported via `scripts/gate-regression.sh`), these tests
  SKIP (return early, pass). With `RABBIT_GATE` unset — a normal local run or a
  release cut — they run in full. This keeps the per-PR GATE from false-failing
  every src-changing PR on expected pre-release drift while preserving the
  release gate. Correctness/logic tests (build_plugin behavior, shipped-route
  wires via build_loop, version consistency) always run.

## Current behaviour

Implemented and merged (`tdd_state: test-green`). The deterministic
`build_plugin.py` assembly + `ship/` collection produce the committed,
`.rabbit`-free `plugins/auto-maintainer/` tree + `.claude-plugin/marketplace.json`
(plugin v0.6.0), regenerated from current src. See `feature.json`.

## Known gaps / deferred (explicit boundaries)

- `userConfig` prompts at enable (tracker token, mode, budget) — §3.10.1,
  deferred (needs adapters).
- Project-local config + port→adapter wiring — §3.10.2, deferred (needs adapters).
- Full configure/run UX, heartbeat install bootstrap — §3.3.2/§3.10.4, later slice.
- Dogfood (rabbit-workflow as adapter #1) — §3.10.5, deferred.
- The maintainer loop itself (PULL/TRIAGE/IMPLEMENT/scheduling) — other features.

