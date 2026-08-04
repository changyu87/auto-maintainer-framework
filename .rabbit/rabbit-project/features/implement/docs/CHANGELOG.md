# Changelog — implement

All notable changes to this feature are recorded here. Versions follow the
spec/contract `version:` frontmatter. Owner: changyu87.

## 0.12.0 — 2026-08-03

`already_done` — a TERMINAL already-satisfied Handoff outcome.

- **Handoff schema 1.1.0 -> 1.2.0 (additive, backward-compatible).** Adds
  `already_done` to the `status` value space — a TERMINAL already-satisfied
  outcome the doer emits when the requested change is already present on `main`
  (a genuine no-op, nothing to implement). It is DISTINCT from the retryable
  `blocked`: `blocked` is a cannot-proceed signal scheduling re-dispatches,
  whereas `already_done` is terminal and lets scheduling record the item resolved
  so it is not re-dispatched. Evidence is carried in the existing `artifact` slot
  as `{kind: "already-on-main", ref: <commit-sha>}` — the commit on `main` that
  already carries the fix. An `already_done` handoff opens NO PR and, this wave,
  does NOT close the source issue (left OPEN). No existing field or status
  changed; a prior consumer that does not recognize `already_done` treats it as a
  non-`opened` handoff (which it is).
- **`validate_handoff` accepts `already_done` without a `test_verdict`.** Like
  `blocked`/`planned`/legacy `closed`, an `already_done` handoff opened no PR, so
  it is VALID without a verdict; only `opened` requires a passing
  script-produced verdict. No logic change was needed (the predicate already
  treats every non-`opened` status as verdict-free) — the docstring now names
  `already_done` explicitly.
- **Shipped implementer subagent v2.11.0 -> v2.12.0.** Instructs the doer to
  report `status: already_done` (NOT `blocked`) when it determines the requested
  change is already present on `main`, opening no PR and setting
  `artifact = {kind: already-on-main, ref: <commit-sha>}` (resolved via
  `git log`/`git blame`), leaving the source issue open. `blocked` stays for
  genuine cannot-proceed cases and `opened` for real work.
- **Housekeep doc baseline re-anchored** spec.md 386 -> 421 to absorb the
  load-bearing `already_done` content; `already_done` and `already-on-main` added
  to the load-bearing token survival set.

## 0.11.1 — 2026-08-03

