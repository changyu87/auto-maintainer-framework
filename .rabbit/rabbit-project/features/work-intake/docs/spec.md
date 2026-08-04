---
feature: work-intake
version: 0.12.0
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
   (`is_loop_filed`, §3.11.5 — see below), **excludes in-flight items** whose
   issue already has an OPEN loop PR (`is_in_flight`, "In-flight guard" below),
   writes survivors to `work_items`,
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
   - **Exclude labels (`exclude_labels`) — post-fetch NEGATIVE term.** `gh`'s
     per-AND-group union query cannot express negation, so a non-empty
     `exclude_labels` (a flat OR of forbidden labels, normalized by
     safety-governance) is applied post-fetch: any fetched issue carrying ANY
     listed label is DROPPED **before** the comment enrichment. An empty
     `exclude_labels` `[]` is a no-op. This is how a disposed reject
     (`REJECTED_LABEL`) is kept out of PULL.
   The narrowings **compose** (an issue must clear the labels DNF AND match the
   title pattern AND carry no `exclude_labels` label). The default `issue_filter`
   (empty labels + `null` pattern + empty `exclude_labels`)
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
  `Write` (to emit its output file). Enacting a decision is NOT the triager's
  job: an accepted order is implemented by IMPLEMENT; a rejected order's
  disposition (comment + `rejected` label, NO close) is enacted DETERMINISTICALLY
  at TRIAGE-time by scheduling's adapter (see "Reject disposition" below) — never
  by the doer.
