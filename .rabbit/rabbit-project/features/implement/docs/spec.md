---
feature: implement
version: 0.11.1
owner: changyu87
deprecation_criterion: Superseded when the model-backed implement-then-PR doer (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the Handoff schema reaches a breaking major version.
---

# implement

## Purpose

The `IMPLEMENT` adapter state (DESIGN §1.1, §2.6) — the first tick state that
*acts* on work. This feature ships the **dry-run reference adapter**: it turns
the `execution_plan` produced by PRIORITIZE into a list of `handoffs`, **without
performing any work**. It is deterministic and inert — no model, no diff, no
branch, no PR, no tracker write, no filesystem effect.

This is the **`dry-run` rung of the trust ladder** (DESIGN §2.3, §3.8.2:
`dry-run` / `propose` / `auto-merge`). It proves the act-side seam — the
`Handoff` schema, the `execution_plan → handoffs` slot wiring, the signal, and
the per-tick surfacing — with ZERO repo risk. The model-backed
implement-then-PR doer (the `propose` rung, DESIGN §3.6.3) is a separate,
swappable adapter deferred to a later milestone (see Deferred).

## Slot contract (DESIGN §2.6)

```
state      reads           writes    signals
IMPLEMENT  execution_plan  handoffs  OK | BLOCKED
```

- **reads** `execution_plan` **and `work_orders`.** The dry-run adapter uses only
  `execution_plan` (the ordered ids). `work_orders` is read so the ACTING
  IMPLEMENT's per-item dispatch can join each `execution_plan.ordered` id to its
  full WorkOrder record (title/body/decision/url) via
  `agent_dispatch.build_envelopes` — without it the implementer would receive a
  bare id and an empty task and could not enact. The dry-run rung ignores
  `work_orders`. DESIGN §2.6 also lists `workspace` for
  IMPLEMENT, but that is the isolated worktree consumed by the model-backed doer
  (DESIGN §3.6.2). The dry-run adapter does no isolated code work, so it
  deliberately does NOT read `workspace` — keeping the route validator's
  data-readiness check (DESIGN §1.1.1) satisfiable without any predecessor
  writing `workspace`.
- **writes** `handoffs`.
- **emits** `OK` (handoffs produced, or the plan was empty), `BLOCKED` only when
  a plan entry is malformed and cannot be turned into a handoff.

## Handoff schema (this feature owns it)

Machine-first, versioned (DESIGN §3.6.1, §2.6 — the load-bearing seam between
the loop and any implementer).

```json
{
  "schema_version": "1.1.0",
  "work_order_id": "<id>",
  "status": "planned",
  "artifact": {"kind": "none", "ref": null},
  "discovered_work": [],
  "concerns": [],
  "blocked_reason": null
}
```

- `status` — `planned` for the dry-run rung. The schema's value space
  anticipates the doer's `opened` / `blocked` / `partial`, but the dry-run
  adapter only ever emits `planned`.
- `artifact` — `{kind, ref}`. DESIGN §2.6 lists `branch|pr`; the dry-run rung
  adds `none`, with `ref` null.
- `discovered_work` — follow-on items the implementer surfaces (DESIGN §1.3,
  §3.11.3). The dry-run adapter discovers nothing → always empty.
- `concerns` — self-flagged doubts the implementer wants a reviewer/human to
  look harder at on an `opened` handoff (analogous to the superpowers
  `DONE_WITH_CONCERNS` signal; auto-maintainer-framework#212). Mirrors
  `discovered_work`: always present, defaults to empty, surfaced downstream by
  REVIEW and REPORT. The dry-run adapter self-flags nothing → always empty.
- `blocked_reason` — null unless the handoff is blocked.

### Schema version history

- `1.0.0` — initial machine-first Handoff schema.
- `1.1.0` — **additive, backward-compatible**: adds the `concerns` list. A
  1.0.0 consumer reading a 1.1.0 handoff simply ignores the new field; nothing
  that existed in 1.0.0 changed.

## Deterministic correctness gate (DESIGN §3.6.3, FT-A)

IMPLEMENT is the **deterministic correctness gate** of the loop. The
model-backed implementer must not be merely *instructed* to run the target's
tests and *assert* a pass in its Handoff — a model "I ran it, it passed" claim
is untrustworthy (the #255 REVIEW rubber-stamp lesson). The gate is owned by a
SCRIPT, never the model's prose.

### Gate script — `src/test_gate.py`

A script that runs the TARGET feature's test command via `subprocess` and records
a machine-checkable verdict to a known artifact path:

```json
{"feature": "<target-feature-name>", "passed": true, "returncode": 0,
 "summary": "<final line of the test output>"}
```

- Invocation: `test_gate.py <feature-dir> --verdict-out <path>`. The script ships to the installed plugin's lib directory; the implementer subagent invokes it via the deployed Claude Code convention `${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py` (mirroring scheduling's shipped skills), never a dev source-tree path. The invocation is UNCHANGED by the configurability below — the gate resolves its command itself.
- **Configurable test command (`implement_test_command`).** The gate resolves
  the test command from the `implement_test_command` key in the central
  `${project_dir}/.auto-maintainer/config.json`. To stay **self-contained**
  (stdlib-only, byte-for-byte shippable — NO sibling-lib import), the gate reads
  that key with a direct, tolerant stdlib `json.load` of the config file (absent
  file / absent key / unreadable → the `null` default), NOT by importing
  `safety-governance`. The `implement_test_command` KEY is owned by
  `safety-governance`'s schema (and written by its `configure` surface); the gate
  is a contract-bound READER of that one key. Three-way:
  - **`null` / absent (default)** — run the target feature's
    `<feature-dir>/test/run.py` via `[sys.executable, run.py]` (the historical
    behavior; a **missing** `run.py` is a FAILED verdict `"no test/run.py found
    for target feature"`, so no PR is opened).
  - **a shell command string** — run THAT command instead (`shell=True`, cwd =
    the feature dir); exit 0 = pass. For repos whose tests are not the rabbit
    `test/run.py` convention (e.g. `pytest`). The verdict `summary` is the final
    output line as before.
  - **the sentinel `"none"` / `"skip"`** (case-insensitive) — the gate is
    SKIPPED: it writes a `passed=True`, `returncode=0` verdict with summary
    `"implement test-gate skipped (implement_test_command=none)"` and exits 0, so
    a PR may open without an implement-time test verdict (the explicit opt-out
    for harness-less repos; verification defers to the GATE-state
    `regression_command`).
  A `--test-command` CLI arg overrides the config-read value (the determinism
  seam tests drive); `--project-dir` selects the config location (else
  `$CLAUDE_PROJECT_DIR` / cwd). Resolution is tolerant — a missing/unreadable
  config or absent key falls back to the `null` (run.py) default, never crashing;
  the gate remains stdlib-only.
- `passed` is `True` only when the resolved command exits 0 (or the gate is
  skipped). A failing suite, a missing `test/run.py` (in the default mode), or
  any nonzero exit yields `passed=False`.
- The gate ALWAYS writes the verdict artifact (pass or fail) and mirrors the
  target's pass/fail in its OWN exit code so a caller detects it deterministically.
- The verdict is byte-deterministic for a given target: same input → equal
  recorded verdict.

### Handoff `test_verdict` evidence + validity predicate

The Handoff schema gains an OPTIONAL `test_verdict` evidence field carrying the
SCRIPT-produced verdict described above. `implement.py` exposes a deterministic
validity predicate `validate_handoff(handoff) -> ValidationResult(valid, reason)`:

- An `opened` handoff (an `accepted` order that opened a PR) is VALID **only**
  when it carries a `test_verdict` whose `passed` is `True`. An opened handoff
  with a missing or failing verdict is INVALID.
- A non-`opened` handoff (`planned` dry-run, `blocked`; legacy `closed`)
  opened no PR and so requires no verdict to be valid. (The doer no longer emits
  `closed` — reject disposition moved to TRIAGE — but `validate_handoff` stays
  tolerant of a legacy `closed` handoff for backward compatibility.)

The pass embedded in an opened handoff is the SCRIPT's recorded result, never
the model's claim — a fabricated "passed" cannot survive the gate because the
script ran the actual (possibly failing) target.

## Behaviour

1. Read `execution_plan` from `TickContext`.
2. For each `work_order_id` in `execution_plan.ordered` (in order), emit one
   handoff with `status="planned"`, `artifact={"kind":"none","ref":null}`,
   `discovered_work=[]`, `blocked_reason=null`.
3. Process the WHOLE plan — there is no budget cap (a per-task cap is not the
   spec's budget; the real token-ceiling budget, DESIGN §3.8.4, lives in
   safety-governance and only bites the model-backed doer).
4. Write `handoffs` and emit `OK`. An empty plan yields `handoffs=[]`, `OK`.
   A malformed entry (missing/empty id) yields a `BLOCKED` handoff for that
   entry with `blocked_reason` set, and the state signal is `BLOCKED`.

The state is a pure function of `execution_plan`: same input → byte-identical
handoffs.

## Adapter factory convention

Wired as a route-as-data adapter (adapter-wiring's
`factory(runtime) -> (StateManifest, run_callable)` convention), consumed by
`scheduling.run_tick` via `DEFAULT_ADAPTER_MAP`. Manifest declares
`reads=["execution_plan", "work_orders"]`, `writes=["handoffs"]`,
`emits=["OK", "BLOCKED"]`.

## Shipped implementer subagent (the propose-rung doer)

This feature also ships the **model-backed implementer subagent** as a
deployable artifact at `ship/agents/auto-maintainer-implementer.md` — the
`propose`-rung doer dispatched at the IMPLEMENT agent-state (DESIGN §3.6.2/§3.6.3).
It is **protocol-free**: it holds no built-in knowledge of the Handoff schema or
the output path. Its rendered prompt is the complete handoff contract (the
`## Task` / `## Inputs` / `## Handoff` envelope produced by agent-dispatch).

- **Role.** Given exactly ONE work order in its prompt, it implements the
  ACCEPTED order (it owns the WHAT, DESIGN §2.1), runs the project's checks, and
  **opens a PR against the default branch — never merges**. If it cannot complete
  the order it reports `status: blocked` and leaves no open PR. It does NOT close
  issues: a rejected order's disposition (comment + `rejected` label) is enacted
  DETERMINISTICALLY at TRIAGE (work-intake `gh_issue_reject_sink`, wired by
  scheduling), never by the doer.
- **Never reports `planned`; enacts accepted-only orders (v2.9.0).** `planned` is
  the DRY-RUN adapter's status, NOT the agent's — the shipped implementer reports
  only `opened` or `blocked`, never `planned` (and no longer `closed` — the
  reject→close branch is removed). PRIORITIZE fans out ACCEPTED-ONLY orders to
  IMPLEMENT, so the doer only ever sees an accepted order; a rejected order never
  reaches it. ROBUSTNESS: if the `## Inputs` work order lacks the source issue's
  title/body (an under-filled envelope), the agent FETCHES it from the work
  order's ref/url (`gh issue view <number> --repo <owner/repo> --json
  title,body,comments`) before enacting rather than bailing — an under-informed
  envelope becomes real work or an honest `blocked`, never a silent `planned`
  no-op.
- **Deterministic test-gate on the accept path (v2.6.0, DESIGN §3.6.3).** After
  committing and BEFORE `gh pr create`, the subagent MUST invoke the gate script
  at its deployed location `${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py` against the touched target feature; the gate runs `run.py` and
  records the `test_verdict`. The subagent may only report `status: opened` when
  that SCRIPT-produced verdict passes, and it embeds the verdict in the Handoff's
  `test_verdict` field — the pass is the script's recorded result, never the
  model's prose. A failing or missing verdict is NOT an open: the order is
  reported `status: blocked` with no open PR.
- **Regenerates a committed build tree in-PR (v2.7.0,
  auto-maintainer-framework#354).** Some repos check a built distribution tree
  (a plugin/package tree assembled from source) into version control, guarded by
  a build-drift guard that verifies the committed tree matches its source. On the
  accept path, after committing the code change and BEFORE the self-review, the
  subagent determines whether its edits touched source mirrored into such a
  committed tree; if so, it runs the repo's build step and commits the
  regenerated tree in the SAME PR, so a shipped-source change lands drift-free in
  one PR (green under the build-drift guard). A change touching only non-mirrored
  source (docs/tests, or a repo with no committed build tree) does NOT trigger a
  regen. This prevents the two-PRs-for-one-change churn where a shipped-src edit
  merges with drift and the guard then forces a second, regen-only PR.
- **Supersede-on-retry: closes a prior open loop-PR for the same issue (v2.8.0).**
  On the accept path, BEFORE `gh pr create`, the subagent checks for an EXISTING
  open `auto-maintainer`-labelled PR that resolves the SAME source issue
  (`gh pr list --label auto-maintainer --state open --json
  number,closingIssuesReferences`, matched to this issue). If one exists it is a
  PRIOR attempt this re-land supersedes, so the subagent CLOSES it
  (`gh pr close <n> --comment "superseded by the re-land"`) as it opens the
  replacement — a stale duplicate never lingers to conflict or to generate the
  un-executable "close PR X" work that otherwise loops forever (the loop has no
  other close-PR path). This is the ONLY PR the subagent ever closes, and only
  when it resolves the same issue as the PR being opened; no prior open PR for the
  issue ⇒ no-op. Complements Phase 2 park (work-intake) which bounds any residual
  non-convergence.
- **Script-backed worktree setup + explicit PR base (deterministic; fixes
  wrong-base stacking).** The worktree-creation and PR-open steps were previously
  PROMPT-TIER prose (`git worktree add … origin/<default>` with a discretionary
  "fetch first if needed", `gh pr create --base <default>` with `<default>` a
  model-filled placeholder). During a back-to-back drain burst this let consecutive
  implementer runs branch off the PREVIOUS loop branch instead of `main` and open
  **wrong-base STACKED PRs** (e.g. #844 based on #831's head, #846 on #844's), which
  INTEGRATE then refuses (never-merge-wrong-base) and nothing recovers — the loop
  piles up unmergeable PRs and never converges. Per spec-rules §4 (Script-Backed
  Orchestration), the order-critical git sequence now lives in a DETERMINISTIC
  companion script shipped to `${CLAUDE_PLUGIN_ROOT}/lib/` (mirroring
  `test_gate.py`), which the subagent INVOKES rather than hand-runs. The script
  UNCONDITIONALLY: (1) resolves the repo default branch
  (`gh repo view --json defaultBranchRef`), (2) `git fetch origin <default>`,
  (3) `git -c core.hooksPath=/dev/null worktree add <wt> -b <branch> origin/<default>`
  — start-point is the
  FRESHLY-FETCHED remote ref, NEVER local `HEAD`/whatever is currently checked out,
  and (4) opens the PR with an EXPLICIT `gh pr create --base <default>` (never an
  inferred/tracked base). So the PR base can never drift to a sibling loop branch,
  regardless of what a prior burst run left checked out. **The worktree add (and
  any checkout it does) runs HOOKS-FREE via `-c core.hooksPath=/dev/null`** so the
  TARGET repo's `post-checkout` hook does NOT fire in the disposable worktree — the
  implementer's throwaway tree is for mechanical edit/commit/push and never needs
  the repo's checkout-render hooks (a repo whose `post-checkout` fails in a fresh
  worktree, e.g. ssbdci-grimlock's `render_nested_components`, would otherwise be a
  fragility/failure source; this mirrors verify-integrate's reconcile/GATE
  hooks-free fix). The script owns the
  computed values (default branch, branch name, worktree path) with an injectable
  runner for deterministic tests; the agent `.md` calls it in place of the raw
  git/gh prose (agent version bumped). The subagent's own-worktree isolation intent
  is preserved (its own worktree, main checkout undisturbed).
- **Pre-handoff self-review (v2.3.0).** On the accept path, after committing and
  BEFORE `gh pr create`, the subagent runs a structured self-review against its
  OWN committed diff (it reads the actual diff, not its intent) and fixes any gap
  before opening the PR. The checklist has four lenses: **completeness** (did it
  do exactly what the issue asked — nothing more, nothing less), **quality**
  (follows existing patterns; no overbuild / YAGNI), **discipline** (only
  in-scope changes; separate problems go to `discovered_work[]`, not the diff),
  and **checks** (the project's tests/build pass on the committed change). The
  self-review is a self-check, not a licence to expand scope: an out-of-scope fix
  is surfaced as `discovered_work[]`, and an order that cannot be done cleanly
  within scope yields `status: blocked` with no open PR. (Adopted from Claude's
  superpowers `subagent-driven-development` implementer "Before reporting"
  checklist; the interactive "ask questions mid-task" behaviour is deliberately
  NOT adopted — the unattended loop's equivalent is `status: blocked`.)
- **Emits `concerns[]` on an opened handoff (v2.5.0,
  auto-maintainer-framework#212).** The subagent is the PRODUCER of the Handoff's
  `concerns[]` field (added to the schema in v1.1.0). On the accept path, after
  the self-review passes and the PR is opened, it populates `concerns[]` with
  residual doubts it could NOT resolve itself and wants the REVIEW gate / a human
  to look harder at on this very PR (a "done, with concerns" signal). A concern
  is distinct from a `discovered_work[]` item: `discovered_work[]` is a SEPARATE
  new problem to be filed as its own issue, whereas a concern is a doubt about
  THIS change (an unsure design tradeoff, a thinly-tested edge case, a guessed
  intent, an unverified interaction). It is never a substitute for `status:
  blocked` (a genuine blocker is a block, not a concern); when there are no
  doubts, `concerns[]` stays `[]`.
- **`discovered_work` is for NEW problems only (v2.4.0,
  auto-maintainer-framework#224).** The subagent must NOT emit a discovery for
  anything it already knows is tracked or open — in particular the dependencies
  it is blocked on (items cited in its own `blocked_reason`) or any issue named
  in its prompt. REPORT files `discovered_work` verbatim as new issues, so
  re-surfacing a known/open item creates duplicate tracker noise. This is the
  implementer-side complement to REPORT's dedup-vs-open guard
  (auto-maintainer-framework#224 fix 1).
- **PR provenance label (v2.1.0).** An opened PR is stamped with the
  `auto-maintainer` label (`gh pr create --label auto-maintainer`, creating the
  label if absent). This is the §3.7 hand-off seam to `verify-integrate`: VERIFY
  finds the loop's own open PRs by querying `gh pr list --label auto-maintainer
  --state open`. The label is the only coupling between IMPLEMENT and the
  VERIFY/INTEGRATE chain (no durable PR-ledger).
- **PR closes its source issue on merge (`Closes #<n>` in the body).** The
  accept-path `gh pr create` MUST include a `--body` that embeds the GitHub
  closing keyword `Closes #<source-issue-number>` (the issue the work order
  came from). This makes GitHub's
  native machinery **auto-close the source issue when — and only when — the PR
  merges**: in `propose` mode the PR is never merged so the issue correctly
  stays open; in `auto-merge` mode INTEGRATE's merge closes it. This closes the
  merged-issue lifecycle GAP (the loop otherwise NEVER closed a source issue on
  the accept→merge path). It ALSO populates the PR's `closingIssuesReferences`,
  which is the field the supersede-on-retry match (below) and
  `verify-integrate`'s orphaned-PR detection both query — so writing the closing
  keyword REPAIRS both of those, which were silent no-ops without it. The issue
  number comes from the `## Inputs` work order (the ROBUSTNESS fetch already
  recovers it when the envelope lacks it); if no source issue number is
  resolvable, the body omits the keyword rather than guessing. The
  `auto-maintainer` label and the `Closes #<n>` body are complementary: the
  label is the loop→VERIFY coupling, the keyword is the PR→issue coupling.
- **Isolation — the subagent manages its OWN worktree (v2.0.0,
  auto-maintainer-framework#143 follow-up).** It is dispatched WITHOUT the
  `isolation: "worktree"` adapter flag. That flag uses Claude Code's worktree
  isolation, which **sandboxes the subagent's file writes to the worktree** — so
  the subagent's Handoff file could not reach the shared main-workspace
  `dispatch-out/`, breaking the file-based handoff. Instead, the
  subagent (a coding agent with `Bash`) creates its OWN git worktree off the
  default branch OUTSIDE the repo tree (`git worktree add`), does all editing /
  committing there, opens the PR, then removes the worktree (DESIGN §3.6.2: the
  doer owns its workspace). Because the
  subagent is NOT Claude-sandboxed, it CAN write its Handoff to the absolute
  `output_path`. The main checkout's branch/index/uncommitted state is never
  disturbed; `git status` on the main tree stays clean.
- **Output.** Writes its Handoff to the (absolute) file named in the prompt's
  `## Handoff` section and replies with only a short ack — its output never
  passes through the orchestrator's context.
- **Governance is external.** The subagent always acts for real when dispatched;
  the trust-ladder gate, budget window, and acted-ledger idempotency
  (DESIGN §3.8.2/§3.8.4/§3.2.4) are enforced upstream by `scheduling.run_tick`,
  which only dispatches it under `propose`+ modes. The subagent itself carries no
  mode logic.
- **Frontmatter.** Lifecycle-compliant (`version`, `owner`,
  `deprecation_criterion`), `model: opus`, and a coding toolset that includes
  `Bash` and `Write`.

This ships the subagent *definition*; the run_tick governance that arms it
(S2.1a/S2.1b) already exists. Adapter-map wiring of the IMPLEMENT state to this
subagent and live verification of the propose path are exercised at packaging
and config time.

## Invariants

- Deterministic: no model, no wall-clock, no randomness, no network, no
  filesystem. Pure function of `execution_plan`.
- Inert / propose-nothing-to-the-world: never creates a branch, PR, commit, or
  tracker change. After a tick that runs it, `git status` is clean.
- No budget cap: processes every plan entry.
- Reads only `execution_plan` (NOT `workspace`); writes only `handoffs`.
- One handoff per ordered plan entry, in plan order.
- The shipped implementer subagent `ship/agents/auto-maintainer-implementer.md`
  exists with lifecycle-compliant frontmatter (`version`, `owner`,
  `deprecation_criterion`), `model: opus`, and a coding toolset that includes
  `Bash` and `Write`.

## Deferred (NOT in this slice)

- **The model-backed implement-then-PR doer's run-side adapter** (DESIGN
  §3.6.2/§3.6.3) — the dry-run adapter remains the wired reference; the
  subagent *definition* for the `propose` rung is now shipped (see "Shipped
  implementer subagent"), but selecting it as the IMPLEMENT adapter and reading
  `workspace` is exercised at adapter-map/config time, not here.
- **TDD implementer adapter** (DESIGN §3.6.4) — rabbit's path, optional bundle.
- **Trust-ladder mode selection + budget token ceiling** (DESIGN §3.8.2/§3.8.4)
  — governance that gates the doer; lives in safety-governance.
- **Durable filing of `discovered_work` via REPORT** (DESIGN §3.11.3) — the
  dry-run adapter discovers nothing, so there is nothing to file yet.
