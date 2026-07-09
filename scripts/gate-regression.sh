#!/usr/bin/env bash
# gate-regression.sh — the auto-maintainer SELF-repo regression command.
#
# Runs every rabbit-project feature's self-contained test/run.py and exits
# nonzero if any fails. It is the regression the GATE state (verify-integrate,
# DESIGN §2.2 [v2]) runs against each REVIEW-passed PR in its disposable
# integration worktree; wire it by setting, in the project-local
# ${CLAUDE_PROJECT_DIR}/.auto-maintainer/config.json:
#
#     "regression_command": "bash scripts/gate-regression.sh"
#
# It runs from the repo root (the GATE integration worktree). The loop's
# environment must have the test deps (python3 + pyyaml). A generic maintained
# project sets its OWN regression_command (pytest / npm test / …) instead.
#
# Owner: rabbit-workflow team
# Deprecation criterion: superseded if the feature test runners are replaced by
#   a single project-level test entrypoint.
set -u
rc=0
for d in .rabbit/rabbit-project/features/*/; do
  if [ -f "${d}test/run.py" ]; then
    ( cd "$d" && python3 test/run.py ) || { echo "FAILED: ${d}"; rc=1; }
  fi
done
exit "$rc"
