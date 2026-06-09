#!/usr/bin/env python3
# tdd-step.py — read and transition the tdd_state of a feature.
#
# Usage:
#   tdd-step.py show <feature-dir>
#   tdd-step.py next <feature-dir>
#   tdd-step.py transitions <feature-dir>
#   tdd-step.py transition <feature-dir> <new-state> [--force] [--spec-no-change-reason <reason>]
#   tdd-step.py abort <feature-dir> --reason <code>
#
# Exit:
#   0 success
#   1 transition denied or invalid input
#   2 invocation error

import importlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path as _Path

# Pull in the centralized [rabbit] print renderer from the contract feature
# (spec Inv 9). The module always lives at
# <repo_root>/.claude/features/contract/scripts/rabbit_print.py, but tdd-step.py
# itself is published to TWO locations at different depths: the SOURCE copy at
# .claude/features/tdd-subagent/scripts/ and the DEPLOYED copy at
# .claude/agents/tdd-subagent/scripts/ (#561). A fixed `parents[N]` offset only
# resolves correctly from one of them, so we anchor on the repo root instead.
def _contract_scripts_dir():
    # Prefer the cwd-based git toplevel (consistent with the #583 repo-root
    # resolution). RABBIT_ROOT (plugin mode) wins verbatim when set.
    candidates = []
    env = os.environ.get("RABBIT_ROOT")
    if env:
        candidates.append(_Path(env))
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0 and out.stdout.strip():
            candidates.append(_Path(out.stdout.strip()))
    except Exception:
        pass
    for root in candidates:
        cand = root / ".claude" / "features" / "contract" / "scripts"
        if (cand / "rabbit_print.py").is_file():
            return cand
    # Robust fallback: walk upward from the script's own location until a
    # .claude/features/contract/scripts/rabbit_print.py is found. Works from any
    # copy depth without relying on cwd or git.
    here = _Path(__file__).resolve()
    for parent in here.parents:
        cand = parent / ".claude" / "features" / "contract" / "scripts"
        if (cand / "rabbit_print.py").is_file():
            return cand
    # Last resort: the historical source-location offset. Keeps behaviour
    # defined even when nothing above matched.
    return here.parents[2] / "contract" / "scripts"


_CONTRACT_SCRIPTS = _contract_scripts_dir()
if str(_CONTRACT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACT_SCRIPTS))
from rabbit_print import rabbit_block, rabbit_print, rabbit_subline  # noqa: E402



def _rbt_ok(msg):
    sys.stdout.write(msg + "\n")
    sys.stdout.flush()


def _rbt_alert(msg):
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def _repo_root():
    # Resolve the repo root from the CURRENT WORKING DIRECTORY, not from the
    # script's own location (#583). Under worktree-isolated dispatch the
    # subagent runs the MAIN deployed copy of this script while operating in
    # its worktree; resolving via the script dir would yield the MAIN
    # toplevel and leak the scope marker / feature.json bookkeeping into the
    # dispatcher's main tree. The cwd is the worktree under isolation and the
    # main repo on the headless/main path, so cwd-based resolution is correct
    # for both. RABBIT_ROOT (plugin mode) still wins verbatim.
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=False,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


REPO_ROOT = _repo_root()


def resolve_spec_path(feature_dir, name):
    """Resolve a feature spec/contract doc FILE, preferring the flat docs/
    layout and falling back to the specs/ layout.

    `name` is a leaf filename such as "spec.md" or "contract.md".
    Resolution prefers <feature_dir>/docs/<name>; if that file does not
    exist it falls back to <feature_dir>/specs/<name>. When neither exists
    the specs/ candidate is returned, so downstream existence checks report
    a canonical path. The docs/ tree may also hold sibling subdirectories
    (e.g. docs/bugs/); resolution targets the flat docs/<name> file only.

    Logic mirrors contract.lib.checks.resolve_spec_path; duplicated here so
    tdd-step.py has no cross-script import dependency for path resolution.
    """
    docs_candidate = os.path.join(feature_dir, "docs", name)
    if os.path.isfile(docs_candidate):
        return docs_candidate
    return os.path.join(feature_dir, "specs", name)


