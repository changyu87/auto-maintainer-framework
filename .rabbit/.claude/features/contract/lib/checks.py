"""contract.lib.checks — library API for contract enforcement / validation.

Holds the logic that used to live in each scripts/enforcement/check-*.py
and scripts/validate-feature.py. Each function returns a CheckResult; no
function calls sys.exit, prints, or raises on contract-violation conditions.

Per spec Inv 29.

Version: 1.2.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when a native rabbit CLI exposes equivalent bindings.
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class CheckResult:
    """Outcome of a single check.

    passed   - True iff the check found no violations.
    messages - Human-readable lines (one per issue or summary line).
    """

    passed: bool
    messages: List[str] = field(default_factory=list)


# ---------- shared helpers ----------------------------------------------------

def get_repo_root():
    env_root = os.environ.get("RABBIT_ROOT")
    if env_root:
        return env_root
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def resolve_spec_path(feature_root, name):
    """Resolve a feature spec/contract doc path, preferring the flat docs/
    layout and falling back to the specs/ layout.

    name is a leaf filename such as "spec.md" or "contract.md". Resolution
    prefers <feature_root>/docs/<name>; if that file does not exist it falls
    back to <feature_root>/specs/<name>. When neither exists the specs/
    candidate is returned, so downstream existence checks report a canonical
    path. The docs/ tree may also hold sibling subdirectories (e.g.
    docs/bugs/); resolution targets the flat docs/<name> file only and never
    such a subdirectory.
    """
    docs_candidate = os.path.join(feature_root, "docs", name)
    if os.path.isfile(docs_candidate):
        return docs_candidate
    return os.path.join(feature_root, "specs", name)


def resolve_changelog_path(feature_root):
    """Resolve a feature's CHANGELOG.md, preferring the flat docs/ layout
    and falling back to the feature-root layout.

    Resolution prefers <feature_root>/docs/CHANGELOG.md; if that file does
    not exist it falls back to <feature_root>/CHANGELOG.md. When neither
    exists the feature-root candidate is returned, so downstream existence
    checks report a canonical path.
    """
    docs_candidate = os.path.join(feature_root, "docs", "CHANGELOG.md")
    if os.path.isfile(docs_candidate):
        return docs_candidate
    return os.path.join(feature_root, "CHANGELOG.md")


# ---------- check_tests_non_interactive --------------------------------------

_INTERACTIVE_PATTERNS = [
    (re.compile(r'(?<![A-Za-z0-9_.])input\s*\('), "bare input() call"),
    (re.compile(r'getpass\s*\.\s*getpass\s*\('), "getpass.getpass()"),
    (re.compile(r'click\s*\.\s*prompt\s*\('), "click.prompt()"),
    (re.compile(r'click\s*\.\s*confirm\s*\('), "click.confirm()"),
]


def _strip_comments(code: str) -> str:
    lines = code.splitlines()
    filtered = [line for line in lines if not re.match(r'^\s*#', line)]
    return "\n".join(filtered)


def check_tests_non_interactive(feature_dir: str) -> CheckResult:
    """Inv 10: <feature-dir>/test/*.py must not use interactive constructs."""
    test_dir = os.path.join(feature_dir, "test")
    if not os.path.isdir(test_dir):
        return CheckResult(True, [f"OK: no test/ in {feature_dir} (vacuous)"])
    messages: List[str] = []
    for dirpath, _, filenames in os.walk(test_dir):
        for fname in filenames:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fname)
            with open(fpath) as f:
                raw = f.read()
            code = _strip_comments(raw)
            for pattern, desc in _INTERACTIVE_PATTERNS:
                if pattern.search(code):
                    messages.append(f"VIOLATION: {fpath} uses {desc}.")
                    break
    if messages:
        messages.append(f"FAIL: {len(messages)} interactive construct(s) found in {test_dir}.")
        return CheckResult(False, messages)
    return CheckResult(True, [f"OK: no interactive constructs in {test_dir}"])


# ---------- check_sentinel ---------------------------------------------------

# The literal sentinel string is owned by contract.lib.policy_block per Inv 46;
# this module is permitted to reference it for two purposes only:
#   (1) check_sentinel — greps dispatcher scripts for the sentinel (Inv 17);
#   (2) validate_agent_prompt_sentinel — validates Agent tool inputs (Inv 56 b).
# Per Inv 56's sentinel-source-uniqueness rule, defining the literal anywhere
# else in the codebase is a contract violation.
_SENTINEL = "RABBIT-POLICY-BLOCK-v1"

# Inv 17's scope is DISPATCH/AGENT-PROMPT SCRIPTS ONLY — the same set Inv 56
# names: scripts that wrap contract's prompt assembler (build-prompt.py) and so
# must reproduce the policy-block sentinel. A dispatch script is identified by
# its source referencing the build-prompt assembler it wraps; ordinary library
# and test source carry no such reference and is OUT of scope (it should not
# carry the sentinel — e.g. lib/publish.py, lib/notification_emitter.py,
# test/run.py). Matching the build-prompt reference rather than the sentinel
# literal keeps the predicate independent of Inv 56's source-uniqueness rule,
# which forbids the literal from appearing in dispatch-script source at all.
_BUILD_PROMPT_REF_RE = re.compile(r"build[-_]prompt")


def _is_dispatch_script(text: str) -> bool:
    """True iff a .py file's source identifies it as a dispatch/agent-prompt
    script (Inv 17 scope): it references the build-prompt assembler it wraps."""
    return bool(_BUILD_PROMPT_REF_RE.search(text))


def check_sentinel(path: str) -> CheckResult:
    """Inv 17: dispatch scripts must contain the policy-block sentinel.

    When PATH is a single file the file is checked directly (callers passing an
    explicit dispatch-script path are not regressed). When PATH is a directory
    the walk requires the sentinel ONLY in dispatch/agent-prompt scripts
    (identified by _is_dispatch_script) — ordinary library and test .py source
    is out of scope and never flagged, matching Inv 17's documented
    dispatch-scripts-only intent and the convention that plain library files
    carry no sentinel.
    """
    if not os.path.exists(path):
        return CheckResult(False, [f"ERROR: not a file or directory: {path}"])
    missing: List[str] = []
    if os.path.isfile(path):
        with open(path) as f:
            if _SENTINEL not in f.read():
                missing.append(f"MISSING sentinel in: {path}")
    else:
        for dirpath, _, filenames in os.walk(path):
            for fname in filenames:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(dirpath, fname)
                with open(fpath) as f:
                    content = f.read()
                if not _is_dispatch_script(content):
                    continue
                if _SENTINEL not in content:
                    missing.append(f"MISSING sentinel in: {fpath}")
    if missing:
        return CheckResult(False, missing)
    return CheckResult(True, [f"OK: sentinel present in {path}"])


# ---------- check_naming -----------------------------------------------------

_BANNED_PREFIXES = ["rbt-"]


def check_naming(root: str) -> CheckResult:
    """Inv 7 + Inv 12: artifact names start with 'rabbit-'; 'rbt-' is banned."""
    if not os.path.isdir(root):
        return CheckResult(False, [f"ERROR: not a directory: {root}"])
    claude_dir = os.path.join(root, ".claude")
    if not os.path.isdir(claude_dir):
        return CheckResult(True, [f"OK: no .claude tree at {root} (vacuous)"])

    messages: List[str] = []
    flagged = set()

    def flag(label: str, name: str, path: str, reason: str) -> None:
        if path in flagged:
            return
        flagged.add(path)
        messages.append(f"VIOLATION: {label} {path} — {reason} ('{name}')")

    for sub, label in (("commands", "command"), ("agents", "agent")):
        d = os.path.join(claude_dir, sub)
        if os.path.isdir(d):
            for fname in os.listdir(d):
                if not fname.endswith(".md"):
                    continue
                base = fname[:-3]
                if base in ("README", "CHANGELOG"):
                    continue
                if not base.startswith("rabbit-"):
                    flag(label, base, os.path.join(d, fname), "must start with 'rabbit-'")

    skills_dir = os.path.join(claude_dir, "skills")
    if os.path.isdir(skills_dir):
        for entry in os.listdir(skills_dir):
            full = os.path.join(skills_dir, entry)
            if not os.path.isdir(full):
                continue
            if not entry.startswith("rabbit-"):
                flag("skill", entry, full + "/", "must start with 'rabbit-'")

    docs_path = os.path.join(claude_dir, "docs")
    for dirpath, dirnames, filenames in os.walk(claude_dir):
        if os.path.abspath(dirpath).startswith(os.path.abspath(docs_path)):
            dirnames.clear()
            continue
        for fname in filenames:
            for bad in _BANNED_PREFIXES:
                if fname.startswith(bad):
                    flag("file", fname, os.path.join(dirpath, fname),
                         f"deprecated '{bad}' prefix banned (use 'rabbit-' or no prefix)")
                    break

    if messages:
        messages.append(f"FAIL: {len(messages)} naming violation(s) under {claude_dir}")
        return CheckResult(False, messages)
    return CheckResult(True, [
        f"OK: all artifacts under {claude_dir} start with 'rabbit-'; "
        f"no banned prefixes ({_BANNED_PREFIXES}) outside docs/"
    ])


# ---------- check_imports_resolve --------------------------------------------

_AT_REL_RE = re.compile(r'@\./([^\s]+)')
_CLAUDE_PATH_RE = re.compile(
    r'\.claude/(?:features|hooks|skills|commands|agents)/[a-z][a-z0-9-]+'
    r'(?:/[^\s`)\]\'",]+)?'
)


def check_imports_resolve(feature_dir: str) -> CheckResult:
    """Inv 22: every @<path> import and .claude/<surface>/<name> path in docs/*.md resolves."""
    docs_dir = os.path.join(feature_dir, "docs")
    if not os.path.isdir(docs_dir):
        return CheckResult(True, [f"OK: no docs/ in {feature_dir} (vacuous)"])
    repo_root = get_repo_root()
    if not repo_root:
        return CheckResult(False, ["ERROR: cannot determine repo root"])

    messages: List[str] = []
    for root, _, files in os.walk(docs_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            filepath = os.path.join(root, fname)
            if "/archive/" in filepath:
                continue
            with open(filepath) as f:
                content = f.read()
            for match in _AT_REL_RE.finditer(content):
                path = match.group(1)
                if "{{" in path:
                    continue
                if not os.path.exists(os.path.join(repo_root, path)):
                    messages.append(f"MISSING: {path} (in {filepath})")
            for match in _CLAUDE_PATH_RE.finditer(content):
                path = match.group(0)
                if "{{" in path:
                    continue
                if not os.path.exists(os.path.join(repo_root, path)):
                    messages.append(f"MISSING: {path} (in {filepath})")
    if messages:
        messages.append("FAIL: one or more import/path references are missing")
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: all import and feature path references resolve"])


# ---------- check_symlinks_resolve -------------------------------------------

def check_symlinks_resolve(root: str) -> CheckResult:
    """Inv 21: no dangling symlinks under <root>/.claude/."""
    claude_dir = os.path.join(root, ".claude")
    if not os.path.isdir(claude_dir):
        return CheckResult(True, [f"OK: no .claude/ at {root} (vacuous)"])
    dangling: List[str] = []
    for dirpath, dirnames, filenames in os.walk(claude_dir, followlinks=False):
        for fname in filenames + dirnames:
            full = os.path.join(dirpath, fname)
            if os.path.islink(full):
                target = os.path.realpath(full)
                if not target or not os.path.exists(target):
                    dangling.append(full)
    if dangling:
        messages = [f"DANGLING: {link}" for link in sorted(dangling)]
        messages.append(f"FAIL: dangling symlinks found under {root}/.claude")
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: all symlinks under .claude/ resolve"])


# ---------- check_template_producer_consistency ------------------------------

# Inv 16: producer-field set MUST be derived from a live source, not hardcoded.
# Live source: bug.json.schema.json properties (the contract schema producers
# write against). Loaded lazily at module-load time from disk; if the schema
# is unreadable the set falls back to an empty set and the check fails loudly
# rather than silently passing.
_BUG_SCHEMA_PATH = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "schemas", "bug.json.schema.json",
))


def _load_producer_fields() -> set:
    try:
        with open(_BUG_SCHEMA_PATH) as f:
            schema = json.load(f)
        return set(schema.get("properties", {}).keys())
    except (OSError, json.JSONDecodeError):
        return set()


_PRODUCER_FIELDS = _load_producer_fields()
_TEMPLATE_METADATA = {"template_version"}


def check_template_producer_consistency(template_path: str) -> CheckResult:
    """Inv 16: template top-level keys are a subset of the live producer field set."""
    try:
        with open(template_path) as f:
            data = json.load(f)
    except Exception as e:
        return CheckResult(False, [f"ERROR: failed to parse {template_path}: {e}"])
    messages: List[str] = []
    for k in data.keys():
        if k in _TEMPLATE_METADATA:
            continue
        if k not in _PRODUCER_FIELDS:
            messages.append(
                f"UNKNOWN KEY: '{k}' in {template_path} is not in the producer field set"
            )
    if messages:
        messages.append("FAIL: template-schema-producer consistency check failed")
        return CheckResult(False, messages)
    return CheckResult(True, [
        "OK: all template keys are consistent with the live producer field set"
    ])


# ---------- check_numbered_lists ---------------------------------------------

# Line-anchored patterns: an ordered-list item or heading whose number uses a
# decimal sub-number or letter suffix. Matched with rx.match() at line start.
_NUMBERED_PATTERNS = [
    (re.compile(r"^\s*#{1,6}\s+\d+\.\d+(?:\.\d+)*\b"), "heading-decimal"),
    (re.compile(r"^\s*#{1,6}\s+\d+[a-z]\b"), "heading-letter"),
    (re.compile(r"^\s*[-*+]?\s*\d+\.\d+(?:\.\d+)*\.\s"), "list-decimal"),
    (re.compile(r"^\s*[-*+]?\s*\d+[a-z][.):]\s"), "list-letter"),
]

# Prose patterns (Inv 26): a numbering keyword (phase|step|item|part|section,
# singular or plural) IMMEDIATELY followed by a fractional (N.M) or
# letter-suffixed (Na / N.a) index anywhere in running text or a table cell.
# Matched with rx.search() on a line whose inline code spans have been
# stripped. The keyword anchor + the requirement that the index directly
# follow the keyword keep legitimate decimal literals out of scope: semver /
# version strings (1.12.0, v1.5.0), schema_version/template_version values
# (2.16.0), invariant references (Inv 26), dotted filenames (foo.1.5.bar),
# currency ($5.50) carry no preceding numbering keyword, and a number that
# does not immediately follow the keyword ("phase completed in 1.5 seconds")
# is excluded by the \s+ adjacency requirement. Documentation examples written
# inside `inline code spans` are stripped before matching, so a feature's own
# spec describing the `phase 1.5` example does not trip its own check.
_NUMBERED_PROSE_PATTERNS = [
    (re.compile(r"\b(?:phase|step|item|part|section)s?\s+\d+\.\d+", re.IGNORECASE),
     "prose-decimal"),
    (re.compile(r"\b(?:phase|step|item|part|section)s?\s+\d+\.?[a-z]\b", re.IGNORECASE),
     "prose-letter"),
]
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_NUMBERED_SKIP = ("/archive/", "/docs/superpowers/")


def _numbered_is_skipped(path: str) -> bool:
    norm = path.replace(os.sep, "/")
    return any(s in norm for s in _NUMBERED_SKIP)


def _numbered_check_file(path: str) -> List[tuple]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        return [(0, "read-error", str(e))]
    violations = []
    in_fence = False
    for i, line in enumerate(lines, start=1):
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        matched = False
        for rx, name in _NUMBERED_PATTERNS:
            if rx.match(line):
                violations.append((i, name, line.rstrip("\n")))
                matched = True
                break
        if matched:
            continue
        # Prose patterns run on the line with inline code spans removed so a
        # numbered index inside backticks (a literal/example) is not flagged.
        bare = _INLINE_CODE_RE.sub("", line)
        for rx, name in _NUMBERED_PROSE_PATTERNS:
            if rx.search(bare):
                violations.append((i, name, line.rstrip("\n")))
                break
    return violations


def _numbered_collect(target: str):
    if os.path.isfile(target):
        if target.endswith(".md") and not _numbered_is_skipped(target):
            yield target
        return
    for dirpath, dirnames, filenames in os.walk(target):
        if _numbered_is_skipped(dirpath + os.sep):
            dirnames[:] = []
            continue
        for fname in filenames:
            if fname.endswith(".md"):
                p = os.path.join(dirpath, fname)
                if not _numbered_is_skipped(p):
                    yield p


def check_numbered_lists(targets: List[str]) -> CheckResult:
    """Inv 26: reject decimal/letter-suffix numbering in Markdown ordered
    lists & headings, AND prose-embedded fractional/letter numbering anchored
    to a numbering keyword (phase|step|item|part|section)."""
    messages: List[str] = []
    for target in targets:
        if not os.path.exists(target):
            messages.append(f"ERROR: not a file or directory: {target}")
            continue
        for md in _numbered_collect(target):
            for line_no, name, content in _numbered_check_file(md):
                messages.append(f"{md}:{line_no}: {name} {content}")
    if messages:
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: no numbered-list violations"])


# ---------- check_invariant_monotonic_order ----------------------------------

# Features listed here are skipped by check_invariant_monotonic_order while
# their spec.md still has out-of-order invariant numbering. Add an entry only
# for a feature pending a renumber; remove it once that renumber lands. The
# list is empty — the check fully validates every feature on disk without skips.
_MONOTONIC_KNOWN_ISSUES = []

_INVARIANTS_HEADING_RE = re.compile(r"^(##|###)\s+Invariants\b")
_ANY_HEADING_RE = re.compile(r"^(#{1,6})\s+")
_NUMBERED_ITEM_RE = re.compile(r"^(\d+)\.\s")


def _contiguous_opt_in(feat_dir: str) -> bool:
    """Return True iff the feature has opted into the strict CONTIGUOUS tier.

    Opt-in is data-driven and per-feature (mirrors the Inv 41
    `housekeeping_clean` strict-tier opt-in): a feature opts in by declaring
    `"contiguous_invariants": true` at the top level of its OWN feature.json.
    Absent/false ⇒ baseline (strictly-increasing, gaps-tolerated) tier only.

    A test-only env override `RABBIT_CONTIGUOUS_INVARIANTS` (comma-separated
    feature names) REPLACES the feature.json-derived opt-in set so a hermetic
    fixture test can drive the strict tier without writing feature.json; it
    is never set in production runs. Mirrors the Inv 41
    `RABBIT_HISTORICAL_TAGS_CLEANED` override.
    """
    override = os.environ.get("RABBIT_CONTIGUOUS_INVARIANTS")
    feat_name = os.path.basename(os.path.realpath(feat_dir))
    if override is not None:
        names = {n.strip() for n in override.split(",") if n.strip()}
        return feat_name in names
    fj_path = os.path.join(feat_dir, "feature.json")
    if not os.path.isfile(fj_path):
        return False
    try:
        with open(fj_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("contiguous_invariants") is True)


def check_invariant_monotonic_order(feature_dirs: List[str]) -> CheckResult:
    """Inv 30: each '## Invariants' / '### Invariants' section's top-level
    numbered items MUST appear in strictly increasing order (baseline tier,
    all features); features that opt in via `feature.json
    "contiguous_invariants": true` are ADDITIONALLY required to number
    contiguously 1..N with no holes (strict tier).

    feature_dirs - iterable of feature directory paths. Features named in
    _MONOTONIC_KNOWN_ISSUES are skipped (pending renumber). Features without
    a spec.md (resolved via resolve_spec_path — docs/ preferred, specs/
    fallback) are also silently skipped.

    Returns CheckResult; messages include 'SKIP:' lines for skipped features
    and 'VIOLATION:' lines for any non-monotonic step or (opted-in) hole.
    """
    messages: List[str] = []
    violations: List[str] = []

    for feat_dir in feature_dirs:
        feat_name = os.path.basename(os.path.realpath(feat_dir))
        if feat_name in _MONOTONIC_KNOWN_ISSUES:
            messages.append(
                f"SKIP: {feat_name} (in KNOWN_ISSUES — pending renumber)"
            )
            continue
        spec_path = resolve_spec_path(feat_dir, "spec.md")
        if not os.path.isfile(spec_path):
            continue
        try:
            with open(spec_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except (OSError, UnicodeDecodeError) as e:
            violations.append(f"VIOLATION: {feat_name}: cannot read spec.md: {e}")
            continue

        contiguous = _contiguous_opt_in(feat_dir)
        in_section = False
        section_header = None
        prev_num = 0
        in_fence = False
        for line in lines:
            stripped = line.strip()
            # track fenced code blocks so we don't count list-like content inside them
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            # any heading ends or potentially opens a section
            if _ANY_HEADING_RE.match(line):
                if _INVARIANTS_HEADING_RE.match(line):
                    in_section = True
                    section_header = stripped
                    prev_num = 0
                else:
                    in_section = False
                    section_header = None
                    prev_num = 0
                continue
            if not in_section:
                continue
            m = _NUMBERED_ITEM_RE.match(line)
            if m:
                n = int(m.group(1))
                if n <= prev_num:
                    violations.append(
                        f"VIOLATION: {feat_name}:{section_header}: "
                        f"{prev_num} → {n} not monotonic"
                    )
                elif contiguous and n != prev_num + 1:
                    missing = ", ".join(str(g) for g in range(prev_num + 1, n))
                    violations.append(
                        f"VIOLATION: {feat_name}:{section_header}: "
                        f"{prev_num} → {n} not contiguous (missing {missing}); "
                        f"feature opted into contiguous_invariants — "
                        f"run scripts/reflow-invariants.py"
                    )
                prev_num = n

    if violations:
        return CheckResult(False, messages + violations)
    if not messages:
        messages.append("OK: no feature dirs supplied (vacuous)")
    else:
        messages.append("OK: all checked features monotonic")
    return CheckResult(True, messages)


# ---------- validate_feature -------------------------------------------------


def _build_schema_resolver(schemas_dir, root_schema):
    """Build a jsonschema RefResolver that resolves the sibling $refs in
    feature.json.schema.json against the local schemas/ directory.

    feature.json.schema.json uses RELATIVE $refs (e.g. "manifest.schema.json")
    to its sibling schema files. Out of the box jsonschema treats those as
    remote URLs and tries to fetch them, raising RefResolutionError. We
    preload every sibling *.schema.json into the resolver's store keyed by
    BOTH its bare filename (the literal $ref string) and its declared $id, and
    set the resolver's base URI to the schemas/ dir (file:// scheme) so any
    other relative ref also resolves locally without a network round-trip.
    """
    import jsonschema  # type: ignore

    store = {}
    for entry in os.listdir(schemas_dir):
        if not entry.endswith(".json"):
            continue
        path = os.path.join(schemas_dir, entry)
        try:
            with open(path) as f:
                doc = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        store[entry] = doc
        doc_id = doc.get("$id")
        if isinstance(doc_id, str) and doc_id:
            store[doc_id] = doc

    base_uri = Path(schemas_dir).as_uri() + "/"
    return jsonschema.RefResolver(base_uri=base_uri, referrer=root_schema, store=store)


_VALID_TDD_STATES = {"spec", "spec-update", "test-red", "impl", "sync-deployed",
                     "test-green", "review", "merged", "deprecated"}


def validate_feature(feature_dir: str) -> CheckResult:
    """Validate a feature directory against the feature-skeleton schema.

    Returns CheckResult(passed=True, [...]) on success.
    Returns CheckResult(passed=False, [...]) on validation errors.
    Retired features (status=retired) short-circuit to passed=True with a
    RETIRED: notice (spec Inv 28b).
    """
    if not feature_dir:
        return CheckResult(False, ["ERROR: feature_dir is empty"])
    if not os.path.isdir(feature_dir):
        return CheckResult(False, [f"ERROR: not a directory: {feature_dir}"])

    expected_name = os.path.basename(os.path.realpath(feature_dir))
    errors: List[str] = []

    feature_json_path = os.path.join(feature_dir, "feature.json")

    # Inv 28b: retired feature short-circuit
    if os.path.isfile(feature_json_path):
        try:
            with open(feature_json_path) as f:
                early = json.load(f)
            if early.get("status") == "retired":
                return CheckResult(True, [
                    f"RETIRED: {feature_dir} (status=retired; structural checks skipped)"
                ])
        except (json.JSONDecodeError, OSError):
            pass

    def err(msg: str) -> None:
        errors.append(f"ERROR: {msg}")

    # Required files / dirs
    if not os.path.isfile(feature_json_path):
        err("missing feature.json")
    # Spec/contract resolution prefers the flat docs/ layout and falls back
    # to specs/ (resolve_spec_path). Error messages name the specs/ candidate
    # returned when neither layout exists.
    spec_md = resolve_spec_path(feature_dir, "spec.md")
    if not os.path.isfile(spec_md):
        err("missing specs/spec.md")
    elif os.path.getsize(spec_md) == 0:
        err("specs/spec.md is empty")
    contract_md = resolve_spec_path(feature_dir, "contract.md")
    if not os.path.isfile(contract_md):
        err("missing specs/contract.md")
    elif os.path.getsize(contract_md) == 0:
        err("specs/contract.md is empty")
    run_py = os.path.join(feature_dir, "test", "run.py")
    if not os.path.isfile(run_py):
        err("missing test/run.py")
    elif not os.access(run_py, os.X_OK):
        err("test/run.py not executable")

    if not os.path.isfile(feature_json_path):
        errors.append(f"FAIL: {len(errors)} error(s) in {feature_dir}")
        return CheckResult(False, errors)

    try:
        with open(feature_json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        err("feature.json is not valid JSON")
        errors.append(f"FAIL: {len(errors)} error(s) in {feature_dir}")
        return CheckResult(False, errors)

    # Schema validation (jsonschema optional dep)
    schemas_dir = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "schemas",
    ))
    schema_path = os.path.join(schemas_dir, "feature.json.schema.json")
    if os.path.isfile(schema_path):
        try:
            import jsonschema  # type: ignore
            with open(schema_path) as f:
                schema = json.load(f)
            # feature.json.schema.json carries RELATIVE sibling $refs
            # (manifest/runtime/configuration/prompts.schema.json). A bare
            # jsonschema.validate() cannot resolve those and raises
            # RefResolutionError — NOT a ValidationError — the moment it
            # descends into one (i.e. for any feature.json with a manifest).
            # Build a RefResolver whose store preloads every sibling schema
            # so refs resolve locally (no network), then validate against it.
            resolver = _build_schema_resolver(schemas_dir, schema)
            validator = jsonschema.Draft7Validator(schema, resolver=resolver)
            try:
                first = next(iter(validator.iter_errors(data)), None)
                if first is not None:
                    err(f"feature.json: schema violation: {first.message}")
            except jsonschema.exceptions.RefResolutionError as e:
                err(f"feature.json: schema ref could not be resolved: {e}")
        except ImportError:
            pass

    name = data.get("name", "")
    if not name:
        err("feature.json: missing name")
    elif name != expected_name:
        err(f"feature.json: name '{name}' does not match directory name '{expected_name}'")

    version = data.get("version", "")
    if not version:
        err("feature.json: missing version")
    elif not re.match(r"^\d+\.\d+\.\d+$", version):
        err(f"feature.json: version '{version}' is not semver (X.Y.Z)")

    owner = data.get("owner", "")
    if not owner:
        err("feature.json: missing owner")
    elif isinstance(owner, dict):
        err("feature.json: owner must be a flat string, not an object")

    tdd_state = data.get("tdd_state", "")
    if not tdd_state:
        err("feature.json: missing tdd_state")
    elif tdd_state not in _VALID_TDD_STATES:
        err(f"feature.json: invalid tdd_state '{tdd_state}' "
            f"(allowed: {' | '.join(sorted(_VALID_TDD_STATES))})")

    if not data.get("summary", ""):
        err("feature.json: missing summary")

    # `surface` is DEPRECATED: `manifest` is the single source of
    # truth for a feature's published artifacts. `surface` is no longer
    # required — its absence is valid. When a feature still carries it (for
    # backwards compatibility during the migration sweep), it MUST remain a
    # well-formed object so a malformed legacy mirror is still caught; but a
    # feature.json that omits it entirely validates cleanly.
    if "surface" in data:
        surface = data["surface"]
        if not isinstance(surface, dict):
            err("feature.json: surface (deprecated) must be an object when present")
        else:
            for key in ("hooks", "commands", "agents", "skills"):
                if key in surface and not isinstance(surface[key], list):
                    err(f"feature.json: surface.{key} must be an array")

    if not data.get("deprecation_criterion", ""):
        err("feature.json: missing deprecation_criterion")

    if errors:
        errors.append(f"FAIL: {len(errors)} error(s) in {feature_dir}")
        return CheckResult(False, errors)
    return CheckResult(True, [f"PASS: {feature_dir}"])


# ---------------------------------------------------------------------------
# Meta-contract validation: manifest, runtime, and configuration arms
# are validated independently; each section is optional in feature.json.
# ---------------------------------------------------------------------------

_PUBLISH_API_ENUM = frozenset({
    "publish_skill",
    "publish_command",
    "publish_agent",
    "publish_hook",
    "publish_settings",
    "publish_file",
    "publish_generated",
})


def _validate_manifest(manifest):
    """Validate a manifest declaration. Returns list of error message strings."""
    errors = []
    if not isinstance(manifest, list):
        errors.append(f"manifest must be an array, got {type(manifest).__name__}")
        return errors
    for i, item in enumerate(manifest):
        if not isinstance(item, dict):
            errors.append(f"manifest[{i}] must be an object, got {type(item).__name__}")
            continue
        if "api" not in item:
            errors.append(f"manifest[{i}] missing required 'api' field")
            continue
        if "args" not in item:
            errors.append(f"manifest[{i}] missing required 'args' field")
            continue
        if item["api"] not in _PUBLISH_API_ENUM:
            errors.append(f"manifest[{i}]: unknown publish api {item['api']!r} (valid: {sorted(_PUBLISH_API_ENUM)})")
        if not isinstance(item["args"], dict):
            errors.append(f"manifest[{i}].args must be an object, got {type(item['args']).__name__}")
        extra_keys = set(item.keys()) - {"api", "args"}
        if extra_keys:
            errors.append(f"manifest[{i}]: unexpected keys {sorted(extra_keys)} (only api and args allowed)")
    return errors


_RUNTIME_EVENT_ENUM = frozenset({"Stop", "SessionStart", "UserPromptSubmit", "PreToolUse"})

def _load_runtime_api_enum():
    """Source the closed runtime API enum from runtime.schema.json (Inv 33).

    The schema at ../schemas/runtime.schema.json is the SOLE source of
    truth; this function extracts the enum from
    definitions.call_list.items.properties.api.enum. Raises ImportError
    on missing/malformed schema so the dependency is surfaced loudly
    rather than silenced by a stale fallback (machine-first).
    """
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "runtime.schema.json"
    try:
        schema = json.loads(schema_path.read_text())
    except FileNotFoundError as exc:
        raise ImportError(
            f"runtime.schema.json not found at {schema_path}; "
            "checks.py requires it as the source of truth for _RUNTIME_API_ENUM (Inv 33)"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ImportError(
            f"runtime.schema.json at {schema_path} is malformed JSON: {exc}; "
            "checks.py cannot derive _RUNTIME_API_ENUM (Inv 33)"
        ) from exc
    try:
        enum = schema["definitions"]["call_list"]["items"]["properties"]["api"]["enum"]
    except (KeyError, TypeError) as exc:
        raise ImportError(
            f"runtime.schema.json at {schema_path} is missing "
            "definitions.call_list.items.properties.api.enum (Inv 33)"
        ) from exc
    if not isinstance(enum, list) or not all(isinstance(x, str) for x in enum):
        raise ImportError(
            f"runtime.schema.json enum at {schema_path} is not a list of strings (Inv 33)"
        )
    return frozenset(enum)


_RUNTIME_API_ENUM = _load_runtime_api_enum()


def _validate_runtime(runtime):
    """Validate a runtime declaration. Returns list of error message strings."""
    errors = []
    if not isinstance(runtime, dict):
        errors.append(f"runtime must be an object, got {type(runtime).__name__}")
        return errors
    for event, calls in runtime.items():
        if event not in _RUNTIME_EVENT_ENUM:
            errors.append(f"runtime: unknown event {event!r} (valid: {sorted(_RUNTIME_EVENT_ENUM)})")
            continue
        if not isinstance(calls, list):
            errors.append(f"runtime[{event!r}] must be an array, got {type(calls).__name__}")
            continue
        for i, item in enumerate(calls):
            if not isinstance(item, dict):
                errors.append(f"runtime[{event!r}][{i}] must be an object")
                continue
            if "api" not in item:
                errors.append(f"runtime[{event!r}][{i}] missing required 'api' field")
                continue
            if "args" not in item:
                errors.append(f"runtime[{event!r}][{i}] missing required 'args' field")
                continue
            if item["api"] not in _RUNTIME_API_ENUM:
                errors.append(f"runtime[{event!r}][{i}]: unknown runtime api {item['api']!r}")
            if not isinstance(item["args"], dict):
                errors.append(f"runtime[{event!r}][{i}].args must be an object")
            extra_keys = set(item.keys()) - {"api", "args"}
            if extra_keys:
                errors.append(f"runtime[{event!r}][{i}]: unexpected keys {sorted(extra_keys)}")
    return errors


_STORAGE_TYPE_ENUM = frozenset({"marker-file", "json-key", "json-array", "json-array-templated"})

_MUTATION_API_ENUM = frozenset({
    "write_marker",
    "delete_marker",
    "set_json_key",
    "delete_json_key",
    "append_json_array",
    "remove_json_array_value",
    "run_feature_script",
})

_COLOR_ENUM = frozenset({"red", "green", "yellow"})


def _validate_api_call(item, ctx):
    """Validate a single {api, args} mutation call. Returns list of errors."""
    errors = []
    if not isinstance(item, dict):
        errors.append(f"{ctx}: must be an object, got {type(item).__name__}")
        return errors
    if "api" not in item:
        errors.append(f"{ctx}: missing required 'api' field")
        return errors
    if "args" not in item:
        errors.append(f"{ctx}: missing required 'args' field")
        return errors
    if item["api"] not in _MUTATION_API_ENUM:
        errors.append(f"{ctx}: unknown mutation api {item['api']!r}")
    if not isinstance(item["args"], dict):
        errors.append(f"{ctx}.args must be an object")
    extra = set(item.keys()) - {"api", "args"}
    if extra:
        errors.append(f"{ctx}: unexpected keys {sorted(extra)}")
    return errors


def _validate_configuration(configuration):
    """Validate a configuration declaration. Returns list of error message strings."""
    errors = []
    if not isinstance(configuration, list):
        errors.append(f"configuration must be an array, got {type(configuration).__name__}")
        return errors
    for i, entry in enumerate(configuration):
        ctx = f"configuration[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{ctx} must be an object")
            continue
        if "id" not in entry:
            errors.append(f"{ctx} missing required 'id'")
            continue
        if "subcommand" not in entry:
            errors.append(f"{ctx} missing required 'subcommand'")
            continue
        has_values = "values" in entry
        has_actions = "actions" in entry
        if has_values == has_actions:
            errors.append(f"{ctx} must declare exactly one of 'values' or 'actions' (oneOf)")
        if has_values:
            if not isinstance(entry["values"], dict):
                errors.append(f"{ctx}.values must be an object")
            else:
                for k, call in entry["values"].items():
                    errors.extend(_validate_api_call(call, f"{ctx}.values[{k!r}]"))
                # Cross-field check: if a default is declared for a values-style
                # entry, it must name one of the declared value keys. Otherwise
                # the default is unreachable.
                if "default" in entry and entry["default"] not in entry["values"]:
                    errors.append(
                        f"{ctx}: default {entry['default']!r} is not a key in values; "
                        f"got values keys {sorted(entry['values'].keys())}"
                    )
                # Same check for alert-on: if declared, it must name a values key.
                if "alert-on" in entry and entry["alert-on"] not in entry["values"]:
                    errors.append(
                        f"{ctx}: alert-on {entry['alert-on']!r} is not a key in values; "
                        f"got values keys {sorted(entry['values'].keys())}"
                    )
        if has_actions:
            if not isinstance(entry["actions"], dict):
                errors.append(f"{ctx}.actions must be an object")
            else:
                for k, call in entry["actions"].items():
                    errors.extend(_validate_api_call(call, f"{ctx}.actions[{k!r}]"))
        if "storage" in entry:
            storage = entry["storage"]
            if not isinstance(storage, dict):
                errors.append(f"{ctx}.storage must be an object")
            elif "type" not in storage:
                errors.append(f"{ctx}.storage: missing required 'type' field")
            elif storage["type"] not in _STORAGE_TYPE_ENUM:
                errors.append(f"{ctx}.storage: unknown storage type {storage['type']!r}")
        if "alert-message" in entry:
            am = entry["alert-message"]
            if not isinstance(am, dict):
                errors.append(f"{ctx}.alert-message must be an object")
            else:
                for k in ("text", "icon", "color"):
                    if k not in am:
                        errors.append(f"{ctx}.alert-message missing required '{k}'")
                if "color" in am and am["color"] not in _COLOR_ENUM:
                    errors.append(f"{ctx}.alert-message.color must be one of {sorted(_COLOR_ENUM)}, got {am['color']!r}")
    return errors


def validate_meta_contract(feature_dir):
    """Validate a feature's meta-contract sections (manifest/runtime/configuration).

    Each section is optional. Returns a CheckResult; passed=True iff every
    declared section validates against its schema rules.
    """
    feature_json_path = os.path.join(feature_dir, "feature.json")
    if not os.path.isfile(feature_json_path):
        return CheckResult(passed=False, messages=[f"feature.json missing at {feature_json_path}"])
    try:
        with open(feature_json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return CheckResult(passed=False, messages=[f"feature.json invalid JSON: {e}"])
    if not isinstance(data, dict):
        return CheckResult(passed=False, messages=[f"feature.json must be a JSON object, got {type(data).__name__}"])

    errors = []
    if "manifest" in data:
        errors.extend(_validate_manifest(data["manifest"]))
    if "runtime" in data:
        errors.extend(_validate_runtime(data["runtime"]))
    if "configuration" in data:
        errors.extend(_validate_configuration(data["configuration"]))
    if "prompts" in data:
        errors.extend(_validate_prompts(data["prompts"]))

    if errors:
        return CheckResult(passed=False, messages=errors)
    return CheckResult(passed=True, messages=["meta-contract sections valid (or absent)"])


# ---------- prompts (Inv 43, 53) ---------------------------------------------

_PROMPT_KIND_ENUM = frozenset({"skill", "subagent"})
_PROMPT_ID_RE = re.compile(r"^[a-z][a-z0-9-]*$")
_PROMPT_SLOT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PROMPT_REQUIRED = {"id", "kind", "inject", "slots"}
_PROMPT_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")
_UNIVERSAL_POLICY_PATH = ".claude/features/policy/philosophy.md"


def _validate_prompts(prompts):
    """Schema-shape validation for the prompts section. Returns list of errors."""
    errors = []
    if not isinstance(prompts, list):
        errors.append(f"prompts must be an array, got {type(prompts).__name__}")
        return errors
    for i, entry in enumerate(prompts):
        ctx = f"prompts[{i}]"
        if not isinstance(entry, dict):
            errors.append(f"{ctx} must be an object")
            continue
        missing = _PROMPT_REQUIRED - set(entry.keys())
        if missing:
            errors.append(f"{ctx} missing required field(s): {sorted(missing)}")
            continue
        extra = set(entry.keys()) - _PROMPT_REQUIRED
        if extra:
            errors.append(f"{ctx} has unexpected key(s): {sorted(extra)}")
        if not isinstance(entry["id"], str) or not _PROMPT_ID_RE.match(entry["id"]):
            errors.append(f"{ctx}.id must match '^[a-z][a-z0-9-]*$', got {entry['id']!r}")
        if entry["kind"] not in _PROMPT_KIND_ENUM:
            errors.append(f"{ctx}.kind must be one of {sorted(_PROMPT_KIND_ENUM)}, got {entry['kind']!r}")
        if not isinstance(entry["inject"], list):
            errors.append(f"{ctx}.inject must be an array")
        elif len(entry["inject"]) < 1:
            errors.append(f"{ctx}.inject must have at least 1 item (minItems: 1)")
        else:
            for j, p in enumerate(entry["inject"]):
                if not isinstance(p, str):
                    errors.append(f"{ctx}.inject[{j}] must be a string")
        if not isinstance(entry["slots"], list):
            errors.append(f"{ctx}.slots must be an array")
        else:
            for j, s in enumerate(entry["slots"]):
                if not isinstance(s, str) or not _PROMPT_SLOT_RE.match(s):
                    errors.append(
                        f"{ctx}.slots[{j}] must match '^[a-z][a-z0-9_]*$', got {s!r}"
                    )
    return errors


def validate_prompts_section(feature_dir: str) -> CheckResult:
    """Per-feature prompts-section schema validation.

    Inv 45: per-feature wrapper used by validate_meta_contract. Validates
    schema shape only; does NOT run cross-feature uniqueness, template
    existence, or inject-path existence checks (those live in
    check_prompts_section).
    """
    feature_json_path = os.path.join(feature_dir, "feature.json")
    if not os.path.isfile(feature_json_path):
        return CheckResult(False, [f"feature.json missing at {feature_json_path}"])
    try:
        with open(feature_json_path) as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        return CheckResult(False, [f"feature.json invalid JSON: {e}"])
    if not isinstance(data, dict):
        return CheckResult(False, [f"feature.json must be a JSON object"])
    if "prompts" not in data:
        return CheckResult(True, ["OK: no prompts section (vacuous)"])
    errors = _validate_prompts(data["prompts"])
    if errors:
        return CheckResult(False, errors)
    return CheckResult(True, ["OK: prompts section valid"])


def check_prompts_section(features_root: str) -> CheckResult:
    """Cross-feature prompts-section lint.

    Inv 45: walks every <features_root>/*/feature.json, validates each
    present prompts section against prompts.schema.json, asserts
    globally-unique ids, asserts every inject path exists on disk,
    asserts every entry's inject list includes the universal policy
    file (.claude/features/policy/philosophy.md), asserts the
    convention-resolved template at
    .claude/features/contract/templates/prompts/<id>.txt exists,
    and asserts bidirectional slot/placeholder correspondence.
    """
    if not os.path.isdir(features_root):
        return CheckResult(False, [f"ERROR: not a directory: {features_root}"])

    # The features_root is .../.claude/features ; the repo root is two parents up.
    repo_root = os.path.normpath(os.path.join(features_root, "..", ".."))
    templates_dir = os.path.join(
        features_root, "contract", "templates", "prompts"
    )

    messages: List[str] = []
    seen_ids = {}  # id -> first feature seen on

    for entry_name in sorted(os.listdir(features_root)):
        feat_dir = os.path.join(features_root, entry_name)
        feature_json_path = os.path.join(feat_dir, "feature.json")
        if not os.path.isfile(feature_json_path):
            continue
        try:
            with open(feature_json_path) as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            messages.append(f"ERROR: {feature_json_path}: invalid JSON: {e}")
            continue
        if not isinstance(data, dict) or "prompts" not in data:
            continue
        prompts = data["prompts"]
        # Schema-shape errors first
        shape_errors = _validate_prompts(prompts)
        for e in shape_errors:
            messages.append(f"{entry_name}: {e}")
        if not isinstance(prompts, list):
            continue
        for i, entry in enumerate(prompts):
            if not isinstance(entry, dict):
                continue
            ctx = f"{entry_name}:prompts[{i}]"
            pid = entry.get("id")
            if isinstance(pid, str):
                if pid in seen_ids:
                    messages.append(
                        f"{ctx}: duplicate id {pid!r} (already declared by {seen_ids[pid]}); "
                        f"prompt ids must be globally unique"
                    )
                else:
                    seen_ids[pid] = entry_name
            # inject path existence + universal policy
            inject = entry.get("inject", [])
            if isinstance(inject, list):
                if _UNIVERSAL_POLICY_PATH not in inject:
                    messages.append(
                        f"{ctx}: inject must include the universal policy file "
                        f"{_UNIVERSAL_POLICY_PATH!r}"
                    )
                for path in inject:
                    if not isinstance(path, str):
                        continue
                    full = os.path.join(repo_root, path)
                    if not os.path.exists(full):
                        messages.append(
                            f"{ctx}: inject path does not exist on disk: {path}"
                        )
            # convention-resolved template existence
            if isinstance(pid, str):
                tpl_path = os.path.join(templates_dir, f"{pid}.txt")
                if not os.path.isfile(tpl_path):
                    messages.append(
                        f"{ctx}: missing convention-resolved template at "
                        f"templates/prompts/{pid}.txt for id {pid!r}"
                    )
                else:
                    # slot/placeholder bidirectional correspondence
                    slots = entry.get("slots", [])
                    if isinstance(slots, list):
                        try:
                            with open(tpl_path, encoding="utf-8") as tf:
                                body = tf.read()
                        except (OSError, UnicodeDecodeError) as e:
                            messages.append(f"{ctx}: cannot read template: {e}")
                            continue
                        placeholders = set(_PROMPT_PLACEHOLDER_RE.findall(body))
                        slot_set = set(s for s in slots if isinstance(s, str))
                        orphan_ph = placeholders - slot_set
                        orphan_slots = slot_set - placeholders
                        for ph in sorted(orphan_ph):
                            messages.append(
                                f"{ctx}: template placeholder {{{{{ph}}}}} not declared in slots"
                            )
                        for s in sorted(orphan_slots):
                            messages.append(
                                f"{ctx}: slot {s!r} not used as placeholder in template"
                            )

    if messages:
        return CheckResult(False, messages)
    return CheckResult(True, ["OK: prompts sections valid (or absent) across all features"])


# ---------- validate_agent_prompt_sentinel (Inv 56 b) ------------------------

_AGENT_SENTINEL_BYPASS_REL = os.path.join(".rabbit", "agent-sentinel-bypass")
_AGENT_SENTINEL_VIOLATION_MSG = (
    "Agent dispatch missing RABBIT-POLICY-BLOCK-v1 sentinel — "
    "prompt must be assembled via contract's build-prompt.py / dispatcher "
    "script (see contract Inv 46, Inv 56)."
)


def validate_agent_prompt_sentinel(tool_input: dict, *, repo_root: str) -> CheckResult:
    """Inv 56 (b): validate an Agent tool call's prompt carries the sentinel.

    Returns CheckResult(passed=True, []) when either:
      (i)  the bypass marker <repo_root>/.rabbit/agent-sentinel-bypass exists, OR
      (ii) tool_input["prompt"] is a str containing the literal sentinel
           "RABBIT-POLICY-BLOCK-v1".

    Otherwise returns CheckResult(passed=False, [canonical violation message]).

    The bypass-marker check runs first so an operator can globally opt out
    of sentinel validation without touching every Agent caller (e.g. during
    a migration window). The marker is an opt-in escape hatch — its absence
    is the normal state. A non-str or missing prompt always fails when the
    marker is absent.
    """
    # (i) bypass marker → unconditional pass
    bypass_path = os.path.join(repo_root, _AGENT_SENTINEL_BYPASS_REL)
    if os.path.exists(bypass_path):
        return CheckResult(True, [])
    # (ii) defensive type check + sentinel substring
    prompt = tool_input.get("prompt")
    if isinstance(prompt, str) and _SENTINEL in prompt:
        return CheckResult(True, [])
    return CheckResult(False, [_AGENT_SENTINEL_VIOLATION_MSG])
