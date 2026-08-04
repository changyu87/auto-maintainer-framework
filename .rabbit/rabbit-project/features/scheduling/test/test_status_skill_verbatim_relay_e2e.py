#!/usr/bin/env python3
"""End-to-end conformance tests for the SHIPPED /auto-maintainer:status skill's
relay wording.

status.py renders a MULTI-LINE human view (since v0.14.0), but the v0.1.0 status
SKILL.md still told the model the script "prints one status line" and to "report
the line ... verbatim" — so the model wrote a one-line prose summary and the full
formatted view stayed hidden inside Claude Code's folded Bash tool output.

The fix is a skill-relay-prose change ONLY (status.py is UNCHANGED): the status
skill body MUST instruct the model to reproduce status.py's FULL rendered human
view VERBATIM in a fenced code block in its reply, NOT a one-line summary. These
tests assert that wiring + the frontmatter version bump (0.1.0 -> 0.2.0), without
asserting exact prose.

Owner: changyu87
"""

import os

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_STATUS_SKILL = os.path.join(_FEATURE_DIR, "ship", "skills", "status", "SKILL.md")


def _read(path):
    with open(path, "r") as f:
        return f.read()


def _parse_frontmatter(path):
    text = _read(path)
    assert text.startswith("---\n"), ("missing frontmatter open", path)
    body = text[4:]
    end = body.index("\n---\n")
    block = body[:end]
    fields = {}
    for line in block.splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    return fields


def _body(path):
    text = _read(path)
    body = text[4:]
    end = body.index("\n---\n")
    return body[end + 5:]


def test_status_skill_exists_with_required_metadata():
    """The shipped status skill exists and carries the required lifecycle
    metadata (spec-rules §3), with the version bumped to 0.2.0."""
    assert os.path.isfile(_STATUS_SKILL), _STATUS_SKILL
    fm = _parse_frontmatter(_STATUS_SKILL)
    for key in ("name", "description", "version", "owner",
                "deprecation_criterion"):
        assert key in fm and fm[key], (key, fm)
    assert fm["name"] == "status", fm["name"]
    assert fm["version"] == "0.2.0", ("version must bump to 0.2.0", fm["version"])


def test_status_skill_invokes_backing_script():
    """The status skill stays script-backed: it invokes status.py and owns no
    Python (spec-rules §4 Script-Backed Orchestration)."""
    body = _body(_STATUS_SKILL)
    assert "status.py" in body, body


def test_status_skill_instructs_verbatim_fenced_relay():
    """The skill body MUST instruct a VERBATIM relay inside a fenced code block of
    the FULL rendered view (not a one-line summary)."""
    body = _body(_STATUS_SKILL).lower()
    assert "verbatim" in body, body
    assert "fenced" in body or "code block" in body, body


def test_status_skill_drops_stale_one_line_wording():
    """The stale v0.1.0 wording is corrected: the body must NOT tell the model the
    script prints 'one status line' nor to 'report the line' verbatim."""
    body = _body(_STATUS_SKILL).lower()
    assert "one status line" not in body, body
    assert "report the line" not in body, body


def test_status_skill_dispatches_no_subagent():
    """The status skill is a guided, non-dispatching relay (no Agent(/subagent)."""
    body = _body(_STATUS_SKILL)
    assert "Agent(" not in body, "status skill must not dispatch an Agent"
    assert "subagent_type=" not in body, "status skill must not name a subagent"
