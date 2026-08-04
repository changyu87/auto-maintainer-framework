#!/usr/bin/env python3
"""scope-guard.py v2.6.0 — PreToolUse hook enforcing repo-wide default-deny.

Standalone mode (legacy): any write inside the repo root is denied unless:
  (a) the target basename is on the filename allowlist, or
  (b) a .rabbit-scope-active marker exists in some ancestor of the target, or
  (c) a .rabbit-scope-active-<feature> per-feature marker exists at repo root
      and the target is inside that feature's directory, or
  (d) a .rabbit-scope-override file at repo root grants a bypass: session
      (any write, marker retained), one-time (any single write, consumed), or
      the file-scoped one-time:<repo-relative-path> form (a single write to
      ONLY the declared path, consumed; preferred least-privilege variant), or
  (e) a .rabbit/.runtime/decompose-active marker authorizes the target's
      feature directory (Inv 47): a bounded, auto-cleared pass-through for
      batch work spanning several features — see decompose_authorized().

Plugin mode (Inv 17): when <repo_root>/.rabbit/.runtime/mode contains the
literal string "plugin", scope-guard takes a different branch — see
plugin_decide(). Detection happens per-invocation by reading the mode file.
The decompose-context pass-through (Inv 47) is honored in BOTH modes.

Pre-evaluation (Inv 18): before any decision logic in either mode, the
one-shot bypass-once marker .rabbit/.runtime/scope-bypass-once is consumed
(deleted) if present, and the current write is ALLOWED unconditionally.
Consume-before-evaluate so a failed edit cannot leave a persistent bypass.

Writes outside the repo root are unrestricted.
The .rabbit-scope-active marker file itself is always exempt.
"""

import datetime
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


def _git_toplevel(start: Path) -> Optional[Path]:
    try:
        out = subprocess.check_output(
            ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
        )
        return Path(out.decode().strip())
    except Exception:
        return None


# BUG-57: normalize REPO_ROOT through os.path.realpath so the string-prefix
# comparisons in decide() are not defeated by a symlinked cwd or worktree
# (e.g., /tmp/work -> /var/folders/.../work). Without realpath, abs_path
# may begin with the resolved path while REPO_ROOT remains the symlink, so
# `abs_path.startswith(REPO_ROOT)` returns False and every write is treated
# as "outside the repo" -> silently ALLOWED.
_raw_root = _git_toplevel(Path(__file__).resolve().parent)
REPO_ROOT = Path(os.path.realpath(str(_raw_root))) if _raw_root else None


# Inv 41: rabbit-cage owns three bootstrap files at the repo root, OUTSIDE its
# feature directory: install.sh (the one-liner shell wrapper), install.py, and
# the root README.md (install.py + README.md are publish_file destinations in
# rabbit-cage's manifest; install.sh is the committed bootstrap that drives
# install.py main()). The standalone per-feature marker gate authorizes writes
# only INSIDE the named feature's directory, so a rabbit-cage TDD cycle editing
# these owned root files would otherwise need an ad-hoc override. When the
# active marker is `.rabbit-scope-active-rabbit-cage`, writes to exactly these
# repo-root basenames are authorized with no override. EXPLICIT + MINIMAL: the
# carve-out never broadens to arbitrary root paths, and ONLY rabbit-cage's
# marker authorizes them.
RABBIT_CAGE_OWNED_ROOT = ("install.sh", "install.py", "README.md")


_SPEC_MD_PATTERN = None
_PLUGIN_SPEC_MD_PATTERN = None


# The per-feature documentation home dual-reads three layouts during the
# coexistence window so a mid-migration feature matches the spec-artifact
# carve-out regardless of which layout it currently uses. Mirrors
# contract.lib.checks.resolve_spec_path. Two recognized shapes:
#   - `<dir>/spec.md` where `<dir>` is `specs` (current) or `docs/spec`
#     (older nested) — only the basename `spec.md` is carved out here.
#   - the FLAT `docs/` home: `docs/spec.md`, `docs/contract.md`, and
#     `docs/CHANGELOG.md` are siblings directly under `docs/`. All three
#     spec-artifact basenames are carved out here so the per-feature move of
#     spec + contract + changelog into the flat layout is permitted without
#     a scope marker.
# A later phase drops the legacy `docs/spec/` alternative once every feature
# has migrated to the flat `docs/` home.
_SPEC_DIR_ALT = r"(?:specs|docs/spec)"
_FLAT_DOCS_ARTIFACT_ALT = r"docs/(?:spec|contract|CHANGELOG)\.md"
_SPEC_ARTIFACT_TAIL = (
    r"(?:" + _SPEC_DIR_ALT + r"/spec\.md|" + _FLAT_DOCS_ARTIFACT_ALT + r")"
)


