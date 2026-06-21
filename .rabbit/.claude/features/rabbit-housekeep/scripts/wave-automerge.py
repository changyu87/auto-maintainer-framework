#!/usr/bin/env python3
"""wave-automerge.py — gated auto-merge decision for a housekeeping wave's PR.

For a USER-INSTALLED `/rabbit-housekeep` run (the manual skill invocation, NOT
the autonomous loop), a wave still CREATES its PR for the audit trail, but on
green gates it should not be left pending for the user to merge by hand
(issue #1191). This script owns the script-tier DECISION: given a wave's
HANDOFF gates, the PR's mergeable/CI state, and the honest-reduction verdict,
it emits `merge` only when ALL of the following hold — else `leave-open`:

  - HANDOFF gates: tdd_state == "test-green", test_result == "pass",
    spec_compliance == "pass" (the rabbit-feature-touch HANDOFF schema).
  - PR is mergeable/clean: mergeable == "MERGEABLE" and merge_state_status is
    a clean state (CLEAN / HAS_HOOKS / UNSTABLE-but-CI-green is excluded — see
    below) and CI checks are green (ci_status == "pass").
  - The honest-reduction outcome held: verdict in {"reduced", "no-op"} — a
    measured reduction OR an honest already-clean no-op (the #1190 honesty
    semantics). A "no-op" is a PASSING reduction outcome, never a failure.

The decision is deterministic and locatable to a source artifact (spec-rules
§1): a failed gate is NAMED in the `reasons` list, so a `leave-open` verdict is
auditable. Any failed gate leaves the PR OPEN for human attention, exactly as
today.

This script makes the DECISION only; the SKILL.md performs the resulting
`gh pr merge` action on a `merge` decision. The gating signals are passed in by
the caller (machine-first), so the decision is testable without shelling out to
`gh`. The `gather` subcommand is a convenience that collects the PR-side
signals (mergeable / merge state / CI) via `gh pr view` so the caller can merge
them with the HANDOFF gates before calling `decide`.

Subcommands:

  decide
    Read a JSON object on stdin with the gating signals and print a JSON
    decision object on stdout:
      {
        "pr": <int|null>,
        "decision": "merge" | "leave-open",
        "reasons": ["<gate>: <why>", ...]   # empty when decision == merge
      }
    Recognised signals (all optional; a missing/blank signal that a gate
    requires causes that gate to FAIL closed — auto-merge is opt-in to safety):
      pr, tdd_state, test_result, spec_compliance, verdict,
      mergeable, merge_state_status, ci_status

  gather --pr <N>
    Collect the PR-side signals via `gh pr view <N> --json mergeable,
    mergeStateStatus,statusCheckRollup` and print them as a JSON object
    ({"pr", "mergeable", "merge_state_status", "ci_status"}). The caller merges
    these with the HANDOFF gates + verdict and feeds the union to `decide`.

Exit:
  0 success (decision printed; `decide` always exits 0 when the payload parsed)
  2 invocation error (bad/empty JSON payload, missing --pr, bad subcommand)

Version: 0.1.0
Owner: rabbit-workflow team
Deprecation criterion: when housekeeping is provided natively by the rabbit CLI
    as a first-class measured-reduction subcommand that lands its own waves.
"""

from __future__ import annotations

import json
import subprocess
import sys

# A wave's honest-reduction outcome holds for either a measured reduction or an
# honest already-clean no-op (#1190): both are PASSING outcomes.
PASSING_VERDICTS = ("reduced", "no-op")

# GitHub mergeStateStatus values that mean the PR can land cleanly. BLOCKED,
# DIRTY, BEHIND, and DRAFT are excluded; an UNSTABLE state (a non-required
# check pending/failing) is NOT treated as clean here — the separate ci_status
# gate must independently be "pass", so auto-merge stays conservative.
CLEAN_MERGE_STATES = ("CLEAN", "HAS_HOOKS")


