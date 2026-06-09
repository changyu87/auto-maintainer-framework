"""contract.lib.producers — content producers for publish_generated.

Each producer takes producer-specific named args (forwarded from a feature's
MANIFEST entry) plus keyword-only `feature_dir` and `repo_root` context, and
returns generated content as a string. The producer registry maps the
kebab-case producer names declared in MANIFESTs to these Python functions;
`call_producer(name, args, *, feature_dir, repo_root)` is the dispatcher
that lib.publish.publish_generated invokes.

Path-arg convention (applies to every producer's path args):
  - Absolute paths pass through unchanged.
  - Relative paths starting with ".claude/" resolve against repo_root.
  - All other relative paths resolve against feature_dir.

Future producers documented but not implemented here:
  - compose-template(template, args) — template substitution. Deferred per
    the meta-contract design doc until first concrete need.

Version: 1.0.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when the rabbit CLI exposes native content producers.
"""

import json
import os


def _resolve(path: str, feature_dir: str, repo_root: str) -> str:
    """Resolve a producer arg path per the module-level convention."""
    if os.path.isabs(path):
        return path
    if path.startswith(".claude/"):
        return os.path.join(repo_root, path)
    return os.path.join(feature_dir, path)


def read_file(path: str, *, feature_dir: str, repo_root: str) -> str:
    """Return the contents of `path` (resolved per the module-level
    convention) as a string. Raises FileNotFoundError if the file is
    missing — caller (typically publish_generated) propagates the error.
    """
    with open(_resolve(path, feature_dir, repo_root)) as f:
        return f.read()


def expand_at_imports(file: str, *, feature_dir: str, repo_root: str) -> str:
    """Read `file` and expand every line of the form `@<path>` (one path
    per line, no whitespace inside the path) by substituting the contents
    of <path>. Non-import lines pass through unchanged. Expansion is one
    level deep — imported content is NOT recursively re-scanned for
    further @-imports (matches Claude Code's @-import semantics). If an
    imported file lacks a trailing newline, one is appended so the
    composed output keeps clean line structure.
    """
    with open(_resolve(file, feature_dir, repo_root)) as f:
        content = f.read()
    out = []
    for line in content.splitlines(keepends=True):
        stripped = line.strip()
        if (
            stripped.startswith("@")
            and len(stripped) > 1
            and not any(c.isspace() for c in stripped)
        ):
            imported = open(_resolve(stripped[1:], feature_dir, repo_root)).read()
            if not imported.endswith("\n"):
                imported += "\n"
            out.append(imported)
        else:
            out.append(line)
    return "".join(out)


def generate_claude_md(policy_source: str, header_source: str, *,
                       feature_dir: str, repo_root: str) -> str:
    """Compose a CLAUDE.md by emitting the header text from `header_source`
    (a JSON file with a top-level `header` string) followed by a blank line
    and one `@<path>` line per `.md` file found under `policy_source`. The
    @-import paths in the output are written repo-root-relative (the form
    Claude Code expects). Policy files are emitted in alphabetical filename
    order — callers control order via filename prefixes if needed.
    """
    header_path = _resolve(header_source, feature_dir, repo_root)
    with open(header_path) as f:
        header = json.load(f)["header"]

    policy_dir = _resolve(policy_source, feature_dir, repo_root)
    policy_files = sorted(
        f for f in os.listdir(policy_dir) if f.endswith(".md")
    )
    rel_policy_dir = os.path.relpath(policy_dir, repo_root)
    imports = "\n".join(f"@{rel_policy_dir}/{f}" for f in policy_files)
    return f"{header}\n\n{imports}\n"


# Registry: MANIFEST kebab-case producer names → Python functions.
# Populated by Tasks 2-4 (read-file, expand-at-imports, generate-claude-md).
PRODUCERS = {
    "read-file": read_file,
    "expand-at-imports": expand_at_imports,
    "generate-claude-md": generate_claude_md,
    # "compose-template": deferred — see module docstring.
}


def call_producer(name: str, args: dict, *,
                  feature_dir: str, repo_root: str) -> str:
    """Dispatch `name` to its registered producer with `args` (forwarded as
    kwargs) plus the keyword-only context params. Returns the producer's
    generated content as a string. Raises KeyError if `name` is not in the
    registry — feature MANIFEST authors are responsible for using valid
    producer names.
    """
    if name not in PRODUCERS:
        raise KeyError(f"unknown producer: {name}")
    return PRODUCERS[name](feature_dir=feature_dir, repo_root=repo_root, **args)
