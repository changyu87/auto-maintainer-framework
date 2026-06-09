"""contract.lib.publish — API library for deploying feature artifacts to the workspace.

Each function implements one publish API call as declared in a feature's MANIFEST
section. All functions accept API args as explicit params plus keyword-only context
params (feature_dir, repo_root) and return CheckResult.

All publish operations are idempotent: if the destination already matches the source
(by SHA-256 for files, by content equality for generated), the call is a no-op.

Version: 1.1.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when the rabbit CLI exposes native artifact publishing.
"""

import hashlib
import json
import os
import shutil
from pathlib import Path

from lib.checks import CheckResult


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def publish_file(source: str, dest: str, *, feature_dir: str, repo_root: str) -> CheckResult:
    """Deploy source (feature-dir-relative) to dest (repo-root-relative), idempotent.

    Returns CheckResult(passed=False) if source does not exist.
    Returns CheckResult(passed=True) on success (copy) or no-op (unchanged).
    """
    src_path = os.path.join(feature_dir, source)
    dst_path = os.path.join(repo_root, dest)
    if not os.path.isfile(src_path):
        return CheckResult(False, [f"ERROR: source not found: {src_path}"])
    if os.path.isfile(dst_path) and _sha256_file(src_path) == _sha256_file(dst_path):
        return CheckResult(True, [f"OK: {dest} unchanged (no-op)"])
    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    shutil.copy(src_path, dst_path)
    return CheckResult(True, [f"OK: {dest} published"])


def publish_skill(source: str, *, feature_dir: str, repo_root: str) -> CheckResult:
    """Deploy a skill's SKILL.md to .claude/skills/<skill-name>/SKILL.md.

    source — feature-dir-relative path, e.g. "skills/rabbit-foo/SKILL.md".
    Skill name is the name of the source file's parent directory.
    """
    skill_name = Path(source).parent.name
    dest = f".claude/skills/{skill_name}/{Path(source).name}"
    return publish_file(source, dest, feature_dir=feature_dir, repo_root=repo_root)


def publish_command(source: str, *, feature_dir: str, repo_root: str) -> CheckResult:
    """Deploy a command file to .claude/commands/<basename>.

    source — feature-dir-relative path, e.g. "commands/rabbit-do.md".
    """
    dest = f".claude/commands/{Path(source).name}"
    return publish_file(source, dest, feature_dir=feature_dir, repo_root=repo_root)


def publish_agent(source: str, *, feature_dir: str, repo_root: str) -> CheckResult:
    """Deploy an agent file to .claude/agents/<basename>.

    source — feature-dir-relative path, e.g. "agents/rabbit-helper.md".
    """
    dest = f".claude/agents/{Path(source).name}"
    return publish_file(source, dest, feature_dir=feature_dir, repo_root=repo_root)


