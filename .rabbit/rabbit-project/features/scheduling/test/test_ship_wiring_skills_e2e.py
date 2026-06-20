#!/usr/bin/env python3
"""End-to-end conformance tests for scheduling's SHIPPED wiring-config skills.

This cycle SHIPS two guided wiring-config skills into scheduling's ship/ tree (so
the packaging build's _copy_tree(ship_dir, plugin_root) collects them verbatim):

  - ship/skills/route/SKILL.md        (/auto-maintainer:route)
      a guided route.json editor — recommends keeping DEFAULT_ROUTE; on a user
      edit it walks the change and calls route_config.py to validate + write;
      dispatches NO subagent.
  - ship/skills/adapter-map/SKILL.md  (/auto-maintainer:adapter-map)
      a guided adapter-map.json editor — recommends DEFAULT_ADAPTER_MAP; to wire
      an agent the user gives subagent_type (+ port) and the skill calls
      adapter_map_config.py (fills the entry from AGENT_PORT_TEMPLATES for a known
      port, validates, writes); dispatches NO subagent.

These tests prove the SHIPPED skills are real, well-formed (frontmatter +
required metadata), invoke the backing scripts (not hand-rolled JSON / Python),
recommend the defaults, and dispatch NO subagent — without asserting prose:

  1. Both ship SKILL.md files exist + carry the required lifecycle metadata
     (name/description/version/owner/deprecation_criterion).
  2. Each skill body INVOKES its backing script (route_config.py /
     adapter_map_config.py) — the script-tier orchestration rule (spec-rules §4).
  3. Neither skill body carries a model-assembled runtime-placeholder bash block
     (no `<...>` placeholder inside a fenced bash block) — the no-prompt-tier-bash
     rule (spec-rules §4 Script-Backed Orchestration).
  4. Neither skill dispatches a subagent (no `Agent(` / `subagent_type=` call in
     the body) — both are guided, non-dispatching CLIs.
  5. Each skill RECOMMENDS keeping the default (route / adapter-map).

scheduling CONSUMES adapter-wiring + agent-dispatch UNCHANGED. The skill CONTENT
is authored + skill-creator-validated by the orchestrator and placed verbatim;
these tests assert the wiring + metadata, not the prose.

Owner: changyu87
"""

import os

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SHIP_DIR = os.path.join(_FEATURE_DIR, "ship")
_ROUTE_SKILL = os.path.join(_SHIP_DIR, "skills", "route", "SKILL.md")
_ADAPTER_MAP_SKILL = os.path.join(_SHIP_DIR, "skills", "adapter-map", "SKILL.md")

_REQUIRED_META = ("name", "description", "version", "owner",
                  "deprecation_criterion")


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
    """The SKILL.md body BELOW the frontmatter block."""
    text = _read(path)
    body = text[4:]
    end = body.index("\n---\n")
    return body[end + 5:]


def _fenced_bash_blocks(body):
    """Yield the contents of each ```...``` fenced code block (any language fence
    or bare ```), so a model-assembled runtime-placeholder check can scan them."""
    blocks = []
    parts = body.split("```")
    # Odd-indexed parts are inside fences.
    for i in range(1, len(parts), 2):
        block = parts[i]
        # Drop an optional language tag on the first line.
        if "\n" in block:
            block = block.split("\n", 1)[1]
        blocks.append(block)
    return blocks


def test_both_wiring_skills_exist_with_required_metadata():
    """Both shipped wiring skills exist and carry the required lifecycle metadata
    (spec-rules §3): name/description/version/owner/deprecation_criterion."""
    for path, name in ((_ROUTE_SKILL, "route"),
                       (_ADAPTER_MAP_SKILL, "adapter-map")):
        assert os.path.isfile(path), path
        fm = _parse_frontmatter(path)
        for key in _REQUIRED_META:
            assert key in fm and fm[key], (path, key, fm)
        assert fm["name"] == name, (path, fm["name"])
        # Owner is the rabbit-workflow team for distributed components, OR the
        # repo owner (matching the sibling shipped skills' convention).
        assert fm["owner"], (path, fm)


def test_route_skill_invokes_backing_script():
    """The route skill body INVOKES route_config.py (script-tier orchestration,
    spec-rules §4) — it does NOT hand-roll route.json editing in prose."""
    body = _body(_ROUTE_SKILL)
    assert "route_config.py" in body, body


def test_adapter_map_skill_invokes_backing_script():
    """The adapter-map skill body INVOKES adapter_map_config.py (script-tier
    orchestration) — it does NOT hand-roll adapter-map.json editing in prose."""
    body = _body(_ADAPTER_MAP_SKILL)
    assert "adapter_map_config.py" in body, body


def test_wiring_skills_have_no_runtime_placeholder_bash():
    """Neither skill body carries a model-assembled runtime-placeholder bash block
    (a `<...>` placeholder the model fills at invocation time) — the no-prompt-tier
    -bash rule (spec-rules §4 Script-Backed Orchestration). A `${CLAUDE_*}` env
    expansion is NOT a model-assembled placeholder and is allowed."""
    import re
    placeholder = re.compile(r"<[a-zA-Z][a-zA-Z0-9 _-]*>")
    for path in (_ROUTE_SKILL, _ADAPTER_MAP_SKILL):
        for block in _fenced_bash_blocks(_body(path)):
            assert not placeholder.search(block), (path, block)


def test_wiring_skills_dispatch_no_subagent():
    """Neither wiring skill dispatches a subagent (no Agent( / subagent_type= in
    the body) — both are guided, non-dispatching CLIs (spec / SKILL.md §4 the
    no-subagent-dispatching constraint does not list these)."""
    for path in (_ROUTE_SKILL, _ADAPTER_MAP_SKILL):
        body = _body(path)
        assert "Agent(" not in body, (path, "must not dispatch an Agent")
        assert "subagent_type=" not in body, (path, "must not name a subagent")


def test_route_skill_recommends_default():
    """The route skill RECOMMENDS keeping the default route (the spec's guidance:
    recommend defaults)."""
    body = _body(_ROUTE_SKILL).lower()
    assert "default" in body and "recommend" in body, body


def test_adapter_map_skill_recommends_default():
    """The adapter-map skill RECOMMENDS the default map."""
    body = _body(_ADAPTER_MAP_SKILL).lower()
    assert "default" in body and "recommend" in body, body