Hooks-free worktree add (implementer's disposable worktree).

- **`src/open_pr.py` `setup_worktree` runs the worktree add HOOKS-FREE.** The
  `git worktree add` invocation now inserts `-c core.hooksPath=/dev/null`
  immediately after `git`
  (`git -c core.hooksPath=/dev/null worktree add <wt> -b <branch>
  origin/<default>`), so the TARGET repo's `post-checkout` hook (e.g.
  ssbdci-grimlock's `render_nested_components`) never fires in the implementer's
  disposable worktree — the throwaway tree is only for mechanical
  edit/commit/push and never needs the repo's checkout-render hooks. Mirrors
  verify-integrate's reconcile/GATE hooks-off fix.
- **Everything else unchanged.** Resolve default branch
  (`gh repo view --json defaultBranchRef`), `git fetch origin <default>`,
  worktree start-point ALWAYS `origin/<default>` (never local HEAD), and
  `gh pr create --base <default>` (explicit base) are untouched. The injectable
  runner is preserved.

## 0.11.0 — 2026-07-31

Script-backed worktree setup + explicit PR base (fixes wrong-base PR stacking).

- **New deterministic companion script `src/open_pr.py`.** The shipped
  implementer's order-critical git sequence — resolve the repo default branch
  (`gh repo view --json defaultBranchRef`), `git fetch origin <default>`,
  `git worktree add <wt> -b <branch> origin/<default>`, and
  `gh pr create --base <default>` — moved out of PROMPT-TIER agent prose into a
  self-contained, stdlib-only script with an INJECTABLE runner (spec-rules §4
  Script-Backed Orchestration). The worktree start-point is ALWAYS the
  freshly-fetched `origin/<default>` (never local HEAD), and the PR base is
  ALWAYS an explicit `--base <default>` (never inferred/tracked).
- **Fixes the wrong-base STACKED PR bug (#844/#846).** During a back-to-back
  drain burst the old prose let consecutive implementer runs branch off the
  PREVIOUS loop branch and open PRs based on a sibling loop branch (#844 based on
  #831's head, #846 on #844's), which INTEGRATE refused (never-merge-wrong-base)
  and nothing recovered. The script's unconditional origin/<default> start-point
  and explicit base make the base impossible to drift.
- **Agent `.md` bumped v2.10.0 -> v2.11.0.** The subagent now INVOKES the script
  at the deployed `${CLAUDE_PLUGIN_ROOT}/lib/open_pr.py` path (mirroring
  `test_gate.py`) via a `setup` and a `create` subcommand, in place of the raw
  git/gh base prose. Own-worktree isolation and every other step (test-gate,
  self-review, supersede-on-retry, build-tree regen, `Closes #<n>`) are
  preserved.
- **contract.md 0.7.0 -> 0.8.0** adds `open_pr.py` to `provides.scripts` and
  `invokes`. Housekeep doc baseline re-anchored (spec.md 355 -> 378, contract.md
  35 -> 36).
- **Follow-on (out of this feature's scope):** packaging-config's `build_plugin`
  `_LIBS` must register `lib/open_pr.py` (mirroring `test_gate.py`) so the script
  deploys to the installed plugin — a separate packaging release.

## 0.7.0 — 2026-07-09

In-PR regeneration of a committed build tree (auto-maintainer-framework#354).

- **Shipped implementer regenerates a committed build tree in-PR.** When a repo
  checks a built distribution tree (a plugin/package tree assembled from source)
  into version control under a build-drift guard, the shipped implementer
  subagent (`ship/agents/auto-maintainer-implementer.md`, now v2.7.0) — on the
  accept path, after committing the code change and BEFORE the self-review —
  determines whether its edits touched source mirrored into that committed tree
  and, if so, runs the repo's build step and commits the regenerated tree in the
  SAME PR. This keeps a shipped-source change drift-free in one PR (green under
  the build-drift guard) instead of merging with drift and forcing a second,
  regen-only PR (two PRs for one logical change).
- **Non-mirrored changes do not regenerate.** A change touching only source not
  mirrored into a committed build tree (docs/tests, or a repo with no committed
  build tree) does NOT trigger a regen, avoiding pointless churn.
- **No dev-path leak.** The regen instruction is phrased generically (a repo's
  build step / committed distribution tree); the shipped body still references
  NEITHER `rabbit-project` NOR `.rabbit`.
- **Spec.** `docs/spec.md` documents the in-PR build-tree regeneration under
  "Shipped implementer subagent".

## 0.6.1 — 2026-06-23

Deployment-correctness fix for the shipped implementer subagent's gate
invocation.

- **Gate invoked via the deployed plugin path.** The shipped implementer
  subagent (`ship/agents/auto-maintainer-implementer.md`) invoked the
  deterministic test-gate via a DEV source-tree path
  (`python3 <repo>/rabbit-project/features/implement/src/test_gate.py ...`),
  which is non-functional in the installed Claude Code plugin and leaked the
  dev layout. The accept-path example now uses the deployed convention
  `python3 "${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py" <feature-dir> --verdict-out
  <verdict-path>`, mirroring scheduling's start/adapter-map shipped skills.
- **No source-tree leak in the shipped body.** The shipped subagent body now
  references NEITHER `rabbit-project` NOR `.rabbit`; e2e tests assert the
  deployed invocation path and the absence of either substring.
- **Accept-path semantics unchanged.** The gate still runs the touched
  target's `run.py`, records the `test_verdict`, and `status: opened` still
  requires a passing SCRIPT-produced verdict. Only the invocation path changed.
- **Spec.** `docs/spec.md` documents that the gate ships to the plugin lib and
  is invoked at `${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py`.

## 0.6.0

- FT-A (DESIGN §3.6.3): IMPLEMENT is the deterministic correctness gate. Ships
  `src/test_gate.py` (a self-contained script with no rabbit-framework runtime
  dependency) that runs a target feature's `test/run.py` via subprocess and
  records a machine-checkable verdict `{feature, passed, returncode, summary}`.
  Adds `validate_handoff()` to `implement.py`: an opened handoff is valid only
  with a passing SCRIPT-produced `test_verdict`; a missing/failing verdict is
  invalid.
