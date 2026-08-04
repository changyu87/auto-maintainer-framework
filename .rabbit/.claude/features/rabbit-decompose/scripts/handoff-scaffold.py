#!/usr/bin/env python3
"""handoff-scaffold.py — rabbit-decompose mode/root resolver + Step 4 scaffold
hand-off orchestrator.

Makes Step 1 source-root resolution and Step 4 scaffold hand-off SCRIPT-tier
(spec-rules §4 Script-Backed Orchestration; spec Invariants 5 and 6). This is
the single canonical resolver: both Step 1 (WHERE the decomposition source
lives) and Step 4 (mode-correct scaffolder dispatch) read from it, so they
cannot disagree and the model never hand-resolves an ambiguous `<repo>`.

Responsibilities:

  1. Resolve the rabbit root (from --rabbit-root, else the cwd, which in a
     rabbit session IS the mode-correct rabbit root) and DETECT MODE
     deterministically by REUSING the canonical
     resolver `rabbit-meta.lib.mode_detection.detect_mode` (lazy-imported,
     mirroring contract.lib.runtime.write_mode_marker). NOT a single
     hard-coded `<repo>/.rabbit/.runtime/mode` path read.
  2. Resolve the DECOMPOSITION SOURCE ROOT — the directory the skill points
     Glob/Read at to analyze the project:
       - plugin     -> rabbit_root.parent (the user project; the rabbit-root
                       is the vendored `.rabbit/` install dir, so the project
                       to decompose is its PARENT — matching
                       scaffold-feature.py._detect_plugin_mode, where
                       project_root = rabbit_root.parent).
       - standalone -> the rabbit root itself (the repo root).
  3. Author the batch temp file with a SCRIPT-OWNED timestamp (no
     model-assembled `<ts>`): the script owns the path and content.
  4. Dispatch the scaffolder on the mode-correct branch:
       - plugin     -> the rabbit-feature-scaffold SKILL batch interface
                       (scaffold-batch.py --batch <file>, one project-map
                       mutation). The skill is rabbit-feature's declared
                       cross-feature interface; calling its companion
                       scaffold-batch.py is a contract INVOKE of that published
                       interface, NOT a shell-out to the rabbit-feature
                       scaffolder implementation detail. scaffold-batch.py
                       mirrors scaffold-feature.py's exit codes 0/1/2 and runs
                       in the same cwd/plugin-mode context, so it is a drop-in.
       - standalone -> emit the per-feature rabbit-feature-scaffold plan
                       (batch form is plugin-only; the dispatcher then runs
                       one `Skill("rabbit-feature-scaffold", ...)` per feature)

The spec-create seeding (Step 4 part B) stays inline in the SKILL body as
sequential `Skill(...)` calls under the two-level-nesting constraint — that is
NOT this script's job.

CLI:
  handoff-scaffold.py --features <accepted.json> [--rabbit-root <path>]
                      [--plan-only]
  handoff-scaffold.py --source-root [--rabbit-root <path>]
  handoff-scaffold.py --detect-existing [--features <candidates.json>]
                      [--rabbit-root <path>]
  handoff-scaffold.py --decompose-context set --features <accepted.json>
                      [--rabbit-root <path>] [--operation <label>]
  handoff-scaffold.py --decompose-context clear [--rabbit-root <path>]

  --features     path to a JSON array of accepted/candidate features:
                 [{"name": "<kebab>", "globs": ["..."]}, ...]
  --rabbit-root  the rabbit root used for mode detection. Default: the cwd,
                 which in a rabbit session IS the mode-correct rabbit root —
                 the vendored `.rabbit/` directory in plugin mode, the repo
                 root in standalone mode.
  --plan-only    resolve mode + author the batch file (plugin) but do NOT
                 invoke the scaffolder; print the decision as JSON and exit 0.
                 Used by the SKILL body's dry-run and by the test.
  --source-root  Step 1 mode: resolve mode + the decomposition source root
                 ONLY (no --features required); print JSON and exit 0.
  --detect-existing  Pre-Step-2 detection mode (#925): resolve mode + read the
                 project-map.json and report whether the project is ALREADY
                 decomposed (a non-empty features map). When it is, emit the
                 SUMMARY of existing feature names plus the three-way branch
                 the SKILL offers (skip / add / re-decompose); when a candidate
                 list is supplied via --features, classify candidates into
                 already_rabbified vs new so the "add" branch proposes ONLY the
                 new/unrabbified features. Also SCANS the on-disk features/ root
                 and surfaces feature_dirs_on_disk + orphan_feature_dirs (dirs
                 present on disk but absent from project-map.json, including the
                 absent-map case) so a partial/aborted decompose's inconsistent
                 state is detectable (#1040). A by-design GREENFIELD dir
                 (feature.json `paths: []`, intentionally never in the map) is
                 EXCLUDED from orphan_feature_dirs; a dir with an absent/malformed
                 feature.json stays an orphan, the safe classification (#1042).
                 No --features required.
  --decompose-context  Manage the decompose-context scope-guard pass-through
                 marker (#923) — the bounded, auto-cleared replacement for the
                 manual `.rabbit-scope-override = session` workaround. `set`
                 writes <repo_root>/.rabbit/.runtime/decompose-active with the
                 accepted feature NAMES (requires --features) BEFORE the batch
                 work; `clear` deletes it (idempotent) AFTER, so the marker
                 never lingers. While present, scope-guard ALLOWS writes inside
                 any named feature's directory without a per-feature marker.
  --operation    optional decompose-context label recorded in the marker's
                 `operation` field (default: "rabbit-decompose batch scaffold").

Output (always JSON on stdout):
  Step 4 (--features):
    {
      "mode": "vendored" | "plugin" | "standalone",
      "branch": "batch" | "per-feature",
      "batch_file": "<abs path>" | null,
      "source_root": "<abs path>",
      "features": [ {"name": ..., "globs": [...]}, ... ],
      "dispatched": <bool>,         # false under --plan-only
      "vendored_commit_warning": "<str>" | null  # non-null in vendored/plugin
                                    # mode: commit the scaffold to the user repo
                                    # BEFORE rabbit-feature-touch (its worktree
                                    # branches from HEAD, sees committed files
                                    # only); null in standalone mode.
    }
  Step 1 (--source-root):
    {
      "mode": "vendored" | "plugin" | "standalone",
      "source_root": "<abs path>"
    }
  Pre-Step-2 (--detect-existing):
    {
      "mode": "vendored" | "plugin" | "standalone",
      "project_map_path": "<abs path>",
      "existing": <bool>,                 # true iff non-empty features map
      "existing_features": [ "<name>", ... ],   # sorted; [] when not existing
      "options": ["skip", "add", "re-decompose"],   # [] when not existing
      "already_rabbified": [ {"name": ..., "globs": [...]}, ... ],
      "new": [ {"name": ..., "globs": [...]}, ... ],   # the "add" candidates
      "feature_dirs_on_disk": [ "<name>", ... ],  # sorted dirs under features/
      "orphan_feature_dirs": [ "<name>", ... ]    # on-disk, absent from map,
                                                  # and NOT by-design greenfield
    }

Exit:
  0 success (plan computed; scaffolder dispatched unless
    --plan-only/--source-root/--detect-existing)
  1 scaffolder dispatch failed
  2 invocation error (bad args, unreadable/invalid features file)

Mode comparisons that select the vendored path dual-accept BOTH the legacy
"plugin" value and the post-rename "vendored" value (spec Invariant 10, #988):
the canonical value resolved by rabbit-meta's detect_mode is currently
"plugin", with a coordinated rename to "vendored" planned (#980). Dual-accepting
both keeps this script correct before AND after that rename. The legacy
"plugin" arm is removed only after the rename completes and the old value is
fully retired (coexistence-window deprecation).

Version: 0.9.0
Owner: rabbit-workflow team
Deprecation criterion: when Step 4 scaffold hand-off is provided natively by
    the rabbit CLI, retiring this companion script.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


# The vendored-mode values that select the vendored (plugin) path. The
# canonical mode value resolved by rabbit-meta's detect_mode is currently
# "plugin"; a coordinated rename to "vendored" is planned (#980). Every
# mode comparison that selects the vendored path dual-accepts BOTH values
# (spec Invariant 10, #988) so this script is correct before AND after the
# rename and never silently falls through to the standalone path. This is an
# inline local constant, NOT a cross-feature import. Coexistence-window
# deprecation: the legacy "plugin" entry is removed only after the #980 rename
# completes and the old value is fully retired.
_VENDORED_MODES = ("vendored", "plugin")


# The mode-aware commit-the-scaffold warning surfaced in the Step 4 plan JSON
# and reflected in SKILL.md Step 8 (Report). In vendored/plugin mode
# rabbit-feature-touch's create-branch step runs the TDD subagent inside a
# per-session git worktree branched from the host repo's HEAD, and a worktree
# contains only COMMITTED files. The scaffolded
# `.rabbit/rabbit-project/features/<name>/` dirs and seeded specs are NOT
# committed by decompose, so a feature-touch run immediately after a greenfield
# decompose creates a worktree that does not contain the new feature dir and the
# TDD subagent has nothing to implement. This warning instructs the user to
# commit the scaffold to the user repo BEFORE running rabbit-feature-touch. It
# is vendored-only: standalone mode has no HEAD-based worktree, so the value is
# None there.
_VENDORED_COMMIT_WARNING = (
    "Vendored/plugin mode: COMMIT the scaffolded "
    ".rabbit/rabbit-project/features/<name>/ dirs and seeded specs to the user "
    "repo (e.g. a PR to main) BEFORE running rabbit-feature-touch. "
    "rabbit-feature-touch's create-branch runs the TDD cycle inside a git "
    "worktree branched from the host repo's HEAD, and a worktree only contains "
    "committed files — an uncommitted scaffold is invisible to it, so the TDD "
    "subagent would have nothing to implement."
)


def _err(msg: str) -> None:
    sys.stderr.write(f"ERROR: {msg}\n")


def _resolve_rabbit_meta_mode_detection() -> "callable | None":
    """Lazy-load rabbit-meta.lib.mode_detection.detect_mode.

    Walk upward from this script's own location until a
    `.claude/features/rabbit-meta/lib/mode_detection.py` is found, then load
    detect_mode via importlib (mirrors contract.lib.runtime.write_mode_marker
    and tdd-step.py's repo-root-anchored resolution; works from any copy
    depth). Returns the callable, or None when rabbit-meta is unavailable.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = (parent / ".claude" / "features" / "rabbit-meta"
                / "lib" / "mode_detection.py")
        if cand.is_file():
            try:
                spec = importlib.util.spec_from_file_location(
                    "rabbit_meta_mode_detection", str(cand))
                if spec is None or spec.loader is None:
                    return None
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.detect_mode
            except (ImportError, AttributeError, OSError):
                return None
    return None


