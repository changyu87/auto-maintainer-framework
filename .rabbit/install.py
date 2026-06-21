#!/usr/bin/env python3
"""install.py — install rabbit in vendored mode at <project>/.rabbit/ + dev-test helper.

This module has two distinct roles:

  1. User-facing MVP installer (main()):
     Invoked by install.sh after the upstream tarball is extracted. Lays
     down the minimum file closure needed for the user-promised surfaces:

       1. Drift-protected Claude on session start (policy block via CLAUDE.md)
       2. rabbit-feature-scaffold <name> <path-glob> (declare a feature mapping)
       3. Scope-guard blocking edits to declared-feature paths
       4. .rabbit/.runtime/scope-bypass-once one-shot override marker

     Each (source, dest) tuple at module top is explicit so the installer
     doesn't depend on the publish flow having been run against the source
     tarball.

     After the closure copy, main() RUNS THE PUBLISH FLOW against the freshly
     installed tree (canonicalize_installed_surfaces) so the installed manifest
     surfaces are byte-identical to what the Stop hook's check_manifest_drift
     republishes at runtime. This makes a fresh install drift-free on its first
     Stop even when a committed deployed surface shipped stale.

     Excludes development surfaces: test/, docs/, scripts/enforcement/,
     deferred features (tdd-subagent), retired
     tombstones (tdd-state-machine).

     Usage:
         install.py --src <extracted-tarball-dir> --target <project>/.rabbit
         install.py --update    (self-fetch upstream; infer target from script
                                 location — see Inv 22g)

  2. Publish flow (run_publish_loop()):
     Walks every <target>/.claude/features/*/feature.json manifest and invokes
     each declared API via contract.lib.publish. Invoked from main() (via
     canonicalize_installed_surfaces) to canonicalize installed surfaces, and
     also imported directly by rabbit-cage test suites
     (test-deployed-hooks-execute.py, test-install-publish-loop.py) to exercise
     the publish flow against a freshly copied .claude tree.

Version: 6.11.0
Owner: rabbit-workflow team
Deprecation criterion: when rabbit's per-project vendored model is superseded
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

# Inv 26/27 (amended #848): the default --update self-fetch ref is now resolved
# DYNAMICALLY from GitHub's latest published release (see resolve_latest_release
# below). HARDCODED_STABLE_DEFAULT is the OFFLINE FALLBACK — used only when the
# latest-release lookup fails (network/offline, API outage). MUST byte-equal
# install.sh's RABBIT_FALLBACK_REF (single source of truth). The literal value
# "dev" is FORBIDDEN here (enforced by test-install-py-default-ref-not-dev.py).
HARDCODED_STABLE_DEFAULT = "v9.0.26"

# Inv 22h: env-var infinite-loop guard for the --update self-exec branch. The
# OLD process sets this to "1" before os.execv; the NEW process (started by
# os.execv) inherits the env, sees the marker, and skips the re-exec branch.
# One re-exec per --update invocation is enough.
_REEXEC_GUARD = "RABBIT_INSTALL_REEXEC_DONE"

# ───────────────────────────────────────────────────────────────────────────
# MVP file closure as (src_rel, dst_rel) tuples — explicit source→destination
# ───────────────────────────────────────────────────────────────────────────

# Top-level files (same path on both sides)
SAME_PATH_FILES = [
    "CLAUDE.md",
    "README.md",
    "install.py",
    ".claude/settings.json",
]

# Hooks: source → deployed
HOOKS = [
    (".claude/features/rabbit-cage/hooks/scope-guard.py", ".claude/hooks/scope-guard.py"),
    (".claude/features/rabbit-cage/hooks/session-start-dispatcher.py", ".claude/hooks/session-start-dispatcher.py"),
    (".claude/features/rabbit-cage/hooks/stop-dispatcher.py", ".claude/hooks/stop-dispatcher.py"),
    (".claude/features/rabbit-cage/hooks/user-prompt-submit-dispatcher.py", ".claude/hooks/user-prompt-submit-dispatcher.py"),
    (".claude/features/rabbit-cage/hooks/_dispatcher_lib.py", ".claude/hooks/_dispatcher_lib.py"),
    (".claude/features/rabbit-cage/hooks/restart_snapshot.py", ".claude/hooks/restart_snapshot.py"),
]

# Skills: source SKILL.md → deployed SKILL.md
SKILLS = [
    (".claude/features/rabbit-feature/skills/rabbit-feature-scaffold/SKILL.md", ".claude/skills/rabbit-feature-scaffold/SKILL.md"),
    (".claude/features/rabbit-feature/skills/rabbit-feature-touch/SKILL.md", ".claude/skills/rabbit-feature-touch/SKILL.md"),
    (".claude/features/rabbit-feature/skills/rabbit-feature-scope/SKILL.md", ".claude/skills/rabbit-feature-scope/SKILL.md"),
    (".claude/features/rabbit-issue/skills/rabbit-issue/SKILL.md", ".claude/skills/rabbit-issue/SKILL.md"),
    (".claude/features/rabbit-spec/skills/rabbit-spec-update/SKILL.md", ".claude/skills/rabbit-spec-update/SKILL.md"),
    (".claude/features/rabbit-decompose/skills/rabbit-decompose/SKILL.md", ".claude/skills/rabbit-decompose/SKILL.md"),
    (".claude/features/rabbit-housekeep/skills/rabbit-housekeep/SKILL.md", ".claude/skills/rabbit-housekeep/SKILL.md"),
]

# Agents: source → deployed
AGENTS = [
    (".claude/features/rabbit-spec/agents/rabbit-spec-creator.md", ".claude/agents/rabbit-spec-creator.md"),
    (".claude/features/tdd-subagent/agents/rabbit-tdd-subagent.md", ".claude/agents/rabbit-tdd-subagent.md"),
]

# Commands: source → deployed
COMMANDS: list[tuple[str, str]] = [
    (".claude/features/rabbit-cage/commands/rabbit-refresh.md", ".claude/commands/rabbit-refresh.md"),
    (".claude/features/rabbit-cage/commands/rabbit-project.md", ".claude/commands/rabbit-project.md"),
    (".claude/features/rabbit-cage/commands/rabbit-update.md", ".claude/commands/rabbit-update.md"),
    (".claude/features/rabbit-cage/commands/rabbit-cage-config.md", ".claude/commands/rabbit-cage-config.md"),
    (".claude/features/rabbit-housekeep/commands/rabbit-housekeep.md", ".claude/commands/rabbit-housekeep.md"),
]

# Per-feature sub-path includes (whole subset; same path on both sides)
FEATURE_INCLUDES: dict[str, list[str]] = {
    "contract": [
        "feature.json",
        "lib/__init__.py",
        "lib/runtime.py",
        "lib/checks.py",
        "lib/policy_block.py",
        "lib/mutation.py",
        "lib/producers.py",
        "lib/publish.py",
        "scripts/build-prompt.py",
        "scripts/rabbit_print.py",
        "scripts/validate-feature.py",
        "scripts/validate-meta-contract.py",
        "scripts/find-feature.py",
        "scripts/policy-block.py",
        "scripts/check-release-update.py",
        "lib/config_dispatch.py",
        "schemas/feature.json.schema.json",
        "schemas/runtime.schema.json",
        "schemas/prompts.schema.json",
        "schemas/project-map.json.schema.json",
        "schemas/manifest.schema.json",
        "schemas/rabbit-print.schema.json",
        "templates/spec-template.md",
        "templates/contract-template.md",
        "templates/feature-json-template.json",
        "templates/project-map-template.json",
        "templates/prompts/spec-create.txt",
        "templates/prompts/tdd-subagent.txt",
    ],
    "policy": [
        "feature.json",
        "philosophy.md",
        "spec-rules.md",
        "coding-rules.md",
    ],
    "rabbit-cage": [
        "feature.json",
        "settings.json",
        "README.md",
        "install.py",
        "policy-header.json",
        "hooks/scope-guard.py",
        "hooks/stop-dispatcher.py",
        "hooks/session-start-dispatcher.py",
        "hooks/user-prompt-submit-dispatcher.py",
        "hooks/_dispatcher_lib.py",
        "hooks/restart_snapshot.py",
        "commands/rabbit-refresh.md",
        "commands/rabbit-project.md",
        "commands/rabbit-update.md",
        "commands/rabbit-cage-config.md",
        "scripts/rabbit-update.py",
        "scripts/rabbit-cage-config.py",
        "scripts/rabbit-project.py",
        "scripts/rabbit-project-set-path.py",
        "scripts/rabbit-project-map.py",
        "lib/__init__.py",
        "lib/project_map_reader.py",
        "lib/runtime_root.py",
    ],
    "rabbit-meta": [
        "feature.json",
        "lib/__init__.py",
        "lib/mode_detection.py",
        "lib/generate_claude_md.py",
        "lib/generate_readme.py",
        "templates/CLAUDE.md.template",
        "templates/README.md.template",
    ],
    "rabbit-feature": [
        "feature.json",
        "commands/rabbit-tdd-autonomous.md",
        "scripts/rabbit-tdd-autonomous-config.py",
        "scripts/audit-owner.py",
        "scripts/format-feature-context.py",
        "scripts/resolve-scope.py",
        "scripts/scaffold-feature.py",
        "skills/rabbit-feature-scaffold/SKILL.md",
        "skills/rabbit-feature-scaffold/scripts/scaffold-batch.py",
        "skills/rabbit-feature-scope/SKILL.md",
        "skills/rabbit-feature-touch/SKILL.md",
        "skills/rabbit-feature-touch/scripts/feature-touch.py",
    ],
    "rabbit-issue": [
        "feature.json",
        "skills/rabbit-issue/SKILL.md",
        "scripts/_gh.py",
        "scripts/file-item.py",
        "scripts/item-status.py",
        "scripts/list-items.py",
        "github/ISSUE_TEMPLATE/file-item.yml",
        "github/workflows/issue-form-autolabel.yml",
    ],
    "rabbit-spec": [
        "feature.json",
        "agents/rabbit-spec-creator.md",
        "scripts/dispatch-spec-creator.py",
        "skills/rabbit-spec-update/SKILL.md",
    ],
    "rabbit-decompose": [
        "feature.json",
        "skills/rabbit-decompose/SKILL.md",
        "scripts/handoff-scaffold.py",
    ],
    "rabbit-housekeep": [
        "feature.json",
        "skills/rabbit-housekeep/SKILL.md",
        "commands/rabbit-housekeep.md",
        "scripts/measure-reduction.py",
        "scripts/check-script-backed.py",
        "scripts/resolve-housekeep-scope.py",
    ],
    "tdd-subagent": [
        "feature.json",
        "agents/rabbit-tdd-subagent.md",
        "scripts/dispatch-tdd-subagent.py",
        "scripts/tdd-step.py",
    ],
}


def closure_source_rels() -> list[str]:
    """Every repo-relative SOURCE path the install closure reads, in order.

    The single enumeration of SAME_PATH_FILES + HOOKS + SKILLS + AGENTS +
    COMMANDS + FEATURE_INCLUDES sources. `check_install_sources_exist` and the
    install-integrity tests consume this so the closure is described in exactly
    one place.
    """
    rels: list[str] = list(SAME_PATH_FILES)
    rels += [src for src, _dst in HOOKS]
    rels += [src for src, _dst in SKILLS]
    rels += [src for src, _dst in AGENTS]
    rels += [src for src, _dst in COMMANDS]
    for feature, paths in FEATURE_INCLUDES.items():
        base = f".claude/features/{feature}"
        rels += [f"{base}/{rel}" for rel in paths]
    return rels


def check_install_sources_exist(repo_root) -> list[str]:
    """Inv 21 integrity guard (#880): return the closure source paths that do
    NOT exist as files under `repo_root`.

    install.main() requires EVERY closure source to exist (copy_one aborts on a
    missing source), so a surface retirement that leaves a stale closure entry
    silently breaks `curl … install.sh | bash` on every fresh install. This is
    the deterministic, importable check that catches that class: it is run as a
    fail-loud self-check inside main() (before any copy) and exercised against
    the real repo by the rabbit-cage install-integrity tests. It is also
    importable by the cross-feature contract gate so a surface change to ANY
    feature — not only rabbit-cage — is screened against the install closure.

    Returns the (sorted, de-duplicated) list of missing repo-relative source
    paths; an empty list means the closure is intact.
    """
    root = Path(repo_root)
    missing = {rel for rel in closure_source_rels() if not (root / rel).is_file()}
    return sorted(missing)


def copy_one(src_root: Path, dst_root: Path, src_rel: str, dst_rel: str) -> bool:
    src = src_root / src_rel
    dst = dst_root / dst_rel
    if not src.is_file():
        print(f"error: missing required source file: {src_rel}", file=sys.stderr)
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    # Preserve executable bit for shell + python scripts copied to user-runnable locations
    if dst.suffix in (".py", ".sh") or "scripts/" in dst_rel or "hooks/" in dst_rel:
        os.chmod(dst, os.stat(dst).st_mode | 0o111)
    return True


def copy_one_tolerant(
    src_root: Path, dst_root: Path, src_rel: str, dst_rel: str, tolerate_shrink: bool
) -> bool:
    """copy_one, but under a tolerated closure SHRINK (Inv 49 #968) a source
    absent from `src_root` is DROPPED — not a hard failure.

    On --update the OLD install.py drives the refresh with its stale closure,
    which may name surfaces the NEW source legitimately RETIRED. When
    `tolerate_shrink` is set (the pre-re-exec --update self-fetch window), a
    missing source is skipped silently and the run continues to the Inv 22h
    re-exec into the NEW install.py, which then validates its OWN corrected
    closure strictly. When `tolerate_shrink` is False (fresh install,
    explicit-src dev path, or post-re-exec NEW closure) the strict copy_one
    abort applies, so a genuine dangling ref still fails.
    """
    if tolerate_shrink and not (src_root / src_rel).is_file():
        return True
    return copy_one(src_root, dst_root, src_rel, dst_rel)


def _rewrite_one(path: Path, rabbit_root: str) -> None:
    """Apply the two Inv 19 edits in place to a single settings.json file.

    (a) Sets env.RABBIT_ROOT to `rabbit_root`; creates the env block if
        absent; overwrites any existing RABBIT_ROOT key.
    (b) Replaces every literal '$(git rev-parse --show-toplevel)' occurrence
        with '$RABBIT_ROOT' inside any hooks[<event>][].hooks[].command
        string. No other fields touched.

    Idempotent: re-running on an already-rewritten file is a no-op.
    """
    with open(path) as f:
        data = json.load(f)

    env_block = data.get("env")
    if not isinstance(env_block, dict):
        env_block = {}
        data["env"] = env_block
    env_block["RABBIT_ROOT"] = rabbit_root

    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for _event, entries in hooks.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                inner = entry.get("hooks")
                if not isinstance(inner, list):
                    continue
                for h in inner:
                    if not isinstance(h, dict):
                        continue
                    cmd = h.get("command")
                    if isinstance(cmd, str):
                        h["command"] = cmd.replace(
                            "$(git rev-parse --show-toplevel)", "$RABBIT_ROOT"
                        )

    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def rewrite_settings_for_plugin(dst_root: Path) -> None:
    """Inv 19: rewrite both the deployed settings.json and the feature-local
    source copy so that `check_manifest_drift` (which republishes from the
    source) produces a result identical to the deployed copy.

    Applies `_rewrite_one` to:
      - <dst_root>/.claude/settings.json (deployed copy)
      - <dst_root>/.claude/features/rabbit-cage/settings.json (source copy,
        guarded with `is_file()` — only present when the rabbit-cage feature
        directory was shipped in the install closure).

    Idempotent: re-running on already-rewritten files is a no-op.
    """
    rabbit_root = str(dst_root.resolve())
    deployed_settings = dst_root / ".claude/settings.json"
    if deployed_settings.is_file():
        _rewrite_one(deployed_settings, rabbit_root)
    source_settings = dst_root / ".claude/features/rabbit-cage/settings.json"
    if source_settings.is_file():
        _rewrite_one(source_settings, rabbit_root)


# Single source of truth for the inner `.rabbit/.gitignore` ephemeral ignore
# list. `write_rabbit_gitignore` renders it; `untrack_ignored_rabbit_ephemerals`
# matches tracked `.rabbit/` files against it (via `git check-ignore` over the
# rendered file) on `--update`. Keep the list HERE only — never hardcode a
# second copy of the ephemeral names elsewhere.
_RABBIT_GITIGNORE_BODY = (
    "# rabbit-owned ephemerals — never commit these\n"
    ".runtime/\n"
    "prompts/\n"
    "tdd-report-*.json\n"
    "impl-suggestion-*.json\n"
    ".scope-active-*\n"
    ".scope-bypass-once\n"
    ".rabbit-restart-snapshot\n"
    ".rabbit-prompt-counter\n"
    ".rabbit-update-restart-needed\n"
    "__pycache__/\n"
    "*.pyc\n"
)


def write_rabbit_gitignore(dst_root: Path) -> None:
    (dst_root / ".gitignore").write_text(_RABBIT_GITIGNORE_BODY)


def untrack_ignored_rabbit_ephemerals(dst_root: Path) -> None:
    """`--update` self-heal (#1137): index-only untrack any file TRACKED under
    `.rabbit/` that NOW matches the inner ephemeral ignore list.

    Background: `write_rabbit_gitignore` writes the inner `.rabbit/.gitignore`
    list. When a NEW ephemeral joins that list (e.g. `.rabbit-prompt-counter`,
    #1135), gitignore alone does NOT untrack a file an existing vendored host
    repo already committed — it keeps churning until a human runs
    `git rm --cached`. This converges those Strategy D full-vendor installs on
    `--update` with no manual step.

    Match set = the SAME source of truth `write_rabbit_gitignore` renders: we
    ask `git check-ignore` (which evaluates the just-written
    `.rabbit/.gitignore`) which tracked `.rabbit/` paths it ignores, so the
    gitignore glob semantics are never reimplemented here.

    `git rm --cached` is index-only: the working-tree file stays on disk. Only
    files that BOTH (a) are tracked AND (b) match the current inner ignore list
    are touched — nothing else. Degrades to a clean no-op when `dst_root` is a
    standalone install (not a `.rabbit` dir), when `.rabbit/` is untracked
    entirely, when not in a git repo, or when git is unavailable.
    """
    if dst_root.name != ".rabbit":
        return  # standalone install — no vendored host repo to heal
    host_root = dst_root.parent

    def _git(*args: str, stdin: str | None = None):
        try:
            return subprocess.run(
                ["git", "-C", str(host_root), *args],
                capture_output=True, text=True, input=stdin)
        except Exception:
            return None

    # Files currently tracked under `.rabbit/` (host-relative paths). Empty /
    # error (not a repo, git absent, `.rabbit/` untracked) -> clean no-op.
    listed = _git("ls-files", "-z", "--", ".rabbit")
    if listed is None or listed.returncode != 0 or not listed.stdout:
        return
    tracked = [p for p in listed.stdout.split("\0") if p]
    if not tracked:
        return

    # Of those, the ones git would ignore per the inner `.rabbit/.gitignore`.
    # `--no-index` is REQUIRED: by default check-ignore never reports a TRACKED
    # path as ignored (a tracked file is, by git's rules, not "ignored"), but
    # tracked-yet-now-ignored is exactly the state we must heal — so evaluate
    # the patterns directly, irrespective of index membership. Paths are fed via
    # `--stdin -z` (NUL-separated) because `-z` requires `--stdin`.
    checked = _git("check-ignore", "--no-index", "-z", "--stdin",
                   stdin="\0".join(tracked))
    # check-ignore exits 1 when NO paths are ignored (a no-op) and 0 when some
    # are; any other code (or a missing process) -> degrade to no-op.
    if checked is None or checked.returncode not in (0, 1) or not checked.stdout:
        return
    to_untrack = [p for p in checked.stdout.split("\0") if p]
    if not to_untrack:
        return

    _git("rm", "--cached", "-q", "--", *to_untrack)


# Strategy D host-gitignore tokens (#1086 / #1085, full-vendor). Strategy D
# supersedes Strategy A (#1052/#1060). The vendored install lives at
# <host>/.rabbit/; under D the host repo tracks the WHOLE `.rabbit/` — BOTH the
# tool tree (`.rabbit/.claude/`) AND the rabbit-project work
# (`.rabbit/rabbit-project/`) — ignoring ONLY the ephemerals
# (`.rabbit/.runtime/`, plus whatever the inner `.rabbit/.gitignore` covers).
# Tracking the tool too makes vendored == standalone for VCS: a worktree of the
# host repo is self-contained, so the proven standalone machinery works
# unchanged. A blanket `.rabbit/` host ignore makes every cycle commit silently
# no-op (no impl_commit, no PR); the A-shape additionally hid the tool tree.
_HOST_IGNORE_TOKENS = (".rabbit/.runtime/",)
# Over-broad / superseded host ignores Strategy D MIGRATES (removes) rather than
# retains. This covers BOTH the legacy blanket `.rabbit/`/`.rabbit` line AND the
# A-shape `.rabbit/.claude/` tool-tree ignore (#1052), which D now tracks.
_HOST_MIGRATE_IGNORES = (".rabbit", ".rabbit/", ".rabbit/.claude", ".rabbit/.claude/")


def write_host_gitignore(dst_root: Path) -> None:
    """Strategy D (#1086/#1085, full-vendor): manage the HOST repo `.gitignore`
    so a vendored install tracks the WHOLE `.rabbit/` — BOTH the tool tree
    (`.rabbit/.claude/`) AND the work (`.rabbit/rabbit-project/`) — ignoring
    ONLY the ephemerals (`.rabbit/.runtime/`). Tracking tool+work makes the
    vendored install self-contained for VCS (vendored == standalone), so the
    cycle's commits and PRs land in the host repo and a host-repo worktree is
    complete.

    Vendored-only: acts solely when `dst_root` is a `.rabbit` install dir (the
    host repo is then `dst_root.parent`). A standalone install (dst_root is the
    repo root itself) leaves the host `.gitignore` untouched.

    Idempotent + migrating: a pre-existing blanket `.rabbit/` (or `.rabbit`)
    ignore line is REMOVED, and the SUPERSEDED A-shape `.rabbit/.claude/`
    tool-tree ignore (#1052) is REMOVED too (D tracks the tool) — migrated
    away, not merely shadowed; the D ephemeral token is appended only when
    absent; unrelated user entries are preserved. Re-running converges to the
    same content.
    """
    if dst_root.name != ".rabbit":
        return  # standalone install — nothing to manage in the host repo
    host_root = dst_root.parent
    gi_path = host_root / ".gitignore"

    existing = gi_path.read_text().splitlines() if gi_path.is_file() else []

    kept: list[str] = []
    have_tokens: set[str] = set()
    for line in existing:
        stripped = line.strip()
        # Drop the over-broad blanket ignore and the superseded A-shape tool
        # ignore (migration to D).
        if stripped in _HOST_MIGRATE_IGNORES:
            continue
        if stripped in _HOST_IGNORE_TOKENS:
            have_tokens.add(stripped)
        kept.append(line)

    missing = [t for t in _HOST_IGNORE_TOKENS if t not in have_tokens]
    if missing:
        if kept and kept[-1].strip() != "":
            kept.append("")
        kept.append("# rabbit vendored install — ignore ONLY ephemerals; track "
                    "the whole .rabbit/ tool+project (Strategy D, #1086)")
        kept.extend(missing)

    content = "\n".join(kept).rstrip("\n") + "\n"
    gi_path.write_text(content)


def _local_src_marker(src_root: Path | None) -> str:
    """Inv 48: derive a MEANINGFUL pin label for a local `--src` install that
    resolves no published ref (RABBIT_INSTALLED_REF unset).

    A local checkout has no release tag, so the pin must NOT be the bare
    sentinel `unknown` — that string flows verbatim into both the SessionStart
    version box (`rabbit vunknown`) and the update-check headline (`current:
    unknown ... on channel unknown`), comparing a real release against a
    non-version. Instead derive `local-<short-sha>` from the source git
    checkout via a read-only `git -C <src_root> rev-parse --short HEAD`, and
    fall back to the literal `local` when no SHA is resolvable (git absent,
    `src_root` is not a checkout, `src_root` is None). Never raises.
    """
    if src_root is not None:
        try:
            out = subprocess.run(
                ["git", "-C", str(src_root), "rev-parse", "--short", "HEAD"],
                capture_output=True, text=True, check=False,
            )
            if out.returncode == 0:
                sha = out.stdout.strip()
                if sha:
                    return f"local-{sha}"
        except (OSError, ValueError):
            pass
    return "local"


def write_version_pin(dst_root: Path, src_root: Path | None = None) -> None:
    """Write the install pin to <dst_root>/.version.

    Precedence: an explicit `RABBIT_INSTALLED_REF` env var (the published
    install / `--update` path) wins verbatim. When it is unset or empty — the
    local `--src` install case — derive a meaningful local marker from
    `src_root` (Inv 48) rather than pinning the literal `unknown`.
    """
    label = os.environ.get("RABBIT_INSTALLED_REF", "").strip()
    if not label:
        label = _local_src_marker(src_root)
    (dst_root / ".version").write_text(label + "\n")


def fetch_upstream(repo: str, ref: str, dest: Path) -> Path:
    """Inv 22g: download the upstream tarball into `dest` and return the
    extracted `rabbit-workflow-*` dir.

    Uses stdlib urllib.request + tarfile only (Inv 4 Tech Stack). Raises on
    any failure (URLError / OSError / tarfile.ReadError / StopIteration);
    callers translate the exception into a clean exit 1.
    """
    url = f"https://github.com/{repo}/archive/{ref}.tar.gz"
    tarball = dest / "rabbit.tar.gz"
    with urllib.request.urlopen(url) as resp, open(tarball, "wb") as f:
        shutil.copyfileobj(resp, f)
    with tarfile.open(tarball) as tar:
        tar.extractall(dest)
    return next(
        p for p in dest.iterdir()
        if p.is_dir() and p.name.startswith("rabbit-workflow-")
    )


def resolve_latest_release(repo: str) -> str | None:
    """Inv 26/27 (#848): resolve the latest published release's tag_name.

    Reuses the contract-owned `fetch_upstream_version` (the SAME logic the
    update-check and /rabbit-update use) by importing
    `.claude/features/contract/scripts/check-release-update.py`. Returns the
    tag string on success, or None on ANY failure (missing contract script,
    network/offline, API outage, empty tag) so the caller can fall back to the
    hardcoded offline default. For deterministic testing the resolved tag may
    be injected via the RABBIT_UPDATE_TEST_LATEST env var (mirrors
    /rabbit-update check), bypassing the network fetch.
    """
    injected = os.environ.get("RABBIT_UPDATE_TEST_LATEST")
    if injected is not None:
        tag = injected.strip()
        return tag or None

    # Locate the running install.py's repo/install root, then the contract
    # release-check helper relative to it. The running install.py lives at
    # either <root>/install.py (deployed) or <root>/.claude/features/rabbit-cage/
    # install.py (source); probe both candidate roots.
    here = Path(__file__).resolve()
    candidates = [here.parent]                       # <root>/install.py
    if len(here.parents) >= 4:
        candidates.append(here.parents[3])           # source layout root
    cru_path = None
    for root in candidates:
        p = root / ".claude/features/contract/scripts/check-release-update.py"
        if p.is_file():
            cru_path = p
            break
    if cru_path is None:
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "install_check_release_update", str(cru_path))
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tag = module.fetch_upstream_version(repo, "")
    except Exception:  # noqa: BLE001 — never block install on a resolution error
        return None
    if not isinstance(tag, str) or not tag.strip():
        return None
    return tag.strip()


def _parse_version(ref: str | None) -> tuple[int, ...] | None:
    """Inv 27 downgrade guard (#850): extract a comparable semver tuple from a
    ref string, or None when the ref carries no semver.

    Recognizes a leading `vX.Y.Z` / `X.Y.Z` (optional `v`, any extra suffix
    ignored) anywhere in the ref. A dead release-branch ref like
    `release/1.12.0` yields `(1, 12, 0)`; `v9.0.26` yields `(9, 0, 26)`. Refs
    with no embedded semver (a bare branch name, a SHA, `dev`) return None — the
    caller treats an unparseable side as "cannot compare" and does NOT block.
    """
    if not ref:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", ref)
    if not m:
        return None
    return tuple(int(g) for g in m.groups())


def _is_strictly_newer(candidate: str | None, installed: str | None) -> bool:
    """Inv 27 downgrade guard (#850): True only when `candidate` parses to a
    semver STRICTLY greater than `installed`'s semver.

    Returns False when the candidate is older-or-equal. When EITHER side carries
    no parseable semver the comparison is indeterminate; this returns True
    (do-not-block) so the guard never refuses an upgrade it merely failed to
    parse — the guard's job is to catch the proven-older case, not to gate on
    ambiguity.
    """
    cand = _parse_version(candidate)
    inst = _parse_version(installed)
    if cand is None or inst is None:
        return True
    return cand > inst


def _installed_version_for_update(args: argparse.Namespace) -> str | None:
    """Inv 27 downgrade guard (#850): read the currently-installed ref from the
    update target's `.version`, for the strictly-newer comparison.

    The target is `args.target` when supplied explicitly, otherwise the
    inferred install root (the directory containing the running install.py,
    matching Inv 22g (b)). Returns the trimmed `.version` content, or None when
    it is absent/unreadable/empty (in which case the guard cannot compare and
    does NOT block).
    """
    target = args.target
    if target is None:
        target = Path(__file__).resolve().parent
    version_file = Path(target) / ".version"
    try:
        text = version_file.read_text().strip()
    except OSError:
        return None
    return text or None


def render_changelog_summary(old_ref: str | None, new_ref: str | None,
                             tags) -> str:
    """Inv 46 (#924, re-sourced by #931): render a brief, DETERMINISTIC
    post-update changelog summary from the LIVE `vX.Y.Z` release track — the
    git tags carried in the source tree — NOT AI-inferred and NOT the dead
    `release/1.x` root `CHANGELOG.md`.

    `tags` is an iterable of `(tag, subject)` pairs, where `tag` is a release
    tag name (e.g. `v9.4.12`) and `subject` is its annotation subject line.
    Selects the tags whose name parses to a semver (via `_parse_version`,
    Inv 27) STRICTLY newer than `old_ref` and newer-than-or-equal-to `new_ref`
    (`old < tag <= new`), orders them newest-first, names the
    `old_ref -> new_ref` range, lists each selected tag with its subject line
    verbatim, and closes with a pointer to the release history.

    Returns the empty string for a no-op (same version, or no parseable
    upgrade range) — the caller then prints nothing. Pure (no IO, no network,
    no inference). Tags with no parseable semver are skipped.
    """
    if not new_ref:
        return ""
    if old_ref is not None and old_ref == new_ref:
        return ""
    old_t = _parse_version(old_ref)
    new_t = _parse_version(new_ref)
    if old_t is None or new_t is None or not (new_t > old_t):
        return ""

    selected: list[tuple[tuple[int, int, int], str, str]] = []
    for tag, subject in tags:
        tag_t = _parse_version(tag)
        if tag_t is None:
            continue
        if old_t < tag_t <= new_t:
            selected.append((tag_t, tag, subject))
    # Newest-first regardless of the input iteration order.
    selected.sort(key=lambda e: e[0], reverse=True)

    out: list[str] = [f"Changelog {old_ref} -> {new_ref}:"]
    for _t, tag, subject in selected:
        if subject:
            out.append(f"  [{tag}] {subject}")
        else:
            out.append(f"  [{tag}]")
    out.append("See the release history: the vX.Y.Z release tags (git tag).")
    return "\n".join(out)


def _collect_release_tags(src_root: Path) -> list[tuple[str, str]]:
    """Inv 46 (#931): enumerate the `vX.Y.Z` release tags in the source tree
    via a read-only `git -C <src_root> tag`, returning `(tag, subject)` pairs
    where `subject` is each tag's annotation subject line.

    The source tree is the freshly-fetched (or `--src`-supplied) upstream git
    checkout. Best-effort: when git is unavailable, the tree is not a git
    checkout, or the command fails, returns an empty list — the summary then
    degrades cleanly (prints nothing) rather than failing the install.
    """
    try:
        res = subprocess.run(
            ["git", "-C", str(src_root), "tag", "--list", "v[0-9]*",
             "--format=%(refname:short)%09%(contents:subject)"],
            capture_output=True, text=True, check=False,
        )
    except (OSError, ValueError):
        return []
    if res.returncode != 0:
        return []
    tags: list[tuple[str, str]] = []
    for line in res.stdout.splitlines():
        if not line.strip():
            continue
        name, _, subject = line.partition("\t")
        tags.append((name.strip(), subject.strip()))
    return tags


def emit_changelog_summary(old_ref: str | None, new_ref: str | None,
                           src_root: Path) -> None:
    """Inv 46 (#924, re-sourced by #931): print the post-update changelog
    summary to stdout after a successful `--update`, sourcing the text from the
    LIVE `vX.Y.Z` git tags in `<src_root>` — NOT the dead-track root
    `CHANGELOG.md`.

    The source tree is the freshly-fetched (or `--src`-supplied) upstream git
    checkout, so its tags are the just-installed release track. Best-effort:
    when git is unavailable, no tags fall in the range, or the range is a
    no-op, prints nothing — the summary is a courtesy, never a new failure
    mode.
    """
    tags = _collect_release_tags(src_root)
    summary = render_changelog_summary(old_ref, new_ref, tags)
    if summary:
        print(summary)


def run_publish_loop(target_root: str) -> int:
    """Dev-test helper: enumerate every <target_root>/.claude/features/*/feature.json
    and invoke each MANIFEST API via contract.lib.publish. Continues past
    failures; returns the count of failed calls (0 == success).

    Skips features with status == 'retired' and features with no manifest.
    Writes one stderr line per failure naming the feature + API.

    Invoked from main() via canonicalize_installed_surfaces to make the
    installed surfaces canonical (drift-free first Stop, #851), and also
    imported directly by rabbit-cage test suites that exercise the publish
    flow against a freshly copied .claude tree
    (test-deployed-hooks-execute.py, test-install-publish-loop.py).
    """
    contract_dir = os.path.join(target_root, ".claude/features/contract")
    if contract_dir not in sys.path:
        sys.path.insert(0, contract_dir)
    try:
        from lib import publish  # noqa: PLC0415
    except ImportError as e:
        sys.stderr.write(f"install: cannot import contract.lib.publish: {e}\n")
        return 1

    features_root = os.path.join(target_root, ".claude/features")
    if not os.path.isdir(features_root):
        return 0

    failures = 0
    for name in sorted(os.listdir(features_root)):
        fdir = os.path.join(features_root, name)
        fj = os.path.join(fdir, "feature.json")
        if not os.path.isfile(fj):
            continue
        try:
            with open(fj) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            sys.stderr.write(f"install: {name}: malformed feature.json: {e}\n")
            failures += 1
            continue
        if not isinstance(data, dict) or data.get("status") == "retired":
            continue
        manifest = data.get("manifest") or []
        for entry in manifest:
            api_name = entry.get("api", "")
            args = entry.get("args") or {}
            fn = getattr(publish, api_name, None)
            if fn is None:
                sys.stderr.write(
                    f"install: {name}: unknown publish API {api_name!r}\n")
                failures += 1
                continue
            try:
                result = fn(**args, feature_dir=fdir, repo_root=target_root)
            except Exception as e:  # noqa: BLE001
                sys.stderr.write(
                    f"install: {name}::{api_name} raised: {e}\n")
                failures += 1
                continue
            if not getattr(result, "passed", False):
                for msg in getattr(result, "messages", []) or []:
                    sys.stderr.write(f"install: {name}::{api_name}: {msg}\n")
                failures += 1
    return failures


def canonicalize_installed_surfaces(dst_root: Path) -> None:
    """Inv 43: run the publish flow against the freshly installed target so
    every feature's manifest surfaces are CANONICAL — byte-identical to what
    the Stop hook's `check_manifest_drift` republishes from source at runtime.

    Without this, install lays down the COMMITTED deployed surfaces verbatim. A
    committed copy that was never republished after a source change (stale at
    ref-cut time) ships stale, and the user's FIRST Stop runs the same publish
    APIs from source, finds a diff, rebuilds, and reports
    "Surface drift detected - rebuilt: ..." for edits the user never made.
    Republishing here makes the installed surfaces match the runtime republish,
    so the first Stop is a clean no-op.

    Reuses `run_publish_loop`, which walks each installed feature.json manifest
    and invokes the SAME contract-owned publish APIs (`contract.lib.publish`)
    that `check_manifest_drift` calls — install output == runtime-republish
    output by construction.

    Sets `RABBIT_ROOT` in the environment for the duration of the loop so
    `publish_hook` emits the PLUGIN command form (`$RABBIT_ROOT/.claude/hooks/...`)
    — matching the `rewrite_settings_for_plugin` rewrite and what the deployed
    Stop hook (which runs with `RABBIT_ROOT` set) republishes. Without it,
    `publish_hook` would emit the standalone git-rev-parse form and append a
    duplicate entry beside the rewritten one.

    Degrades gracefully: a non-zero failure count (or an unexpected error) is
    reported to stderr but does NOT fail the install — the closure copy already
    laid down working surfaces; canonicalization is a best-effort upgrade.
    """
    prev_rabbit_root = os.environ.get("RABBIT_ROOT")
    os.environ["RABBIT_ROOT"] = str(dst_root.resolve())
    try:
        failures = run_publish_loop(str(dst_root))
    except Exception as e:  # noqa: BLE001 — never let canonicalization break install
        sys.stderr.write(
            f"install: surface canonicalization skipped (publish flow error: "
            f"{e}); installed closure retained\n"
        )
        return
    finally:
        if prev_rabbit_root is None:
            os.environ.pop("RABBIT_ROOT", None)
        else:
            os.environ["RABBIT_ROOT"] = prev_rabbit_root
    if failures:
        sys.stderr.write(
            f"install: surface canonicalization had {failures} publish "
            f"failure(s); installed closure retained (see lines above)\n"
        )


def _publish_settings_merge(src_root: Path, dst_root: Path) -> bool:
    """Inv 22d: route the --update settings.json write through
    contract.lib.publish.publish_settings so non-rabbit hook entries,
    user-added permissions, and user-set env vars survive the refresh.

    The merge reads its source from the feature-local copy under
    <target>/.claude/features/rabbit-cage/settings.json — the closure copy
    that the per-feature refresh loop has already overwritten with the
    fresh upstream bytes. Returns True on success; False on any failure.
    """
    contract_dir = str(dst_root / ".claude/features/contract")
    if contract_dir not in sys.path:
        sys.path.insert(0, contract_dir)
    from lib import publish  # noqa: PLC0415

    feature_dir = str(dst_root / ".claude/features/rabbit-cage")
    result = publish.publish_settings(
        source="settings.json",
        feature_dir=feature_dir,
        repo_root=str(dst_root),
    )
    return bool(result.passed)


def main() -> int:
    parser = argparse.ArgumentParser(description="Install rabbit into a target directory")
    parser.add_argument("--src", required=False, default=None, type=Path,
                        help="extracted upstream source dir (optional under --update; self-fetched if omitted)")
    parser.add_argument("--target", required=False, default=None, type=Path,
                        help="target install dir (optional under --update; inferred from script location if omitted)")
    parser.add_argument("--update", action="store_true",
                        help="refresh an existing install in place (Inv 22)")
    # Inv 27: shell-agnostic ref-selection flags for --update self-fetch.
    # Precedence (highest wins): --version/--ref > --channel main|dev > RABBIT_REF
    # env > dynamic latest-release lookup > HARDCODED_STABLE_DEFAULT offline
    # fallback. The named development channel is main-centric: 'main' resolves to
    # ref 'main' (the default development tip); 'dev' coexists as an explicit
    # opt-in channel during the transition. Never silent 'dev'.
    parser.add_argument("--version", default=None,
                        help="upstream ref to install (branch, tag, or SHA); shell-agnostic alternative to RABBIT_REF env var")
    parser.add_argument("--ref", default=None,
                        help="alias for --version")
    parser.add_argument("--channel", default=None, choices=["stable", "main", "dev"],
                        help="opt-in shorthand: 'main' for the default development tip, 'dev' for the legacy bleeding-edge channel (coexistence), 'stable' for the dynamic latest-release default")
    args = parser.parse_args()
    # Inv 22h skip-condition (i): capture whether --src was explicitly supplied
    # BEFORE the self-fetch branch overwrites args.src. Explicit --src is the
    # dev-test path and the install.sh first-install path; both pin the
    # in-memory code to the source-of-truth content, so no re-exec is needed.
    args.src_was_explicit = args.src is not None

    # Inv 22g: --src and --target are optional under --update; outside --update
    # both remain required (matches the original argparse contract).
    if not args.update:
        missing = []
        if args.src is None:
            missing.append("--src")
        if args.target is None:
            missing.append("--target")
        if missing:
            parser.error("the following arguments are required: " + ", ".join(missing))

    # Inv 22g: tempdir keeps the extracted upstream tarball alive for the
    # entire main() body when self-fetching. Always opened so the cleanup
    # path is uniform; only populated when fetching.
    fetch_tmp = tempfile.TemporaryDirectory()
    try:
        # Inv 22g (a): self-fetch when --update is set and --src is omitted.
        if args.update and args.src is None:
            repo = os.environ.get("RABBIT_REPO", "changyu87/rabbit-workflow")
            # Inv 26/27 precedence ladder. NEVER silent 'dev'. The default
            # (no explicit flag/env) resolves the latest published release
            # DYNAMICALLY, falling back to the hardcoded offline default only
            # when resolution fails (#848).
            if args.version is not None:
                ref = args.version
            elif args.ref is not None:
                ref = args.ref
            elif args.channel == "main":
                ref = "main"
            elif args.channel == "dev":
                ref = "dev"
            elif "RABBIT_REF" in os.environ:
                ref = os.environ["RABBIT_REF"]
            else:
                ref = resolve_latest_release(repo)
                if ref is None:
                    ref = HARDCODED_STABLE_DEFAULT
                    print(
                        f"warning: could not resolve latest release; "
                        f"falling back to {HARDCODED_STABLE_DEFAULT}",
                        file=sys.stderr,
                    )
                # Inv 27 downgrade guard (#850): on the DYNAMIC-DEFAULT channel
                # the update ACTION must track the update-CHECK and never go
                # BACKWARDS. Compare the resolved latest against the currently
                # installed ref (<target>/.version); if it is not strictly
                # newer, do NOT fetch or rewrite anything — report up-to-date and
                # exit 0. Explicit overrides (--version/--ref/--channel dev/
                # RABBIT_REF) bypass this guard above: an explicit operator
                # choice — even an intentional downgrade — is honored verbatim.
                installed_ref = _installed_version_for_update(args)
                if installed_ref is not None and not _is_strictly_newer(
                    ref, installed_ref
                ):
                    print(
                        f"already up to date: installed {installed_ref} is "
                        f"current; latest release {ref} is not newer "
                        f"(no downgrade)"
                    )
                    return 0
            url = f"https://github.com/{repo}/archive/{ref}.tar.gz"
            try:
                fetched = fetch_upstream(repo, ref, Path(fetch_tmp.name))
            except Exception as e:  # noqa: BLE001 — surface root cause uniformly
                print(f"error: fetch failed: {url}: {e}", file=sys.stderr)
                return 1
            args.src = fetched
            # Inv 22e wiring: ensure .version reflects the fetched ref.
            if "RABBIT_INSTALLED_REF" not in os.environ:
                os.environ["RABBIT_INSTALLED_REF"] = ref

        # Inv 22g (b): infer --target from script location when --update is
        # set and --target is omitted. Sanity-check: must look like a rabbit
        # install root (.claude/ + .version present).
        if args.update and args.target is None:
            inferred = Path(__file__).resolve().parent
            if not (inferred / ".claude").exists() or not (inferred / ".version").exists():
                print(
                    f"error: --target inferred as {inferred} is not a rabbit "
                    f"install root (missing .claude/ or .version); pass --target explicitly",
                    file=sys.stderr,
                )
                return 1
            args.target = inferred

        return _main_with_args(args)
    finally:
        fetch_tmp.cleanup()


def _main_with_args(args: argparse.Namespace) -> int:
    src_root: Path = args.src.resolve()
    dst_root: Path = args.target.resolve()

    if not src_root.is_dir():
        print(f"error: --src is not a directory: {src_root}", file=sys.stderr)
        return 1
    if not args.update and dst_root.exists() and any(dst_root.iterdir()):
        print(f"error: --target exists and is not empty: {dst_root}", file=sys.stderr)
        return 1

    # Inv 21 integrity self-check (#880): fail loud — naming the offending
    # path(s) — when the hardcoded closure references a source the extracted
    # tarball does not contain. Without this, copy_one's per-file abort still
    # fires but only as a late "missing required source file" once the loop
    # reaches the stale entry; this surfaces the whole dangling set up front and
    # makes a surface retirement that orphaned a closure entry diagnosable.
    #
    # Inv 49 (#968): the gate is ASYMMETRIC by mode. On --update, the OLD
    # (locally-installed) install.py drives the refresh and validates ITS OWN
    # stale closure — which still names surfaces the NEW source legitimately
    # RETIRED — against that new source. Applied verbatim here, the OLD
    # closure's retired entries would abort as dangling, making any release
    # that crosses a retirement permanently un-installable from an older
    # install. So in the pre-re-exec --update window (self-fetch path: --update,
    # not explicit --src, re-exec guard not yet set) a missing source is a
    # TOLERATED closure SHRINK — dropped, not aborted. The refresh proceeds to
    # the Inv 22h re-exec, after which the NEW install.py runs THIS same check
    # with the re-exec guard set and its OWN corrected closure: a path the NEW
    # closure REQUIRES but the NEW source omits is a real dangling ref and
    # HARD-FAILS. A fresh install and the explicit-src dev path (no skew) keep
    # the strict abort.
    tolerate_shrink = (
        args.update
        and not args.src_was_explicit
        and os.environ.get(_REEXEC_GUARD) != "1"
    )
    missing_sources = check_install_sources_exist(src_root)
    if missing_sources and not tolerate_shrink:
        print(
            "error: install closure references source files absent from --src "
            "(dangling required-file; a retired surface left a stale "
            "install.py entry):",
            file=sys.stderr,
        )
        for rel in missing_sources:
            print(f"  {rel}", file=sys.stderr)
        return 1

    # Inv 22e: print version transition before any refresh so the operator
    # sees the pin movement even if a later copy fails partway through. The
    # captured (old_ref, new_ref) also drive the post-install changelog
    # summary (Inv 46) once the refresh has completed.
    update_old_ref: str | None = None
    update_new_ref: str | None = None
    if args.update:
        version_file = dst_root / ".version"
        if version_file.is_file():
            update_old_ref = version_file.read_text().strip()
            update_new_ref = os.environ.get("RABBIT_INSTALLED_REF", "unknown")
            print(f"updating {update_old_ref} -> {update_new_ref}")

    dst_root.mkdir(parents=True, exist_ok=True)
    ok = True

    # Inv 22h: capture the running install.py bytes BEFORE the SAME_PATH_FILES
    # loop overwrites <dst>/install.py. After the copy, dst/install.py and the
    # running __file__ resolve to the same path on disk; comparing the file to
    # itself is always equal (vacuous skip). Capture pre-copy to detect skew.
    _running_install_bytes: bytes | None = None
    if args.update and not args.src_was_explicit:
        try:
            _running_install_bytes = Path(__file__).resolve().read_bytes()
        except OSError:
            _running_install_bytes = None

    # Inv 22d: on --update, settings.json is written via publish_settings
    # below (merge-aware). On fresh install, it ships via raw copy in the
    # SAME_PATH_FILES loop. Skip the raw copy under --update to avoid
    # clobbering user-added permissions / env vars before the merge runs.
    same_path_to_copy = SAME_PATH_FILES
    if args.update:
        same_path_to_copy = [r for r in SAME_PATH_FILES if r != ".claude/settings.json"]

    for rel in same_path_to_copy:
        ok &= copy_one_tolerant(src_root, dst_root, rel, rel, tolerate_shrink)

    # Inv 22h: re-exec into the freshly-copied <target>/install.py AFTER the
    # SAME_PATH_FILES copy (which wrote the NEW install.py to disk) AND BEFORE
    # the HOOKS / SKILLS / AGENTS / COMMANDS / FEATURE_INCLUDES loops run.
    # Without the re-exec, the in-memory interpreter is still executing the
    # OLD install.py with the OLD FEATURE_INCLUDES / SAME_PATH_FILES constants
    # and every new closure entry added upstream is silently skipped (#297).
    # Skip conditions: not --update, explicit --src, loop-guard already set,
    # byte-identical new install.py.
    if (
        ok
        and args.update
        and not args.src_was_explicit
        and os.environ.get(_REEXEC_GUARD) != "1"
    ):
        new_install_py = dst_root / "install.py"
        if new_install_py.is_file() and (
            _running_install_bytes is None
            or new_install_py.read_bytes() != _running_install_bytes
        ):
            sys.stderr.write(
                f"re-execing into {new_install_py} with updated closure\n"
            )
            sys.stderr.flush()
            os.environ[_REEXEC_GUARD] = "1"
            # Reuse the ALREADY-RESOLVED source tree for the child rather than
            # re-fetching from the network. The parent already self-fetched (or
            # was handed) `src_root`; passing it as an explicit `--src` makes the
            # child validate its NEW closure against the SAME tree the parent
            # fetched (deterministic, no second network round-trip) and skips a
            # redundant second re-exec/self-fetch (explicit `--src` disables
            # both per Inv 22h). Without this, the child re-fetches upstream and
            # would spuriously fail its fresh-install closure check whenever the
            # NEW closure names a surface not yet published upstream.
            child_argv = list(sys.argv[1:])
            if "--src" not in child_argv:
                child_argv += ["--src", str(src_root)]
            os.execv(
                sys.executable,
                [sys.executable, str(new_install_py), *child_argv],
            )
            # os.execv does not return

    for src_rel, dst_rel in HOOKS:
        ok &= copy_one_tolerant(src_root, dst_root, src_rel, dst_rel, tolerate_shrink)

    for src_rel, dst_rel in SKILLS:
        ok &= copy_one_tolerant(src_root, dst_root, src_rel, dst_rel, tolerate_shrink)

    for src_rel, dst_rel in AGENTS:
        ok &= copy_one_tolerant(src_root, dst_root, src_rel, dst_rel, tolerate_shrink)

    for src_rel, dst_rel in COMMANDS:
        ok &= copy_one_tolerant(src_root, dst_root, src_rel, dst_rel, tolerate_shrink)

    for feature, paths in FEATURE_INCLUDES.items():
        base = f".claude/features/{feature}"
        for rel in paths:
            full = f"{base}/{rel}"
            ok &= copy_one_tolerant(src_root, dst_root, full, full, tolerate_shrink)

    if not ok:
        print("install: aborting due to missing source files", file=sys.stderr)
        return 1

    if args.update:
        # Merge runs AFTER the per-feature refresh has overwritten the
        # feature-local source copy with the fresh upstream bytes, so the
        # merge source is the new content.
        if not _publish_settings_merge(src_root, dst_root):
            print("install: publish_settings merge failed", file=sys.stderr)
            return 1

    rewrite_settings_for_plugin(dst_root)
    write_rabbit_gitignore(dst_root)
    write_host_gitignore(dst_root)
    # #1137: on --update, self-heal vendored hosts that already committed a file
    # now in the inner ignore list (e.g. .rabbit-prompt-counter, #1135) by
    # index-only untracking it. Runs AFTER write_rabbit_gitignore so the match
    # set reflects the freshly written list. No-op outside --update / vendored.
    if args.update:
        untrack_ignored_rabbit_ephemerals(dst_root)
    write_version_pin(dst_root, src_root)

    # Inv 43: canonicalize surfaces AFTER rewrite_settings_for_plugin so the
    # publish flow reads the already-rewritten ($RABBIT_ROOT) source settings —
    # matching what check_manifest_drift republishes at runtime — and makes the
    # fresh install drift-free on its first Stop.
    canonicalize_installed_surfaces(dst_root)

    total_files = sum(1 for p in dst_root.rglob("*") if p.is_file())
    print(f"Installed {total_files} files to {dst_root}")

    # Inv 46 (#924, re-sourced by #931): after a successful --update, show a
    # brief, deterministic changelog summary of what changed between the OLD
    # and NEWLY-INSTALLED version, sourced from the live vX.Y.Z git tags in the
    # source tree (NOT inferred, NOT the dead-track root CHANGELOG.md).
    if args.update:
        emit_changelog_summary(update_old_ref, update_new_ref, src_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