def publish_hook(event: str, source: str, matcher: str = "*", *,
                 feature_dir: str, repo_root: str) -> CheckResult:
    """Deploy a hook script to .claude/hooks/ and register it in .claude/settings.json.

    event   — Claude Code event: Stop | SessionStart | UserPromptSubmit | PreToolUse.
    source  — feature-dir-relative path, e.g. "hooks/stop-dispatcher.py".
    matcher — hook matcher pattern (default "*").

    Idempotent: re-running with unchanged hook file and already-registered command
    is a no-op. Existing settings.json fields are preserved (read-modify-write).
    """
    hook_name = Path(source).name
    hook_dest = f".claude/hooks/{hook_name}"
    result = publish_file(source, hook_dest, feature_dir=feature_dir, repo_root=repo_root)
    if not result.passed:
        return result

    settings_path = os.path.join(repo_root, ".claude", "settings.json")
    if os.path.isfile(settings_path):
        try:
            with open(settings_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    else:
        data = {}
    if not isinstance(data, dict):
        data = {}

    # Inv 42: command MUST be CWD-independent. Two forms are emitted based on
    # the caller's runtime env. Plugin form is used when RABBIT_ROOT is set
    # (deployed `.rabbit/` install where install.py writes env.RABBIT_ROOT into
    # settings.json). Standalone form is used otherwise (dev workspace with
    # .git present). /bin/sh substitutes the env var or runs the git command at
    # hook-fire time.
    rabbit_root_env = os.environ.get("RABBIT_ROOT")
    if rabbit_root_env:
        command = f"$RABBIT_ROOT/.claude/hooks/{hook_name}"
    else:
        command = f"$(git rev-parse --show-toplevel)/.claude/hooks/{hook_name}"
    legacy_command = f".claude/hooks/{hook_name}"
    hooks_section = data.setdefault("hooks", {})
    event_entries = hooks_section.setdefault(event, [])

    # Migration pass: upgrade any legacy bare-relative entry in place to the
    # new form, preserving the surrounding matcher and any sibling hooks.
    migrated = False
    for entry in event_entries:
        for h in entry.get("hooks", []):
            if h.get("command") == legacy_command:
                h["command"] = command
                migrated = True

    # Idempotency: if (after migration) the new-form command is already
    # present, no append is needed.
    already_present = any(
        h.get("command") == command
        for entry in event_entries
        for h in entry.get("hooks", [])
    )
    if already_present:
        if migrated:
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(settings_path, "w") as f:
                json.dump(data, f, indent=2)
            return CheckResult(True, [f"OK: {hook_dest} migrated to repo-root-anchored path under {event}"])
        return CheckResult(True, [f"OK: {hook_dest} already registered under {event} (no-op)"])

    event_entries.append({"matcher": matcher,
                           "hooks": [{"type": "command", "command": command}]})
    os.makedirs(os.path.dirname(settings_path), exist_ok=True)
    with open(settings_path, "w") as f:
        json.dump(data, f, indent=2)
    return CheckResult(True, [f"OK: {hook_dest} deployed and registered under {event}"])


def publish_settings(source: str, *, feature_dir: str, repo_root: str) -> CheckResult:
    """Merge the feature's settings.json into .claude/settings.json (idempotent).

    source — feature-dir-relative path to the settings JSON source file.

    Per spec Inv 36: the destination ``hooks`` section is the shared registration
    surface for every feature's ``publish_hook`` calls, so a raw file copy
    clobbers cross-feature hook registrations. The merge rule is:
      - non-hooks fields: source-wins (overlay every source key onto existing);
      - hooks section: start from existing event entries and, for each event in
        source, append source hook entries whose ``command`` strings are not
        already present in the destination event's flattened command set.
    Idempotent: when the merged result equals the on-disk content, the call is
    a no-op (no write, message contains "no-op").
    """
    src_path = os.path.join(feature_dir, source)
    if not os.path.isfile(src_path):
        return CheckResult(False, [f"ERROR: source not found: {src_path}"])

    with open(src_path) as f:
        src_data = json.load(f)

    dst_path = os.path.join(repo_root, ".claude", "settings.json")
    existing = {}
    if os.path.isfile(dst_path):
        try:
            with open(dst_path) as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                existing = loaded
        except (json.JSONDecodeError, OSError):
            existing = {}

    merged = dict(existing)
    for key, value in src_data.items():
        if key == "hooks":
            continue
        merged[key] = value

    existing_hooks = existing.get("hooks", {}) if isinstance(existing.get("hooks"), dict) else {}
    src_hooks = src_data.get("hooks", {}) if isinstance(src_data.get("hooks"), dict) else {}
    merged_hooks = {event: list(entries) for event, entries in existing_hooks.items()}
    for event, src_entries in src_hooks.items():
        if event not in merged_hooks:
            merged_hooks[event] = list(src_entries)
            continue
        existing_commands = {
            h.get("command")
            for entry in merged_hooks[event]
            for h in entry.get("hooks", [])
        }
        for src_entry in src_entries:
            for h in src_entry.get("hooks", []):
                cmd = h.get("command")
                if cmd not in existing_commands:
                    merged_hooks[event].append(src_entry)
                    existing_commands.add(cmd)
                    break

    if merged_hooks:
        merged["hooks"] = merged_hooks
    elif "hooks" in merged:
        del merged["hooks"]

    if os.path.isfile(dst_path) and merged == existing:
        return CheckResult(True, ["OK: .claude/settings.json unchanged (no-op)"])

    os.makedirs(os.path.dirname(dst_path), exist_ok=True)
    with open(dst_path, "w") as f:
        json.dump(merged, f, indent=2)
    return CheckResult(True, ["OK: .claude/settings.json published (merged)"])


def publish_generated(target: str, producer: str, args: dict, *,
                      feature_dir: str, repo_root: str) -> CheckResult:
    """Invoke a named content producer and write its output to target (idempotent).

    target   — repo-root-relative path to write, e.g. "CLAUDE.md".
    producer — producer name resolved via lib.producers.call_producer (Plan B.4).
    args     — arguments forwarded to the producer function.

    Late-imports lib.producers so this module is importable before B.4 lands.
    Returns CheckResult(passed=False) if lib.producers is unavailable.
    """
    try:
        from lib import producers  # noqa: PLC0415
        content = producers.call_producer(producer, args,
                                          feature_dir=feature_dir, repo_root=repo_root)
    except (ImportError, AttributeError) as e:
        return CheckResult(False, [f"ERROR: lib.producers unavailable: {e}"])

    target_path = os.path.join(repo_root, target)
    current = ""
    if os.path.isfile(target_path):
        with open(target_path) as f:
            current = f.read()
    if content == current:
        return CheckResult(True, [f"OK: {target} unchanged (no-op)"])
    target_dir = os.path.dirname(target_path)
    if target_dir:
        os.makedirs(target_dir, exist_ok=True)
    with open(target_path, "w") as f:
        f.write(content)
    return CheckResult(True, [f"OK: {target} generated via {producer}"])
