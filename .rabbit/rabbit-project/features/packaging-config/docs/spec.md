---
feature: packaging-config
version: 0.1.0
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
    └── lib/{fsm_contracts,tick_orchestrator}.py
```

The built `plugins/auto-maintainer/` tree is **committed** (so a GitHub clone
includes it — required by the install flow below).

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
- **Shipped control skills** — `scheduling`'s `ship/skills/start` and
  `ship/skills/stop` land as `plugins/auto-maintainer/skills/{start,stop}`
  (`/auto-maintainer:start`, `/auto-maintainer:stop`).
- **Version bump** — `plugin.json` + `marketplace.json` `version` → `0.2.0`.

Added invariants (TDD targets): the built tree contains the start/stop skills and
all five loop libs; still **no `.rabbit` leak**; still idempotent; the shipped
`/auto-maintainer:start`'s `run_tick` import path resolves entirely within the
plugin (self-contained — Claude copies only the plugin dir).

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

## Current behaviour

None yet — feature is in `tdd_state: spec`.

## Known gaps / deferred (explicit boundaries)

- `userConfig` prompts at enable (tracker token, mode, budget) — §3.10.1,
  deferred (needs adapters).
- Project-local config + port→adapter wiring — §3.10.2, deferred (needs adapters).
- Full configure/run UX, heartbeat install bootstrap — §3.3.2/§3.10.4, later slice.
- Dogfood (rabbit-workflow as adapter #1) — §3.10.5, deferred.
- The maintainer loop itself (PULL/TRIAGE/IMPLEMENT/scheduling) — other features.

## Open questions

- Where exactly the core libs sit inside the plugin (`lib/` vs a package dir) and
  how future skills/hooks import them — settle when the first loop-driving
  component lands.
- Whether to also publish a tagged release / pin `version` per release vs. rely on
  marketplace `version` field alone (leaning explicit `version`).
