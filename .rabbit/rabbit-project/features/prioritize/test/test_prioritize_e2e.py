#!/usr/bin/env python3
"""End-to-end + unit tests for the prioritize PRIORITIZE adapter state.

PRIORITIZE reads the `work_orders` slot (the array of decision-carrying
WorkOrder dicts TRIAGE produced), keeps only those with
`decision == "accepted"`, preserves TRIAGE's order (identity / FIFO — there is
no severity/priority key on a WorkOrder), back-fills `status[id] = "pending"`
for every planned order, writes the `execution_plan` slot, and emits OK when the
plan has at least one entry else EMPTY.

PRIORITIZE is DETERMINISTIC and effect-free: it is a pure function of
`work_orders` — no model, no wall-clock, no randomness, no network, no
filesystem. The same input yields a byte-identical plan.

The e2e tests drive PRIORITIZE exactly as tick-orchestrator will — building a
real fsm-contracts TickContext, registering the `work_orders` + `execution_plan`
slots, seeding `work_orders`, running the state, and committing its StateResult
through `fc.apply_result` under the manifest + signal vocabulary
(bounded-scope).

Owner: changyu87
"""

import json
import os
import sys

_FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SRC = os.path.join(_FEATURE_DIR, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_FSM_SRC = os.path.join(
    os.path.dirname(_FEATURE_DIR), "fsm-contracts", "src")
if _FSM_SRC not in sys.path:
    sys.path.insert(0, _FSM_SRC)

import fsm_contracts as fc  # noqa: E402
import prioritize as pr  # noqa: E402


# --------------------------------------------------------------------------
# Fixtures — a builder for accepted/rejected WorkOrder dicts and a fresh ctx.
# --------------------------------------------------------------------------

def _order(oid, decision="accepted", reason="", labels=None, body="body",
           title=None):
    """Build a minimal WorkOrder dict in the shape TRIAGE writes. PRIORITIZE
    consumes `id`, `decision`, and (for same-feature serialization) `labels`,
    `body`, and `title`; the remaining fields carry sane filler. The default
    `title` is a plain string with no conventional prefix, so it declares no
    feature unless a caller passes one explicitly."""
    return {
        "schema_version": "1.0.0",
        "id": oid,
        "work_item_id": oid.replace("wo-", ""),
        "title": title if title is not None else f"title for {oid}",
        "body": body,
        "url": f"https://github.com/acme/widget/issues/{oid}",
        "labels": list(labels) if labels is not None else [],
        "decision": decision,
        "reason": reason,
        "created_at": "2026-05-01T10:00:00Z",
    }


def _fresh_ctx(work_orders=None):
    ctx = fc.TickContext()
    # `work_orders` is the upstream slot work-intake's TRIAGE owns; PRIORITIZE
    # only reads it. The test registers it as the array slot tick-orchestrator
    # would, with no cross-feature import (PRIORITIZE consumes only the dict
    # `id` + `decision` fields, not work-intake's WorkOrder type).
    ctx.register_slot("work_orders", {"type": "array"}, version="1.0.0")
    ctx.register_slot(
        pr.EXECUTION_PLAN_SLOT["name"],
        pr.EXECUTION_PLAN_SLOT["schema"],
        version=pr.EXECUTION_PLAN_SLOT["version"],
    )
    if work_orders is not None:
        ctx.write("work_orders", work_orders)
    return ctx


# ==========================================================================
# Behaviour: the ExecutionPlan slot descriptor is typed, machine-first, versioned
# and mirrors work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
# ==========================================================================

def test_execution_plan_slot_descriptor_is_versioned():
    slot = pr.EXECUTION_PLAN_SLOT
    assert slot["name"] == "execution_plan"
    assert slot["schema"] == {"type": "object"}
    assert slot["version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION
    assert pr.EXECUTION_PLAN_SCHEMA_VERSION == "1.0.0"


# ==========================================================================
# Behaviour: per-state manifest is {reads: [work_orders],
# writes: [execution_plan], emits: [OK, EMPTY]} and conforms to the
# fsm-contracts manifest shape.
# ==========================================================================

def test_prioritize_manifest_declares_reads_writes_emits():
    m = pr.PRIORITIZE_MANIFEST
    assert isinstance(m, fc.StateManifest)
    assert m.reads == ("work_orders",)
    assert m.writes == ("execution_plan",)
    assert set(m.emits) == {"OK", "EMPTY"}


def test_prioritize_signal_vocabulary_is_closed():
    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    assert vocab.is_member("OK")
    assert vocab.is_member("EMPTY")
    assert not vocab.is_member("MAYBE")


# ==========================================================================
# E2E Behaviour: all-accepted orders -> execution_plan.ordered preserves the
# input order; status backfilled "pending" for each; signal OK.
# ==========================================================================

def test_prioritize_e2e_accepted_preserves_order_and_backfills_pending():
    orders = [_order("wo-3"), _order("wo-1"), _order("wo-2")]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)

    assert fc.validate_state_result(result).passed is True
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert isinstance(plan, dict)
    # Identity / FIFO ordering: relative order preserved exactly (NOT sorted).
    assert plan["ordered"] == ["wo-3", "wo-1", "wo-2"]
    # Every planned order starts "pending".
    assert plan["status"] == {"wo-3": "pending", "wo-1": "pending",
                              "wo-2": "pending"}
    assert plan["schema_version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION


# ==========================================================================
# E2E Behaviour: mixed accepted/rejected -> only accepted appear, in order;
# rejected excluded from both `ordered` and `status`; signal OK.
# ==========================================================================

def test_prioritize_e2e_mixed_excludes_rejected_preserves_order():
    orders = [
        _order("wo-10", decision="accepted"),
        _order("wo-11", decision="rejected", reason="not open"),
        _order("wo-12", decision="accepted"),
        _order("wo-13", decision="rejected", reason="stale"),
    ]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)
    assert result.signal == "OK"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == ["wo-10", "wo-12"]
    assert plan["status"] == {"wo-10": "pending", "wo-12": "pending"}
    # Rejected orders are absent from both maps.
    assert "wo-11" not in plan["status"]
    assert "wo-13" not in plan["status"]
    assert "wo-11" not in plan["ordered"]
    assert "wo-13" not in plan["ordered"]


# ==========================================================================
# E2E Behaviour: zero accepted (empty slot) -> ordered=[], status={}, EMPTY.
# ==========================================================================

def test_prioritize_e2e_empty_work_orders_emits_empty():
    ctx = _fresh_ctx(work_orders=[])

    result = pr.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == []
    assert plan["status"] == {}
    assert plan["schema_version"] == pr.EXECUTION_PLAN_SCHEMA_VERSION


# ==========================================================================
# E2E Behaviour: all-rejected -> ordered=[], status={}, signal EMPTY.
# ==========================================================================

def test_prioritize_e2e_all_rejected_emits_empty():
    orders = [
        _order("wo-20", decision="rejected", reason="not open"),
        _order("wo-21", decision="rejected", reason="malformed"),
    ]
    ctx = _fresh_ctx(work_orders=orders)

    result = pr.run(ctx)
    assert result.signal == "EMPTY"

    vocab = fc.SignalVocabulary(pr.PRIORITIZE_SIGNALS)
    fc.apply_result(ctx, pr.PRIORITIZE_MANIFEST, result, vocab)

    plan = ctx.read("execution_plan")
    assert plan["ordered"] == []
    assert plan["status"] == {}


# ==========================================================================
# Behaviour: determinism — same input twice yields a byte-identical plan.
# ==========================================================================

def test_prioritize_is_deterministic_byte_identical():
    orders = [_order("wo-3"), _order("wo-1"), _order("wo-2")]

    ctx_a = _fresh_ctx(work_orders=orders)
    ctx_b = _fresh_ctx(work_orders=orders)

    plan_a = pr.run(ctx_a).writes["execution_plan"]
    plan_b = pr.run(ctx_b).writes["execution_plan"]

    assert json.dumps(plan_a, sort_keys=True) == json.dumps(plan_b,
                                                            sort_keys=True)


# ==========================================================================
# Behaviour (scope guard): the v1 execution_plan carries NO `groups` key —
# parallel grouping is deferred to v2 (DESIGN §1.1 [v2]).
# ==========================================================================

def test_prioritize_plan_has_no_groups_key():
    ctx = _fresh_ctx(work_orders=[_order("wo-1")])
    plan = pr.run(ctx).writes["execution_plan"]
    assert "groups" not in plan
    # The v1 plan surface is exactly schema_version + ordered + status.
    assert set(plan.keys()) == {"schema_version", "ordered", "status"}


# ==========================================================================
# Behaviour: the factory follows the adapter-wiring convention —
# factory(runtime) -> (StateManifest, run_callable). The returned callable IS
# the state's run; the returned manifest IS PRIORITIZE_MANIFEST.
# ==========================================================================

def test_factory_returns_manifest_and_run_callable():
    manifest, run = pr.factory({})
    assert manifest is pr.PRIORITIZE_MANIFEST
    assert callable(run)

    ctx = _fresh_ctx(work_orders=[_order("wo-1"), _order("wo-2")])
    result = run(ctx)
    assert result.signal == "OK"
    assert result.writes["execution_plan"]["ordered"] == ["wo-1", "wo-2"]


# ==========================================================================
# Same-feature serialization (issue #214). Two work orders that touch the SAME
# feature would each bump that feature's shared metadata off the same `main` and
# collide on merge, so PRIORITIZE keeps at most one per feature per tick (the
# FIFO-first wins) and defers the rest. Cross-feature orders stay parallel.
# ==========================================================================

def _plan(work_orders):
    return pr.run(_fresh_ctx(work_orders=work_orders)).writes["execution_plan"]


def test_same_feature_label_orders_serialize_fifo_first_wins():
    # Two orders both labelled feature:scheduling -> only the FIFO-first fans
    # out this tick; the second is deferred (absent from the plan).
    plan = _plan([
        _order("wo-1", labels=["feature:scheduling"]),
        _order("wo-2", labels=["feature:scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1"]
    assert plan["status"] == {"wo-1": "pending"}


def test_cross_feature_label_orders_stay_parallel():
    # Different feature: labels -> both fan out (the non-colliding case).
    plan = _plan([
        _order("wo-1", labels=["feature:scheduling"]),
        _order("wo-2", labels=["feature:packaging-config"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_generic_labelled_different_feature_orders_stay_parallel():
    # The #214 detection-fix regression guard: two orders sharing ONLY a generic
    # `enhancement` label, but naming DIFFERENT features in their bodies, must
    # NOT serialize against each other (generic labels are not feature keys).
    plan = _plan([
        _order("wo-1", labels=["enhancement"], body="Component: scheduling"),
        _order("wo-2", labels=["enhancement"], body="Component: prioritize"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_shared_generic_label_alone_does_not_serialize():
    # Orders sharing only generic labels (enhancement/bug/filed-by:*) with no
    # provable feature stay parallel — serialize only on a proven shared feature.
    plan = _plan([
        _order("wo-1", labels=["enhancement", "filed-by:autonomous-maintainer"]),
        _order("wo-2", labels=["bug", "filed-by:autonomous-maintainer"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_orders_with_no_provable_feature_stay_parallel():
    # No labels, no Component: line -> no provable feature -> all stay parallel.
    plan = _plan([_order("wo-1"), _order("wo-2"), _order("wo-3")])
    assert plan["ordered"] == ["wo-1", "wo-2", "wo-3"]


def test_component_body_line_serializes_same_feature():
    # A `Component:` body line is an authoritative feature signal even with no
    # feature: label present.
    plan = _plan([
        _order("wo-1", body="Some text.\nComponent: scheduling\n"),
        _order("wo-2", body="Other.\nComponent: scheduling\n"),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_component_label_prefix_is_recognized():
    # The `component:<name>` label prefix is honored alongside `feature:<name>`.
    plan = _plan([
        _order("wo-1", labels=["component:scheduling"]),
        _order("wo-2", labels=["component:scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_feature_detection_is_case_and_punctuation_insensitive():
    # A label-declared feature and a body-declared feature with different case
    # resolve to the same key and serialize.
    plan = _plan([
        _order("wo-1", labels=["feature:Scheduling"]),
        _order("wo-2", body="component:  scheduling  "),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_multi_feature_body_radius_claims_each_feature():
    # A multi-feature Component: radius (split on +,&/,) claims BOTH features, so
    # a later order touching EITHER is deferred; an unrelated feature stays.
    plan = _plan([
        _order("wo-1", body="Component: scheduling, prioritize"),
        _order("wo-2", labels=["feature:prioritize"]),
        _order("wo-3", labels=["feature:packaging-config"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-3"]


def test_and_is_not_split_as_a_feature_connector():
    # "and" must NOT split a feature name: "command-and-control" is ONE feature,
    # not three, so two orders naming it serialize (not falsely cross-feature).
    plan = _plan([
        _order("wo-1", body="Component: command-and-control"),
        _order("wo-2", body="Component: command-and-control"),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_serialization_preserves_fifo_for_kept_orders():
    # Interleaved features: the FIFO-first of each feature is kept in input order;
    # later same-feature orders are dropped without reordering the survivors.
    plan = _plan([
        _order("wo-1", labels=["feature:a"]),
        _order("wo-2", labels=["feature:b"]),
        _order("wo-3", labels=["feature:a"]),  # deferred (a claimed by wo-1)
        _order("wo-4", labels=["feature:c"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2", "wo-4"]


def test_rejected_orders_never_claim_a_feature():
    # A rejected order is excluded before serialization, so it cannot claim a
    # feature slot and starve an accepted same-feature order.
    plan = _plan([
        _order("wo-1", decision="rejected", reason="stale",
               labels=["feature:scheduling"]),
        _order("wo-2", decision="accepted", labels=["feature:scheduling"]),
    ])
    assert plan["ordered"] == ["wo-2"]


def test_serialization_is_deterministic():
    orders = [
        _order("wo-1", labels=["feature:scheduling"]),
        _order("wo-2", labels=["feature:scheduling"]),
        _order("wo-3", labels=["feature:packaging-config"]),
    ]
    plan_a = _plan(orders)
    plan_b = _plan(orders)
    assert json.dumps(plan_a, sort_keys=True) == json.dumps(
        plan_b, sort_keys=True)


def test_serialized_plan_still_has_no_groups_key():
    # Serialization only shrinks `ordered`; the v1 plan surface is unchanged.
    plan = _plan([
        _order("wo-1", labels=["feature:scheduling"]),
        _order("wo-2", labels=["feature:scheduling"]),
    ])
    assert set(plan.keys()) == {"schema_version", "ordered", "status"}


# ==========================================================================
# Title-prefix feature detection (issue #257). Label-less issues (e.g. the
# historical #205/#207) carry no feature: label and no Component: body line, so
# they would fan out in parallel and collide. _features_for ALSO derives the
# feature from a conventional title prefix — `name:` (take name) and
# `type(scope):` (take scope) — while EXCLUDING a bare conventional-commit type
# without a scope (so `fix:`/`docs:` do not group unrelated orders, the #216
# over-serialization regression).
# ==========================================================================

def _titled(oid, title):
    # A label-less, body-less order whose ONLY feature signal is its title.
    return _order(oid, labels=[], body="No feature signal here.", title=title)


def test_labelless_bare_name_title_prefix_serializes():
    # Two label-less orders titled `scheduling: ...` serialize (FIFO-first wins).
    plan = _plan([
        _titled("wo-1", "scheduling: add retry backoff"),
        _titled("wo-2", "scheduling: tighten heartbeat window"),
    ])
    assert plan["ordered"] == ["wo-1"]
    assert plan["status"] == {"wo-1": "pending"}


def test_labelless_scoped_conventional_title_prefix_serializes():
    # `type(scope):` headers serialize on the SCOPE (the feature), whatever the
    # conventional-commit type — `feat(scheduling)` and `fix(scheduling)` group.
    plan = _plan([
        _titled("wo-1", "feat(scheduling): add a tick"),
        _titled("wo-2", "fix(scheduling): correct a tick"),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_labelless_cross_feature_title_prefixes_stay_parallel():
    # Different title-derived features stay parallel (the non-colliding case).
    plan = _plan([
        _titled("wo-1", "scheduling: add retry backoff"),
        _titled("wo-2", "fix(work-intake): drop a stale order"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_bare_conventional_commit_type_titles_do_not_serialize():
    # A bare `type:` prefix with NO scope names no feature, so unrelated `fix:`
    # and `docs:` orders are NOT grouped (the #216 over-serialization guard).
    plan = _plan([
        _titled("wo-1", "fix: correct a typo"),
        _titled("wo-2", "docs: clarify a heading"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_two_bare_fix_titles_still_stay_parallel():
    # Even two `fix:` orders must stay parallel — a bare type is not a feature.
    plan = _plan([
        _titled("wo-1", "fix: one thing"),
        _titled("wo-2", "fix: another unrelated thing"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_title_prefix_agrees_with_label_and_body_signals():
    # A title-derived feature unifies with the label/body signals: an order
    # titled `scheduling: ...` is deferred behind a feature:scheduling order.
    plan = _plan([
        _order("wo-1", labels=["feature:scheduling"], body="b",
               title="title for wo-1"),
        _titled("wo-2", "scheduling: another change"),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_title_prefix_detection_is_case_insensitive():
    # The title-derived key normalizes case like the other signals.
    plan = _plan([
        _titled("wo-1", "Scheduling: capitalized prefix"),
        _order("wo-2", labels=["feature:scheduling"], body="b",
               title="title for wo-2"),
    ])
    assert plan["ordered"] == ["wo-1"]


# ==========================================================================
# Authoritative target_feature field (issue #258). TRIAGE stamps each order's
# blast-radius target feature(s) onto a `target_feature` field; PRIORITIZE reads
# THAT authoritative field instead of re-scraping labels/body/title. When the
# field is present it is the single source of truth (even an explicit empty list,
# a proven no-feature order); only a MISSING field triggers the fallback
# re-derivation (back-compat for older slots / orders built outside TRIAGE).
# ==========================================================================

def _tf(oid, target_feature, **kw):
    # An order carrying an explicit authoritative target_feature field.
    order = _order(oid, **kw)
    order["target_feature"] = list(target_feature)
    return order


def test_target_feature_field_serializes_same_feature():
    plan = _plan([
        _tf("wo-1", ["scheduling"]),
        _tf("wo-2", ["scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1"]
    assert plan["status"] == {"wo-1": "pending"}


def test_target_feature_field_cross_feature_stays_parallel():
    plan = _plan([
        _tf("wo-1", ["scheduling"]),
        _tf("wo-2", ["packaging-config"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_target_feature_field_takes_precedence_over_labels_body_title():
    # The authoritative field, NOT the stale labels/body/title, decides the
    # feature: two orders whose fields disagree (different features) stay
    # parallel even though their labels would have collided.
    plan = _plan([
        _tf("wo-1", ["scheduling"], labels=["feature:packaging-config"]),
        _tf("wo-2", ["prioritize"], labels=["feature:packaging-config"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_explicit_empty_target_feature_means_no_feature():
    # An explicit empty list is a PROVEN no-feature order — it must stay parallel
    # and NOT fall back to re-scraping the labels (which would serialize these).
    plan = _plan([
        _tf("wo-1", [], labels=["feature:scheduling"]),
        _tf("wo-2", [], labels=["feature:scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_multi_feature_target_field_claims_each_feature():
    plan = _plan([
        _tf("wo-1", ["prioritize", "scheduling"]),
        _tf("wo-2", ["scheduling"]),       # deferred (scheduling claimed)
        _tf("wo-3", ["packaging-config"]),
    ])
    assert plan["ordered"] == ["wo-1", "wo-3"]


def test_missing_target_feature_field_falls_back_to_rescraping():
    # No field at all -> fall back to re-deriving from labels (back-compat): two
    # feature:scheduling-labelled orders WITHOUT the field still serialize.
    plan = _plan([
        _order("wo-1", labels=["feature:scheduling"]),
        _order("wo-2", labels=["feature:scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1"]


def test_target_feature_field_is_case_normalized():
    plan = _plan([
        _tf("wo-1", ["Scheduling"]),
        _tf("wo-2", ["scheduling"]),
    ])
    assert plan["ordered"] == ["wo-1"]


# ==========================================================================
# Bracket-prefix title feature detection (defense-in-depth for the live
# parallel-collision bug). A filing convention observed in the live pool titles
# issues `[scope@team] ...` or `[scope] ...` (e.g.
# `[dci-team-atlassian-sharepoint@dci-team] jira skill ...`). When TRIAGE fails
# to stamp `target_feature` and the title carries no conventional-commit prefix,
# PRIORITIZE FALLS BACK to the leading `[...]` token's scope part (before any
# `@team`) as the feature key. The authoritative target_feature stays primary;
# the bracket parse is fallback-only.
# ==========================================================================

def test_bracket_prefix_scope_at_team_is_feature_key():
    # The leading [scope@team] token yields feature 'scope' (before the @team).
    assert pr._title_feature(
        "[dci-team-atlassian-sharepoint@dci-team] jira skill add"
    ) == "dci-team-atlassian-sharepoint"


def test_bracket_prefix_bare_scope_is_feature_key():
    # A bare [scope] with no @team yields feature 'scope'.
    assert pr._title_feature("[foo] bar") == "foo"


def test_non_bracket_title_still_uses_conventional_path():
    # A title with no bracket falls through to the conventional-commit path.
    assert pr._title_feature("feat(scheduling): add a tick") == "scheduling"
    assert pr._title_feature("scheduling: add retry backoff") == "scheduling"
    # A bare conventional-commit type still names no feature.
    assert pr._title_feature("fix: correct a typo") is None


def test_bracket_prefix_scope_is_case_normalized():
    # The bracket-derived key normalizes case like the other signals.
    assert pr._title_feature("[Foo@team] bar") == "foo"


def test_labelless_bracket_scope_orders_serialize_fifo_first_wins():
    # E2E: two label-less, target_feature-less orders sharing a bracket scope
    # serialize to one fanned-out id; the second defers.
    plan = _plan([
        _titled("wo-1",
                "[dci-team-atlassian-sharepoint@dci-team] jira skill add"),
        _titled("wo-2",
                "[dci-team-atlassian-sharepoint@dci-team] jira skill fix"),
    ])
    assert plan["ordered"] == ["wo-1"]
    assert plan["status"] == {"wo-1": "pending"}


def test_labelless_cross_bracket_scope_orders_stay_parallel():
    # Different bracket scopes stay parallel (the non-colliding case).
    plan = _plan([
        _titled("wo-1", "[foo@team] one"),
        _titled("wo-2", "[bar@team] two"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]


def test_authoritative_target_feature_ignores_title_bracket():
    # An order WITH an authoritative target_feature ignores its title bracket:
    # two orders whose brackets would collide but whose fields differ stay
    # parallel.
    plan = _plan([
        _tf("wo-1", ["scheduling"], title="[shared@team] one"),
        _tf("wo-2", ["prioritize"], title="[shared@team] two"),
    ])
    assert plan["ordered"] == ["wo-1", "wo-2"]
