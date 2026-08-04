#!/usr/bin/env python3
"""scaffold-batch.py — companion script for the rabbit-feature-scaffold skill.

Owns the skill's batch + single delegation surface (spec-rules.md §4
Script-Backed Orchestration). The skill is the user-facing scaffold primitive
for adding one feature OR a batch of features in any mode; callers
(rabbit-decompose included) invoke the skill — which runs this script — rather
than shelling out to scaffold-feature.py --batch directly. This is the declared
skill-level interface for both modes; it does not re-implement scaffolding, it
delegates to the rabbit-feature scaffolder.

Forms (all delegate to scaffold-feature.py):

  scaffold-batch.py --batch <features.json>
      Pass a JSON array file straight through to scaffold-feature.py --batch.
      Each array entry is an object {"name": ..., "globs": [...]} (globs
      OPTIONAL — empty/absent ⇒ greenfield feature). Plugin mode only.

  scaffold-batch.py --list "<name> [glob ...]; <name> [glob ...]; ..."
      Inline batch: ';'-separated entries, each a whitespace-separated
      "name [glob ...]". Normalized into the JSON array shape and handed to
      scaffold-feature.py --batch. Plugin mode only.

  scaffold-batch.py <name> [glob ...]
      Single mode — delegates byte-for-byte to scaffold-feature.py's existing
      single-feature surface (plugin: `<name> [glob...]`). The single-feature
      invocation surface and all existing behaviour are preserved.

Exit codes mirror scaffold-feature.py:
  0 success
  1 scaffolding / validation failure
  2 invocation error

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: when feature scaffolding is exposed as a native rabbit
    CLI subcommand that owns both single and batch modes.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path


def _usage(stream=sys.stderr) -> None:
    stream.write(
        "usage: scaffold-batch.py --batch <features.json>\n"
        "       scaffold-batch.py --list \"<name> [glob ...]; ...\"\n"
        "       scaffold-batch.py <name> [glob ...]   (single mode)\n"
    )


def _resolve_scaffolder() -> Path | None:
    """Locate scaffold-feature.py under rabbit-feature/scripts/. This script
    lives at skills/rabbit-feature-scaffold/scripts/scaffold-batch.py, so the
    rabbit-feature root is parents[3] (scripts → rabbit-feature-scaffold →
    skills → rabbit-feature)."""
    here = Path(__file__).resolve()
    candidate = here.parents[3] / "scripts" / "scaffold-feature.py"
    if candidate.is_file():
        return candidate
    return None


def _delegate(scaffolder: Path, args: list[str]) -> int:
    proc = subprocess.run([sys.executable, str(scaffolder), *args])
    return proc.returncode


def _parse_list(spec: str) -> tuple[list[dict] | None, str | None]:
    """Parse a ';'-separated inline batch spec into the JSON array shape.

    Each entry is "name [glob ...]" (whitespace-separated). Returns
    (entries, None) on success or (None, error) on a malformed entry.
    """
    entries: list[dict] = []
    for raw in spec.split(";"):
        chunk = raw.strip()
        if not chunk:
            continue
        parts = chunk.split()
        name = parts[0]
        globs = parts[1:]
        entries.append({"name": name, "globs": globs})
    if not entries:
        return None, "no entries parsed from --list spec"
    return entries, None


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        _usage(sys.stdout if argv and argv[0] in ("-h", "--help") else sys.stderr)
        return 0 if argv and argv[0] in ("-h", "--help") else 2

    scaffolder = _resolve_scaffolder()
    if scaffolder is None:
        sys.stderr.write("ERROR: scaffold-feature.py not found\n")
        return 2

    if argv[0] == "--batch":
        if len(argv) != 2:
            sys.stderr.write(
                "ERROR: --batch requires exactly one argument (path to JSON file)\n"
            )
            return 2
        return _delegate(scaffolder, ["--batch", argv[1]])

    if argv[0] == "--list":
        if len(argv) != 2:
            sys.stderr.write(
                "ERROR: --list requires exactly one quoted argument\n"
            )
            return 2
        entries, err = _parse_list(argv[1])
        if err:
            sys.stderr.write(f"ERROR: {err}\n")
            return 2
        # Normalize to the JSON array shape scaffold-feature.py --batch expects.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="scaffold-batch-", delete=False
        ) as fh:
            json.dump(entries, fh)
            batch_path = fh.name
        try:
            return _delegate(scaffolder, ["--batch", batch_path])
        finally:
            try:
                Path(batch_path).unlink()
            except OSError:
                pass

    # Single mode: pass <name> [glob ...] straight through to the existing
    # single-feature surface. Existing behaviour preserved byte-for-byte.
    return _delegate(scaffolder, argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
