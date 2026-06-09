#!/usr/bin/env python3
"""Companion script for the rabbit-feature-touch SKILL.md.

Owns the orchestration logic that involves computed values and mode-aware
branching, so the SKILL.md body stays script-tier (spec-rules.md §4
Script-Backed Orchestration) instead of carrying bash blocks with runtime
placeholders the model would assemble at invocation time.

Subcommands:

  create-branch [--multi] <feature-name> <request>
      Assemble the deterministic feature-touch branch name from the
      `feat/<feature-name>-<keywords>` pattern (or
      `feat/<feature-name>-multi-<keywords>` with --multi) and start the cycle
      on it (Step 2). `<keywords>` = the first 2–4 request words, lowercased,
      hyphen-joined, with non-alphanumerics stripped. The script owns the
      branch-name computation so the SKILL body stays script-tier (no
      model-assembled `git checkout -b <branch-name>` step). Emits a single
      JSON line `{"branch", "worktree", "mode"}` on success.
      Mode-aware (#1087 / Strategy D, #1085):
        - standalone: plain `git checkout -b <branch>` in the current repo
          (it already has a dedicated repo/HEAD). `worktree` is null.
        - plugin/vendored: the feature-touch git ops would otherwise run on
          the HOST repo's SINGLE shared HEAD, so two concurrent sessions stomp
          each other (#1059). Because the WHOLE `.rabbit/` is tracked
          (Strategy D, shipped by #1086), a worktree of the HOST repo is
          SELF-CONTAINED — tool (`.rabbit/.claude`) AND work
          (`.rabbit/rabbit-project`) at consistent paths — so the proven
          STANDALONE worktree machinery works unchanged. This subcommand
          creates a PER-SESSION git worktree of the host repo OUTSIDE the
          tracked tree (at `<host>/.rabbit-worktrees/session-<token>/`, NEVER
          under `.rabbit/`) via `git worktree add -b <branch> <path> HEAD` and
          emits its path as `worktree`. Each session gets its own HEAD, so
          concurrent sessions never stomp the host HEAD. The caller runs the
          rest of the cycle from `<worktree>/.rabbit`.

  resolve-spec-path <feature-name>
      Print the resolved spec path for a feature. Prefers the flat
      `docs/spec.md` layout (ratified migration target) and falls back only
      to the legacy `docs/spec/spec.md` for any not-yet-migrated nested-docs
      feature. The dead specs/ fallback has been removed. Mode-aware: detects
      standalone vs plugin mode from <repo_root>/.rabbit/.runtime/mode and
      picks the matching feature_dir prefix. In plugin/vendored mode the path
      is emitted relative to the CURRENT WORKING DIRECTORY (the rabbit session
      cwd, which IS the `.rabbit/` install dir), so the consumer
      dispatch-tdd-subagent.py — which resolves --spec against its cwd — finds
      it without a doubled `.rabbit/.rabbit/` prefix (#1061). Standalone mode
      emits repo-root-relative as before (repo_root == cwd there).

  resolve-contract-path <feature-name>
      Like resolve-spec-path, for the contract: prefers flat
      `docs/contract.md`, then the legacy `docs/spec/contract.md`.

  commit-spec <feature-name> <summary>
      Stage and commit the feature's spec change (if any) BEFORE the TDD
      subagent is dispatched (Step 5). Mode-aware:
        - standalone: feature_dir = .claude/features/<name>/, `git add`.
        - plugin:     feature_dir = .rabbit/rabbit-project/features/<name>/,
                      `git add -f` (host .gitignore typically ignores
                      .rabbit/, and without -f the add silently produces an
                      empty staged diff so no commit lands).
      Skips the commit when the staged diff against the resolved spec path
      is empty. Commit message pattern:
      `spec(<feature-name>): update spec for <summary>`.

  dispatch-prompt <feature-name> --spec <path> [--impl-suggestion <path>]
                  [--worktree <path>]
      Assemble the Step-5 tdd-subagent dispatch argv (a single shell-quoted
      command line printed to stdout) that the SKILL body runs to produce the
      subagent prompt. The mode-aware `--worktree` branch is a computed step,
      so per the SKILL.md Authoring Standard (spec-rules.md §4 Script-Backed
      Orchestration) it is OWNED HERE rather than assembled inline in the
      SKILL.md. The argv always carries `--scope <feature-name> --spec <path>`
      (plus `--impl-suggestion <path>` when given). When the create-branch
      JSON's `worktree` value is present (a vendored per-session worktree,
      #1125), the path is resolved to an ABSOLUTE path (the create-branch
      output may be repo-relative like `.rabbit-worktrees/session-xxxx`) and
      `--worktree <abs>` is appended so `dispatch-tdd-subagent.py` anchors
      every emitted LOCK marker / git add+commit / publish repo_root / UNLOCK
      at the worktree (the `--worktree`/`--cwd` arg is OWNED BY tdd-subagent
      and consumed verbatim; Inv 65). When there is NO per-session worktree
      (standalone, or vendored with an empty/absent worktree value) NO
      `--worktree` is appended and the argv is byte-identical to the
      pre-wiring form (hard back-compat requirement).

All paths are resolved relative to the repo root, which the script derives
by walking up from the cwd to the nearest ancestor containing a `.git`
entry (file or directory, so git worktrees are handled).

Version: 0.9.0
Owner: rabbit-workflow team
Deprecation criterion: when feature-touch orchestration is natively handled
by the rabbit CLI or by Claude Code's native workflow mechanism.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
import subprocess
import sys
from pathlib import Path

# Cross-feature Step-5 dispatch prompt assembler (owned by tdd-subagent). This
# feature WIRES the worktree value into its already-shipped --worktree/--cwd
# arg (#1128, Inv 65); it never modifies the script (contract `never` block).
_DISPATCH_TDD_SUBAGENT = (
    ".claude/features/tdd-subagent/scripts/dispatch-tdd-subagent.py"
)


def _repo_root(start: Path) -> Path:
    """Walk up from start to the nearest ancestor containing a `.git` entry."""
    cur = start.resolve()
    for cand in (cur, *cur.parents):
        if (cand / ".git").exists():
            return cand
    # Fall back to cwd if no .git found (e.g. tests in a temp dir).
    return cur


# Plugin-mode content values, dual-accepted. rabbit-meta's `detect_mode`
# emits `vendored` as the current synonym for the legacy `plugin` value
# (#980); scaffold-feature.py dual-accepts both via its own _VENDORED_MODES
# (Inv 44). _mode() mirrors that so a current `vendored` install resolves the
# plugin feature_dir prefix and the `git add -f` staging form instead of
# silently falling through to standalone (#1045).
_VENDORED_MODES = ("vendored", "plugin")


def _mode(repo_root: Path) -> str:
    """Detect rabbit mode from <repo_root>/.rabbit/.runtime/mode.

    Returns 'plugin' when the marker content is a plugin-mode value
    (`vendored` or the legacy `plugin`); otherwise 'standalone' (marker
    absent or any other content).
    """
    marker = repo_root / ".rabbit/.runtime/mode"
    if marker.is_file() and marker.read_text(encoding="utf-8").strip() in _VENDORED_MODES:
        return "plugin"
    return "standalone"


def _feature_dir(repo_root: Path, feature: str, mode: str) -> Path:
    if mode == "plugin":
        return repo_root / ".rabbit/rabbit-project/features" / feature
    return repo_root / ".claude/features" / feature


def _resolve_doc(feature_dir: Path, name: str) -> Path:
    """Resolve a doc surface (spec.md / contract.md) across the doc layouts.

    Preference order:
      1. flat    docs/<name>        (ratified migration target — PREFERRED)
      2. legacy  docs/spec/<name>   (not-yet-migrated nested-docs layout)
    Returns the first that exists; defaults to the flat docs/ target when none
    exists yet (so new resolutions point at the ratified location). The dead
    specs/ fallback is removed — every feature has migrated to flat docs/.
    """
    flat = feature_dir / "docs" / name
    legacy = feature_dir / "docs/spec" / name
    for cand in (flat, legacy):
        if cand.is_file():
            return cand
    return flat


def _spec_path(feature_dir: Path) -> Path:
    """Prefer flat docs/spec.md; fall back to legacy docs/spec/spec.md."""
    return _resolve_doc(feature_dir, "spec.md")


def _contract_path(feature_dir: Path) -> Path:
    """Prefer flat docs/contract.md; fall back to legacy docs/spec/contract.md."""
    return _resolve_doc(feature_dir, "contract.md")


def _emit_relative(base: Path, path: Path) -> None:
    # Emit `base`-relative when possible, else absolute.
    try:
        print(str(path.relative_to(base)))
    except ValueError:
        print(str(path))


def _emit_base(repo_root: Path, mode: str) -> Path:
    """Pick the base directory a resolved doc path is emitted relative to.

    The consumer of resolve-spec-path / resolve-contract-path
    (tdd-subagent's dispatch-tdd-subagent.py) validates and resolves the
    emitted path relative to its CURRENT WORKING DIRECTORY, and it shares the
    rabbit session cwd with this producer. In a vendored install that cwd IS
    the `.rabbit/` install dir, while `repo_root` is the HOST git toplevel
    (the parent of `.rabbit/`). Emitting relative to `repo_root` there yields
    a leading `.rabbit/` the consumer then re-anchors onto its `.rabbit/` cwd
    (`.rabbit/.rabbit/...`), so the file is not found (#1061). Emitting
    relative to cwd in vendored mode strips that prefix
    (`rabbit-project/features/<name>/docs/spec.md`) so the consumer resolves
    it correctly. Standalone mode is unchanged: repo_root == cwd there, so
    repo-root-relative and cwd-relative coincide; we keep repo_root for
    parity with the existing standalone contract.
    """
    if mode == "plugin":
        return Path.cwd().resolve()
    return repo_root


def _keywords(request: str, count: int = 4) -> str:
    """First `count` request words, lowercased, alnum-only, hyphen-joined."""
    words = []
    for tok in request.split():
        cleaned = re.sub(r"[^a-z0-9]", "", tok.lower())
        if cleaned:
            words.append(cleaned)
        if len(words) == count:
            break
    return "-".join(words)


def _rabbit_tracked_at_head(repo_root: Path) -> bool:
    """True when `.rabbit/` is tracked at HEAD (Strategy D precondition).

    A `git worktree add ... HEAD` only carries paths that are committed at
    HEAD. Strategy D (#1086) assumes the WHOLE `.rabbit/` is tracked so the
    new worktree is self-contained (tool + work present). On a fresh vendored
    install where `.rabbit/` is gitignored and not yet committed, HEAD carries
    no `.rabbit/`, so the worktree would be TOOLLESS (#1112).
    """
    r = subprocess.run(
        ["git", "ls-tree", "HEAD", ".rabbit"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return r.returncode == 0 and bool(r.stdout.strip())


def _session_worktrees_exist(repo_root: Path) -> bool:
    """True when at least one per-session worktree dir exists for this host."""
    wt_root = repo_root / ".rabbit-worktrees"
    if not wt_root.is_dir():
        return False
    return any(p.name.startswith("session-") for p in wt_root.iterdir())


def _is_main_worktree(repo_root: Path) -> bool:
    """True when cwd is the host's MAIN worktree (the shared main HEAD).

    In the main worktree `git rev-parse --git-dir` and `--git-common-dir`
    resolve to the same path; in a linked (per-session) worktree they differ.
    """
    def _resolve(flag: str) -> str:
        r = subprocess.run(
            ["git", "rev-parse", flag],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        return str(Path(r.stdout.strip()).resolve()) if r.returncode == 0 else ""

    git_dir = _resolve("--git-dir")
    common_dir = _resolve("--git-common-dir")
    return bool(git_dir) and git_dir == common_dir


def _session_worktree_path(repo_root: Path) -> Path:
    """Per-session worktree path OUTSIDE the tracked tree.

    Sits at `<host>/.rabbit-worktrees/session-<token>/` — a sibling of (NEVER
    under) the tracked `.rabbit/`. The random token keeps two concurrent
    sessions on the same branch keywords from colliding on the path.
    """
    token = secrets.token_hex(4)
    return repo_root / ".rabbit-worktrees" / f"session-{token}"


def cmd_create_branch(feature: str, request: str, multi: bool) -> int:
    keywords = _keywords(request)
    if not keywords:
        sys.stderr.write(
            "ERROR: could not derive branch keywords from request\n"
        )
        return 2
    infix = "-multi" if multi else ""
    branch = f"feat/{feature}{infix}-{keywords}"

    repo_root = _repo_root(Path.cwd())
    mode = _mode(repo_root)

    if mode == "plugin":
        # Strategy D: run the whole cycle in a per-session worktree of the
        # host repo, placed OUTSIDE the tracked .rabbit/ tree, so concurrent
        # vendored sessions never share/stomp the host's single HEAD (#1059).
        # The worktree is self-contained because the whole .rabbit/ is tracked
        # (#1086), so the proven standalone worktree machinery is reused here.
        # #1112 precondition: VERIFY the whole .rabbit/ is actually tracked at
        # HEAD. On a fresh vendored install where .rabbit/ is gitignored and not
        # yet committed, `git worktree add ... HEAD` would yield a TOOLLESS
        # worktree (no .rabbit/ inside), and a subsequent commit-spec from the
        # main-tree cwd would silently land on the host's shared main HEAD. Fail
        # loudly instead of emitting that toolless worktree path.
        if not _rabbit_tracked_at_head(repo_root):
            sys.stderr.write(
                "ERROR: vendored create-branch precondition unmet — `.rabbit/` "
                "is not tracked at HEAD (Strategy D requires the whole "
                "`.rabbit/` to be committed so the per-session worktree is "
                "self-contained). Commit `.rabbit/` first "
                "(e.g. `git add -f .rabbit && git commit -m \"vendor rabbit\"`), "
                "then retry. Refusing to build a toolless worktree (#1112).\n"
            )
            return 1
        worktree = _session_worktree_path(repo_root)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        # Capture git's chatter (it prints "HEAD is now at ..." to stdout) so
        # only the JSON result lands on this script's stdout.
        r = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(worktree), "HEAD"],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
        if r.returncode != 0:
            sys.stderr.write(
                f"ERROR: git worktree add -b {branch} {worktree} failed\n"
                f"{r.stderr}"
            )
            return 1
        print(json.dumps(
            {"branch": branch, "worktree": str(worktree), "mode": "vendored"}
        ))
        return 0

    # Standalone: the repo already has a dedicated HEAD; plain checkout -b.
    r = subprocess.run(
        ["git", "checkout", "-b", branch],
        check=False,
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(f"ERROR: git checkout -b {branch} failed\n{r.stderr}")
        return 1
    print(json.dumps({"branch": branch, "worktree": None, "mode": "standalone"}))
    return 0


def cmd_resolve_spec_path(feature: str) -> int:
    repo_root = _repo_root(Path.cwd())
    mode = _mode(repo_root)
    feature_dir = _feature_dir(repo_root, feature, mode)
    _emit_relative(_emit_base(repo_root, mode), _spec_path(feature_dir))
    return 0


def cmd_resolve_contract_path(feature: str) -> int:
    repo_root = _repo_root(Path.cwd())
    mode = _mode(repo_root)
    feature_dir = _feature_dir(repo_root, feature, mode)
    _emit_relative(_emit_base(repo_root, mode), _contract_path(feature_dir))
    return 0


def cmd_commit_spec(feature: str, summary: str) -> int:
    repo_root = _repo_root(Path.cwd())
    mode = _mode(repo_root)
    feature_dir = _feature_dir(repo_root, feature, mode)

    # #1112 guard: in vendored mode the whole TDD cycle runs inside a
    # per-session worktree (Strategy D). If a per-session worktree exists but
    # this command is running from the host's MAIN worktree (shared main HEAD —
    # e.g. the dispatcher never cd'd into the worktree), committing here would
    # silently pollute the host's main branch and leave the feature branch
    # empty. Refuse and point at the worktree.
    if (
        mode == "plugin"
        and _is_main_worktree(repo_root)
        and _session_worktrees_exist(repo_root)
    ):
        sys.stderr.write(
            "ERROR: refusing to commit the spec onto the host's shared main "
            "HEAD while a per-session worktree exists. Run commit-spec from "
            "inside the worktree's `.rabbit/` directory "
            "(see `.rabbit-worktrees/session-*`), not from the main-tree "
            "`.rabbit/` cwd (#1112).\n"
        )
        return 1

    # Mode-aware staging: plugin mode needs -f because the host .gitignore
    # typically ignores .rabbit/.
    add_cmd = ["git", "add"]
    if mode == "plugin":
        add_cmd.append("-f")
    add_cmd.append(str(feature_dir))
    subprocess.run(add_cmd, cwd=repo_root, check=False)

    spec = _spec_path(feature_dir)
    # Empty-diff skip: only commit when the staged spec actually changed.
    diff = subprocess.run(
        ["git", "diff", "--cached", "--quiet", "--", str(spec)],
        cwd=repo_root,
        check=False,
    )
    if diff.returncode == 0:
        print(f"NOOP: no staged spec change for {feature}; skipping commit")
        return 0

    msg = f"spec({feature}): update spec for {summary}"
    commit = subprocess.run(
        ["git", "commit", "-m", msg], cwd=repo_root, check=False
    )
    if commit.returncode != 0:
        print(f"ERROR: git commit failed for {feature}", file=sys.stderr)
        return 1
    print(f"OK: committed spec change for {feature}")
    return 0


def cmd_dispatch_prompt(
    feature: str,
    spec: str,
    impl_suggestion: str | None,
    worktree: str | None,
) -> int:
    """Assemble the Step-5 tdd-subagent dispatch argv (#1125).

    Prints a single shell-quoted command line to stdout. The argv always
    invokes dispatch-tdd-subagent.py with `--scope`/`--spec` (and
    `--impl-suggestion` when given). When a per-session worktree value is
    present it is resolved to an ABSOLUTE path and appended as
    `--worktree <abs>`, so the emitted prompt anchors the subagent at the
    worktree (Inv 63). When the worktree value is absent/empty NO `--worktree`
    is appended and the argv is byte-identical to the pre-wiring form.
    """
    argv = [
        "python3",
        _DISPATCH_TDD_SUBAGENT,
        "--scope",
        feature,
        "--spec",
        spec,
    ]
    if impl_suggestion:
        argv += ["--impl-suggestion", impl_suggestion]
    # A per-session worktree exists only in vendored mode; standalone (and
    # vendored with no worktree) emits a null/empty value here, so the back-
    # compat path appends nothing. Resolve a possibly repo-relative value to
    # an absolute path so dispatch-tdd-subagent.py anchors its baked slots
    # regardless of the subagent's inherited cwd.
    if worktree:
        argv += ["--worktree", str(Path(worktree).resolve())]
    # shlex.join is 3.8+; this script targets 3.7, so quote-and-join manually.
    print(" ".join(shlex.quote(tok) for tok in argv))
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        sys.stderr.write(
            "usage: feature-touch.py "
            "{create-branch|resolve-spec-path|resolve-contract-path"
            "|commit-spec|dispatch-prompt} ...\n"
            "  create-branch [--multi] <feature-name> <request>\n"
            "  resolve-spec-path <feature-name>\n"
            "  resolve-contract-path <feature-name>\n"
            "  commit-spec <feature-name> <summary>\n"
            "  dispatch-prompt <feature-name> --spec <path> "
            "[--impl-suggestion <path>] [--worktree <path>]\n"
        )
        return 2
    sub = argv[1]
    if sub == "create-branch":
        rest = argv[2:]
        multi = False
        if rest and rest[0] == "--multi":
            multi = True
            rest = rest[1:]
        if len(rest) != 2:
            sys.stderr.write(
                "usage: feature-touch.py create-branch [--multi] "
                "<feature-name> <request>\n"
            )
            return 2
        return cmd_create_branch(rest[0], rest[1], multi)
    if sub == "resolve-spec-path":
        if len(argv) != 3:
            sys.stderr.write(
                "usage: feature-touch.py resolve-spec-path <feature-name>\n"
            )
            return 2
        return cmd_resolve_spec_path(argv[2])
    if sub == "resolve-contract-path":
        if len(argv) != 3:
            sys.stderr.write(
                "usage: feature-touch.py resolve-contract-path <feature-name>\n"
            )
            return 2
        return cmd_resolve_contract_path(argv[2])
    if sub == "commit-spec":
        if len(argv) != 4:
            sys.stderr.write(
                "usage: feature-touch.py commit-spec <feature-name> <summary>\n"
            )
            return 2
        return cmd_commit_spec(argv[2], argv[3])
    if sub == "dispatch-prompt":
        rest = argv[2:]
        if not rest:
            sys.stderr.write(
                "usage: feature-touch.py dispatch-prompt <feature-name> "
                "--spec <path> [--impl-suggestion <path>] [--worktree <path>]\n"
            )
            return 2
        feature = rest[0]
        spec = None
        impl_suggestion = None
        worktree = None
        flags = rest[1:]
        i = 0
        while i < len(flags):
            flag = flags[i]
            if flag in ("--spec", "--impl-suggestion", "--worktree", "--cwd"):
                if i + 1 >= len(flags):
                    sys.stderr.write(f"ERROR: {flag} requires a value\n")
                    return 2
                value = flags[i + 1]
                if flag == "--spec":
                    spec = value
                elif flag == "--impl-suggestion":
                    impl_suggestion = value
                else:  # --worktree / --cwd
                    worktree = value
                i += 2
                continue
            sys.stderr.write(f"ERROR: unknown dispatch-prompt flag: {flag}\n")
            return 2
        if not spec:
            sys.stderr.write("ERROR: dispatch-prompt requires --spec <path>\n")
            return 2
        return cmd_dispatch_prompt(feature, spec, impl_suggestion, worktree)
    sys.stderr.write(f"unknown subcommand: {sub!r}\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
