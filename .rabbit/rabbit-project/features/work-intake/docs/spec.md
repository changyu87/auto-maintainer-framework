---
feature: work-intake
version: 0.9.0
owner: changyu87
deprecation_criterion: Superseded when the tracker I/O model changes incompatibly (e.g. multi-tracker support, or the WorkItem / WorkOrder / DiscoveredIssue schema reaches a breaking major version).
---

# work-intake

## Purpose

The read-side first adapter: fetch actionable work from the tracker into the
blackboard. The GitHub-Issues `PULL` adapter fetches the repo's open issues into
the `work_items` slot each tick, replacing the `DEMO_WORK` stub.

> Design references: DESIGN.md §3.4.2 (GitHub-Issues PULL adapter), §2.6 (PULL
> contract: reads —, writes `work_items`, signals `OK`|`EMPTY`; the `WorkItem`
> slot schema), §3.5.1 (intake/normalize). Tool-tier: **CLI** (`gh`) — spec-rules §1.

## Paths governed

Greenfield. Code under `.../features/work-intake/src/`.

## Public surface (slice 1)

1. **`WorkItem` slot schema** — the typed shape of a tracker item:
   `{ id, number, title, body, url, state, labels: [str], author, created_at,
   updated_at, comments: [{author, created_at, body}] }`. Machine-first,
   versioned (`schema_version`). Owned here (fsm-contracts deferred the concrete
   domain slot schemas to their owning features). Downstream features
   (TRIAGE/PRIORITIZE/IMPLEMENT) consume it. `comments` carries the issue's
   human follow-up discussion (the latest guidance often lives there, not the
   body) and is **bounded** — the most recent `MAX_COMMENTS_PER_ITEM` comments,
   each body capped at `MAX_COMMENT_BODY_CHARS` — so a long thread cannot bloat
   the rendered triager/implementer envelope.

2. **`PULL` state** — `run(TickContext) -> StateResult` (fsm-contracts contract).
   Fetches the configured repo's **open** issues (narrowed by the optional
   `issue_filter`, item 5 below), maps each to a `WorkItem`,
   under the `work_own_filings=False` opt-out **excludes loop-filed items**
   (`is_loop_filed`, §3.11.5 — see below), writes survivors to `work_items`,
   emits `OK` if any remain else `EMPTY`. Per-state manifest:
   `{ reads: [], writes: ["work_items"], emits: ["OK", "EMPTY"] }`.

3. **Injectable issue source (determinism seam)** — the production source shells
   the deterministic **`gh` CLI** (`gh issue list --state open --json
   number,title,body,url,state,labels,author,createdAt,updatedAt`), which carries
   its own auth. The source is INJECTABLE so tests pass a stub returning fixture
   issues — no network, fully deterministic (spec-rules §1: a failure is locatable
   to the fetch boundary, not a flaky live call). `gh issue list` does **not**
   return comments, so the source ALSO shells `gh issue view <number> --json
   comments` per pulled issue to attach the (bounded) discussion thread. The
   underlying subprocess `runner` is injectable, and a per-issue comment fetch
   that fails is tolerated (the item keeps an empty `comments`) — a flaky comment
   read must never sink the whole PULL.

4. **Repo resolution** — slice 1 resolves the target repo from the project's `gh`
   default / git remote, or an injectable `repo` argument. Explicit config
   (repo, token) beyond the `issue_filter` (item 5) is deferred to the
   configuration feature.