def _spec_md_pattern():
    """Cached regex matching a feature's spec-artifact carve-out paths under
    <REPO_ROOT>/.claude/features/<feature>/.

    <feature> is a single path segment (matched as `[^/]+`). Inv 5: writes to
    the feature's spec artifacts are permitted regardless of scope-marker
    state so rabbit-feature-touch Step 3 spec-authoring can update them
    without an override. The carve-out dual-reads the `specs/spec.md` /
    `docs/spec/spec.md` forms AND the flat `docs/{spec,contract,CHANGELOG}.md`
    layout during the coexistence window.
    """
    global _SPEC_MD_PATTERN
    if _SPEC_MD_PATTERN is None and REPO_ROOT is not None:
        _SPEC_MD_PATTERN = re.compile(
            r"^" + re.escape(str(REPO_ROOT))
            + r"/\.claude/features/[^/]+/" + _SPEC_ARTIFACT_TAIL + r"$"
        )
    return _SPEC_MD_PATTERN


def _plugin_spec_md_pattern():
    """Cached regex matching a plugin feature's spec-artifact carve-out paths
    under <REPO_ROOT>/.rabbit/rabbit-project/features/<feature>/.

    <feature> is a single path segment (matched as `[^/]+`). Inv 17 clause
    (a2): plugin-mode writes to a freshly scaffolded feature's spec artifacts
    are permitted regardless of scope-marker state so the rabbit-spec-creator
    subagent can write initial spec bodies. Mirrors standalone Inv 5, including the flat
    `docs/{spec,contract,CHANGELOG}.md` layout dual-read.
    """
    global _PLUGIN_SPEC_MD_PATTERN
    if _PLUGIN_SPEC_MD_PATTERN is None and REPO_ROOT is not None:
        _PLUGIN_SPEC_MD_PATTERN = re.compile(
            r"^" + re.escape(str(REPO_ROOT))
            + r"/\.rabbit/rabbit-project/features/[^/]+/"
            + _SPEC_ARTIFACT_TAIL + r"$"
        )
    return _PLUGIN_SPEC_MD_PATTERN


def abspath(p: str) -> str:
    # BUG-57: resolve symlinks in the resulting absolute path so REPO_ROOT
    # prefix checks succeed even when cwd is a symlink to the actual repo
    # root. realpath collapses both relative components and intermediate
    # symlinks.
    if p.startswith("/"):
        return os.path.realpath(p)
    return os.path.realpath(os.path.join(os.getcwd(), p))


def find_feature_path(repo_root: Path, feature: str) -> Optional[str]:
    """Run find-feature.py; return repo-relative path or None."""
    script = repo_root / ".claude" / "features" / "contract" / "scripts" / "find-feature.py"
    if not script.exists():
        return None
    try:
        # sys is already imported at module level; do not re-import here.
        out = subprocess.check_output(
            [sys.executable, str(script), str(repo_root), "lookup", feature],
            stderr=subprocess.DEVNULL,
        )
        s = out.decode().strip()
        return s or None
    except Exception:
        return None


def _override_marker_path() -> Optional[Path]:
    """Inv 25: per-mode canonical location for the session-override marker.

    Plugin mode (presence of <REPO_ROOT>/.rabbit/.runtime/mode == "plugin"):
        <REPO_ROOT>/.rabbit/.rabbit-scope-override
    Standalone mode:
        <REPO_ROOT>/.rabbit-scope-override

    Returns None if REPO_ROOT could not be resolved.
    """
    if REPO_ROOT is None:
        return None
    mode_file = REPO_ROOT / ".rabbit" / ".runtime" / "mode"
    if mode_file.is_file():
        try:
            # Inv 49: dual-accept both the new "vendored" value and the legacy
            # "plugin" value (the canonical rename is owned by rabbit-meta).
            if mode_file.read_text().strip() in ("vendored", "plugin"):
                return REPO_ROOT / ".rabbit" / ".rabbit-scope-override"
        except Exception:
            pass
    return REPO_ROOT / ".rabbit-scope-override"


def consume_bypass_once() -> bool:
    """Inv 18: consume-before-evaluate semantics for the one-shot bypass.

    If <repo_root>/.rabbit/.runtime/scope-bypass-once exists, delete it
    FIRST (so failed edits cannot leave a persistent bypass) and return
    True. Returns False when no marker is present.
    """
    if REPO_ROOT is None:
        return False
    marker = REPO_ROOT / ".rabbit" / ".runtime" / "scope-bypass-once"
    if not marker.is_file():
        return False
    try:
        marker.unlink()
    except OSError:
        pass
    return True


