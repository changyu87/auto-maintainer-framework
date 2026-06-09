"""contract.lib.policy_block — canonical policy-block framing renderer.

Holds the sentinel line, header banner, per-file section separator
convention, and footer banner used by every policy-block consumer
(scripts/policy-block.py and scripts/build-prompt.py).

The canonical framing is the ONLY policy-block contract. Inlining these
strings elsewhere is a drift hazard: two copies will diverge silently and
downstream prompt parsers (e.g. the Stop hook's sentinel check) will miss
matches. Every producer of policy-block-framed content MUST route through
render_policy_block.

Public API:
  render_policy_block(paths: list[str]) -> str
      Returns the canonical policy block: sentinel line, header banner,
      one section per path (separator line of '──────── <basename> ────────'
      plus the file's full contents), and the footer banner. Raises
      FileNotFoundError if any path does not exist.

Per spec Inv 46.

Version: 1.0.0
Owner: rabbit-workflow team (contract)
Deprecation criterion: when Claude Code exposes a native policy-injection
    mechanism that replaces the rabbit policy-block contract.
"""

import os
from typing import List


SENTINEL = "RABBIT-POLICY-BLOCK-v1"

_HEADER = """\
═══════════════════════════════════════════════════════════════════════════════
MANDATORY POLICY — READ THIS BEFORE ANY ACTION
═══════════════════════════════════════════════════════════════════════════════

You are operating within the rabbit workflow. The following policy files are
NOT optional reading. They govern every choice you make in this invocation.
Failure to comply is a constitution violation.

If you have not yet internalized these principles, STOP and read them now
before doing anything else. Re-read them whenever you are uncertain about
how to proceed. They are the source of truth for every decision in this
session.
"""

_FOOTER = """\
═══════════════════════════════════════════════════════════════════════════════
END POLICY — internalize the above, then proceed. Every action must reflect it.
═══════════════════════════════════════════════════════════════════════════════"""

_SEPARATOR_BAR = "─" * 18


def _render_section(path: str) -> str:
    label = os.path.basename(path)
    with open(path) as f:
        body = f.read()
    return f"{_SEPARATOR_BAR} {label} {_SEPARATOR_BAR}\n{body}"


def render_policy_block(paths: List[str]) -> str:
    """Return the canonical policy block as a single string.

    paths - ordered list of policy file paths. Each file's basename is the
            section label and its full contents are the section body.

    Raises FileNotFoundError if any path does not exist (matches the
    upstream contract caller's "fail loud on missing policy file" expectation).
    """
    for p in paths:
        if not os.path.isfile(p):
            raise FileNotFoundError(p)

    parts = [SENTINEL, "", _HEADER]
    for p in paths:
        parts.append(_render_section(p))
    parts.append(_FOOTER)
    return "\n".join(parts)