def usage():
    sys.stderr.write(
        "usage:\n"
        "  tdd-step.py show <feature-dir>\n"
        "  tdd-step.py next <feature-dir>\n"
        "  tdd-step.py transitions <feature-dir>\n"
        "  tdd-step.py transition <feature-dir> <new-state> [--force] [--spec-no-change-reason <reason>]\n"
        "  tdd-step.py abort <feature-dir> --reason <code>\n"
    )


_FORWARD = {
    "spec":           "spec-update",
    "spec-update":    "test-red",
    "test-red":       "impl",
    "impl":           "sync-deployed",
    "sync-deployed":  "test-green",
    "test-green":     "deprecated",
    "deprecated":     "",
}

# Alternate forward transitions. Each state's primary next is in _FORWARD;
# additional valid forward targets (no --force required) are listed here.
# test-green has two valid forward paths: deprecated (retirement) and
# spec-update (next cycle restart, e.g. for rabbit-feature-touch).
_FORWARD_ALT = {
    "test-green": ["spec-update"],
}

_VALID_STATES = {
    "spec", "spec-update", "test-red", "impl",
    "sync-deployed", "test-green", "deprecated",
}


def forward_next(state):
    return _FORWARD.get(state, "")


def forward_targets(state):
    """Return the list of all valid forward targets from `state` (no --force).

    The primary target (from _FORWARD) comes first; alternates follow.
    Returns [] when state is terminal.
    """
    targets = []
    primary = _FORWARD.get(state, "")
    if primary:
        targets.append(primary)
    for alt in _FORWARD_ALT.get(state, []):
        if alt and alt not in targets:
            targets.append(alt)
    return targets


def is_valid_state(state):
    return state in _VALID_STATES


def _feature_json_path(d):
    return os.path.join(d, "feature.json")


def read_state(d):
    fj = _feature_json_path(d)
    if not os.path.isfile(fj):
        sys.stderr.write(f"ERROR: no feature.json in {d}\n")
        return None, 2
    try:
        with open(fj, "r") as f:
            data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"ERROR: failed to parse {fj}: {e}\n")
        return None, 2
    return data.get("tdd_state", "") or "", 0


def write_state(d, new, spec_no_change_reason=""):
    fj = _feature_json_path(d)
    with open(fj, "r") as f:
        data = json.load(f)
    data["tdd_state"] = new
    data["updated"] = date.today().isoformat()
    if spec_no_change_reason:
        data["spec_no_change_reason"] = spec_no_change_reason
    # Atomic write: stage to a temp file in the same directory, then rename.
    # The same-directory tempfile guarantees os.replace is a true atomic rename
    # on POSIX filesystems and avoids cross-device move issues.
    fd, tmp = tempfile.mkstemp(prefix=".feature.json.", dir=os.path.dirname(fj) or ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, fj)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def _load_checks_module(repo_root):
    """Load contract.lib.checks from REPO_ROOT/.claude/features/.

    Returns the module on success or None when the library is unavailable.
    Spec Inv 13: tdd-step.py imports the check functions from
    contract.lib.checks (NOT subprocess to enforcement CLI scripts).
    """
    if not repo_root:
        return None
    features_dir = os.path.join(repo_root, ".claude", "features")
    checks_path = os.path.join(features_dir, "contract", "lib", "checks.py")
    if not os.path.isfile(checks_path):
        return None
    inserted = features_dir not in sys.path
    if inserted:
        sys.path.insert(0, features_dir)
    try:
        return importlib.import_module("contract.lib.checks")
    except Exception:
        return None
    finally:
        # Remove the entry we added so callers that embed tdd-step.py in a
        # long-lived process do not inherit a polluted sys.path (BUG-6).
        if inserted:
            try:
                sys.path.remove(features_dir)
            except ValueError:
                pass


def _run_enforcement_checks(d, repo_root):
    checks = _load_checks_module(repo_root)
    if checks is None:
        return
    template_path = os.path.join(
        repo_root, ".claude", "features", "contract", "templates", "bug-template.json",
    )

    def _emit(result, warn_msg):
        if result is None or not getattr(result, "passed", True):
            if warn_msg:
                _rbt_alert(rabbit_block(rabbit_subline(warn_msg, color="red")))

    def _safe(fn, args, warn_msg):
        try:
            res = fn(*args)
        except Exception:
            res = None
            _emit(res, warn_msg)
            return
        _emit(res, warn_msg)

    _safe(checks.check_tests_non_interactive, (d,),
          f"WARNING: R3 check failed for {d} — tests may have interactive constructs")
    _safe(checks.check_sentinel, (d,), f"WARNING: sentinel check failed for {d}")
    _safe(checks.check_naming, (d,), f"WARNING: naming check failed for {d}")
    _safe(checks.check_imports_resolve, (d,), f"WARNING: R-import-resolve check failed for {d}")
    _safe(checks.check_symlinks_resolve, (repo_root,), "WARNING: symlink-resolve check failed")
    _safe(checks.check_template_producer_consistency, (template_path,),
          "WARNING: template-schema-producer consistency check failed")


