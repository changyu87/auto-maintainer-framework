#!/usr/bin/env python3
"""build-prompt.py — assemble a complete prompt file from a registered
prompts entry (per spec Inv 46).

Walks every <repo>/.claude/features/*/feature.json looking for a `prompts`
entry whose `id` matches --callable-id, then assembles:

    <policy block from render_policy_block(entry.inject)>
    <blank line>
    <slot-substituted body of templates/prompts/<id>.txt>

and writes it to <runtime_root>/prompts/<id>-<pid>-<ts>.txt where <ts> is
YYYYMMDD-HHMMSS-ms and <runtime_root> is the canonical single-`.rabbit`
runtime root resolved by rabbit-cage's rabbit_runtime_root (Inv 52). Prints
the absolute path of the written file to stdout.

Usage:
  build-prompt.py --callable-id <id> [--slot name=value ...]

Repo root is resolved from:
  1. $RABBIT_ROOT environment variable (test harness override), or
  2. `git rev-parse --show-toplevel` rooted at this script's directory.

The prompts dir anchors at rabbit_runtime_root(repo_root): in a vendored
install repo_root already IS the `.rabbit` dir, so an unconditional
`<repo_root>/.rabbit/prompts` join doubled the segment to
`<host>/.rabbit/.rabbit/prompts`; rabbit_runtime_root collapses that to one.

Exit:
  0 success
  1 read error / missing slot / orphan placeholder / template missing
  2 unknown --callable-id / invocation error (bad args, no repo root)

Version: 1.1.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when prompt-contract assembly is native to Claude Code.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time

# Make contract.lib.policy_block importable.
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..")))

from lib.policy_block import render_policy_block  # noqa: E402


_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


def get_repo_root():
    env_root = os.environ.get("RABBIT_ROOT")
    if env_root:
        return env_root
    script_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "-C", script_dir, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def rabbit_runtime_root(repo_root):
    """Resolve the canonical single-`.rabbit` runtime root for `repo_root` via
    rabbit-cage's `rabbit_runtime_root` resolver (Inv 52), lazy-imported from the
    install's feature lib using the same importlib.util pattern rabbit-cage's
    session-start dispatcher and other contract scripts use.

    Falls back to the inline basename rule when the resolver cannot be imported
    (degenerate / partial install) so the prompts dir still lands on a single-
    `.rabbit` path.
    """
    resolver_path = os.path.join(
        repo_root, ".claude", "features", "rabbit-cage",
        "lib", "runtime_root.py")
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rabbit_cage_runtime_root", resolver_path)
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.rabbit_runtime_root(str(repo_root))
    except (FileNotFoundError, ImportError, AttributeError, OSError):
        pass
    rp = os.path.normpath(str(repo_root))
    return rp if os.path.basename(rp) == ".rabbit" else os.path.join(rp, ".rabbit")


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="build-prompt.py",
        description="Assemble a registered prompts entry into a complete prompt file.",
    )
    p.add_argument("--callable-id", required=True,
                   help="The id of the prompts entry to assemble.")
    p.add_argument("--slot", action="append", default=[],
                   metavar="NAME=VALUE",
                   help="Slot value substitution. May be repeated.")
    return p.parse_args(argv)


def parse_slots(slot_args):
    """Parse --slot NAME=VALUE pairs into a dict. Returns (dict, error_msg or None)."""
    out = {}
    for raw in slot_args:
        if "=" not in raw:
            return None, f"--slot must be NAME=VALUE, got {raw!r}"
        name, _, value = raw.partition("=")
        if not name:
            return None, f"--slot name is empty in {raw!r}"
        out[name] = value
    return out, None


def find_entry(repo_root, callable_id):
    """Walk .claude/features/*/feature.json and find the prompts entry with the
    matching id. Returns (entry, feature_name) or (None, None) if not found."""
    features_root = os.path.join(repo_root, ".claude", "features")
    if not os.path.isdir(features_root):
        return None, None
    for entry_name in sorted(os.listdir(features_root)):
        fjson = os.path.join(features_root, entry_name, "feature.json")
        if not os.path.isfile(fjson):
            continue
        try:
            with open(fjson) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        prompts = data.get("prompts")
        if not isinstance(prompts, list):
            continue
        for entry in prompts:
            if isinstance(entry, dict) and entry.get("id") == callable_id:
                return entry, entry_name
    return None, None


def substitute_slots(body, slots):
    """Replace each {{name}} with slots[name] (single pass per slot)."""
    for name, value in slots.items():
        body = body.replace("{{" + name + "}}", value)
    return body


def main():
    args = parse_args(sys.argv[1:])

    slots, err = parse_slots(args.slot)
    if err is not None:
        print(f"ERROR: {err}", file=sys.stderr)
        sys.exit(2)

    repo_root = get_repo_root()
    if not repo_root:
        print("ERROR: cannot determine repo root", file=sys.stderr)
        sys.exit(2)

    entry, feature_name = find_entry(repo_root, args.callable_id)
    if entry is None:
        print(f"ERROR: no prompts entry with id {args.callable_id!r} "
              f"found in any .claude/features/*/feature.json", file=sys.stderr)
        sys.exit(2)

    declared_slots = entry.get("slots", []) or []
    missing_slots = [s for s in declared_slots if s not in slots]
    if missing_slots:
        print(f"ERROR: missing required slots for {args.callable_id!r}: "
              f"{missing_slots}", file=sys.stderr)
        sys.exit(1)

    # Resolve inject paths.
    inject_paths = []
    for rel in entry.get("inject", []) or []:
        if not isinstance(rel, str):
            print(f"ERROR: inject path is not a string: {rel!r}", file=sys.stderr)
            sys.exit(1)
        full = os.path.join(repo_root, rel)
        if not os.path.isfile(full):
            print(f"ERROR: inject path does not exist: {full}", file=sys.stderr)
            sys.exit(1)
        inject_paths.append(full)

    # Render the policy block.
    try:
        policy_block = render_policy_block(inject_paths)
    except FileNotFoundError as e:
        print(f"ERROR: inject file missing: {e}", file=sys.stderr)
        sys.exit(1)

    # Resolve and read the convention-resolved template.
    tpl_path = os.path.join(
        repo_root, ".claude", "features", "contract",
        "templates", "prompts", f"{args.callable_id}.txt",
    )
    if not os.path.isfile(tpl_path):
        print(f"ERROR: missing template for id {args.callable_id!r} at "
              f"{tpl_path}", file=sys.stderr)
        sys.exit(1)
    try:
        with open(tpl_path) as f:
            body = f.read()
    except OSError as e:
        print(f"ERROR: cannot read template {tpl_path}: {e}", file=sys.stderr)
        sys.exit(1)

    # Scan for orphan {{...}} placeholders in the RAW template body, BEFORE
    # slot substitution. A placeholder is "orphan" when its name is NOT in
    # the entry's declared slots list. Scanning before substitution avoids
    # false positives from slot values that legitimately contain
    # double-brace text (slot values are user data and MAY contain such
    # patterns — e.g. spec prose describing the placeholder syntax).
    declared_slot_names = set(declared_slots)
    all_found = set(_PLACEHOLDER_RE.findall(body))
    orphans = sorted(all_found - declared_slot_names)
    if orphans:
        print(f"ERROR: orphan placeholder(s) in template: {orphans}",
              file=sys.stderr)
        sys.exit(1)

    body = substitute_slots(body, slots)

    assembled = policy_block + "\n\n" + body

    # Write to <runtime_root>/prompts/<id>-<pid>-<ts>.txt, anchored at the
    # canonical single-`.rabbit` runtime root (Inv 52). In a vendored install
    # repo_root IS the `.rabbit` dir, so an unconditional `<repo_root>/.rabbit`
    # join doubled the segment (#1073); rabbit_runtime_root collapses that.
    out_dir = os.path.join(rabbit_runtime_root(repo_root), "prompts")
    os.makedirs(out_dir, exist_ok=True)
    now = time.time()
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) \
        + f"-{int((now % 1) * 1000):03d}"
    out_name = f"{args.callable_id}-{os.getpid()}-{ts}.txt"
    out_path = os.path.join(out_dir, out_name)
    with open(out_path, "w") as f:
        f.write(assembled)

    print(out_path)
    sys.exit(0)


if __name__ == "__main__":
    main()
