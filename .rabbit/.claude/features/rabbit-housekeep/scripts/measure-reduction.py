#!/usr/bin/env python3
"""measure-reduction.py — deterministic per-artifact line accounting and
before/after reduction diff for a housekeeping wave.

Housekeeping value is MEASURED, never asserted by judgment. A reword pass
that claims "slimmed" while line counts are unchanged is a failure, not a
success. This script makes the before/after delta a deterministic,
script-tier number so a housekeeping test can assert ACTUAL reduction.

Two subcommands:

  count [--docs-only] <path> [<path> ...]
    Print a JSON object mapping each path to its line count, plus a
    `__total__` key. A directory argument is walked recursively; only text
    files are counted (binary files are skipped). The output is the
    machine-first snapshot a wave records BEFORE it edits.

    With `--docs-only`, a directory argument is restricted to the DOC
    SURFACES a reduction wave actually slims — `docs/spec.md`,
    `docs/contract.md`, and each `skills/*/SKILL.md` — instead of the whole
    feature tree. This keeps the Step-7 `reduced` verdict scoped to the
    surfaces the wave targets, so the mandated housekeeping test the wave
    adds under `test/` (wave overhead, not bloat) does not flip the verdict
    to `reduced: false`. `docs/CHANGELOG.md` is excluded by design: a wave
    GROWS it. A file argument is counted as-is regardless of the flag.

  diff <before.json> <after.json>
    Read two `count` snapshots and print a JSON object describing the
    per-artifact and total reduction:
      {
        "per_artifact": {"<path>": {"before": N, "after": M, "delta": M-N}},
        "total_before": ..., "total_after": ..., "total_delta": ...,
        "reduced": <bool>, "removed_paths": [...], "added_paths": [...]
      }
    `total_delta` is after - before (negative means lines were removed).
    `reduced` is True iff total_delta < 0 (a real, measured reduction).
    Exit 0 always for diff; the reduction VERDICT is the `reduced` field,
    which the caller (a housekeeping test) asserts on — the script reports,
    it does not gate.

Exit:
  0 success
  2 invocation error (bad args, unreadable snapshot)

Version: 0.2.0
Owner: rabbit-workflow team
Deprecation criterion: when line-accounting is provided natively by the
    rabbit CLI as a housekeeping subcommand.
"""

from __future__ import annotations

import json
import os
import sys


def _is_probably_text(path: str) -> bool:
    """Heuristic: a file is text if its first 4 KiB contain no NUL byte and
    decodes as UTF-8. Binary artifacts are excluded from line accounting."""
    try:
        with open(path, "rb") as f:
            chunk = f.read(4096)
    except OSError:
        return False
    if b"\x00" in chunk:
        return False
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _count_lines(path: str) -> int:
    """Count logical lines in a text file. Splits on newline and drops the
    trailing empty fragment a final newline produces, so a 3-line file with
    a trailing newline counts as 3 (not 4)."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if text == "":
        return 0
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines = lines[:-1]
    return len(lines)


def _iter_doc_surfaces(feature_dir):
    """Yield the doc surfaces a reduction wave slims under a feature directory:
    `docs/spec.md`, `docs/contract.md`, and each `skills/*/SKILL.md`. Missing
    surfaces are skipped (a feature without a contract still counts its spec).
    `docs/CHANGELOG.md` is intentionally NOT a doc surface — a wave grows it."""
    for rel in (os.path.join("docs", "spec.md"),
                os.path.join("docs", "contract.md")):
        fp = os.path.join(feature_dir, rel)
        if os.path.isfile(fp) and _is_probably_text(fp):
            yield fp
    skills_dir = os.path.join(feature_dir, "skills")
    if os.path.isdir(skills_dir):
        for name in sorted(os.listdir(skills_dir)):
            fp = os.path.join(skills_dir, name, "SKILL.md")
            if os.path.isfile(fp) and _is_probably_text(fp):
                yield fp


def _iter_files(paths, docs_only=False):
    """Yield every text file reachable from the given paths. Directories are
    walked recursively (sorted for determinism); files are yielded directly.
    Non-text files are skipped.

    With docs_only, a directory argument is treated as a feature directory and
    restricted to its doc surfaces; a file argument is yielded as-is."""
    for p in paths:
        if os.path.isdir(p):
            if docs_only:
                yield from _iter_doc_surfaces(p)
                continue
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    fp = os.path.join(root, name)
                    if _is_probably_text(fp):
                        yield fp
        elif os.path.isfile(p):
            if _is_probably_text(p):
                yield p
        else:
            sys.stderr.write(f"ERROR: not a file or directory: {p}\n")
            raise SystemExit(2)


def cmd_count(argv):
    docs_only = False
    paths = []
    for a in argv:
        if a == "--docs-only":
            docs_only = True
        else:
            paths.append(a)
    if not paths:
        sys.stderr.write(
            "usage: measure-reduction.py count [--docs-only] <path> [<path> ...]\n"
        )
        return 2
    result = {}
    total = 0
    for fp in _iter_files(paths, docs_only=docs_only):
        n = _count_lines(fp)
        result[os.path.normpath(fp)] = n
        total += n
    out = dict(sorted(result.items()))
    out["__total__"] = total
    print(json.dumps(out, indent=2))
    return 0


def _load_snapshot(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        sys.stderr.write(f"ERROR: cannot read snapshot {path}: {e}\n")
        raise SystemExit(2)
    if not isinstance(data, dict):
        sys.stderr.write(f"ERROR: snapshot {path} is not a JSON object\n")
        raise SystemExit(2)
    return {k: v for k, v in data.items() if k != "__total__"}


def cmd_diff(before_path, after_path):
    before = _load_snapshot(before_path)
    after = _load_snapshot(after_path)

    all_keys = sorted(set(before) | set(after))
    per_artifact = {}
    for k in all_keys:
        b = int(before.get(k, 0))
        a = int(after.get(k, 0))
        per_artifact[k] = {"before": b, "after": a, "delta": a - b}

    total_before = sum(int(v) for v in before.values())
    total_after = sum(int(v) for v in after.values())
    total_delta = total_after - total_before

    removed_paths = sorted(set(before) - set(after))
    added_paths = sorted(set(after) - set(before))

    out = {
        "per_artifact": per_artifact,
        "total_before": total_before,
        "total_after": total_after,
        "total_delta": total_delta,
        "reduced": total_delta < 0,
        "removed_paths": removed_paths,
        "added_paths": added_paths,
    }
    print(json.dumps(out, indent=2))
    return 0


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage:\n"
            "  measure-reduction.py count [--docs-only] <path> [<path> ...]\n"
            "  measure-reduction.py diff <before.json> <after.json>\n"
        )
        return 2
    sub = argv[0]
    rest = argv[1:]
    if sub == "count":
        return cmd_count(rest)
    if sub == "diff":
        if len(rest) != 2:
            sys.stderr.write(
                "usage: measure-reduction.py diff <before.json> <after.json>\n"
            )
            return 2
        return cmd_diff(rest[0], rest[1])
    sys.stderr.write(f"ERROR: unknown subcommand {sub!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
