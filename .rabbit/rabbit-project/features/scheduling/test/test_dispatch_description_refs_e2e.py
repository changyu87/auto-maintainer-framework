#!/usr/bin/env python3
"""End-to-end conformance tests for issue/PR-named Agent dispatch descriptions.

This cycle enhances run_tick's `_dispatch_description(dispatch_entry, name, env)`
so the Agent dispatch description NAMES the issue/PR each subagent works on. When
parallel subagents (e.g. an IMPLEMENT per-item set, or a REVIEW once dispatch over
several PRs) fan out, their descriptions then show DISTINCT, recognizable names
instead of a single generic "<state> dispatch" label.

Precedence (deterministic, pure):

  PER_ITEM (the envelope carries `item`, e.g. a work-order id
      'wo-owner/repo#275') -> ALWAYS name the item ref. The base is the
      explicit `dispatch_entry['description']` when present, else the state
      name; the item ref is appended -> 'auto-maintainer implement #275' /
      'IMPLEMENT #275'. A dict item prefers pr_ref / number / id. When the
      item yields NO derivable ref, fall back to the explicit description
      (verbatim) when present, else '<name> dispatch'.
  ONCE (no `item`) -> (0) an explicit `dispatch_entry['description']` still
      WINS verbatim; else scan `env['inputs']` list-of-dicts, collecting a ref
      per element from `pr_ref` (REVIEW verdicts) or `number` (TRIAGE
      work_items) -> 'REVIEW #276, #277' / 'TRIAGE #275, #276' (de-duped,
      order-preserving, capped ~6 with '+K more'); else the existing
      '<name> dispatch' fallback.

The per_item-always-names-the-ref rule (this cycle) is the dogfood fix: the
IMPLEMENT adapter entry carries an explicit description 'auto-maintainer
implement', so under the old explicit-wins-verbatim precedence EVERY per-item
implementer subagent showed the same static label with no number, defeating
distinct parallel names.

A small pure helper `_dispatch_refs(env)` collects the refs; both it and
`_dispatch_description` are pure and deterministic. The dispatch PROMPT body,
output_path, schema, signal logic, and cardinality are UNCHANGED — only the
description label.

scheduling CONSUMES its siblings UNCHANGED via sys.path; it does not edit them.

Owner: changyu87
"""

import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FEATURES = os.path.dirname(_FEATURE_DIR)
for _dep in ("fsm-contracts", "tick-orchestrator", "durable-state",
             "lifecycle-dispositions", "work-intake", "adapter-wiring",
             "prioritize", "implement", "agent-dispatch", "safety-governance",
             "verify-integrate"):
    _dep_src = os.path.join(_FEATURES, _dep, "src")
    if _dep_src not in sys.path:
        sys.path.insert(0, _dep_src)

import run_tick as rt  # noqa: E402


# --- (a) per_item: the description names the work order's trailing #NNN ------

def test_implement_per_item_description_names_the_item_number():
    """A per_item IMPLEMENT dispatch over a work-order id 'wo-owner/repo#275'
    derives '#275' (substring after the LAST '#')."""
    env = {"item": "wo-owner/repo#275", "inputs": {}}
    desc = rt._dispatch_description({}, "IMPLEMENT", env)
    assert "#275" in desc, desc
    assert desc == "IMPLEMENT #275", desc


def test_per_item_dict_prefers_pr_ref_then_number_then_id():
    """A dict item prefers pr_ref, then number, then id."""
    env = {"item": {"pr_ref": "owner/repo#42", "number": 9, "id": "x"},
           "inputs": {}}
    assert "#42" in rt._dispatch_description({}, "VERIFY", env)

    env = {"item": {"number": 9, "id": "x"}, "inputs": {}}
    assert "#9" in rt._dispatch_description({}, "VERIFY", env)

    env = {"item": {"id": "wo-owner/repo#7"}, "inputs": {}}
    assert "#7" in rt._dispatch_description({}, "VERIFY", env)


# --- (b) once: scan inputs list-of-dicts for pr_ref / number ----------------

def test_review_once_description_names_verdict_pr_refs():
    """A REVIEW once dispatch reads `verdicts` (pr_ref owner/repo#276,#277);
    the description names both PR numbers."""
    env = {
        "inputs": {
            "verdicts": [
                {"pr_ref": "owner/repo#276", "ok": True},
                {"pr_ref": "owner/repo#277", "ok": False},
            ]
        }
    }
    desc = rt._dispatch_description({}, "REVIEW", env)
    assert "#276" in desc, desc
    assert "#277" in desc, desc
    assert desc == "REVIEW #276, #277", desc