def _run_spec_update_checks(d, repo_root):
    """Inv 12: run check_numbered_lists against the feature's spec/contract.

    The spec.md and contract.md files are resolved dual-read (flat docs/
    preferred, specs/ fallback) via resolve_spec_path, with the legacy
    docs/spec/ files retained as a final fallback. Targeting the resolved
    files keeps the flat docs/ layout from sweeping sibling subtrees (e.g.
    docs/bugs/) into the check.

    Non-blocking: a failed CheckResult emits a warning via rabbit_print but
    does not fail the spec-update -> test-red transition.
    """
    checks = _load_checks_module(repo_root)
    if checks is None:
        return
    candidates = [
        resolve_spec_path(d, "spec.md"),
        resolve_spec_path(d, "contract.md"),
        os.path.join(d, "docs", "spec", "spec.md"),
        os.path.join(d, "docs", "spec", "contract.md"),
    ]
    targets = [t for t in candidates if os.path.isfile(t)]
    if not targets:
        return
    try:
        res = checks.check_numbered_lists(targets)
    except Exception:
        return
    if res is None or getattr(res, "passed", True):
        return
    messages = getattr(res, "messages", []) or []
    detail = "; ".join(messages[:3]) if messages else "(no detail)"
    _rbt_alert(rabbit_block(rabbit_subline(
        f"WARNING: numbered-list check failed: {detail}",
        color="red",
    )))


def _post_transition_hooks(cur, new, d):
    """Run state-specific post-write hooks.

    Inv 10: on entry into test-green, run enforcement checks.
    Inv 12: on the spec-update -> test-red edge, run the numbered-list
    check.
    All hooks are best-effort and never block the transition.
    """
    if new == "test-green":
        _post_test_green_hooks(d)
    if cur == "spec-update" and new == "test-red":
        _run_spec_update_checks(d, REPO_ROOT)


def _post_test_green_hooks(d):
    _run_enforcement_checks(d, REPO_ROOT)


# --- abort subcommand helpers (Inv 50–53) -------------------------------

_ABORT_ACCEPTED_STATES = {"test-red", "impl", "sync-deployed"}

# The mode-marker values that select the vendored scope-marker path. The
# on-disk `.runtime/mode` value is being renamed from "plugin" to "vendored";
# every mode comparison that selects the vendored path MUST dual-accept BOTH
# spellings during the coexistence window so the path stays correct before and
# after the rename. Kept in lock-step with dispatch-tdd-subagent.py's
# `_VENDORED_MODES` and rabbit-cage scope-guard's `_VENDORED_MODES`.
_VENDORED_MODES = ("vendored", "plugin")


def _scope_marker_path_for_abort(repo_root, feature_name):
    """Per-mode scope-marker absolute path (Inv 52, mirroring Inv 12).

    Standalone (mode marker absent or not in _VENDORED_MODES):
      <repo_root>/.rabbit-scope-active-<feature>
    Vendored (.rabbit/.runtime/mode or .runtime/mode in _VENDORED_MODES):
      <rabbit_root>/.rabbit/.runtime/scope-active-<feature> (or
      <repo_root>/.runtime/scope-active-<feature> when repo_root IS the
      rabbit-root per Inv 47).

    The mode value is dual-accepted ('vendored' or the legacy 'plugin') to
    match dispatch-tdd-subagent.py._scope_marker_path / scope-guard's
    `_VENDORED_MODES` during the rename coexistence window. A raw
    `== "plugin"` here made a `vendored` marker fall through to the standalone
    path, so scope-guard could not find the marker in vendored installs.

    Logic duplicated from dispatch-tdd-subagent.py._scope_marker_path so
    tdd-step.py has no cross-script import dependency.
    """
    if not repo_root:
        return ""
    candidates = (
        (os.path.join(repo_root, ".runtime", "mode"),
         os.path.join(repo_root, ".runtime", f"scope-active-{feature_name}")),
        (os.path.join(repo_root, ".rabbit", ".runtime", "mode"),
         os.path.join(repo_root, ".rabbit", ".runtime",
                      f"scope-active-{feature_name}")),
    )
    for mode_file, vendored_path in candidates:
        try:
            with open(mode_file) as f:
                if f.read().strip() in _VENDORED_MODES:
                    return vendored_path
        except (OSError, IOError):
            continue
    return os.path.join(repo_root, f".rabbit-scope-active-{feature_name}")