def _decompose_expired(expires: Optional[str]) -> bool:
    """Inv 47: return True when an OPTIONAL `expires` ISO-8601 timestamp is
    present AND already in the past. An absent expiry never expires; an
    unparseable expiry is treated as expired (a broken bound disables the
    pass-through rather than granting it indefinitely)."""
    if expires is None:
        return False
    raw = str(expires).strip()
    if not raw:
        return False
    # Accept a trailing 'Z' (Python <3.11 fromisoformat rejects it).
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        deadline = datetime.datetime.fromisoformat(raw)
    except ValueError:
        return True
    now = datetime.datetime.now(datetime.timezone.utc)
    if deadline.tzinfo is None:
        # Naive timestamp — interpret as UTC for the comparison.
        deadline = deadline.replace(tzinfo=datetime.timezone.utc)
    return deadline <= now


def decompose_authorized(abs_path: str) -> bool:
    """Inv 47: decompose-context pass-through.

    Reads <repo_root>/.rabbit/.runtime/decompose-active. When the marker is
    present, well-formed JSON `{operation, features, expires?}`, un-expired,
    and carries a non-empty `features` list, return True if `abs_path` is
    inside any named feature's directory (resolved via find-feature.py, the
    same lookup the per-feature markers use). A malformed, empty-`features`,
    or expired marker is treated as ABSENT (returns False) so a broken or
    stale marker never silently widens scope. The orchestration that wrote
    the marker owns deleting it on completion; scope-guard never mutates it.
    """
    if REPO_ROOT is None:
        return False
    marker = REPO_ROOT / ".rabbit" / ".runtime" / "decompose-active"
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text())
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if _decompose_expired(data.get("expires")):
        return False
    features = data.get("features")
    if not isinstance(features, list) or not features:
        return False
    for feature in features:
        if not isinstance(feature, str) or not feature:
            continue
        rel = find_feature_path(REPO_ROOT, feature)
        if not rel:
            continue
        feature_abs = str(REPO_ROOT) + "/" + rel
        if abs_path == feature_abs or abs_path.startswith(feature_abs + "/"):
            return True
    return False


