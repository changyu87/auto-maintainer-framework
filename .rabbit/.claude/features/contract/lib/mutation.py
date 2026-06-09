"""contract.lib.mutation — API library for state-mutation primitives used by
the per-feature config dispatcher when a feature's CONFIGURATION declares a
value change.

Each function implements one mutation API call as declared in a feature's
CONFIGURATION section. Functions accept their declared args plus keyword-only
context params (repo_root for filesystem APIs; feature_dir for the script
escape hatch) and return CheckResult.

All mutations are idempotent: re-running with unchanged effective state
returns a CheckResult whose messages contain "no-op". (The
`run_feature_script` escape hatch delegates idempotency to the invoked
script.)

Version: 1.0.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when the rabbit CLI exposes native configuration mutation.
"""

import json
import os
import subprocess

from lib.checks import CheckResult


def write_marker(path: str, content: str, *, repo_root: str) -> CheckResult:
    """Write a marker file at path (repo-root-relative) with given content.

    Idempotent: if the marker already exists with identical content, returns
    passed=True with a 'no-op' message and does not touch the file.
    Parent directories are created automatically.
    """
    dst = os.path.join(repo_root, path)
    if os.path.isfile(dst):
        try:
            with open(dst) as f:
                if f.read() == content:
                    return CheckResult(True, [f"OK: {path} unchanged (no-op)"])
        except OSError:
            pass
    parent = os.path.dirname(dst)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(dst, "w") as f:
        f.write(content)
    return CheckResult(True, [f"OK: {path} written"])


def delete_marker(path: str, *, repo_root: str) -> CheckResult:
    """Delete a marker file at path (repo-root-relative).

    Idempotent: if the marker is already absent, returns passed=True with a
    'no-op' message and does not raise.
    """
    dst = os.path.join(repo_root, path)
    if not os.path.exists(dst):
        return CheckResult(True, [f"OK: {path} absent (no-op)"])
    os.remove(dst)
    return CheckResult(True, [f"OK: {path} deleted"])


def _load_json_or_empty(path: str) -> tuple:
    """Read JSON file. Returns (data, error_msg).

    Missing file -> ({}, None). Malformed JSON -> (None, "...").
    """
    if not os.path.isfile(path):
        return {}, None
    try:
        with open(path) as f:
            return json.load(f), None
    except json.JSONDecodeError as e:
        return None, f"ERROR: malformed JSON in {path}: {e}"


