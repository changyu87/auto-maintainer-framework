# Changelog — work-intake

All notable changes to this feature are recorded here. Versions follow the
`version:` frontmatter in `spec.md` / `contract.md` and the `feature.json`
`version` field.

## feature 0.6.0 / spec 0.6.0 / contract 0.6.0 — 2026-06-23

- **TRIAGE cross-cutting-risk slot** (FT-B, DESIGN §3.5.9). TRIAGE is the only
  state with the whole-batch view, so it now writes a machine-first
  `cross_cutting_risk` slot for VERIFY (§3.7.6) to act on.
- New `CrossCuttingRisk` slot schema `{risk, features, reason}` (versioned,
  machine-first) with `to_dict`/`from_dict` and the `CROSS_CUTTING_RISK_SLOT`
  descriptor (mirrors `WORK_ORDERS_SLOT`).
- `Triage.run` ALWAYS writes `cross_cutting_risk` (a default no-risk verdict when
  no batch annotation is supplied) so VERIFY can always read it;
  `TRIAGE_MANIFEST.writes` now declares both `work_orders` and
  `cross_cutting_risk`.
- New `normalize_cross_cutting_risk(annotation)` — a pure, deterministic
  normalizer/validator folding a batch annotation `{features, reason}` into a
  `CrossCuttingRisk`: `risk=true` only on ≥2 DISTINCT features AND a non-empty
  reason; single-feature/empty/whitespace → no-risk; malformed input rejected.
- `Triage(now=, cross_cutting_annotation=)` accepts the batch annotation,
  normalized once at construction.
- Shipped triager `auto-maintainer-triager` bumped to 1.3.0: it now emits the
  batch-level cross-cutting-risk annotation (affected features + specific reason)
  when blast radii overlap across DIFFERENT features — read-only judgment in its
  handoff, protocol-free.
- The housekeep doc baseline is re-based to the post-FT-B doc surfaces (FT-B
  additively documents the new public surface); the gate stays a re-bloat
  regression guard.
- New tests: CrossCuttingRisk roundtrip + schema_version; slot descriptor;
  normalizer risk=true on ≥2 features+reason; no-risk on single/empty/whitespace;
  validator rejects malformed; TRIAGE always writes the default slot (incl.
  all-rejected); risk=true slot from annotation; manifest declares the write.

## feature 0.5.0 / spec 0.5.0 / contract 0.5.0 — 2026-06-21

- **REPORT dedup-vs-open** (#224, DESIGN §3.5.4 applied to the REPORT side).
  `file_discoveries` previously deduped a discovery ONLY against the loop's own
  `report_ledger` (`known_dedup_keys`); it never checked whether an equivalent
  issue ALREADY EXISTS in the tracker, so a blocked implementer that emitted its
  blocked-on dependencies as `discovered_work` got them filed as NEW issues —
  exact duplicates of already-open ones (the live #222/#223-vs-#209/#210 case).
- `file_discoveries` gains a `known_open` arg (the tick's PULLed open tracker
  items): a discovery whose subject matches an already-open issue is skipped to
  the new `ReportResult.skipped_open` (`[{dedup_key, matched}]`) with NO sink
  call, instead of being filed as duplicate noise. Matching is a deterministic
  normalized-title token-overlap heuristic (`_match_open_issue`); a model-judged
  "is this already tracked?" check is the deferred robust v2. The ledger-dedup
  (`known_dedup_keys` → `skipped_existing`) is unchanged and takes precedence.
- `scheduling._flush_report` now passes the tick's `work_items` into
  `file_discoveries` as `known_open`; open-duplicate skips fold into the
  `reported=<filed>/<skipped>` surface (so they are NOT filed, and the dry-run
  would-file count excludes them). scheduling consumes work-intake unchanged
  except for the added arg.
- New tests: discovery == existing open issue → skipped (no sink call);
  genuinely-new discovery → filed; ledger-dedup precedence over open-match;
  default `known_open` preserves prior behaviour; the overlap heuristic;
  scheduling `_flush_report` end-to-end skip/file/dry-run with open work_items.

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
