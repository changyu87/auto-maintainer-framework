#!/usr/bin/env python3
"""check-release-update.py — deterministic release-channel update probe.

Reads <repo_root>/.version for the locally-installed ref, throttles
the upstream fetch via <runtime_root>/.runtime/last-update-check, where
<runtime_root> is the canonical single-`.rabbit` runtime root resolved by
rabbit-cage's rabbit_runtime_root (Inv 52) — in a vendored install repo_root
already IS the `.rabbit` dir, so anchoring there avoids doubling the segment
(default window 8h, override via RABBIT_UPDATE_CHECK_INTERVAL_SECONDS),
fetches the latest published release's tag_name from the GitHub Releases
API https://api.github.com/repos/<RABBIT_REPO>/releases/latest via stdlib
urllib, and writes a single JSON line to stdout describing the comparison
outcome.

Release model: tags + GitHub Releases (releases cut as vX.Y.Z tags
targeting dev via `gh release create --target dev`). The check tracks the
latest published release regardless of target branch.

Behavior (per contract spec Inv 53):

  - missing/unreadable .version: silent exit 0, no stdout.
  - throttled (now - last_check < interval): silent exit 0, no stdout.
  - fetch failure (URLError, OSError, HTTP non-200, non-JSON body,
    missing/non-string tag_name, any other exception): silent exit 0, no
    stdout. Throttle timestamp IS updated to avoid pounding the upstream on
    transient errors.
  - tag_name == local: stdout '{"newer": false}', exit 0.
  - tag_name != local: stdout '{"newer": true, "channel": <channel>,
    "current": <local>, "new": <tag_name>, "self_update_available": <bool>}',
    exit 0. <channel> is a REAL channel label DISTINCT from the version pin,
    never the version string: a local '--src' install pin ('local-<sha>')
    reports the bare literal 'local'; any other ref reports the ref as-read
    (e.g. 'dev', 'stable', 'main'). <current> is the .version content verbatim.

self_update_available is true when <rabbit_root>/install.py exists AND
contains the literal string 'fetch_upstream' (the self-update marker).
Otherwise false (the consumer suggests the curl|bash fresh-install
fallback).

repo_root is resolved from environment variable RABBIT_ROOT when set
(plugin mode); otherwise falls back to the directory four levels above
this script (.claude/features/contract/scripts -> repo root).

Stdlib only. No subprocess. NEVER blocks the user — every error path
exits 0.

Usage:
    python3 check-release-update.py

Exit codes:
    0 — always (silent on errors; JSON to stdout on success).

Version: 2.2.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when Claude Code exposes a native release-channel
    update notification mechanism that supersedes this helper.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_INTERVAL_SECONDS = 8 * 60 * 60  # 8 hours
DEFAULT_REPO = "changyu87/rabbit-workflow"
FETCH_TIMEOUT_SECONDS = 5


def resolve_repo_root():
    env_root = os.environ.get("RABBIT_ROOT")
    if env_root:
        return env_root
    # .claude/features/contract/scripts/check-release-update.py
    # parents[0]=scripts, [1]=contract, [2]=features, [3]=.claude, [4]=repo_root
    return str(Path(__file__).resolve().parents[4])


def rabbit_runtime_root(repo_root):
    """Resolve the canonical single-`.rabbit` runtime root for `repo_root` via
    rabbit-cage's `rabbit_runtime_root` resolver (Inv 52), lazy-imported from the
    install's feature lib using the same importlib.util pattern rabbit-cage's
    session-start dispatcher and rabbit-spec's dispatcher use.

    Falls back to the inline basename rule when the resolver cannot be imported
    (degenerate / partial install) so the throttle file still lands on a single-
    `.rabbit` path.
    """
    resolver_path = (
        Path(repo_root) / ".claude" / "features" / "rabbit-cage"
        / "lib" / "runtime_root.py"
    )
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "rabbit_cage_runtime_root", str(resolver_path))
        if spec is not None and spec.loader is not None:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module.rabbit_runtime_root(str(repo_root))
    except (FileNotFoundError, ImportError, AttributeError, OSError):
        pass
    rp = os.path.normpath(str(repo_root))
    return rp if os.path.basename(rp) == ".rabbit" else os.path.join(rp, ".rabbit")


def read_version(version_path):
    try:
        with open(version_path) as f:
            return f.read().strip()
    except OSError:
        return None


def resolve_channel(local):
    """Map the .version pin to a REAL channel label distinct from the pin.

    A local `--src` install writes a `local-<sha>` pin; reporting that pin as
    the channel rendered the nonsensical "on channel local-<sha>" banner line.
    For such a pin the channel is the bare literal `local`; any other ref is a
    configured channel ref already (e.g. `dev`, `stable`, `main`, `v1.2.3`) and
    is reported as-read. The channel is never the version string when the pin is
    a `local-*` pin.
    """
    if local == "local" or local.startswith("local-"):
        return "local"
    return local


def read_throttle(ts_path):
    try:
        with open(ts_path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def write_throttle(ts_path):
    try:
        os.makedirs(os.path.dirname(ts_path), exist_ok=True)
        with open(ts_path, "w") as f:
            f.write(str(int(time.time())))
    except OSError:
        # Best-effort; never block.
        pass


def resolve_interval():
    raw = os.environ.get("RABBIT_UPDATE_CHECK_INTERVAL_SECONDS")
    if raw is None:
        return DEFAULT_INTERVAL_SECONDS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_INTERVAL_SECONDS


def fetch_upstream_version(repo, channel):
    # Release model is tags + GitHub Releases: track the latest published
    # release's tag_name rather than fetching .version off a branch ref. The
    # `channel` argument is the resolved channel label, retained only for the
    # comparison payload; it does not select the fetch URL.
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(
        url, headers={"Accept": "application/vnd.github+json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SECONDS) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if status != 200:
                return None
            body = resp.read()
            if isinstance(body, bytes):
                body = body.decode("utf-8", errors="replace")
            data = json.loads(body)
            tag = data.get("tag_name")
            if not isinstance(tag, str):
                return None
            return tag.strip()
    except (urllib.error.URLError, urllib.error.HTTPError, OSError,
            UnicodeDecodeError, json.JSONDecodeError, Exception):  # noqa: BLE001
        return None


def probe_self_update(rabbit_root):
    install = os.path.join(rabbit_root, "install.py")
    if not os.path.isfile(install):
        return False
    try:
        with open(install) as f:
            return "fetch_upstream" in f.read()
    except OSError:
        return False


def main():
    repo_root = resolve_repo_root()
    version_path = os.path.join(repo_root, ".version")
    # Anchor the throttle file at the canonical single-`.rabbit` runtime root
    # (Inv 52). In a vendored install repo_root IS the `.rabbit` dir, so an
    # unconditional `<repo_root>/.rabbit/...` join doubled the segment (#1065);
    # rabbit_runtime_root collapses that to one `.rabbit`.
    ts_path = os.path.join(
        rabbit_runtime_root(repo_root), ".runtime", "last-update-check")

    local = read_version(version_path)
    if local is None or local == "":
        return 0

    interval = resolve_interval()
    last = read_throttle(ts_path)
    now = int(time.time())
    if last and now - last < interval:
        return 0

    repo = os.environ.get("RABBIT_REPO", DEFAULT_REPO)
    channel = resolve_channel(local)
    upstream = fetch_upstream_version(repo, channel)

    # Update throttle after the attempt regardless of outcome.
    write_throttle(ts_path)

    if upstream is None or upstream == "":
        return 0

    if upstream == local:
        sys.stdout.write(json.dumps({"newer": False}) + "\n")
        return 0

    self_update = probe_self_update(repo_root)
    sys.stdout.write(json.dumps({
        "newer": True,
        "channel": channel,
        "current": local,
        "new": upstream,
        "self_update_available": self_update,
    }) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