def plugin_decide(abs_path: str) -> Tuple[bool, str]:
    """Inv 17: plugin-mode decision tree. Called from decide() when
    .rabbit/.runtime/mode == "plugin"."""
    # Carve-outs first: .rabbit/CLAUDE.md and .rabbit/.gitignore are
    # editable user-facing surface even in plugin mode. Inv 25 adds
    # .rabbit/.rabbit-scope-override so the user (or agent) can WRITE
    # the session-override marker at its per-mode canonical location.
    rabbit_root = str(REPO_ROOT) + "/.rabbit"
    if abs_path == rabbit_root + "/CLAUDE.md":
        return True, "ALLOW (plugin carve-out: .rabbit/CLAUDE.md)"
    if abs_path == rabbit_root + "/.gitignore":
        return True, "ALLOW (plugin carve-out: .rabbit/.gitignore)"
    if abs_path == rabbit_root + "/.rabbit-scope-override":
        return True, "ALLOW (plugin carve-out: .rabbit/.rabbit-scope-override)"

    # (a) .rabbit/.claude/** is rabbit's own machinery — DENY always.
    claude_protected = rabbit_root + "/.claude"
    if abs_path == claude_protected or abs_path.startswith(claude_protected + "/"):
        return False, (
            f"DENY write to '{abs_path}' denied: plugin-mode protects "
            f"rabbit's own machinery under '{claude_protected}/'. Edit "
            "user-project files instead."
        )

    # (a1) Decompose-context pass-through (Inv 47). Evaluated AFTER the
    # always-DENY of rabbit's own machinery (so the marker can never grant
    # `.rabbit/.claude/**`) but BEFORE the per-feature marker gate, so batch
    # work across the named feature dirs is authorized without a per-feature
    # scope marker. The marker is honored only while present + un-expired.
    if decompose_authorized(abs_path):
        return True, "ALLOW (decompose-context marker authorizes feature dir)"

    # (a2) Plugin spec-artifact path-pattern carve-out. Evaluated BEFORE
    # the per-feature marker gate so an initial spec write to a freshly
    # scaffolded feature succeeds with no marker. Narrow basename pin —
    # other writes inside the feature dir still flow through (b)/(c).
    plugin_spec_pat = _plugin_spec_md_pattern()
    if plugin_spec_pat and plugin_spec_pat.match(abs_path):
        return True, "ALLOW (plugin path-pattern allowlist: feature spec artifact)"

    # (a-carve-out) .rabbit/rabbit-project/features/<name>/** falls through
    # to the per-feature scope-marker gate (issue #269): these paths hold
    # user-owned plugin feature artifacts (specs, contracts, feature.json
    # scaffolded by rabbit-feature-scaffold) that the dispatcher MUST be
    # able to write to during a normal TDD cycle. Non-features paths under
    # .rabbit/rabbit-project/ (e.g. project-map.json itself) remain
    # always-DENY.
    rp_prefix = rabbit_root + "/rabbit-project"
    if abs_path == rp_prefix or abs_path.startswith(rp_prefix + "/"):
        rp_features_prefix = rp_prefix + "/features/"
        if abs_path.startswith(rp_features_prefix):
            rest = abs_path[len(rp_features_prefix):]
            feature_name = rest.split("/", 1)[0]
            if feature_name:
                marker = (
                    REPO_ROOT / ".rabbit" / ".runtime"
                    / f"scope-active-{feature_name}"
                )
                if marker.is_file():
                    return True, (
                        f"ALLOW (plugin mode: scope-active-{feature_name} "
                        "for rabbit-project feature)"
                    )
                return False, (
                    f"DENY write to '{abs_path}' denied: plugin-mode target "
                    f"matches rabbit-project feature '{feature_name}' but no "
                    f"scope-active marker '.rabbit/.runtime/"
                    f"scope-active-{feature_name}' is present.\n"
                    "\n"
                    "Choose one of the three options below. Both override "
                    "options require explicit in-conversation user "
                    "confirmation and MUST NOT be written speculatively.\n"
                    "\n"
                    "  (1) SESSION OVERRIDE — bypasses scope-guard for the "
                    "entire session. Requires explicit in-conversation user "
                    "confirmation before writing "
                    "'.rabbit/.rabbit-scope-override' with content 'session' "
                    "(Inv 25: plugin-mode canonical location).\n"
                    "\n"
                    "  (2) ONE-TIME OVERRIDE — bypasses scope-guard for a "
                    "single write only. Requires explicit in-conversation "
                    "user confirmation before `touch "
                    ".rabbit/.runtime/scope-bypass-once` (consumed atomically "
                    "by the next scope-guard invocation).\n"
                    "\n"
                    f"  (3) USE rabbit-feature-touch (recommended) — invokes "
                    f"the TDD cycle for feature '{feature_name}', which "
                    f"writes the scope-active marker for you and advances "
                    f"tdd_state."
                )
        # rabbit-project path NOT matching features/<name>/ — always-DENY.
        return False, (
            f"DENY write to '{abs_path}' denied: plugin-mode protects "
            f"rabbit's own machinery under '{rp_prefix}/' (only "
            f"'rabbit-project/features/<name>/**' paths are carved out for "
            "per-feature scope-marker gating)."
        )

    # Load project-map.json. Lazy import so scope-guard does not pay the
    # import cost when no map exists / standalone mode is active.
    try:
        # Module path: .../rabbit-cage/lib/project_map_reader.py
        lib_dir = Path(__file__).resolve().parent.parent / "lib"
        if str(lib_dir) not in sys.path:
            sys.path.insert(0, str(lib_dir))
        import project_map_reader  # type: ignore
    except Exception:
        project_map_reader = None  # type: ignore

    map_dict = None
    if project_map_reader is not None:
        try:
            map_dict = project_map_reader.load_map(str(REPO_ROOT))
        except Exception:
            map_dict = None

    if map_dict is None:
        # (d) No declared features → default-safe ALLOW for any user file.
        return True, "ALLOW (plugin mode: no project-map.json — default safe)"

    matched_feature = None
    try:
        matched_feature = project_map_reader.match_path(
            abs_path, map_dict, str(REPO_ROOT)
        )
    except Exception:
        matched_feature = None

    if matched_feature is None:
        # (d) No matching declared feature → ALLOW.
        return True, "ALLOW (plugin mode: path matches no declared feature)"

    # (b)/(c): does a per-feature scope-active marker exist?
    marker = REPO_ROOT / ".rabbit" / ".runtime" / f"scope-active-{matched_feature}"
    if marker.is_file():
        return True, f"ALLOW (plugin mode: scope-active-{matched_feature})"

    # (c) Declared path, no marker → structured three-option DENY.
    return False, (
        f"DENY write to '{abs_path}' denied: plugin-mode target matches "
        f"declared feature '{matched_feature}' but no scope-active marker "
        f"'.rabbit/.runtime/scope-active-{matched_feature}' is present.\n"
        "\n"
        "Choose one of the three options below. Both override options "
        "require explicit in-conversation user confirmation and MUST NOT "
        "be written speculatively.\n"
        "\n"
        "  (1) SESSION OVERRIDE — bypasses scope-guard for the entire "
        "session. Requires explicit in-conversation user confirmation "
        "before writing '.rabbit/.rabbit-scope-override' with content "
        "'session' (Inv 25: plugin-mode canonical location).\n"
        "\n"
        "  (2) ONE-TIME OVERRIDE — bypasses scope-guard for a single "
        "write only. Requires explicit in-conversation user confirmation "
        "before `touch .rabbit/.runtime/scope-bypass-once` (consumed "
        "atomically by the next scope-guard invocation).\n"
        "\n"
        f"  (3) USE rabbit-feature-touch (recommended) — invokes the TDD "
        f"cycle for feature '{matched_feature}', which writes the "
        f"scope-active marker for you and advances tdd_state."
    )