def _remove_scope_marker(path):
    """Best-effort idempotent removal of the scope marker (Inv 52)."""
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        pass


def _rollback_target(feature_data):
    """Compute the abort rollback target (Inv 53).

    If `_pre_touch_state` is present in feature.json, use it; otherwise
    fall back to `test-red` (entry of the executor portion of the cycle).
    """
    pre = feature_data.get("_pre_touch_state")
    if pre and pre in _VALID_STATES:
        return pre
    return "test-red"


def _write_abort_state(feature_dir, new_state):
    """Write the rolled-back tdd_state and remove `_pre_touch_state`.

    Atomic via same-dir tempfile + os.replace, matching write_state().
    """
    fj = _feature_json_path(feature_dir)
    with open(fj, "r") as f:
        data = json.load(f)
    data["tdd_state"] = new_state
    data["updated"] = date.today().isoformat()
    data.pop("_pre_touch_state", None)
    fd, tmp = tempfile.mkstemp(prefix=".feature.json.", dir=os.path.dirname(fj) or ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp, fj)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def cmd_abort(args):
    """abort <feature-dir> --reason <code> (Inv 50–53).

    Exit:
      0 success (state rolled back, marker released)
      1 rejection (current tdd_state not in accepted set, or read error)
      2 invocation error (missing --reason, missing feature-dir, unknown flag)
    """
    if not args:
        usage(); return 2
    d = args[0]
    rest = args[1:]
    reason = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--reason":
            if i + 1 >= len(rest) or not rest[i + 1]:
                sys.stderr.write("ERROR: --reason requires a non-empty code\n")
                return 2
            reason = rest[i + 1]
            i += 2
        else:
            sys.stderr.write(f"ERROR: unknown flag '{a}'\n")
            return 2
    if reason is None:
        sys.stderr.write("ERROR: abort requires --reason <code>\n")
        return 2

    cur, rc = read_state(d)
    if rc != 0:
        return rc
    if cur not in _ABORT_ACCEPTED_STATES:
        sys.stderr.write(
            f"ERROR: abort rejected from tdd_state '{cur}' "
            f"(accepted: {', '.join(sorted(_ABORT_ACCEPTED_STATES))})\n"
        )
        return 1

    # Read feature.json once to compute rollback target.
    fj = _feature_json_path(d)
    try:
        with open(fj, "r") as f:
            data = json.load(f)
    except Exception as e:
        sys.stderr.write(f"ERROR: failed to parse {fj}: {e}\n")
        return 2
    target = _rollback_target(data)

    # Inv 52: release scope marker BEFORE state rollback so a crash mid-abort
    # still leaves the scope unlocked.
    feature_name = data.get("name") or os.path.basename(os.path.abspath(d))
    marker_path = _scope_marker_path_for_abort(REPO_ROOT, feature_name)
    _remove_scope_marker(marker_path)

    # Inv 53: roll back tdd_state and consume _pre_touch_state.
    _write_abort_state(d, target)

    _rbt_ok(rabbit_block(rabbit_print(
        f"ABORTED: {cur.upper()} -> {target.upper()} (reason: {reason})",
        "🛑", "yellow")))
    return 0


# --- end abort helpers --------------------------------------------------


def cmd_show(args):
    if not args:
        usage(); return 2
    d = args[0]
    state, rc = read_state(d)
    if rc != 0:
        return rc
    print(state)
    return 0


def cmd_next(args):
    if not args:
        usage(); return 2
    d = args[0]
    state, rc = read_state(d)
    if rc != 0:
        return rc
    n = forward_next(state)
    if not n:
        sys.stderr.write(f"ERROR: {state} is terminal, no forward state\n")
        return 1
    print(n)
    return 0


