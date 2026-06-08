"""Resolve the seven TICK adapter ports from a project's ports.json.

`resolve_ports(config_path, contracts_path)` reads the project override file
`ports.json`, validates its `schema_version` against the port-contract bundle,
and returns a dict mapping every one of the seven canonical port names to
either the override script path declared in the config or the sentinel literal
`"default"`.

A port absent from the config map resolves to `"default"` — phase-ports owns
NO default adapter implementation (v1 decision 2); each phase's default lives
in its own sibling feature, so the sentinel is a marker the framework
dereferences elsewhere, never a path inside phase-ports.

Hard errors (PortResolutionError):
  - the config `schema_version` does not equal the contract bundle's
    `schema_version` (the message names both versions), or
  - the config `ports` map contains a key that is not one of the seven
    canonical port names (the message names the offending port).

Uses only the Python standard library (json, pathlib).

Version: 1.0.0
Owner: rabbit-workflow team
Deprecation criterion: superseded when per-port independent contract
  versioning replaces the single-bundle version (deferred to v2).
"""

import json
from pathlib import Path

#: The seven swappable adapter ports, in pipeline order. The four core phases
#: (GUARD, DRAIN, PERSIST, EXIT) are fixed and never appear here.
CANONICAL_PORTS = (
    "PULL", "TRIAGE", "PRIORITIZE", "IMPLEMENT", "VERIFY", "INTEGRATE", "CLEANUP",
)

#: The sentinel an unresolved port resolves to. The default adapter it stands
#: for is owned by a sibling feature, not phase-ports (v1 decision 2).
DEFAULT = "default"


class PortResolutionError(Exception):
    """Raised on a schema_version mismatch or an unknown port key in the
    project ports.json. The message identifies the offending value."""


def _load_json(path):
    with open(path, "r") as f:
        return json.load(f)


def resolve_ports(config_path, contracts_path):
    """Resolve every canonical port to an override path or the default sentinel.

    config_path    — path to the project's ports.json
                     ({"schema_version": ..., "ports": {name: script_path}}).
    contracts_path — path to the shipped port-contracts.json bundle.

    Returns a dict with exactly the seven canonical port names as keys.
    Raises PortResolutionError on a schema_version mismatch or unknown port.
    """
    config = _load_json(Path(config_path))
    contracts = _load_json(Path(contracts_path))

    bundle_version = contracts.get("schema_version")
    config_version = config.get("schema_version")
    if config_version != bundle_version:
        raise PortResolutionError(
            f"ports.json schema_version {config_version!r} does not match "
            f"port-contracts bundle schema_version {bundle_version!r}"
        )

    overrides = config.get("ports", {})
    for name in overrides:
        if name not in CANONICAL_PORTS:
            raise PortResolutionError(
                f"unknown port {name!r} in ports.json "
                f"(valid ports: {', '.join(CANONICAL_PORTS)})"
            )

    return {name: overrides.get(name, DEFAULT) for name in CANONICAL_PORTS}