def decide(target: str) -> Tuple[bool, str]:
    """Decide on one target. Returns (allow, reason_message).
    On deny, reason_message is the full DENY message; on allow, it's the ALLOW reason."""
    abs_path = abspath(target)
    # Resolve symlinks so a symlink into a protected area is caught.
    if os.path.islink(abs_path):
        try:
            abs_path = os.path.realpath(abs_path)
        except Exception:
            pass
    base = os.path.basename(abs_path)

    # 1. Outside the repo entirely -> always allow
    if REPO_ROOT is None or not abs_path.startswith(str(REPO_ROOT) + "/"):
        return True, "ALLOW (outside repo root)"

    # 2. Marker file itself is always exempt
    if base == ".rabbit-scope-active" or base.startswith(".rabbit-scope-active-"):
        return True, "ALLOW (scope marker is exempt)"

    # 3. Allowlisted filenames -> always allow
    if base in ("settings.local.json", ".gitignore", ".rabbit-scope-override"):
        return True, "ALLOW (allowlisted filename)"

    # 3a. Inv 18: .rabbit/.runtime/scope-bypass-once is allowlisted so the
    # user (or a Bash `touch`) can create the marker even in plugin mode
    # where .rabbit/.runtime/** is otherwise denied.
    if abs_path == str(REPO_ROOT) + "/.rabbit/.runtime/scope-bypass-once":
        return True, "ALLOW (scope-bypass-once marker path is allowlisted)"

    # Vendored-mode branch (Inv 17). Read .rabbit/.runtime/mode and dispatch
    # to plugin_decide() when present. Inv 49: dual-accept both the new
    # "vendored" value and the legacy "plugin" value (the canonical rename is
    # owned by rabbit-meta). Otherwise fall through to the standalone tree.
    mode_file = REPO_ROOT / ".rabbit" / ".runtime" / "mode"
    if mode_file.is_file():
        try:
            mode = mode_file.read_text().strip()
        except Exception:
            mode = ""
        if mode in ("vendored", "plugin"):
            return plugin_decide(abs_path)

    # 3b. Path-prefix allowlist — always allow (dispatcher metadata + bug/backlog storage).
    # .rabbit/ is required so rabbit-feature-touch can write
    # .rabbit/impl-suggestion-<feature>.json and .rabbit/tdd-report-<feature>.json
    # during normal feature work without needing a session override.
    # BUG-87: match BOTH the exact directory path AND any path inside it. The
    # exact form (strict equality, never a prefix) is required for directory-
    # creation operations like `mkdir .rabbit` whose target is `.rabbit` with
    # no trailing slash. Strict equality also prevents sibling paths such as
    # `.rabbit-scope-active` and `.rabbit-human-approval-bypass` from matching.
    _ALLOWLIST_PREFIXES = (".claude/bugs", ".claude/backlogs", ".rabbit")
    for _pfx in _ALLOWLIST_PREFIXES:
        _full = str(REPO_ROOT) + "/" + _pfx
        if abs_path == _full or abs_path.startswith(_full + "/"):
            return True, "ALLOW (path-prefix allowlist: bug/backlog/dispatcher metadata)"

    # 3c. Path-pattern allowlist — feature spec artifacts (Inv 5).
    # Permits rabbit-feature-touch Step 3 spec-authoring (which runs in the
    # main session before any per-feature scope marker is set) to write the
    # feature's spec artifacts without an override:
    # `.claude/features/<feature>/specs/spec.md` (or legacy
    # `docs/spec/spec.md`), and the flat `docs/{spec,contract,CHANGELOG}.md`
    # layout. Pattern is narrowly scoped to those exact basenames.
    pattern = _spec_md_pattern()
    if pattern and pattern.match(abs_path):
        return True, "ALLOW (path-pattern allowlist: feature spec artifact)"

    # 3d. Decompose-context pass-through (Inv 47). A bounded, auto-cleared
    # marker authorizing batch work across the named feature dirs; honored
    # only while present + un-expired. Sits alongside the per-feature markers
    # below — additive and coexistent with them and the legacy override.
    if decompose_authorized(abs_path):
        return True, "ALLOW (decompose-context marker authorizes feature dir)"

    # 4a. Per-feature scope markers
    for per_marker in glob.glob(str(REPO_ROOT) + "/.rabbit-scope-active-*"):
        if not os.path.isfile(per_marker):
            continue
        per_feature = os.path.basename(per_marker)[len(".rabbit-scope-active-"):]
        if not per_feature or per_feature == "*":
            continue
        per_path = find_feature_path(REPO_ROOT, per_feature)
        if not per_path:
            # Inv 45: unresolvable marker MUST DENY (not silently fall through).
            # A typo'd or stale per-feature marker would otherwise bypass the
            # write gate. Name the feature and direct the user to verify.
            return False, (
                f"DENY write to '{abs_path}' denied: active scope marker "
                f"'.rabbit-scope-active-{per_feature}' names an unresolvable "
                f"feature '{per_feature}'. Verify the marker name matches "
                f"an existing feature (check .claude/features/ directory) "
                f"and remove the stale marker if needed."
            )
        per_abs = str(REPO_ROOT) + "/" + per_path
        if abs_path.startswith(per_abs):
            return True, f"ALLOW (per-feature scope marker: {per_feature})"
        # Inv 41: rabbit-cage's marker ALSO authorizes its owned repo-root
        # bootstrap files (install.sh, install.py, README.md), which live
        # OUTSIDE its feature directory. Keyed to rabbit-cage's marker alone
        # and to the EXACT owned-root paths — no other feature's marker grants
        # them, and the set never broadens to arbitrary root paths.
        if per_feature == "rabbit-cage" and abs_path in [
            str(REPO_ROOT) + "/" + name for name in RABBIT_CAGE_OWNED_ROOT
        ]:
            return True, (
                "ALLOW (rabbit-cage marker: owned repo-root bootstrap file "
                f"'{base}')"
            )

    # 4. Active scope marker at repo root (the only location ever written).
    scope_marker = REPO_ROOT / ".rabbit-scope-active"
    if scope_marker.is_file():
        scope_feature = ""
        try:
            scope_feature = scope_marker.read_text().strip()
        except Exception:
            pass
        feature_path = find_feature_path(REPO_ROOT, scope_feature) if scope_feature else None
        if scope_feature and not feature_path:
            # Inv 45: global marker names an unresolvable feature → DENY.
            return False, (
                f"DENY write to '{abs_path}' denied: global scope marker "
                f"'.rabbit-scope-active' names an unresolvable feature "
                f"'{scope_feature}'. Verify the marker contents match an "
                f"existing feature (check .claude/features/ directory) "
                f"and remove the stale marker if needed."
            )
        if feature_path:
            feature_abs = str(REPO_ROOT) + "/" + feature_path
            if not abs_path.startswith(feature_abs):
                return False, (
                    f"DENY write to '{abs_path}' denied: outside active scope "
                    f"'{scope_feature}' (allowed: {feature_abs}/). "
                    "Use the rabbit-feature-touch skill for cross-feature work."
                )
            # 4c. Within scoped feature — deny if feature is in test-green state
            feature_json = Path(feature_abs) / "feature.json"
            tdd_state = ""
            try:
                tdd_state = json.loads(feature_json.read_text()).get("tdd_state", "")
            except Exception:
                pass
            if tdd_state == "test-green":
                # Override marker — human-approved bypass of test-green deny
                allow_msg = _consume_override(abs_path)
                if allow_msg:
                    return True, allow_msg
                return False, (
                    f"DENY write to '{abs_path}' denied: feature '{scope_feature}' "
                    "is in test-green state. Invoke the rabbit-feature-touch skill "
                    "to reset the TDD state before editing."
                )
        return True, "ALLOW (under active scope)"

    # 4b-override. Override marker — human-approved bypass for no-scope-marker case
    allow_msg = _consume_override(abs_path)
    if allow_msg:
        return True, allow_msg

    # 5. Default deny — Inv 66: present three explicit options.
    # Force a decision point rather than framing override as a silent
    # procedural next step (the rationalization pattern BUG-1 captured).
    return False, (
        f"DENY write to '{abs_path}' denied: no active scope marker and "
        "file is not on the allowlist (settings.local.json, "
        ".gitignore, .rabbit-scope-override).\n"
        "\n"
        "Choose one of the three options below. Both override options "
        "require explicit in-conversation user confirmation and MUST NOT "
        "be written speculatively.\n"
        "\n"
        "  (1) SESSION OVERRIDE — bypasses scope-guard for the entire "
        "session. Requires explicit in-conversation user confirmation "
        "before writing '.rabbit-scope-override' with content 'session'. "
        "Revoke any time via `/rabbit-cage-config scope-guard on`.\n"
        "\n"
        "  (2) ONE-TIME OVERRIDE — bypasses scope-guard for a single "
        "write only. Requires explicit in-conversation user confirmation "
        "before writing '.rabbit-scope-override' with content 'one-time' "
        "(any single write) or, PREFERRED when the target path is known, the "
        "least-privilege file-scoped form 'one-time:<repo-relative-path>' "
        "(authorizes a single write only to that one declared path).\n"
        "\n"
        "  (3) USE rabbit-feature-touch (recommended) — the correct "
        "governed path for feature edits. Invokes the TDD cycle, advances "
        "tdd_state, and creates a PR; no override needed."
    )


