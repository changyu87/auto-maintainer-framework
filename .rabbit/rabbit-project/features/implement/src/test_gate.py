#!/usr/bin/env python3
"""test_gate.py — the IMPLEMENT deterministic correctness gate (DESIGN §3.6.3).

IMPLEMENT is the loop's deterministic correctness gate. The model-backed
implementer must not be merely *instructed* to run the target's tests and then
*assert* a pass in its Handoff — a model "I ran it, it passed" claim is
untrustworthy (the #255 REVIEW rubber-stamp lesson). This script makes the gate
deterministic: it runs the TARGET feature's `test/run.py` via subprocess and
records a machine-checkable verdict to a known artifact path. The recorded
verdict — not the model's prose — is the source of truth for whether a PR may be
opened.

Self-contained: it has NO rabbit-framework runtime dependency (it does not
import or invoke rabbit's tdd-step.py or any sibling feature). It only shells out
to the target's own run.py.

Usage:
  test_gate.py <feature-dir> --verdict-out <path>

Verdict artifact (JSON):
  {"feature": "<name>", "passed": <bool>, "returncode": <int>,
   "summary": "<final non-empty line of run.py output, or a diagnostic>"}

Exit:
  0 when the target suite passed; nonzero otherwise (mirrors the gate verdict so
  a caller can branch deterministically without re-parsing the artifact).

Version: 0.1.0
Owner: changyu87
Deprecation criterion: Superseded when the model-backed implement-then-PR doer
  (DESIGN §3.6.2/§3.6.3) replaces the dry-run reference adapter, or when the
  Handoff/verdict schema reaches a breaking major version. See docs/spec.md.
"""

import argparse
import json
import os
import subprocess
import sys


def _summary_line(text):
    """The final non-empty line of the run.py output — the conventional
    `N passed, M failed` summary line the feature runners emit. Deterministic:
    pure function of the captured text."""
    for line in reversed(text.splitlines()):
        if line.strip():
            return line.strip()
    return ""


def run_gate(feature_dir, verdict_out):
    """Run the target feature's test/run.py via subprocess and write a verdict to
    `verdict_out`. Returns the verdict dict. A missing run.py or a nonzero exit
    is recorded as passed=False — never a silent pass."""
    feature = os.path.basename(os.path.normpath(feature_dir))
    run_py = os.path.join(feature_dir, "test", "run.py")

    if not os.path.isfile(run_py):
        verdict = {
            "feature": feature,
            "passed": False,
            "returncode": 1,
            "summary": "no test/run.py found for target feature",
        }
    else:
        proc = subprocess.run(
            [sys.executable, run_py],
            capture_output=True, text=True, cwd=feature_dir)
        verdict = {
            "feature": feature,
            "passed": proc.returncode == 0,
            "returncode": proc.returncode,
            "summary": _summary_line(proc.stdout) or _summary_line(proc.stderr),
        }

    with open(verdict_out, "w") as f:
        json.dump(verdict, f, sort_keys=True)
    return verdict


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("feature_dir", help="path to the target feature dir")
    parser.add_argument("--verdict-out", required=True,
                        help="path to write the machine-checkable verdict JSON")
    args = parser.parse_args(argv)

    verdict = run_gate(args.feature_dir, args.verdict_out)
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