- **Stamps `target_feature` from real analysis (issue #258, root-cause fix).**
  The triager MUST analyze each accepted issue's problem — reading the issue body,
  comments, and the affected code — and emit an authoritative `target_feature`
  (the blast-radius scope at plugin+component granularity: the SORTED list of
  normalized feature keys the change will touch). This is the AUTHORITATIVE source
  PRIORITIZE reads to serialize same-feature orders; leaving it empty forces
  PRIORITIZE onto the brittle title-parse fallback (`target_features_for`) and
  lets same-scope orders fan out in parallel and collide. The deterministic
  `target_features_for` remains ONLY as the fallback when the field is absent.
- **Emits rejected orders too (for the deterministic reject disposition).** The
  triager MUST include each rejected issue in `work_orders` with
  `decision: rejected`, a concrete `reason`, and its source `work_item_id` / issue
  ref, so scheduling can enact the reject disposition. PRIORITIZE already forwards
  only `accepted` orders to IMPLEMENT, so a rejected order never reaches the doer.
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
  deliberately NOT a TRIAGE reject — a reject would comment + apply the
  `REJECTED_LABEL` (see "Reject disposition"), whereas a loop-filed discovery must
  stay open AND un-labeled for human triage. `is_loop_filed` lives in work-intake
  (next to the label + `<!-- am-dedup: -->` body marker that `gh_issue_file_sink`
  writes).
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
  is a PULL exclusion (NOT a TRIAGE reject, which would label the issue
  `REJECTED_LABEL` and exclude it); a parked issue stays open and un-labeled with
  its gate-fail comments for a human.
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
- **In-flight guard (convergence) — UNCONDITIONAL PULL EXCLUSION of issues with
  an OPEN loop PR.** An issue whose work is ALREADY IN FLIGHT — i.e. an open
  auto-maintainer PR is already addressing it — must NOT be re-triaged or
  re-implemented; doing so re-opens duplicate/superseding PRs and burns tokens
  re-deriving work the open PR already carries. PULL therefore **EXCLUDES** any
  candidate work_item whose issue ref is a member of an injected
  `in_flight_issue_refs` set, via the pure
  `work_intake.is_in_flight(item, in_flight_issue_refs)`. The excluded issue is
  **LEFT OPEN and untouched** — its open PR's own lifecycle resolves it
  (INTEGRATE/auto-merge on success, RECONCILE on conflict); PULL neither closes,
  comments on, nor labels it. Like the loopback and park guards this is a PULL
  exclusion (NOT a TRIAGE reject, which would apply `REJECTED_LABEL`); an
  in-flight issue stays open and un-labeled.
  - **Bounded scope — work-intake CONSUMES the set, it does NOT compute it.**
    Resolving WHICH issues have an open loop PR requires PR-side reads (listing
    open loop PRs, resolving each PR's closing-issue ref, and/or the acted
    ledger's `opened` entries) that live OUTSIDE work-intake's repo-issue-I/O
    scope. So `in_flight_issue_refs` is threaded IN by `scheduling` exactly as
    the normalized `issue_filter` and `work_own_filings` are — computed by
    scheduling by reusing the EXISTING seams (verify-integrate's open-PR /
    graphql closing-issue resolver already used by RECONCILE same-issue dedup,
    and/or the acted-ledger `opened` entries keyed `owner/repo#N-wo`). work-intake
    adds **no new gh plumbing and no new cross-feature reads** — it applies a set
    it is handed. `Pull(in_flight_issue_refs=...)` DEFAULTS to the empty set, a
    **no-op** (PULL pulls every open issue exactly as before — non-breaking).
  - **`is_in_flight(item, in_flight_issue_refs)`** — a pure, deterministic
    predicate (mirrors `is_parked`): true iff the item's issue ref
    (`owner/repo#N`, derived from the item's `url`/`number`) is in the injected
    set. No I/O.
  - **Merged/closed PRs do NOT exclude.** The set carries ONLY refs with an
    **open** loop PR; an issue whose only loop PR is merged/closed — or which has
    no loop PR — is NOT in the set and flows through PULL normally (the
    already-merged-but-open case is handled elsewhere: IMPLEMENT's `already_done`
    terminal outcome, recorded skip by scheduling).
  - **Propagates to refire.** Because excluded items never enter `work_items`,
    the refire/`_work_remains` predicate (scheduling) does not refire on an
    in-flight issue — the loop converges instead of re-pulling it every tick.

## Reject disposition (deterministic, at TRIAGE — NOT at IMPLEMENT)

A semantically-rejected issue (the triager's `decision: rejected` + `reason`) is
disposed of DETERMINISTICALLY at TRIAGE-time — it is **commented and labeled,
never closed by default** — so a human can see why and the loop stops re-pulling
it. This replaces the retired model where a rejected order was closed by the
IMPLEMENT doer (that reject→close branch is removed from the implementer). Only a
SEMANTIC reject (the AI triager) is disposed here; STRUCTURAL exclusions
(malformed/stale/not-open at the deterministic gate, loop-filed, parked) stay
open and UN-labeled for human triage, exactly as before.

- **`REJECTED_LABEL`** — the fixed label string `auto-maintainer-rejected` a
  disposed reject carries. Owned here (work-intake owns tracker labels);
  `safety-governance`/`packaging-config` reference the SAME literal to exclude it
  from PULL (see below).
- **`reject_dispositions(work_orders) -> [{work_item_id, issue_ref, reason}]`** —
  a pure selector returning the disposition payload for every `decision: rejected`
  order. Deterministic; no I/O.
- **`gh_issue_reject_sink(issue_ref, repo, reason, label=REJECTED_LABEL) ->
  None`** — a NEW injectable tracker sink (mirrors `gh_issue_file_sink`): it
  ENSURES the label exists (`gh label create`, idempotent), posts ONE comment
  carrying the `reason` behind a FIXED machine marker
  (`<!-- auto-maintainer:rejected -->`), and applies the label
  (`gh issue edit <n> --add-label`). It NEVER closes the issue. Idempotent: if the
  item already carries `REJECTED_LABEL` it is a no-op (no duplicate comment).
- **PULL exclusion of the reject label.** The default `issue_filter` (owned by
  `safety-governance`, shipped by `packaging-config`) gains a NEGATIVE
  (exclude-label) term for `REJECTED_LABEL`, so a disposed reject is not re-pulled;
  a human removing the label re-admits it. This is the belt to the suspenders of
  scheduling recording the item `rejected` in `triage_memory`.
- **Enactment + `triage_memory` are scheduling's** — the make_triage adapter calls
  `reject_dispositions` + `gh_issue_reject_sink` for each reject and records a
  `rejected` status in `triage_memory` (landed in the consumers wave). Closing a
  reject remains an explicit opt-in, never the default.

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
  production `gh_issue_file_sink(discovery, repo=None, apply_labels=None) ->
  {tracker_ref, url}`
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
  - **It ALSO stamps the PULL-visibility labels `apply_labels` (bug fix).** A
    filed discovery that carries ONLY `filed-by:autonomous-maintainer` is
    INVISIBLE to a later label-filtered PULL (which filters by the configured
    `issue_filter` labels) — so the loop could never re-pull work it filed for
    itself. The sink therefore accepts an `apply_labels` list (the labels that
    make a filed issue match the active `issue_filter` — safety-governance's
    `issue_filter_apply_labels(config)`, the first AND-group) and, for each,
    ENSURES it exists first (`gh label create <L>`, idempotent, same tolerate
    pattern as the provenance label) then adds it to the `gh issue create
    --label` set alongside `filed-by:autonomous-maintainer`. `apply_labels` is
    `None`/`[]` ⇒ unchanged (only the provenance label). The caller
    (`scheduling` REPORT flush) passes the labels ONLY for `project`-target
    filings (see Target routing); `maintainer-self` filings get `apply_labels=[]`.
- **`file_discoveries(discoveries, sink, known_dedup_keys, known_open,
  apply_labels=None) ->
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
  `apply_labels` (the active `issue_filter`'s PULL-visibility labels, from
  `scheduling`) is forwarded to the sink **only for `project`-target
  discoveries** (so a project filing is re-pullable); a `maintainer-self`
  discovery is filed with `apply_labels=[]` (the fixed MAINTAINER_REPO has its
  own/no filter). `None`/`[]` ⇒ every filing keeps just the provenance label
  (unchanged).
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