def _consume_override(abs_path: Optional[str] = None) -> Optional[str]:
    """If override file present, consume per its mode and return ALLOW message.

    Inv 25: marker path is per-mode (plugin → <rabbit_root>/.rabbit/.rabbit-scope-override,
    standalone → <repo_root>/.rabbit-scope-override) via _override_marker_path().
    The 'used' sibling marker lives next to the override marker in both modes
    so check_marker_consume_alert (Stop hook) finds it under the same repo_root
    that resolves the override.

    Inv 39: three content forms are recognized — `session` (any write, marker
    retained), bare `one-time` (any single write, marker consumed), and the
    file-scoped form `one-time:<repo-relative-path>` (a single write ONLY to
    the declared path, then consumed). `abs_path` is the candidate write
    target; it is required to evaluate the file-scoped form (the call sites in
    decide() pass abs_path). When `abs_path` does not equal the declared path,
    the file-scoped override does NOT match — None is returned and the marker
    is NOT consumed, so the override never widens beyond its declared path.
    """
    override_file = _override_marker_path()
    if override_file is None:
        return None
    used_file = override_file.parent / ".rabbit-scope-override-used"
    if not override_file.is_file():
        return None
    try:
        raw = override_file.read_text()
    except Exception:
        return None
    # The exact-equality forms tolerate any surrounding whitespace, matching
    # the historical all-whitespace-stripped parse.
    mode = "".join(c for c in raw if not c.isspace())
    if mode == "session":
        return "ALLOW (session override active)"
    if mode == "one-time":
        _consume_marker(override_file, used_file)
        return "ALLOW (one-time override consumed)"
    # Inv 39: file-scoped form `one-time:<repo-relative-path>`. Use a
    # trim-only parse (leading/trailing whitespace) so the declared path is
    # preserved verbatim, then resolve it against REPO_ROOT.
    stripped = raw.strip()
    prefix = "one-time:"
    if stripped.startswith(prefix) and REPO_ROOT is not None:
        rel = stripped[len(prefix):].strip()
        if rel and abs_path is not None:
            declared_abs = os.path.realpath(os.path.join(str(REPO_ROOT), rel))
            if abs_path == declared_abs:
                _consume_marker(override_file, used_file)
                return "ALLOW (file-scoped one-time override consumed)"
        # Declared but non-matching (or no candidate target) → no match, and
        # the marker is left in place so it is not widened or wasted.
        return None
    return None


