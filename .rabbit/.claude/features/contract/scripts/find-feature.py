#!/usr/bin/env python3
"""find-feature.py — distributed feature registry lookup.

Usage:
  python3 find-feature.py <repo-root> list
  python3 find-feature.py <repo-root> list-json
  python3 find-feature.py <repo-root> lookup <feature-name>

Scope: dual-detects plugin mode per Inv 20 (amended). The `<repo>`
argument MAY be EITHER the host-project root OR the rabbit install root
(`RABBIT_ROOT` — the `.rabbit/` install dir); the script resolves the
canonical `rabbit_root` from whichever was supplied.

  Plugin mode detection — FIXED precedence + validation per Inv 20(a).
  The vendored-mode marker value is dual-accepted as EITHER "vendored"
  (canonical) OR "plugin" (legacy), so detection stays stable across the
  value rename (deprecation criterion: drop "plugin" once no live install
  carries the legacy value):
    (i)  <repo>/.runtime/mode in {vendored,plugin}         → candidate rabbit_root=<repo>
    (ii) <repo>/.rabbit/.runtime/mode in {vendored,plugin} → candidate rabbit_root=<repo>/.rabbit
    A candidate is accepted only when <rabbit_root>/.claude/ exists as a
    directory; otherwise fall through. This rejects a rogue
    <rabbit_root>/.rabbit/.runtime/mode file (created when a skill wrote
    a relative .rabbit/* path with CWD=<rabbit_root>) so it cannot win
    over the canonical outer marker.

  Standalone scan (no marker matched):
    - <repo>/.claude/features/<name>/feature.json (alphabetical)

  Plugin scan (either detection path matched):
    - <rabbit_root>/.claude/features/<name>/feature.json   (alphabetical)
    - <rabbit_root>/rabbit-project/features/<name>/feature.json (alphabetical)

Directories elsewhere in the repo whose basename happens to be `features`
(project-side, vendor dirs, etc.) are NOT scanned — the no-masquerading
guarantee is preserved by enumerating only the canonical paths above.

Version: 1.5.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when feature discovery is handled natively by the dispatch infrastructure.
"""

import json
import os
import sys
import glob


def _load_json(path):
    """Open with context manager to ensure handle is closed."""
    with open(path) as f:
        return json.load(f)


# Vendored-install marker values accepted as vendored mode (Inv 20).
# "vendored" is the canonical value; "plugin" is the legacy value retained
# for coexistence. Deprecation criterion: drop "plugin" once no live install
# carries the legacy marker value.
_VENDORED_MODE_VALUES = frozenset({"vendored", "plugin"})


def _detect_plugin_rabbit_root(repo):
    """Return rabbit_root if plugin mode is detected AND validated, else None.

    Inv 20(a): fixed precedence (RABBIT_ROOT-as-repo first), each candidate
    validated by requiring <rabbit_root>/.claude/ to exist as a directory
    before accepting. The vendored-mode marker value is dual-accepted as
    either "vendored" (canonical) or "plugin" (legacy).

    Rogue-inner-marker guard: a rogue inner <repo>/.rabbit/.runtime/mode
    file (e.g. created when a skill wrote a relative .rabbit/* path with CWD
    already set to <rabbit_root>) cannot win, because either:
      (i)  the outer <repo>/.runtime/mode is checked first (precedence), or
      (ii) the inner candidate <repo>/.rabbit lacks .claude/ (validation).
    """
    candidates = (
        # First: <repo>/.runtime/mode — repo IS rabbit_root (canonical
        # RABBIT_ROOT-as-repo case per Inv 39; most common caller pattern).
        (os.path.join(repo, '.runtime', 'mode'), repo),
        # Then: <repo>/.rabbit/.runtime/mode — repo is the host root.
        (os.path.join(repo, '.rabbit', '.runtime', 'mode'), os.path.join(repo, '.rabbit')),
    )
    for mode_file, candidate_root in candidates:
        try:
            with open(mode_file) as f:
                if f.read().strip() not in _VENDORED_MODE_VALUES:
                    continue
        except (OSError, IOError):
            continue
        # Validate: candidate must have .claude/ to be a real rabbit_root.
        if os.path.isdir(os.path.join(candidate_root, '.claude')):
            return candidate_root
        # else: fall through to next candidate.
    return None


def iter_feature_jsons(repo):
    """Yield feature.json paths from the canonical scan locations (Inv 20 amended).

    Standalone (no plugin marker): yields only
    `<repo>/.claude/features/<name>/feature.json` (alphabetical).

    Plugin (marker matched via either detection path): yields
    `<rabbit_root>/.claude/features/<name>/feature.json` first then
    `<rabbit_root>/rabbit-project/features/<name>/feature.json`,
    alphabetical within each. No deduplication — callers needing
    uniqueness enforce it themselves.
    """
    rabbit_root = _detect_plugin_rabbit_root(repo)
    if rabbit_root is None:
        for fj in sorted(glob.glob(os.path.join(repo, '.claude', 'features', '*', 'feature.json'))):
            yield fj
    else:
        for fj in sorted(glob.glob(os.path.join(rabbit_root, '.claude', 'features', '*', 'feature.json'))):
            yield fj
        for fj in sorted(glob.glob(os.path.join(rabbit_root, 'rabbit-project', 'features', '*', 'feature.json'))):
            yield fj


def cmd_list(repo):
    for fj in iter_feature_jsons(repo):
        try:
            data = _load_json(fj)
            name = data.get('name', '')
            if name:
                print(name)
        except Exception:
            pass


def cmd_list_json(repo):
    results = []
    for fj in iter_feature_jsons(repo):
        try:
            f = _load_json(fj)
            results.append({
                'name': f.get('name', ''),
                'path': os.path.relpath(os.path.dirname(fj), repo),
                'summary': f.get('summary', ''),
                'tdd_state': f.get('tdd_state', ''),
            })
        except Exception:
            pass
    print(json.dumps(results))


def cmd_lookup(repo, target):
    for fj in iter_feature_jsons(repo):
        try:
            f = _load_json(fj)
            if f.get('name', '') == target:
                print(os.path.relpath(os.path.dirname(fj), repo))
                sys.exit(0)
        except Exception:
            pass
    # Not found — exit 1 (caller checks)
    sys.exit(1)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print(f"usage: {sys.argv[0]} <repo-root> list|list-json|lookup [feature-name]", file=sys.stderr)
        sys.exit(2)

    repo = sys.argv[1]
    subcmd = sys.argv[2]

    if subcmd == 'list':
        cmd_list(repo)
    elif subcmd == 'list-json':
        cmd_list_json(repo)
    elif subcmd == 'lookup':
        if len(sys.argv) < 4:
            print("ERROR: lookup requires <feature-name>", file=sys.stderr)
            sys.exit(2)
        cmd_lookup(repo, sys.argv[3])
    else:
        print(f"ERROR: unknown subcommand '{subcmd}'", file=sys.stderr)
        sys.exit(2)