def _decide(payload):
    """Return the decision dict for a parsed gating payload."""
    reasons = []

    def gate(name, actual, expected, label):
        if actual != expected:
            reasons.append(
                f"{name}: {label} (got {actual!r}, need {expected!r})"
            )

    gate("tdd_state", payload.get("tdd_state"), "test-green",
         "HANDOFF gate not green")
    gate("test_result", payload.get("test_result"), "pass",
         "HANDOFF gate not green")
    gate("spec_compliance", payload.get("spec_compliance"), "pass",
         "HANDOFF gate not green")

    # honest-reduction outcome: reduced OR no-op both pass (#1190).
    verdict = payload.get("verdict")
    if verdict not in PASSING_VERDICTS:
        reasons.append(
            f"verdict: not a passing reduction outcome "
            f"(got {verdict!r}, need one of {list(PASSING_VERDICTS)})"
        )

    # PR mergeable / clean.
    mergeable = payload.get("mergeable")
    if mergeable != "MERGEABLE":
        reasons.append(
            f"mergeable: PR not mergeable (got {mergeable!r}, need 'MERGEABLE')"
        )
    merge_state = payload.get("merge_state_status")
    if merge_state not in CLEAN_MERGE_STATES:
        reasons.append(
            f"merge_state_status: PR not clean (got {merge_state!r}, "
            f"need one of {list(CLEAN_MERGE_STATES)})"
        )

    # CI checks green.
    ci_status = payload.get("ci_status")
    if ci_status != "pass":
        reasons.append(
            f"ci_status: CI checks not green (got {ci_status!r}, need 'pass')"
        )

    return {
        "pr": payload.get("pr"),
        "decision": "merge" if not reasons else "leave-open",
        "reasons": reasons,
    }


def cmd_decide():
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as e:
        sys.stderr.write(f"ERROR: could not parse gating payload as JSON: {e}\n")
        return 2
    if not isinstance(payload, dict):
        sys.stderr.write("ERROR: gating payload must be a JSON object\n")
        return 2
    print(json.dumps(_decide(payload), indent=2))
    return 0


def _ci_status_from_rollup(rollup):
    """Reduce a gh statusCheckRollup list to "pass" | "fail" | "pending".

    Empty rollup (no checks configured) is treated as "pass" — there is no CI
    to be red. Any FAILURE/ERROR/CANCELLED/TIMED_OUT/ACTION_REQUIRED check ->
    "fail"; any PENDING/QUEUED/IN_PROGRESS (and not yet failed) -> "pending";
    else "pass"."""
    if not rollup:
        return "pass"
    failed = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED"}
    pending = {"PENDING", "QUEUED", "IN_PROGRESS", "EXPECTED", "WAITING"}
    saw_pending = False
    for c in rollup:
        # Checks expose `conclusion` (+ `status`); statuses expose `state`.
        concl = (c.get("conclusion") or "").upper()
        state = (c.get("state") or "").upper()
        status = (c.get("status") or "").upper()
        if concl in failed or state in failed:
            return "fail"
        if status in pending or state in pending or (
            status == "COMPLETED" and concl == ""
        ):
            saw_pending = saw_pending or status in pending or state in pending
    return "pending" if saw_pending else "pass"


def cmd_gather(argv):
    pr = None
    i = 0
    while i < len(argv):
        if argv[i] == "--pr" and i + 1 < len(argv):
            pr = argv[i + 1]
            i += 2
        else:
            i += 1
    if not pr:
        sys.stderr.write("usage: wave-automerge.py gather --pr <N>\n")
        return 2
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--json",
         "mergeable,mergeStateStatus,statusCheckRollup"],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(f"ERROR: gh pr view failed: {proc.stderr}\n")
        return 2
    try:
        data = json.loads(proc.stdout)
    except ValueError as e:
        sys.stderr.write(f"ERROR: could not parse gh output: {e}\n")
        return 2
    out = {
        "pr": pr,
        "mergeable": data.get("mergeable"),
        "merge_state_status": data.get("mergeStateStatus"),
        "ci_status": _ci_status_from_rollup(data.get("statusCheckRollup")),
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage:\n"
            "  wave-automerge.py decide   (gating payload on stdin)\n"
            "  wave-automerge.py gather --pr <N>\n"
        )
        return 2
    sub = argv[0]
    if sub == "decide":
        return cmd_decide()
    if sub == "gather":
        return cmd_gather(argv[1:])
    sys.stderr.write(f"ERROR: unknown subcommand {sub!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
