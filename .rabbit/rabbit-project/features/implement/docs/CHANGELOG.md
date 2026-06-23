# Changelog — implement

All notable changes to this feature are recorded here. Versions follow the
spec/contract `version:` frontmatter. Owner: changyu87.

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