def test_triage_once_description_names_work_item_numbers():
    """A TRIAGE once dispatch reads `work_items` (number 275, 276); the
    description names both issue numbers."""
    env = {
        "inputs": {
            "work_items": [
                {"number": 275, "title": "a"},
                {"number": 276, "title": "b"},
            ]
        }
    }
    desc = rt._dispatch_description({}, "TRIAGE", env)
    assert "#275" in desc, desc
    assert "#276" in desc, desc
    assert desc == "TRIAGE #275, #276", desc


# --- per_item: explicit description is a PREFIX, item ref appended ----------

def test_per_item_explicit_description_is_prefix_with_item_ref():
    """The dogfood case: a per_item IMPLEMENT dispatch whose entry carries the
    explicit description 'auto-maintainer implement' (from set-agent) plus a
    work-order item id 'wo-owner/repo#275' names the item ref AS WELL — the
    explicit description is the PREFIX, not the verbatim whole. Without this,
    every parallel implementer subagent showed the same static label with no
    number (the FT-4 gap)."""
    entry = {"description": "auto-maintainer implement"}
    env = {"item": "wo-owner/repo#275", "inputs": {}}
    assert (rt._dispatch_description(entry, "IMPLEMENT", env)
            == "auto-maintainer implement #275")


def test_per_item_explicit_description_falls_back_when_no_ref():
    """A per_item dispatch whose item yields NO derivable ref falls back to the
    explicit description verbatim (graceful, no trailing ' ' / 'None')."""
    entry = {"description": "auto-maintainer implement"}
    env = {"item": {"foo": "bar"}, "inputs": {}}
    assert (rt._dispatch_description(entry, "IMPLEMENT", env)
            == "auto-maintainer implement")


def test_per_item_no_explicit_description_uses_name_prefix():
    """Regression-protect FT-4: a per_item dispatch with NO explicit description
    + an item id '...#275' still derives 'IMPLEMENT #275' (name as prefix)."""
    env = {"item": "wo-owner/repo#275", "inputs": {}}
    assert rt._dispatch_description({}, "IMPLEMENT", env) == "IMPLEMENT #275"


# --- (0) once: explicit description wins verbatim ---------------------------

def test_once_explicit_description_wins_verbatim():
    """A ONCE dispatch (no `item`) with an explicit description is returned
    verbatim even when refs could be derived from inputs."""
    entry = {"description": "review the open PRs once"}
    env = {"inputs": {"verdicts": [{"pr_ref": "owner/repo#276"}]}}
    assert (rt._dispatch_description(entry, "REVIEW", env)
            == "review the open PRs once")


# --- (c) no refs: existing '<name> dispatch' fallback -----------------------

def test_no_refs_falls_back_to_name_dispatch():
    """With no derivable refs (no item, no ref-bearing inputs), the existing
    '<name> dispatch' fallback is used."""
    env = {"inputs": {"work_orders": [{"foo": "bar"}]}}
    assert rt._dispatch_description({}, "PRIORITIZE", env) == "PRIORITIZE dispatch"

    env = {"inputs": {}}
    assert rt._dispatch_description({}, "TRIAGE", env) == "TRIAGE dispatch"


# --- de-dup + order-preserving + truncation ---------------------------------

def test_refs_deduped_and_order_preserving():
    env = {
        "inputs": {
            "verdicts": [
                {"pr_ref": "o/r#5"},
                {"pr_ref": "o/r#3"},
                {"pr_ref": "o/r#5"},
            ]
        }
    }
    assert rt._dispatch_refs(env) == ["#5", "#3"]


def test_long_ref_list_is_truncated_with_plus_k_more():
    items = [{"number": n} for n in range(1, 11)]  # 10 items
    env = {"inputs": {"work_items": items}}
    desc = rt._dispatch_description({}, "TRIAGE", env)
    assert "+" in desc and "more" in desc, desc
    # first six named, remaining four summarized.
    assert "#1" in desc and "#6" in desc, desc
    assert "+4 more" in desc, desc


# --- _dispatch_refs is pure (no mutation of env) ----------------------------

def test_dispatch_refs_does_not_mutate_env():
    env = {"inputs": {"work_items": [{"number": 1}]}, "item": "wo#9"}
    snapshot = repr(env)
    rt._dispatch_refs(env)
    assert repr(env) == snapshot