def _consume_marker(override_file: Path, used_file: Path) -> None:
    """Delete the override marker and create the consumed-alert sibling.

    Shared by bare `one-time` and the file-scoped `one-time:<path>` form so
    both honor the same consume semantics: the marker cannot be reused and the
    Stop-hook check_marker_consume_alert fires on the `used` sibling.
    """
    try:
        override_file.unlink()
    except Exception:
        pass
    try:
        used_file.touch()
    except Exception:
        pass


# ---------- Bash command target extraction ----------

# BUG-72: match `<<` and `<<-` heredoc forms. With `<<-`, bash strips leading
# tab indentation on every body line AND on the closing delimiter, so the
# delimiter may appear indented (preceded by leading whitespace) rather than
# only at column zero. Allow optional leading whitespace before the closing
# delimiter on its own line.
_HEREDOC_RE = re.compile(
    r"<<[- ]*['\"]?([A-Za-z_]\w*)['\"]?[^\n]*\n(.*\n)*?[ \t]*\1\n?",
    re.DOTALL,
)


def _strip_quotes(seg: str) -> str:
    # Remove single-quoted regions
    s = re.sub(r"'[^']*'", " ", seg)
    # Remove double-quoted regions (DOTALL so multi-line strings are fully stripped)
    s = re.sub(r'"[^"]*"', " ", s, flags=re.DOTALL)
    return s


