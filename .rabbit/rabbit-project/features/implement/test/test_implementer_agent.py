#!/usr/bin/env python3
"""Presence + frontmatter-compliance test for the shipped implementer subagent.

This feature ships the model-backed implementer subagent (the propose-rung
doer) as a deployable artifact at ship/agents/auto-maintainer-implementer.md
(spec: "Shipped implementer subagent"). This test guards the shipped file's
PRESENCE and FRONTMATTER COMPLIANCE only — NOT its prose body:

- the shipped file exists;
- its YAML frontmatter parses and carries the lifecycle/identity keys
  (name, description, version, owner, deprecation_criterion);
- model is exactly `opus`;
- the tools list includes BOTH Bash and Write (a coding toolset).

Owner: changyu87
"""

import os

import yaml

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AGENT_PATH = os.path.join(
    _FEATURE_DIR, "ship", "agents", "auto-maintainer-implementer.md")


def _frontmatter():
    with open(_AGENT_PATH, "r") as f:
        text = f.read()
    assert text.startswith("---"), "agent file must open with YAML frontmatter"
    _, fm, _body = text.split("---", 2)
    return yaml.safe_load(fm)


def _body():
    with open(_AGENT_PATH, "r") as f:
        text = f.read()
    _, _fm, body = text.split("---", 2)
    return body


def test_shipped_implementer_agent_exists():
    assert os.path.isfile(_AGENT_PATH), (
        "shipped implementer subagent must exist at "
        "ship/agents/auto-maintainer-implementer.md")


def test_shipped_implementer_frontmatter_has_lifecycle_keys():
    fm = _frontmatter()
    for key in ("name", "description", "version", "owner",
                "deprecation_criterion"):
        assert key in fm, f"frontmatter missing required key: {key}"


def test_shipped_implementer_model_is_opus():
    fm = _frontmatter()
    assert fm["model"] == "opus"


def test_shipped_implementer_tools_include_bash_and_write():
    fm = _frontmatter()
    tools = fm["tools"]
    assert "Bash" in tools
    assert "Write" in tools


def test_shipped_implementer_body_stamps_auto_maintainer_label():
    """v2.1.0: an opened PR is stamped with the `auto-maintainer` label so
    VERIFY can find the loop's own open PRs (spec: PR provenance label).
    The accept path must pass `--label auto-maintainer` to `gh pr create`."""
    assert "--label auto-maintainer" in _body()


def test_shipped_implementer_body_has_self_review_checklist():
    """v2.3.0: on the accept path, BEFORE `gh pr create`, the implementer runs a
    structured pre-handoff self-review against its own committed diff and fixes
    any gap before opening the PR (spec: Pre-handoff self-review). The body must
    carry the self-review section and its checklist lenses."""
    body = _body().lower()
    assert "self-review" in body, "body must describe a pre-handoff self-review"
    # the lenses adopted from the superpowers implementer checklist
    for lens in ("completeness", "quality", "discipline"):
        assert lens in body, f"self-review checklist missing the {lens} lens"
    # it must review the ACTUAL committed diff, not just intent
    assert "diff" in body, "self-review must review the actual committed diff"
