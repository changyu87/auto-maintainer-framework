# Changelog — implement

All notable changes to this feature are recorded here. Versions follow the
spec/contract `version:` frontmatter. Owner: changyu87.

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
