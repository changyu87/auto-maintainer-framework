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

Self-contained: it has NO rabbit-framework runtime dependency and NO sibling
feature import (stdlib-only: json/os/subprocess/sys/argparse), so it ships
byte-for-byte. It resolves a CONFIGURABLE test command from the
`implement_test_command` key, read by a direct, tolerant stdlib json.load of
${project_dir}/.auto-maintainer/config.json — the key is owned by
safety-governance's schema, but the gate is a contract-bound READER of that one
key, never an importer of safety-governance.

Three-way resolution:
  - null / absent (default) — run the target feature's <feature-dir>/test/run.py
    via [sys.executable, run.py]; a missing run.py is a FAILED verdict.
  - a shell command string — run THAT command (shell=True, cwd = the feature
    dir); exit 0 = pass.
  - the sentinel "none" / "skip" (case-insensitive) — SKIP the gate: write a
    passed=True, returncode=0 no-op verdict and exit 0.
A --test-command CLI arg overrides the config value; --project-dir selects the
config location (else $CLAUDE_PROJECT_DIR / cwd). Resolution is tolerant — a
missing/unreadable config or absent key falls back to the run.py default and
never crashes.

Usage:
  test_gate.py <feature-dir> --verdict-out <path>
                [--test-command <cmd>] [--project-dir <dir>]

Verdict artifact (JSON):
  {"feature": "<name>", "passed": <bool>, "returncode": <int>,
   "summary": "<final non-empty line of output, or a diagnostic>"}

Exit:
  0 when the target suite passed (or the gate was skipped); nonzero otherwise
  (mirrors the gate verdict so a caller can branch deterministically without
  re-parsing the artifact).

Version: 0.2.0
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


_SKIP_SENTINELS = frozenset({"none", "skip"})


def _resolve_test_command(project_dir, override):
    """Resolve the configured test command. Tolerant and stdlib-only.

    `override` (from --test-command) wins when not None. Otherwise read the
    `implement_test_command` key from ${project_dir}/.auto-maintainer/config.json
    via a direct json.load — a missing file, unreadable/malformed JSON, or an
    absent key all fall back to None (the run.py default). Never crashes; never
    imports safety-governance."""
    if override is not None:
        return override
    config_path = os.path.join(project_dir, ".auto-maintainer", "config.json")
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        return cfg.get("implement_test_command")
    except Exception:
        return None


def run_gate(feature_dir, verdict_out, test_command=None, project_dir=None):
    """Resolve the configured test command and record a verdict to `verdict_out`.

    Three-way (see module docstring): null/absent -> run.py default; a command
    string -> run it (shell, cwd = feature dir); 'none'/'skip' -> skip. A missing
    run.py (default mode) or a nonzero exit is recorded as passed=False — never a
    silent pass."""
    feature = os.path.basename(os.path.normpath(feature_dir))
    if project_dir is None:
        project_dir = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()

    command = _resolve_test_command(project_dir, test_command)

    if command is None:
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
    elif str(command).strip().lower() in _SKIP_SENTINELS:
        verdict = {
            "feature": feature,
            "passed": True,
            "returncode": 0,
            "summary": "implement test-gate skipped (implement_test_command=none)",
        }
    else:
        proc = subprocess.run(
            command, shell=True, capture_output=True, text=True, cwd=feature_dir)
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
    parser.add_argument("--test-command", default=None,
                        help="override the configured test command (shell string)")
    parser.add_argument("--project-dir", default=None,
                        help="dir containing .auto-maintainer/config.json "
                             "(else $CLAUDE_PROJECT_DIR / cwd)")
    args = parser.parse_args(argv)

    verdict = run_gate(args.feature_dir, args.verdict_out,
                       test_command=args.test_command,
                       project_dir=args.project_dir)
    return 0 if verdict["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
