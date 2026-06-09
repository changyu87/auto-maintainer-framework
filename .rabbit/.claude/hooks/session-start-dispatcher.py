#!/usr/bin/env python3
"""session-start-dispatcher.py — Claude Code SessionStart hook dispatcher.

Enumerates every active feature's `feature.json runtime.SessionStart`
declarations, invokes each declared API via `contract.lib.runtime`,
partitions returns into print/inject/ok/error, and emits at most one
JSON object to stdout.

Inv 20 (plugin-mode RABBIT_ROOT check): when running in plugin mode
(detected by presence of <install_root>/.version), appends a banner
payload to the dispatch result if RABBIT_ROOT is unset or mismatched.

Issue #326 / #449 / #629: prepends a 3-row rabbit box around the centered
version at the start of the SessionStart output (top border of 32 🐇, version
row `🐇 rabbit v<version> 🐇` centered in the box, bottom border), and renders
the welcome line PLAIN (brand prefix only — no ✅ icon, no ━━━ bars). The
version shown is the rabbit RELEASE version (the git tag cut by
release-bump.py): read from <install_root>/.version in plugin mode, derived
from `git describe --tags --abbrev=0` in standalone/dev mode, falling back to
"unknown" when neither is resolvable (#629 Defect 1). The version row is
centered across `2*_BOX_WIDTH - 4` DISPLAY columns so the closing 🐇 lands on
the 32-emoji border on the emoji=2-columns common case (#629 Defect 2).

Issue #503: after the welcome block, INVOKES rabbit-auto-evolve's
`scripts/check-auto-resume.py` (a contract INVOKE, not a cross-feature edit)
and, when it reports `{"resume": true, "action": ...}`, appends a branded
resume banner to the systemMessage and injects the `action` into
additionalContext so Claude Code mechanically self-resumes the loop after a
restart. Absent / erroring script degrades gracefully (no resume surfaced).

Issue #545 (Inv 37): also INVOKES rabbit-auto-evolve's
`scripts/advise-restart.py status` and, while the ADVISORY-restart marker is
present, surfaces ONE advisory line in the banner (icon 🔄, distinct from the
hard #503 resume banner so it reads as OPTIONAL) AND THEN clears the marker
(`advise-restart.py clear`) since the advised restart has now occurred. Absent
/ erroring script degrades gracefully (no advisory line, no clear).

Issue #891: after dispatch, reconciles the mode marker written by
contract.lib.runtime.write_mode_marker — which appends `.rabbit` to its
repo_root arg — back to the SINGLE-`.rabbit` canonical path scope-guard reads.
In a plugin install the dispatcher's resolved root IS the `.rabbit` install dir
(RABBIT_ROOT), so the marker would otherwise land one `.rabbit` too deep; the
reconciliation relocates it to `<parent>/.rabbit/.runtime/mode`. No-op in
standalone mode (root is the repo root, not a `.rabbit` dir).

Version: 1.7.0
Owner: rabbit-workflow team (rabbit-cage)
Deprecation criterion: when Claude Code exposes native SessionStart
    dispatchers that subsume this hook.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

_VERSION_UNKNOWN = "unknown"

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from _dispatcher_lib import (  # noqa: E402
    advisory_restart_payloads,
    clear_advisory_restart,
    dispatch_event,
    render_emission,
)
try:
    from restart_snapshot import write_session_snapshot  # noqa: E402
except ImportError:  # helper absent (partial deploy) — degrade gracefully
    def write_session_snapshot(repo_root):  # noqa: D103
        return


def repo_root() -> Path:
    env = os.environ.get("RABBIT_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    try:
        out = subprocess.check_output(
            ["git", "-C", str(here), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        return here


def _canonical_runtime_root(root: str) -> Path:
    """Resolve the single-`.rabbit` runtime root for `root` via rabbit-cage's
    canonical resolver (Inv 52), lazy-imported from the install's feature lib.

    Falls back to the inline basename rule when the resolver cannot be
    imported (degenerate / partial install) so reconciliation still works.
    """
    resolver_path = (
        Path(root) / ".claude" / "features" / "rabbit-cage"
        / "lib" / "runtime_root.py"
    )
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rabbit_cage_runtime_root", str(resolver_path))
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return Path(module.rabbit_runtime_root(str(root)))
    except (FileNotFoundError, ImportError, AttributeError, OSError):
        pass
    root_path = Path(root)
    return root_path if root_path.name == ".rabbit" else root_path / ".rabbit"


def _reconcile_mode_marker(root: str) -> None:
    """Issue #891 / #1046: pin the mode-marker WRITE path to the SAME location
    scope-guard READS, via the canonical runtime-root resolver (Inv 52).

    `contract.lib.runtime.write_mode_marker(repo_root=X)` APPENDS `.rabbit`
    to its `repo_root` arg, writing `<X>/.rabbit/.runtime/mode`. Its
    `repo_root` is therefore contractually the USER-PROJECT root (parent of
    `.rabbit`). But the dispatcher resolves `root` to `RABBIT_ROOT`, which in
    a plugin install IS the `.rabbit` install dir — so the marker lands one
    `.rabbit` too deep at `<RABBIT_ROOT>/.rabbit/.runtime/mode`, while
    scope-guard reads `<git-toplevel>/.rabbit/.runtime/mode` (single
    `.rabbit`) and mis-detects mode.

    A clean fix would have `write_mode_marker` separate its rabbit-meta
    IMPORT root (which must be `RABBIT_ROOT`, since `.claude/features` lives
    under `.rabbit` in a plugin install) from its WRITE root (the
    user-project root). That signature change lives in the contract feature
    (out of rabbit-cage scope), so this helper does the caller-side
    reconciliation: when `root` is itself a `.rabbit` install dir, relocate
    the doubled marker to the canonical single-`.rabbit` path
    `rabbit_runtime_root(root)/.runtime/mode` and prune the stray doubled tree.

    No-op in standalone mode (root basename != `.rabbit`): there the write
    root and the canonical path already coincide.
    """
    root_path = Path(root)
    if root_path.name != ".rabbit":
        return
    doubled = root_path / ".rabbit" / ".runtime" / "mode"
    if not doubled.is_file():
        return
    canonical = _canonical_runtime_root(root) / ".runtime" / "mode"
    if doubled == canonical:
        return
    try:
        canonical.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(doubled), str(canonical))
    except OSError:
        return
    # Prune the now-stray doubled `.rabbit/.runtime` (and `.rabbit`) tree,
    # but only when empty — never remove a dir that holds other state.
    for stray in (doubled.parent, doubled.parent.parent):
        try:
            stray.rmdir()
        except OSError:
            break


def _check_rabbit_root_env():
    """Inv 20: in plugin mode, return banner payload if RABBIT_ROOT env
    is unset or does not match the expected install root. Returns None
    in standalone mode (no .version file) or when env matches.
    """
    install_root = Path(__file__).resolve().parent.parent.parent
    if not (install_root / ".version").is_file():
        return None
    actual = os.environ.get("RABBIT_ROOT", "")
    expected = str(install_root)
    if actual == expected:
        return None
    text = (
        f"RABBIT_ROOT not set or mismatched. Expected: {expected}\n"
        "Exit Claude, run one of:\n"
        f"    setenv RABBIT_ROOT {expected}   (tcsh)\n"
        f"    export RABBIT_ROOT={expected}   (bash/zsh)\n"
        "Then relaunch Claude."
    )
    return {"type": "banner", "text": text, "icon": "🚨", "color": "red"}


def _read_installed_version(root: str) -> str:
    """Resolve the rabbit RELEASE version for the version box (issues #326,
    #629).

    The box must show the rabbit RELEASE version (the git tag cut by
    release-bump.py, e.g. v1.11.0), NOT rabbit-cage's per-feature spec
    version from feature.json (issue #629 Defect 1).

    Plugin mode: read <install_root>/.version (install_root is the deployed
    dispatcher's parent.parent.parent — same anchor Inv 20 uses). The install
    pin already carries the release ref, so this is unchanged.
    Standalone/dev mode (no .version file): derive the latest release tag via
    `git describe --tags --abbrev=0` run against the resolved repo root.
    Returns "unknown" when no .version and no resolvable tag (graceful: no
    crash when git is absent or the tree has no tags).
    """
    install_root = Path(__file__).resolve().parent.parent.parent
    version_file = install_root / ".version"
    if version_file.is_file():
        try:
            raw = version_file.read_text().strip()
            if raw:
                return raw
        except OSError:
            pass
    tag = _git_latest_tag(root)
    if tag:
        return tag
    return _VERSION_UNKNOWN


def _git_latest_tag(root: str):
    """Return the latest git tag (`git describe --tags --abbrev=0`) for the
    repo at `root`, or None when git is absent, the tree is not a repo, or it
    carries no tags. Never raises (graceful degradation, issue #629)."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(root), "describe", "--tags", "--abbrev=0"],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    tag = out.decode().strip()
    return tag or None


_BOX_WIDTH = 32
_BOX_RABBIT = "\U0001f407"


def _version_box(root: str):
    """Return the SessionStart version-box payloads (issues #326, #449).

    Three `subline` payloads forming a rabbit box around the centered
    version. Each row carries the brand prefix `[🐇 rabbit 🐇]` via the
    dispatcher's subline renderer:
      Row 1: top border of 32 🐇
      Row 2: 🐇 + `rabbit v<version>` centered + 🐇
      Row 3: bottom border of 32 🐇
    Prepended ahead of every other SessionStart payload so the box leads
    the welcome block.

    Column math (issue #629 Defect 2): the border is 32 🐇 and each 🐇
    renders as 2 display columns on the emoji=2-cols common case, so a border
    row spans 64 display columns. The middle row carries a side 🐇 each end
    (4 display columns total), so the version label must be centered across
    the inner field of `2*_BOX_WIDTH - 4` (= 60) DISPLAY columns. The label
    is ASCII (1 column per char), so centering it over 60 character columns
    yields 60 display columns; the full row is then 4 + 60 = 64 = the border
    width, keeping the closing 🐇 on the border. ASSUMPTION: a terminal that
    renders 🐇 as 2 columns; perfect alignment is terminal-dependent (some
    terminals render emoji as 1 column), but this matches the common case and
    is the same width model the borders already assume.
    """
    version = _read_installed_version(root)
    label = version if version.startswith("v") else f"v{version}"
    border = _BOX_RABBIT * _BOX_WIDTH
    inner_cols = 2 * _BOX_WIDTH - 4
    centered = f"rabbit {label}".center(inner_cols)
    middle = f"{_BOX_RABBIT}{centered}{_BOX_RABBIT}"
    return [
        {"type": "subline", "text": border, "color": "green"},
        {"type": "subline", "text": middle, "color": "green"},
        {"type": "subline", "text": border, "color": "green"},
    ]


def _strip_welcome_decoration(payloads):
    """Issue #449: render the welcome line PLAIN (brand prefix only).

    The welcome line is produced by contract's `welcome_with_policy` as a
    `banner` payload (icon ✅, ━━━ bars). Convert that single payload to a
    plain `subline` so render_emission drops the icon and bars while
    leaving every other payload untouched. Mutates the list in place.
    """
    for p in payloads:
        if (p.get("type") == "banner"
                and p.get("text") == "Welcome — governing policies loaded"):
            p.clear()
            p["type"] = "subline"
            p["text"] = "Welcome — governing policies loaded"
            p["color"] = "green"
            break
    return payloads


_AUTO_RESUME_SCRIPT = (
    ".claude/features/rabbit-auto-evolve/scripts/check-auto-resume.py"
)


def _auto_resume_payloads(root: str):
    """Issue #503: INVOKE rabbit-auto-evolve's check-auto-resume.py and, when
    it reports `resume: true`, return SessionStart payloads that surface the
    mechanical loop resume.

    Returns a list of two payloads when a resume is due:
      - a `print` banner line for the human (systemMessage), and
      - an `inject` payload carrying the `action` command so Claude Code
        auto-executes it (additionalContext).

    Returns `[]` when no resume is due OR on any failure path (script absent,
    non-zero exit, unparseable / malformed JSON) — graceful degradation that
    leaves the existing SessionStart behaviour unchanged.
    """
    script = Path(root) / _AUTO_RESUME_SCRIPT
    if not script.is_file():
        return []
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    try:
        data = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict) or not data.get("resume"):
        return []
    action = data.get("action")
    if not isinstance(action, str) or not action:
        return []
    banner = {
        "type": "print",
        "text": f"Auto-resuming rabbit-auto-evolve loop — running {action}",
        "icon": "🔄",
        "color": "green",
    }
    inject = {
        "type": "inject",
        "content": (
            "rabbit-auto-evolve auto-resume: a restart-needed tick was "
            f"detected. Run {action} now to resume the autonomous loop.\n"
        ),
    }
    return [banner, inject]


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        sys.stdout.write(
            "session-start-dispatcher.py — Claude Code SessionStart hook.\n"
            "Enumerates every active feature's runtime.SessionStart declarations, "
            "invokes each via contract.lib.runtime, emits at most one JSON "
            "object to stdout.\n"
        )
        return 0
    try:
        sys.stdin.read()
    except Exception:
        pass
    root = str(repo_root())
    payloads = dispatch_event("SessionStart", root)
    _reconcile_mode_marker(root)
    # Inv 54b: baseline the restart-sensitive surface signature for THIS
    # session (a fresh SessionStart = a restart, so it re-baselines and clears
    # any prior mid-session restart advisory).
    write_session_snapshot(root)
    _strip_welcome_decoration(payloads)
    for i, row in enumerate(_version_box(root)):
        payloads.insert(i, row)
    payloads.extend(_auto_resume_payloads(root))
    advisory = advisory_restart_payloads(root)
    if advisory:
        # Surface the advisory line, then consume the marker — the advised
        # restart has now occurred (Inv 37).
        payloads.extend(advisory)
        clear_advisory_restart(root)
    alert = _check_rabbit_root_env()
    if alert is not None:
        payloads.append(alert)
    emission = render_emission(payloads)
    if emission is not None:
        sys.stdout.write(json.dumps(emission) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