def _resolve_scaffolder() -> "Path | None":
    """Resolve the rabbit-feature-scaffold SKILL batch interface
    (skills/rabbit-feature-scaffold/scripts/scaffold-batch.py) by walking
    upward from this script's location (same anchoring as the rabbit-meta
    resolver).

    This is the declared cross-feature INVOKE: callers reach the scaffolder
    through rabbit-feature's published skill interface, NOT by shelling out to
    scaffold-feature.py directly (that script is rabbit-feature's
    implementation detail). scaffold-batch.py mirrors scaffold-feature.py's
    exit codes 0/1/2 and runs in the same cwd, so it is a drop-in replacement
    for the prior `scaffold-feature.py --batch` dispatch."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        cand = (parent / ".claude" / "features" / "rabbit-feature"
                / "skills" / "rabbit-feature-scaffold"
                / "scripts" / "scaffold-batch.py")
        if cand.is_file():
            return cand
    return None


def _default_rabbit_root() -> str:
    """The current working directory (#906).

    In a rabbit session the cwd IS the mode-correct rabbit root: the vendored
    `.rabbit/` install dir in plugin mode and the repo root in standalone
    mode — exactly what `detect_mode` requires (it returns `plugin` only when
    the rabbit-root's basename is `.rabbit`). The git toplevel is WRONG for a
    plugin install: `.rabbit/` lives inside the user project's git repo, so
    `git rev-parse --show-toplevel` returns the user-project root (the PARENT
    of `.rabbit`), whose basename is not `.rabbit`, mis-classifying the plugin
    install as `standalone`."""
    return os.getcwd()


def _resolve_mode(rabbit_root: str) -> "str | None":
    """Detect plugin/standalone via the canonical rabbit-meta detector.

    Returns the mode string, or None when rabbit-meta's detect_mode is
    unavailable (the caller refuses to guess)."""
    detect_mode = _resolve_rabbit_meta_mode_detection()
    if detect_mode is None:
        return None
    # Mode is resolved by the canonical detector — NOT a hard-coded mode-path
    # read. detect_mode inspects the rabbit-root's structural plugin signature
    # (basename == '.rabbit' with a host sibling).
    return detect_mode(rabbit_root)


def _resolve_source_root(rabbit_root: str, mode: str) -> str:
    """The decomposition SOURCE ROOT — where the skill points Glob/Read.

    In plugin mode the rabbit_root is the vendored `.rabbit/` install dir, so
    the project to decompose is its PARENT (matching
    scaffold-feature.py._detect_plugin_mode, project_root = rabbit_root.parent).
    In standalone mode the source root is the repo root itself."""
    if mode in _VENDORED_MODES:
        return str(Path(rabbit_root).parent)
    return str(Path(rabbit_root))


def _resolve_project_map_path(rabbit_root: str, mode: str) -> str:
    """The project-map.json path for the resolved mode (#925).

    In plugin mode the rabbit_root IS the vendored `.rabbit/` install dir, so
    the project-map lives at `<rabbit_root>/rabbit-project/project-map.json`. In
    standalone mode the rabbit_root is the repo root, so the project-map lives
    at `<rabbit_root>/.rabbit/rabbit-project/project-map.json` (matching the
    `.rabbit/rabbit-project/project-map.json` read declared in
    docs/contract.md)."""
    root = Path(rabbit_root)
    if mode in _VENDORED_MODES:
        return str(root / "rabbit-project" / "project-map.json")
    return str(root / ".rabbit" / "rabbit-project" / "project-map.json")


def _resolve_decompose_marker_path(rabbit_root: str, mode: str) -> str:
    """The decompose-context scope-guard marker path for the resolved mode
    (#923).

    Mirrors `_resolve_project_map_path`'s mode logic so the marker lands at the
    location scope-guard reads — `<repo_root>/.rabbit/.runtime/decompose-active`
    where `repo_root` is the git toplevel. In plugin mode the rabbit_root IS
    the vendored `.rabbit/` install dir, so the marker lives at
    `<rabbit_root>/.runtime/decompose-active`. In standalone mode the
    rabbit_root is the repo root, so it lives at
    `<rabbit_root>/.rabbit/.runtime/decompose-active`."""
    root = Path(rabbit_root)
    if mode in _VENDORED_MODES:
        return str(root / ".runtime" / "decompose-active")
    return str(root / ".rabbit" / ".runtime" / "decompose-active")


def _set_decompose_marker(marker_path: str, feature_names, operation: str
                          ) -> None:
    """Write the decompose-context marker (#923).

    The content matches the scope-guard contract: a non-empty string
    `operation` (a decompose label) and a non-empty list `features` carrying
    the EXACT accepted feature NAMES authorized this batch. `expires` is
    omitted — the orchestration clears the marker promptly on completion, so an
    explicit bound is not required for the deterministic in-process flow."""
    Path(marker_path).parent.mkdir(parents=True, exist_ok=True)
    payload = {"operation": operation, "features": list(feature_names)}
    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _clear_decompose_marker(marker_path: str) -> None:
    """Delete the decompose-context marker (#923); idempotent (a missing
    marker is a no-op)."""
    try:
        os.unlink(marker_path)
    except FileNotFoundError:
        pass


_DEFAULT_DECOMPOSE_OPERATION = "rabbit-decompose batch scaffold"


def _scan_feature_dirs(project_map_path: str):
    """Scan the resolved `features/` root for on-disk feature directories
    (#1040).

    The features root is the SIBLING `features/` directory next to
    `project-map.json` — exactly where scaffold-feature.py writes each feature
    dir (`<rabbit-project>/features/<name>`). Returns the sorted list of
    subdirectory names found there; a missing/empty `features/` dir yields [].
    Stray files at the features root are ignored — only directories count."""
    feats_root = Path(project_map_path).parent / "features"
    if not feats_root.is_dir():
        return []
    return sorted(p.name for p in feats_root.iterdir() if p.is_dir())


def _is_greenfield_feature_dir(project_map_path: str, name: str) -> bool:
    """True iff the on-disk feature dir `name` is a BY-DESIGN greenfield feature
    (#1042) — its `feature.json` declares an EMPTY `paths` list.

    A greenfield feature has `paths: []` and is INTENTIONALLY never registered
    in `project-map.json` (the project-map schema requires non-empty paths), so
    its absence from the map is expected, NOT an orphan. A dir whose
    `feature.json` is absent, unreadable, or malformed, or whose `paths` is
    non-empty/non-list, is NOT treated as greenfield: it stays an orphan (the
    safe classification) and this never raises."""
    fjson = Path(project_map_path).parent / "features" / name / "feature.json"
    try:
        with open(fjson, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return data.get("paths") == []


def _read_existing_features(project_map_path: str):
    """Read the project-map's features map, or {} when absent/empty/unreadable.

    Returns the `features` object (a dict keyed by feature name). A missing
    file, unparseable JSON, or a non-dict/empty `features` map all collapse to
    {} — the first-run signal (#925)."""
    try:
        with open(project_map_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    feats = data.get("features")
    if not isinstance(feats, dict):
        return {}
    return feats


def _parse_args(argv):
    features_file = None
    rabbit_root = None
    plan_only = False
    source_root_only = False
    detect_existing = False
    decompose_context = None
    operation = None
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--features":
            if i + 1 >= len(argv):
                _err("--features requires a path argument")
                return None
            features_file = argv[i + 1]
            i += 2
        elif a == "--rabbit-root":
            if i + 1 >= len(argv):
                _err("--rabbit-root requires a path argument")
                return None
            rabbit_root = argv[i + 1]
            i += 2
        elif a == "--plan-only":
            plan_only = True
            i += 1
        elif a == "--source-root":
            source_root_only = True
            i += 1
        elif a == "--detect-existing":
            detect_existing = True
            i += 1
        elif a == "--decompose-context":
            if i + 1 >= len(argv):
                _err("--decompose-context requires a 'set' or 'clear' argument")
                return None
            decompose_context = argv[i + 1]
            if decompose_context not in ("set", "clear"):
                _err("--decompose-context must be 'set' or 'clear'")
                return None
            i += 2
        elif a == "--operation":
            if i + 1 >= len(argv):
                _err("--operation requires a label argument")
                return None
            operation = argv[i + 1]
            i += 2
        elif a in ("-h", "--help"):
            sys.stdout.write(__doc__ or "")
            sys.exit(0)
        else:
            _err(f"unknown argument: {a}")
            return None
        continue
    if decompose_context == "set" and not features_file:
        _err("--decompose-context set requires --features <accepted.json>")
        return None
    if (not source_root_only and not detect_existing
            and decompose_context is None and not features_file):
        _err("--features <accepted.json> is required (or use --source-root / "
             "--detect-existing / --decompose-context)")
        return None
    return (features_file, rabbit_root, plan_only, source_root_only,
            detect_existing, decompose_context, operation)


def _load_features(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        _err(f"cannot read/parse features file {path}: {e}")
        return None
    if not isinstance(data, list) or not data:
        _err("features file must be a non-empty JSON array")
        return None
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or "name" not in entry:
            _err(f"features[{i}] must be an object with a 'name' field")
            return None
    return data


def _author_batch_file(features) -> str:
    """Write the accepted feature list to a batch temp file with a
    SCRIPT-OWNED timestamp. The script owns the path and the run id — no
    model-assembled `<ts>`."""
    ts = int(time.time())
    fd, path = tempfile.mkstemp(
        prefix=f"decompose-batch-{ts}-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(features, f)
    return path


def main(argv) -> int:
    parsed = _parse_args(argv)
    if parsed is None:
        return 2
    (features_file, rabbit_root, plan_only, source_root_only,
     detect_existing, decompose_context, operation) = parsed

    # --detect-existing accepts an OPTIONAL candidate list; --source-root and
    # --decompose-context clear take none; every other mode requires --features.
    features = None
    needs_features = not (
        source_root_only
        or (detect_existing and features_file is None)
        or decompose_context == "clear"
    )
    if needs_features:
        features = _load_features(features_file)
        if features is None:
            return 2

    if rabbit_root is None:
        rabbit_root = _default_rabbit_root()

    mode = _resolve_mode(rabbit_root)
    if mode is None:
        _err("rabbit-meta mode_detection unavailable (cannot resolve "
             "detect_mode); refusing to guess mode")
        return 2

    source_root = _resolve_source_root(rabbit_root, mode)

    # Decompose-context pass-through (#923): set/clear the scope-guard marker
    # that authorizes batch writes across the accepted features' directories.
    if decompose_context is not None:
        marker_path = _resolve_decompose_marker_path(rabbit_root, mode)
        if decompose_context == "set":
            names = [e.get("name") for e in features if e.get("name")]
            _set_decompose_marker(
                marker_path, names,
                operation or _DEFAULT_DECOMPOSE_OPERATION)
        else:  # clear
            _clear_decompose_marker(marker_path)
        print(json.dumps({
            "mode": mode,
            "decompose_context": decompose_context,
            "marker_path": marker_path,
        }))
        return 0

    # Pre-Step-2 mode (#925): detect an existing decomposition and emit the
    # SUMMARY + three-way branch the SKILL offers when the project-map already
    # carries features.
    if detect_existing:
        project_map_path = _resolve_project_map_path(rabbit_root, mode)
        existing_map = _read_existing_features(project_map_path)
        existing = bool(existing_map)
        existing_names = sorted(existing_map.keys())
        already_rabbified = []
        new = []
        if existing and features:
            for entry in features:
                if entry.get("name") in existing_map:
                    already_rabbified.append(entry)
                else:
                    new.append(entry)
        # Orphan feature-dir detection (#1040): scan the on-disk `features/`
        # root and surface any dir ABSENT from the project-map's features map
        # (treating an absent map as empty, so a partial/aborted decompose that
        # left dirs behind without a project-map is surfaced too). Detection +
        # surfacing only — the adopt-vs-proceed decision stays the caller's.
        # Greenfield exclusion (#1042): a dir absent from the map is a TRUE
        # orphan ONLY when it is NOT a by-design greenfield feature. A greenfield
        # feature has `paths: []` in its feature.json and is INTENTIONALLY never
        # registered in project-map.json, so flagging it would be a false
        # positive. A dir with an absent/malformed feature.json is not provably
        # greenfield, so it stays an orphan (the safe classification).
        feature_dirs_on_disk = _scan_feature_dirs(project_map_path)
        orphan_feature_dirs = [
            d for d in feature_dirs_on_disk
            if d not in existing_map
            and not _is_greenfield_feature_dir(project_map_path, d)
        ]
        print(json.dumps({
            "mode": mode,
            "project_map_path": project_map_path,
            "existing": existing,
            "existing_features": existing_names if existing else [],
            "options": ["skip", "add", "re-decompose"] if existing else [],
            "already_rabbified": already_rabbified,
            "new": new,
            "feature_dirs_on_disk": feature_dirs_on_disk,
            "orphan_feature_dirs": orphan_feature_dirs,
        }))
        return 0

    # Step 1 mode: resolve mode + the decomposition source root only.
    if source_root_only:
        print(json.dumps({"mode": mode, "source_root": source_root}))
        return 0

    if mode in _VENDORED_MODES:
        batch_file = _author_batch_file(features)
        result = {
            "mode": mode,
            "branch": "batch",
            "batch_file": batch_file,
            "source_root": source_root,
            "features": features,
            "dispatched": False,
            "vendored_commit_warning": _VENDORED_COMMIT_WARNING,
        }
        if plan_only:
            print(json.dumps(result))
            return 0
        scaffolder = _resolve_scaffolder()
        if scaffolder is None:
            _err("rabbit-feature-scaffold skill batch interface "
                 "(scaffold-batch.py) unavailable; cannot dispatch batch")
            return 1
        # Decompose-context pass-through (#923): ensure the marker is present
        # BEFORE the scaffolder runs (so its cross-feature writes are
        # authorized) and clear it AFTER — in a finally, so a FAILING scaffolder
        # never leaves a lingering marker. This is OWN-ONLY: when the SKILL's
        # Step 4-A already SET the marker (it spans the later spec-seed step
        # too), the script must NOT clear it out from under that outer
        # orchestration. So it only self-manages the marker it itself created —
        # if a marker is already present, the outer orchestration owns its
        # lifecycle and the script leaves it untouched.
        marker_path = _resolve_decompose_marker_path(rabbit_root, mode)
        owns_marker = not Path(marker_path).is_file()
        if owns_marker:
            names = [e.get("name") for e in features if e.get("name")]
            _set_decompose_marker(
                marker_path, names,
                operation or _DEFAULT_DECOMPOSE_OPERATION)
        try:
            proc = subprocess.run(
                [sys.executable, str(scaffolder), "--batch", batch_file],
                cwd=source_root,
            )
        finally:
            if owns_marker:
                _clear_decompose_marker(marker_path)
        result["dispatched"] = proc.returncode == 0
        print(json.dumps(result))
        return 0 if proc.returncode == 0 else 1

    # standalone — batch form is plugin-only; emit the per-feature plan for
    # the dispatcher to run one Skill("rabbit-feature-scaffold", ...) per
    # accepted feature.
    result = {
        "mode": "standalone",
        "branch": "per-feature",
        "batch_file": None,
        "source_root": source_root,
        "features": features,
        "dispatched": False,
        # Standalone has no HEAD-based worktree, so the commit-the-scaffold
        # warning does not apply.
        "vendored_commit_warning": None,
    }
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
