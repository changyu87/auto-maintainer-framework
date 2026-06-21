# Changelog — work-intake

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field.

## feature 0.4.0 / spec 0.4.0 / contract 0.4.0 — 2026-06-21

- **PULL now includes each issue's comments** (#213). `gh issue list` returns
  only the original body — not comments — so the triager + implementer were
  blind to human follow-up guidance posted as comments (the most current
  guidance, a correction, or a resolution note often lives there). The
  `WorkItem` schema gains an additive `comments` field
  (`[{author, created_at, body}]`), bumping `WORK_ITEM_SCHEMA_VERSION` to
  `1.1.0`; it is carried through TRIAGE onto the `WorkOrder` (the implementer
  reads `work_orders`, not `work_items`), bumping `WORK_ORDER_SCHEMA_VERSION`
  to `1.1.0`. Both are additive (older readers ignore the field).
- **Bounded** so a long thread cannot bloat the rendered triager/implementer
  envelope: the MOST RECENT `MAX_COMMENTS_PER_ITEM` (= 20) comments are kept
  and each body is capped at `MAX_COMMENT_BODY_CHARS` (= 4000).
- **Fetched per pulled issue** via `gh issue view <n> --json comments` (gh's
  `issue list` does not return comments). The subprocess `runner` is INJECTABLE
  (the determinism seam, mirroring `gh_issue_source`/`gh_issue_file_sink`), and
  a per-issue comment fetch failure is TOLERATED (the item keeps an empty
  `comments`) so a flaky comment read never sinks the whole PULL.
- The `comments` field renders automatically into the triager/implementer
  envelopes (agent-dispatch's `render` is generic over the slot dict); the
  shipped `auto-maintainer-triager` prompt now tells the judge to read comments,
  not just the body.
- New tests (`test/test_comments_e2e.py`): parse mapping, the bound (most-recent
  N + body cap), schema roundtrips + version bumps, PULL committing the thread
  into the slot, TRIAGE carrying it onto the WorkOrder, and `gh_issue_source`
  fetching comments via the injected runner with no network (incl. the
  fetch-failure tolerance).

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