def cmd_transitions(args):
    if not args:
        usage(); return 2
    d = args[0]
    state, rc = read_state(d)
    if rc != 0:
        return rc
    targets = forward_targets(state)
    if not targets:
        print("(terminal)")
    else:
        print(" ".join(targets))
    return 0


def cmd_transition(args):
    if len(args) < 2:
        usage(); return 2
    d = args[0]
    new = args[1]
    rest = args[2:]
    force = False
    spec_no_change_reason = ""
    i = 0
    while i < len(rest):
        a = rest[i]
        if a == "--force":
            force = True
            i += 1
        elif a == "--spec-no-change-reason":
            if i + 1 >= len(rest) or not rest[i + 1]:
                sys.stderr.write("ERROR: --spec-no-change-reason requires a non-empty reason\n")
                return 2
            spec_no_change_reason = rest[i + 1]
            i += 2
        else:
            sys.stderr.write(f"ERROR: unknown flag '{a}'\n")
            return 2

    if not is_valid_state(new):
        sys.stderr.write(f"ERROR: '{new}' is not a valid tdd_state\n")
        return 1
    cur, rc = read_state(d)
    if rc != 0:
        return rc
    expected = forward_next(cur)
    valid_forward = forward_targets(cur)

    if cur == "spec-update" and new == "test-red":
        if not spec_no_change_reason:
            spec_diff = ""
            # Dual-read: diff the flat docs/ spec & contract files (preferred
            # target layout) AND the specs/ layout AND the legacy docs/spec/
            # layout. All candidates are passed so a feature on any layout —
            # or mid-migration with changes staged under either path — is not
            # falsely denied.
            spec_pathspecs = [
                os.path.join(d, "docs", "spec.md"),
                os.path.join(d, "docs", "contract.md"),
                os.path.join(d, "specs") + os.sep,
                os.path.join(d, "docs", "spec") + os.sep,
            ]
            try:
                res = subprocess.run(
                    ["git", "-C", REPO_ROOT, "diff", "HEAD", "--", *spec_pathspecs],
                    capture_output=True, text=True, check=False,
                )
                spec_diff = res.stdout if res.returncode == 0 else ""
            except Exception:
                spec_diff = ""
            if not spec_diff:
                sys.stderr.write(
                    "ERROR: spec-update -> test-red requires spec changes (git diff) or --spec-no-change-reason <reason>\n"
                )
                return 1

    if cur == "deprecated":
        sys.stderr.write(f"ERROR: '{cur}' is terminal; cannot transition (even with --force)\n")
        return 1

    if new in valid_forward:
        write_state(d, new, spec_no_change_reason=spec_no_change_reason)
        _post_transition_hooks(cur, new, d)
        _rbt_ok(rabbit_block(rabbit_print(
            f"{cur.upper()} -> {new.upper()}", "🔧", "green")))
        return 0

    if force:
        write_state(d, new, spec_no_change_reason=spec_no_change_reason)
        _post_transition_hooks(cur, new, d)
        _rbt_alert(rabbit_block(rabbit_print(
            f"FORCED: {cur.upper()} -> {new.upper()}", "🔧", "red")))
        _rbt_ok(rabbit_block(rabbit_print(
            f"{cur.upper()} -> {new.upper()}", "🔧", "green")))
        return 0

    forward_msg = " or ".join(valid_forward) if valid_forward else "(terminal)"
    sys.stderr.write(
        f"ERROR: {cur} -> {new} not allowed (forward expected: {forward_msg}). Use --force to override.\n"
    )
    return 1


def main(argv):
    if len(argv) < 1:
        usage(); return 2
    cmd = argv[0]
    rest = argv[1:]
    if cmd == "show":
        return cmd_show(rest)
    if cmd == "next":
        return cmd_next(rest)
    if cmd == "transitions":
        return cmd_transitions(rest)
    if cmd == "transition":
        return cmd_transition(rest)
    if cmd == "abort":
        return cmd_abort(rest)
    if cmd in ("", "-h", "--help", "help"):
        usage()
        return 0
    sys.stderr.write(f"ERROR: unknown subcommand '{cmd}'\n")
    usage()
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except BrokenPipeError:
        sys.exit(0)