def _write_json(path: str, data) -> None:
    """Write data to path as indented JSON. Creates parent dirs."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _set_nested(d: dict, key_path: str, value) -> None:
    """Set value at dotted key_path inside d, creating intermediate dicts.

    If an intermediate node exists but is not a dict, it is overwritten with
    a fresh dict. Mutates d in place.
    """
    parts = key_path.split(".")
    for p in parts[:-1]:
        if not isinstance(d.get(p), dict):
            d[p] = {}
        d = d[p]
    d[parts[-1]] = value


def _get_nested(d, key_path: str):
    """Return value at dotted key_path; raises KeyError if any segment is
    missing or any intermediate value is not a dict.
    """
    parts = key_path.split(".")
    for p in parts:
        if not isinstance(d, dict) or p not in d:
            raise KeyError(key_path)
        d = d[p]
    return d


def set_json_key(file: str, key: str, value, *, repo_root: str) -> CheckResult:
    """Set value at dotted JSON key path in file (repo-root-relative).

    Creates the file (and intermediate dicts) if absent. Preserves all sibling
    keys at every level. Idempotent: if the value already equals the new value,
    returns passed=True with a 'no-op' message and does not rewrite the file.

    On malformed JSON, returns passed=False without modifying the file.
    """
    path = os.path.join(repo_root, file)
    data, err = _load_json_or_empty(path)
    if err is not None:
        return CheckResult(False, [err])
    try:
        existing = _get_nested(data, key)
        if existing == value:
            return CheckResult(True, [f"OK: {file}::{key} unchanged (no-op)"])
    except KeyError:
        pass
    _set_nested(data, key, value)
    _write_json(path, data)
    return CheckResult(True, [f"OK: {file}::{key} set"])


def _delete_nested(d: dict, key_path: str) -> bool:
    """Delete leaf at dotted key_path inside d. Returns True if deleted,
    False if any segment was missing (no-op).
    """
    parts = key_path.split(".")
    for p in parts[:-1]:
        if not isinstance(d, dict) or p not in d or not isinstance(d[p], dict):
            return False
        d = d[p]
    if isinstance(d, dict) and parts[-1] in d:
        del d[parts[-1]]
        return True
    return False


def delete_json_key(file: str, key: str, *, repo_root: str) -> CheckResult:
    """Delete key at dotted JSON path in file (repo-root-relative).

    Idempotent: missing file, missing key, or missing intermediate dict
    is a no-op. The empty parent object is preserved (sibling keys at
    every level are untouched).

    On malformed JSON, returns passed=False without modifying the file.
    """
    path = os.path.join(repo_root, file)
    if not os.path.isfile(path):
        return CheckResult(True, [f"OK: {file} absent (no-op)"])
    data, err = _load_json_or_empty(path)
    if err is not None:
        return CheckResult(False, [err])
    if not _delete_nested(data, key):
        return CheckResult(True, [f"OK: {file}::{key} absent (no-op)"])
    _write_json(path, data)
    return CheckResult(True, [f"OK: {file}::{key} deleted"])


def append_json_array(file: str, key: str, value, *, repo_root: str) -> CheckResult:
    """Append value to a JSON array at dotted key path in file.

    Creates the file, intermediate dicts, and the array if absent. Idempotent:
    if value is already present in the array, returns passed=True with a
    'no-op' message and does not re-append.

    If an existing value at the key is not an array, returns passed=False
    without modifying the file (data preservation).
    """
    path = os.path.join(repo_root, file)
    data, err = _load_json_or_empty(path)
    if err is not None:
        return CheckResult(False, [err])
    try:
        existing = _get_nested(data, key)
    except KeyError:
        existing = None
    if existing is None:
        _set_nested(data, key, [value])
    elif not isinstance(existing, list):
        return CheckResult(False, [
            f"ERROR: {file}::{key} exists but is not an array (type={type(existing).__name__}); refusing to overwrite"
        ])
    elif value in existing:
        return CheckResult(True, [f"OK: {file}::{key} already contains value (no-op)"])
    else:
        existing.append(value)
    _write_json(path, data)
    return CheckResult(True, [f"OK: {file}::{key} appended"])


def remove_json_array_value(file: str, key: str, value, *, repo_root: str) -> CheckResult:
    """Remove value from JSON array at dotted key path in file.

    Idempotent: absent file, absent key, and absent value are all no-ops.
    Removes the first occurrence only (deterministic single-removal). The
    empty array is preserved; the key is not auto-removed.

    If an existing value at the key is not an array, returns passed=False
    without modifying the file (data preservation).
    """
    path = os.path.join(repo_root, file)
    if not os.path.isfile(path):
        return CheckResult(True, [f"OK: {file} absent (no-op)"])
    data, err = _load_json_or_empty(path)
    if err is not None:
        return CheckResult(False, [err])
    try:
        existing = _get_nested(data, key)
    except KeyError:
        return CheckResult(True, [f"OK: {file}::{key} absent (no-op)"])
    if not isinstance(existing, list):
        return CheckResult(False, [
            f"ERROR: {file}::{key} exists but is not an array (type={type(existing).__name__}); refusing to modify"
        ])
    if value not in existing:
        return CheckResult(True, [f"OK: {file}::{key} does not contain value (no-op)"])
    existing.remove(value)
    _write_json(path, data)
    return CheckResult(True, [f"OK: {file}::{key} value removed"])


def run_feature_script(script: str, args: list, *, feature_dir: str) -> CheckResult:
    """Escape hatch: invoke a feature-owned script with given args.

    script — feature-dir-relative path to the script (must be executable).
    args   — list of string arguments to forward.

    Returns CheckResult(passed=True) iff the script exits 0. stdout and
    stderr are captured into the messages list (one entry each, only if
    non-empty). Missing script returns passed=False without raising.

    Intentionally NOT idempotent at this layer — idempotency is the
    responsibility of the invoked script. The escape hatch exists for the
    minority of mutations that don't fit standard primitives (e.g. chmod
    for repo-permissions); new standard primitives are preferred.
    """
    script_path = os.path.join(feature_dir, script)
    if not os.path.isfile(script_path):
        return CheckResult(False, [f"ERROR: script not found: {script_path}"])
    proc = subprocess.run([script_path, *args], capture_output=True, text=True)
    messages = []
    if proc.stdout:
        messages.append(f"stdout: {proc.stdout.rstrip()}")
    if proc.stderr:
        messages.append(f"stderr: {proc.stderr.rstrip()}")
    if proc.returncode != 0:
        messages.insert(0, f"ERROR: {script} exited {proc.returncode}")
        return CheckResult(False, messages)
    messages.insert(0, f"OK: {script} succeeded")
    return CheckResult(True, messages)
