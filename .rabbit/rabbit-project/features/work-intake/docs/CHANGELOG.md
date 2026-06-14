# Changelog — work-intake

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field.

## contract 0.3.0 / spec 0.2.0 — 2026-06-10

- **Ships the real TRIAGE judge as a subagent.** Adds
  `ship/agents/auto-maintainer-triager.md` — the read-only triage judge.
  work-intake owns the TRIAGE domain and the `WorkOrder` schema, so it ships
  this subagent; the build's `ship/` pass copies `ship/agents/` → the plugin's
  `agents/` with NO build change.
- The triager is **protocol-free**: its definition bakes in NO output schema /
  output_path / dispatch-result filename / file-format detail. Those are carried
  by the invocation-envelope prompt agent-dispatch renders at the `TRIAGE`
  agent-state. Its tools are read-only `Read`/`Grep`/`Glob` plus `Write`.
- It is **read-only judgment, not action**: it produces `work_orders` carrying
  `decision: accepted|rejected` + `reason`; it never modifies the tracker or the
  repo (enacting decisions is the later IMPLEMENT acting state's job).
- Contract `provides.agents` now lists `ship/agents/auto-maintainer-triager`.
- New e2e tests (`test/test_triager_ship_e2e.py`): the shipped file parses
  (name, tools, protocol-free body), and the TRIAGE→triager agent-adapter wiring
  validates through adapter-wiring's `build_loop` over a
  `GUARD→DRAIN→PULL→TRIAGE→PERSIST→EXIT` route (TRIAGE resolves to an
  AgentState; data-readiness satisfied). adapter-wiring + agent-dispatch are
  consumed UNCHANGED (test-only imports); no work-intake source was touched.

## contract 0.2.0 / spec 0.1.0 — 2026-06-11

- Slice 2 (TRIAGE validity gate → `work_orders`): the `WorkOrder` slot schema,
  `TRIAGE_MANIFEST` / `TRIAGE_SIGNALS`, and the deterministic `Triage` state
  with an injectable staleness reference time.

## contract 0.1.0 / spec 0.1.0

- Slice 1 (GitHub-Issues PULL adapter): the `WorkItem` slot schema,
  `PULL_MANIFEST` / `PULL_SIGNALS`, an injectable issue source, and the `Pull`
  state mapping open issues into the `work_items` slot.