5. **Issue filter (`issue_filter`) — optional label + title narrowing.** PULL
   optionally narrows WHICH open issues it pulls, from the canonical
   `issue_filter` object `{labels: List[List[str]], title_pattern: str|None}`
   that `safety-governance` owns + normalizes (its `issue_filter(config)`
   accessor) and `scheduling`/`tick-orchestrator` threads into
   `Pull(issue_filter=...)`. work-intake stays repo-I/O-bounded: it CONSUMES the
   already-normalized object (it does NOT read `config.json` — that is
   safety-governance's job), applying it as:
   - **Labels (DNF, OR-of-ANDs) — server-side.** For a non-empty `labels`, the
     production `gh_issue_source` runs **one `gh issue list --state open
     --label <l1> --label <l2> …` query per AND-group** (repeated `--label` is
     `gh`'s native AND) and **unions the results, deduped by issue `number`**
     (`gh` cannot OR labels in one query, so OR is the union of per-group
     queries). An empty `labels` `[]` runs the single existing all-open query.
     Filtering server-side also cuts the per-issue `gh issue view … comments`
     fetches to only the matching issues.
   - **Title (`title_pattern`) — post-fetch.** `gh` has no title query, so a
     non-`null` `title_pattern` is applied as a regex `search` over each fetched
     issue's title, dropping non-matches **before** the comment enrichment. A
     `null` pattern is a no-op.
   The two narrowings **compose** (an issue must clear the labels DNF AND match
   the title pattern). The default `issue_filter` (empty labels + `null` pattern)
   is a no-op: PULL pulls every open issue exactly as before (non-breaking). The
   filter is threaded through the injectable source seam — the source contract is
   `source(repo, issue_filter=None) -> list[WorkItem]` — so tests drive it with a
   stub, and the live-`gh` label/union/title logic is tested against an injected
   `runner` (no network).

## Determinism & testability

The only non-deterministic edge (the live `gh` call) sits behind the injectable
source; tests drive `PULL` with a stub over fixture issues, asserting the
`work_items` slot + `OK`/`EMPTY` signal. No AI/prompt tier anywhere.

## Slice 2 — TRIAGE (validity gate → work_orders)

Turn raw `work_items` into validated `work_orders`. Slice 2 implements a
**deterministic validity gate** only; richer TRIAGE is deferred.

1. **`WorkOrder` slot schema** — a validated, decision-carrying item:
   `{ id, work_item_id, title, body, url, labels, decision: accepted|rejected,
   reason, created_at, comments: [{author, created_at, body}], target_feature:
   [str] }`. Machine-first, versioned. Written to the `work_orders` slot,
   consumed downstream (PRIORITIZE/IMPLEMENT, future). `comments` is carried
   verbatim from the source `WorkItem` so the implementer — which reads
   `work_orders`, not `work_items` — also sees the human discussion thread.
   `target_feature` is the order's blast-radius target feature(s) (issue #258),
   the SORTED list of normalized feature keys TRIAGE computes via the pure
   `target_features_for` (prefixed labels, a `Component:`/`Feature:` body line, a
   conventional title prefix; empty when none provable) so PRIORITIZE reads an
   authoritative field instead of re-scraping.
2. **`TRIAGE` state** — `run(TickContext) -> StateResult`: reads `work_items`,
   applies a deterministic validity gate (well-formed = has a title; not stale =
   updated within a hardcoded window; in-scope = open, non-draft), maps each
   accepted item to a `WorkOrder` (1:1, no decompose) and stamps its
   `target_feature` from the authoritative signals (#258), writes the
   `work_orders` slot, emits `OK` if any accepted else `EMPTY`. Rejected items
   may be recorded with a reason but are not forwarded. Manifest `{reads:
   ["work_items"], writes: ["work_orders"], emits: ["OK","EMPTY"]}`. Script-tier,
   no AI.
3. **Determinism** — pure rules over the in-memory `work_items`; no network, no
   AI. The stale window is hardcoded (config deferred, #17-style).

## Cross-cutting-risk slot (DESIGN §3.5.9, FT-B)

TRIAGE is the only state with the whole-batch view, so it flags when accepted
work orders' blast radii may overlap across **different** features and writes a
machine-first `cross_cutting_risk` slot for VERIFY (§3.7.6) to act on.

1. **`CrossCuttingRisk` slot schema** — `{ risk: bool, features: [str], reason }`,
   versioned (`schema_version`); `CROSS_CUTTING_RISK_SLOT` mirrors
   `WORK_ORDERS_SLOT`. The default value is no-risk (`risk=false`, empty
   `features`, empty `reason`).
2. **`TRIAGE` ALWAYS writes the slot.** Every `Triage.run` writes
   `cross_cutting_risk` (default no-risk when no annotation) so VERIFY can always
   read it. `TRIAGE_MANIFEST.writes` declares both `work_orders` and
   `cross_cutting_risk`.
3. **`normalize_cross_cutting_risk(annotation)`** — a pure, deterministic
   normalizer/validator folding a batch-level annotation `{features, reason}`
   into a `CrossCuttingRisk`: `risk=true` ONLY when ≥2 DISTINCT features AND a
   non-empty reason; single-feature / empty / whitespace → `risk=false`.
   Malformed input (non-mapping, non-list features, non-string entries/reason) is
   REJECTED (`ValueError`/`TypeError`). Same-feature overlap is handled by
   serialization (§3.8.6); this flag is the residual semantic cross-feature case.

## Shipped subagent — the TRIAGE judge

work-intake owns the TRIAGE domain and the `WorkOrder` schema, so it ships the
real triage JUDGE as a subagent: `ship/agents/auto-maintainer-triager.md`. The
build's `ship/` collection copies `ship/agents/` → the plugin's `agents/` with
NO build change, so the shipped file IS the deployed subagent definition.

- **Read-only judgment, not action.** The triager produces `work_orders` each
  carrying `decision: accepted|rejected` + a `reason`; it never modifies the
  tracker or the repo. Its tools are the read-only `Read`/`Grep`/`Glob` plus
  `Write` (to emit its output file). Enacting a decision — close a rejected
  item, implement an accepted one — is the later IMPLEMENT acting state's job,
  not the triager's.
- **Protocol-free, prompt-contracted.** The subagent definition bakes in NO
  output schema, output_path, dispatch-result filename, or file-format detail.
  Those are carried by the invocation-envelope prompt that agent-dispatch
  renders at the `TRIAGE` agent-state. The triager copies the concrete output
  example the prompt embeds.
- **Wiring.** The `TRIAGE` agent-state's adapter-map entry names this subagent
  (`subagent_type: auto-maintainer:auto-maintainer-triager`) with manifest
  `{reads: ["work_items"], writes: ["work_orders"], emits: ["OK","EMPTY"]}`. The
  TRIAGE→triager wiring validates through adapter-wiring's `build_loop` (TRIAGE
  resolves to an AgentState; data-readiness is satisfied because PULL writes the
  `work_items` TRIAGE reads).

The deterministic in-process `Triage` validity gate (Slice 2 below) and this
LLM triage judge coexist: the gate is the script-tier fast path; the judge is
the agent-tier path a project wires at the `TRIAGE` port when richer judgment is
wanted.

- **Loopback / provenance guard (§3.11.5) — CONDITIONAL PULL EXCLUSION, gated on
  `work_own_filings`.** Items the loop filed itself carry the provenance stamp
  `filed_by: autonomous-maintainer` — the `filed-by:autonomous-maintainer` label
  (and the `<!-- am-dedup:... -->` body marker REPORT writes, see Slice 3). The
  owner flipped the policy to **default-ON**: the loop works its own filings.
  `Pull(work_own_filings=...)` is a boolean (DEFAULT `True`, the safety-governance
  knob value): when `True` (default) PULL INCLUDES loop-filed items so they flow
  through TRIAGE/IMPLEMENT like any issue; when `False` (the opt-out) PULL applies
  the deterministic **EXCLUSION** — it drops any work_item for which
  `work_intake.is_loop_filed(item)` is true, so loop-filed items never become
  `work_items` / `work_orders` and stay open for human triage. The exclusion is
  deliberately NOT a TRIAGE reject — a reject would route to the doer's close
  path and CLOSE the discovery. `is_loop_filed` lives in work-intake (next to the
  label + `<!-- am-dedup: -->` body marker that `gh_issue_file_sink` writes).
- **Park guard (Phase 2 convergence) — UNCONDITIONAL PULL EXCLUSION at the retry
  threshold.** An issue whose (bounded) comments record **>= `PARK_THRESHOLD`
  (hardcoded 5)** DISTINCT failed merge attempts — each attempt marked by the
  FIXED string `<!-- auto-maintainer:gate-fail -->` that verify-integrate's
  INTEGRATE posts on the issue (source of truth:
  `verify_integrate.GATE_FAIL_MARKER`), whose JSON payload carries the failed
  `pr_ref` — has failed too many times. PULL **EXCLUDES** it (parks it) via the
  pure `work_intake.is_parked(item)`, so the loop stops re-working it and
  **CONVERGES to idle** instead of looping forever; the issue stays OPEN with its
  gate-fail comments for a human to resolve on the tracker (the loop NEVER stops
  or escalates mid-run — this is how "never escalate" holds). Unlike the loopback
  guard this exclusion is UNCONDITIONAL (not gated on a config knob) and, like it,
  is a PULL exclusion (NOT a TRIAGE reject, which would close the issue).
- **`is_parked` counts DISTINCT attempts, not raw marker occurrences.** Each
  retry is a distinct PR (the implementer supersedes its prior open PR before
  opening a new one), so `is_parked` parses the marker comment's JSON payload and
  counts the number of **distinct `pr_ref` values** across the item's `comments`
  bodies; the count reaches `PARK_THRESHOLD` only after that many genuinely
  distinct failed PRs. This is what makes park a true RETRY counter rather than a
  tick-age timer: INTEGRATE re-posts a gate-fail marker every tick the same
  unchanged PR is re-gated, so counting raw marker occurrences would park an item
  after `PARK_THRESHOLD` *ticks* regardless of how many times it was actually
  retried. A marker whose JSON payload is absent or unparseable (or carries no
  `pr_ref`) falls back to counting that comment as one distinct attempt (keyed by
  its position) so malformed markers never silently defeat the guard.

## Slice 3 — REPORT (outbound filing → DiscoveredIssue)

The **write-side mirror of PULL** (DESIGN §1.3, §2.6, §3.11): an
adapter-swappable OUTBOUND port that turns discoveries into durably-tracked
items. work-intake owns the inbound tracker I/O (PULL), so it also owns the
outbound (REPORT) port, schemas, and default GitHub filing adapter. REPORT is
**out-of-band** — NOT a routed tick state; `scheduling.run_tick` flushes
discoveries through it after the route runs (that wiring + the journaled
idempotency live in scheduling).

- **`DiscoveredIssue` schema** (machine-first, versioned; owned here):
  `{ schema_version, title, body, kind: bug|enhancement|task, severity, target:
  project|maintainer-self, dedup_key, filed_by: "autonomous-maintainer" }`.
  `dedup_key` is a stable caller-supplied key making filing idempotent; `target`
  selects the destination tracker; `filed_by` stamps loop provenance.
- **`ReportResult` schema**: `{ filed: [{dedup_key, tracker_ref, url}],
  skipped_existing: [dedup_key], skipped_open: [{dedup_key, matched}],
  errors: [{dedup_key, reason}] }`. Re-filing an
  existing `dedup_key` is a no-op that returns the prior `tracker_ref`;
  `skipped_open` records a discovery NOT filed because its subject already
  matches an OPEN tracker issue (dedup-vs-open), `matched` being that issue.
- **Injectable filing sink (determinism seam, mirrors `gh_issue_source`).** The
  production `gh_issue_file_sink(discovery, repo=None) -> {tracker_ref, url}`
  shells `gh issue create` with the title/body, the provenance label
  `filed-by:autonomous-maintainer`, and a `<!-- am-dedup:<dedup_key> -->` marker
  appended to the body (so a later PULL/TRIAGE — and a dedup re-scan — can
  recognize the filing). The sink is INJECTABLE so tests pass a stub (no
  network, deterministic; a failure is locatable to the file boundary).
  - **It ENSURES the provenance label exists first (live-found bug).**
    `gh issue create --label <L>` FAILS if label `L` is absent in the repo — and
    a fresh repo has no `filed-by:autonomous-maintainer` label, so every filing
    errored (silently, since `file_discoveries` catches sink errors). The sink
    therefore first runs `gh label create filed-by:autonomous-maintainer
    --description "filed by the autonomous maintainer"` (idempotent — a non-zero
    "already exists" exit is tolerated, NOT raised) before the issue create. Both
    `gh` calls honor `--repo` and the injectable `runner`.
- **`file_discoveries(discoveries, sink, known_dedup_keys, known_open) ->
  ReportResult`** — pure orchestration: for each `DiscoveredIssue`, if its
  `dedup_key` is in `known_dedup_keys` it goes to `skipped_existing` (no sink
  call); else if its subject matches an already-OPEN issue in `known_open`
  (dedup-vs-open, DESIGN §3.5.4 applied to the REPORT side: the loop must not
  file a duplicate of an issue already in the tracker) it goes to `skipped_open`
  (no sink call); otherwise the sink is invoked and the result recorded in
  `filed`; a sink exception is caught and recorded in `errors` (filing one bad
  discovery never aborts the batch). The dedup-vs-open match is a deterministic
  normalized-title token-overlap heuristic (`_match_open_issue`); a model-judged
  "is this already tracked?" check is the deferred robust v2. `known_open` are
  the tick's PULLed open tracker items (scheduling passes its `work_items`).
  Deterministic given the injected sink + known set + open set; no I/O of its own.
- **Target routing (§3.11.6).** `target: project` files into the repo PULL reads
  (the default); `target: maintainer-self` files into the **fixed upstream
  maintainer repo** (`safety_governance.MAINTAINER_REPO`, the dogfood case
  §3.10.5) — **never** the project tracker, with **no fallback**. The sink takes
  the destination `repo` from its caller; scheduling's
  `run_tick._repo_for_target` supplies `MAINTAINER_REPO` for `maintainer-self`
  and the gh-default (project) repo for `project`.
- **Trust interaction (§3.11.7).** Filing is the `file` effect. `file_discoveries`
  itself is pure (it always files through the sink it is handed); the
  trust-ladder GATE lives in `scheduling.run_tick`, which only calls
  `file_discoveries` when `safety_governance.permits("file", mode)` — at
  `dry-run` the intent is logged, not filed; at `propose`/`auto-merge` it files.

## Known gaps / deferred

- **Richer TRIAGE deferred:** dedup-vs-closed (§3.5.3), 1-level decompose
  (§3.5.5), dependency ordering (§3.5.7), WHAT-generation/spec seam (§3.5.8, the
  AI seam).
- **Loopback / provenance guard (§3.11.5)** — IMPLEMENTED + default-ON:
  `work_intake.is_loop_filed` recognizes the provenance stamp; `PULL` includes
  loop-filed items by default (`work_own_filings=True`, the loop works its own
  filings) and EXCLUDES them only under the `work_own_filings=False` opt-out.
- **`PRIORITIZE`** (execution_plan) — separate state, deferred.
- **Label + title issue filtering** — IMPLEMENTED (public surface item 5): PULL
  consumes the normalized `issue_filter` (DNF labels applied server-side via
  per-AND-group `gh --label` union, `title_pattern` applied post-fetch). Owned +
  normalized by `safety-governance`; threaded in by `scheduling`.
- **Non-GitHub trackers**, explicit repo/token config, pagination tuning —
  deferred.

## Interfaces (composition)

- Implements the fsm-contracts state contract so `tick-orchestrator` runs `PULL`
  as a route state.
- Writes the `work_items` slot consumed downstream (TRIAGE/PRIORITIZE/IMPLEMENT,
  future).
- Invokes the external `gh` CLI (declared in the contract). Once integrated,
  `scheduling`'s route swaps `DEMO_WORK` → `PULL` (a separate integration step:
  scheduling route + packaging rebuild).