def extract_bash_targets(cmd: str) -> List[str]:
    """Extract write targets from a bash command string (conservative)."""
    # Join backslash-newline continuations so quoted args are on one line
    cmd = cmd.replace("\\\n", " ")
    # Strip heredoc bodies
    cmd = _HEREDOC_RE.sub(" ", cmd)
    # Strip quoted regions on the FULL command BEFORE segment split (Inv 69
    # extended, BUG-9). Per-segment stripping after splitting on ;|& leaves
    # unbalanced-quote segments whenever ;|& appears inside a quoted argument
    # value, causing false-positive write-target extraction.
    cmd = _strip_quotes(cmd)
    # Split on ; | &
    segments = re.split(r"[;|&]", cmd)
    targets: List[str] = []

    for seg in segments:
        seg = seg.lstrip()
        if not seg:
            continue
        stripped = seg

        # > path or >> path
        for m in re.finditer(r">>?\s*([^\s<>|&;]+)", stripped):
            targets.append(m.group(1))

        # tee [flags] path
        for m in re.finditer(r"\btee\s+(?:-[a-z]+\s+)?([^\s<>|&;]+)", stripped):
            targets.append(m.group(1))

        # sed -i ... last-arg
        for m in re.finditer(r"\bsed\s+-i\s+([^\s<>|&;]+(?:\s+[^\s<>|&;]+)*)", stripped):
            parts = m.group(1).split()
            if parts:
                targets.append(parts[-1])

        # cp / mv last arg
        for verb in ("cp", "mv"):
            pattern = (
                r"\b" + verb + r"\s+(?:-[a-zA-Z]+\s+)*"
                r"[^\s<>|&;]+(?:\s+[^\s<>|&;]+)+"
            )
            for m in re.finditer(pattern, stripped):
                parts = [
                    p for p in m.group(0).split()
                    if p not in (verb,)
                    and not p.startswith("-")
                    and not re.match(r"^\d+$", p)
                    and not re.match(r"^\d*>>?", p)
                ]
                if parts:
                    targets.append(parts[-1])

        # Helper: pretokenize for verb-args extraction; drop flag and
        # redirect tokens (e.g., '2>/dev/null', '>foo', '>>bar') so they
        # are not mistaken for write targets of the verb.
        def _verb_args(rest: str) -> list:
            out = []
            for tok in rest.split():
                if not tok or tok.startswith("-"):
                    continue
                if re.match(r"^\d*>>?", tok) or tok.startswith("<"):
                    continue
                out.append(tok)
            return out

        # rm path[s]
        m = re.match(r"\s*rm\s+(.*)", stripped)
        if m:
            targets.extend(_verb_args(m.group(1)))

        # touch path[s]
        m = re.match(r"\s*touch\s+(.*)", stripped)
        if m:
            targets.extend(_verb_args(m.group(1)))

        # mkdir path[s]
        m = re.match(r"\s*mkdir\s+(.*)", stripped)
        if m:
            targets.extend(_verb_args(m.group(1)))

    return targets


def _emit_deny(reason: str) -> None:
    """Emit the PreToolUse deny-shape JSON to stdout (Inv 29 / contract Inv 66 a).

    Claude Code's PreToolUse contract treats a JSON payload with
    permissionDecision == "deny" as a block; exit code is 0. The reason
    string is surfaced verbatim in the tool-call error.
    """
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    sys.stdout.write(json.dumps(payload))


def main() -> int:
    # BUG-48: surface a minimal --help so operators can introspect the hook.
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        sys.stdout.write(
            "scope-guard.py — PreToolUse hook.\n"
            "Reads a tool-invocation JSON payload on stdin and DENIES (exit 2) "
            "writes that fall outside the active scope; otherwise exits 0.\n"
            "Takes no command-line arguments (state lives in repo-root marker "
            "and override files).\n"
        )
        return 0
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        payload = {}

    tool = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}

    # Inv 29: when the intercepted tool is Agent, delegate to
    # contract.lib.checks.validate_agent_prompt_sentinel (contract Inv 66 b)
    # and emit the PreToolUse deny-shape JSON on failure. Pass-through for
    # all other tool names (file-write enforcement below is unchanged).
    if tool == "Agent":
        try:
            contract_lib = (
                Path(__file__).resolve().parent.parent.parent / "contract" / "lib"
            )
            if str(contract_lib) not in sys.path:
                sys.path.insert(0, str(contract_lib))
            import checks as _contract_checks  # type: ignore
        except Exception as exc:
            # The validator is the SOLE source of truth (contract Inv 66 b);
            # do not inline-reimplement on import failure. Emit a deny-shape
            # so the operator sees the failure rather than a silent allow.
            _emit_deny(
                f"scope-guard: cannot import contract.lib.checks "
                f"for Agent sentinel validation: {exc}"
            )
            return 0
        repo_root_str = str(REPO_ROOT) if REPO_ROOT else ""
        result = _contract_checks.validate_agent_prompt_sentinel(
            tool_input, repo_root=repo_root_str
        )
        if not result.passed:
            msg = result.messages[0] if result.messages else "Agent sentinel validation failed"
            _emit_deny(msg)
            return 0
        # Sentinel present (or bypass active) — allow and fall through to
        # any other targets (Agent has no file-write target, so the loop
        # below is a no-op for this tool).

    targets: List[str] = []
    if tool in ("Write", "Edit"):
        t = tool_input.get("file_path", "")
        if t:
            targets.append(t)
    elif tool == "Bash":
        cmd = tool_input.get("command", "")
        if cmd:
            targets.extend(extract_bash_targets(cmd))

    # Inv 18: consume bypass-once BEFORE any decision so failed edits
    # cannot leave a persistent bypass. When consumed, ALLOW unconditionally.
    if consume_bypass_once():
        return 0

    for t in targets:
        if not t:
            continue
        allow, msg = decide(t)
        if not allow:
            sys.stderr.write(f"scope-guard: {msg}\n")
            return 2

    return 0


if __name__ == "__main__":
    sys.exit(main())
