#!/usr/bin/env python3
"""verify-integrate — slice 1: the read-only VERIFY gate.

The act-side CLOSE of the loop (DESIGN §3.7): after IMPLEMENT opens a PR, VERIFY
is THIN (DESIGN §3.7.1/§3.7.2) — it gates a PR on mergeability + base only. CI is
RECORDED on the Verdict (informational defense-in-depth) but no longer gates `ok`:
the correctness test-gate now lives in IMPLEMENT (FT-A runs the touched feature's
run.py before a PR opens), and the loop's own CI is a hollow byte-compile gate
that a pending/absent run would otherwise wedge every merge on. VERIFY is
READ-ONLY and always safe — it lists the loop's open PRs and derives one Verdict
per PR; it never merges, closes, or writes to GitHub.

VERIFY's substantive job is the CONDITIONAL cross-feature complement run (DESIGN
§3.7.6): when TRIAGE flagged `cross_cutting_risk` (§3.5.9), VERIFY deterministically
runs the at-risk features' run.py suites to catch a semantic cross-feature break
that has no merge conflict (the residual-case serialization §3.8.6 cannot catch).
If ANY complement suite fails, every verdict this tick is marked ok=False with a
specific cross-feature-break reason, so INTEGRATE merges nothing from a batch that
breaks an at-risk sibling. When risk is False (or the slot is absent), VERIFY does
NO complement run and stays thin.

The cross-tick model (refines DESIGN §2.6): a PR's CI runs asynchronously, so
GitHub — not a durable ledger — is the source of truth for the loop's open PRs.
Each tick VERIFY QUERIES `gh pr list --label auto-maintainer --state open`; a PR
opened in tick N is re-checked every tick until its CI goes green. The VERIFY
manifest therefore declares reads=[] (the PR set is sourced LIVE from gh, not a
blackboard slot); it writes the `verdicts` slot.

The only non-deterministic edge — the live `gh` call — sits behind an INJECTABLE
source (Verify(source=...)), so tests drive VERIFY with a stub over fixture PRs
with no network (spec-rules §1: the failure is locatable to the fetch boundary,
never a flaky live call). The default-branch lookup is likewise injectable.

Public surface (slice 1):
  - VERDICT_SCHEMA_VERSION + Verdict — the typed, versioned verdict schema.
  - VERDICTS_SLOT     — the fsm-contracts slot registration descriptor.
  - CROSS_CHECK_SCHEMA_VERSION + CrossCheck — the typed, versioned cross-feature
    complement-run result schema.
  - CROSS_CHECK_SLOT  — the fsm-contracts slot registration descriptor.
  - VERIFY_MANIFEST   — the VERIFY state's {reads, writes, emits} manifest.
  - VERIFY_SIGNALS    — the closed signal set VERIFY may emit (OK | EMPTY).
  - gh_open_pr_source()       — the production source: shells the gh CLI; resolves
    a transient mergeable=UNKNOWN via the bounded poll before returning.
  - poll_mergeability(...)     — the bounded, injectable-runner + injectable-sleep
    re-query that settles a transient mergeable=UNKNOWN to MERGEABLE/CONFLICTING.
  - gh_default_branch_source() — the production default-branch resolver.
  - feature_run_py_path(feature, features_root) — the deterministic resolver of a
    named feature's test/run.py (the complement-runner's locator seam).
  - default_complement_runner(...) — the production complement-runner: shells the
    named feature's test/run.py via subprocess (the determinism seam).
  - derive_verdict(pr_dict, default_branch) — the pure verdict derivation.
  - Verify            — the VERIFY state; run(TickContext) -> StateResult.

Public surface (REVIEW — the ADVISORY quality state, DESIGN §3.7.7):
  - REVIEW_FINDINGS_SCHEMA_VERSION + ReviewFinding + review_finding_record(...) —
    the advisory REVIEW output: a record conforming EXACTLY to work-intake's
    DiscoveredIssue.to_dict ({schema_version, title, body, kind, severity,
    target, dedup_key, filed_by}) so REPORT files it unchanged.
  - REVIEW_FINDINGS_SLOT — the fsm-contracts slot registration descriptor for
    the advisory review_findings array.
  - REVIEW_MANIFEST / REVIEW_SIGNALS — REVIEW's manifest (reads verdicts, writes
    review_findings) + signal set (OK | EMPTY). REVIEW itself has no run()
    here — it is a NON-ACTING agent-state dispatched to auto-maintainer-reviewer.
    REVIEW is NO LONGER a merge gate (the merge rests on IMPLEMENT's run.py gate
    + VERIFY + guardrails + the trust ladder).
  - REVIEW_VERDICT_SCHEMA_VERSION + ReviewVerdict + REVIEW_VERDICTS_SLOT +
    REVIEW_SEVERITIES — the RETAINED review-verdict schema (consumed by
    scheduling + the packaging-config release gate); no longer a merge gate.
  - review_evidence_valid(rv) / batch_is_untrustworthy(review_verdicts) — the
    RETAINED deterministic evidence validators (#255): the packaging-config
    release gate asserts the shipped lib carries them.

Public surface (GATE — the cumulative regression gate, DESIGN §2.2 [v2]):
  - GATE_RESULT_SCHEMA_VERSION + GateResult — the typed, versioned per-gated-PR
    result ({pr_ref, issue_ref, passed, reason, failure_summary}).
  - GATE_RESULTS_SLOT — the fsm-contracts slot registration descriptor.
  - GATE_MANIFEST / GATE_SIGNALS — GATE's manifest (reads verdicts, writes
    gate_results) + signal set (OK).
  - gh_closing_issue_ref(pr_ref, repo) — the production closing-issue resolver.
  - declared_load_bearing_tokens(feature_dir) / missing_load_bearing_tokens(
    feature_dir, tokens=None) / features_with_changed_doc_surfaces(changed_paths,
    features_root) — the pure doc-surface load-bearing-token survival helpers
    (issue #353): a feature declares its must-survive tokens in
    test/load_bearing_tokens.json; GATE fails a doc-touched PR that drops one.
    GATE anchors the declared set to the pre-merge base (not the PR's own copy),
    so a PR cannot bypass the gate by dropping a token AND its declaration
    together (issue #392).
  - Gate — the GATE state; run(TickContext) -> StateResult. Cumulative: a
    disposable integration worktree at current main, per-PR --no-ff merge, the
    doc-surface load-bearing-token survival check (#353), then the regression,
    roll-back-on-fail, exclude, deterministic order. No-op PASS when
    regression_command is null. Never merges main / never calls the merge sink.
  - make_gate(runtime) — the adapter factory scheduling wires GATE with.

Public surface (slice 2 — INTEGRATE + CLEANUP):
  - INTEGRATION_RESULT_SCHEMA_VERSION + IntegrationResult — the typed,
    versioned {merged, skipped, errors, gate_failed, closed_orphaned}
    integration-result schema.
  - gh_issue_comment_sink(issue_ref, body, repo) — the production gate-fail
    comment sink; gate_fail_comment_body(...) builds the marker+JSON body.
  - INTEGRATION_RESULT_SLOT — the fsm-contracts slot registration descriptor.
  - INTEGRATE_MANIFEST / INTEGRATE_SIGNALS — INTEGRATE's manifest (reads ONLY
    verdicts — thin merge, no review coupling) + signal set.
  - gh_pr_merge_sink(pr_ref, repo, base_branch) — the production merge sink,
    MERGE-QUEUE-AWARE: detects a merge queue on the base branch and shells
    `gh pr merge <pr> --auto` (queue) or `gh pr merge <pr> --merge
    --delete-branch` (no queue), capturing gh stderr (the determinism seam).
  - gh_pr_close_sink(pr_ref, repo) — the production orphan close sink: shells
    `gh pr close <pr> --delete-branch --comment <body>` (the determinism seam).
  - gh_closing_issue_state(pr_ref, repo) — the production orphan resolver: the
    closing issue's state ('OPEN'/'CLOSED'/None) VERIFY reads (a READ only).
  - Integrate — the INTEGRATE state; merges only at auto-merge, guardrail-gated;
    CLOSES orphaned loop PRs first (trust-gated on permits('merge', mode)).
  - CLEANUP_MANIFEST / CLEANUP_SIGNALS — CLEANUP's manifest + signal set.
  - Cleanup — the CLEANUP state (v1-thin pass-through; run -> OK).

Version: 0.8.0
Owner: changyu87
Deprecation criterion: Superseded when the loop adopts a non-git VCS backend,
  or a model-backed verify/integrate policy replaces the deterministic gh-based
  gates, or when the Verdict / IntegrationResult / GateResult schemas reach a
  breaking major version. See docs/spec.md.
"""

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import List

# fsm-contracts is a sibling feature; tests inject its src/ onto sys.path
# exactly as the other adapters do, so importing by module name resolves the
# sibling src/ on the path.
import fsm_contracts as fc

# safety-governance is a sibling feature consumed UNCHANGED (the contract's
# `reads`): permits("merge", mode) is the trust-ladder gate and
# merge_guardrails(pr_meta, default_branch) is the §3.8.1 declarative backstop.
# Tests put its src/ on sys.path exactly as for fsm-contracts, so importing by
# module name resolves the sibling src/.
import safety_governance as sg


# The versioned Verdict schema (machine-first; bumped on a breaking change to
# the field set). Slot-schema version, distinct from the feature version.
VERDICT_SCHEMA_VERSION = "1.1.0"


@dataclass(eq=True)
class Verdict:
    """One verdict per open loop PR (DESIGN §3.7, thinned by §3.7.1/§3.7.2).

    `ok` is the conservative AND of the BLOCKING conditions: mergeable AND
    base == default branch. CI is NO LONGER a blocking condition (the correctness
    gate lives in IMPLEMENT); `ci_state` (one of passing|pending|failing|unknown)
    is still RECORDED as informational defense-in-depth but does not flip `ok`.
    `reasons` explains a non-ok verdict (empty when ok). `orphaned` is True when
    the loop PR's closing issue is CLOSED — the driver work is resolved/abandoned,
    so the PR will never merge and INTEGRATE must CLOSE it; an orphaned verdict is
    forced `ok=False`. It defaults False (a PR with no closing issue, or an
    unresolvable one, is conservatively NOT treated as orphaned). `to_dict`/
    `from_dict` give a machine-first, versioned representation for the `verdicts`
    slot.
    """

    pr_ref: str
    url: str
    ok: bool
    ci_state: str
    mergeable: bool
    base: str
    reasons: List[str] = field(default_factory=list)
    orphaned: bool = False

    def to_dict(self):
        return {
            "schema_version": VERDICT_SCHEMA_VERSION,
            "pr_ref": self.pr_ref,
            "url": self.url,
            "ok": self.ok,
            "ci_state": self.ci_state,
            "mergeable": self.mergeable,
            "base": self.base,
            "reasons": list(self.reasons),
            "orphaned": self.orphaned,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            pr_ref=d["pr_ref"],
            url=d["url"],
            ok=d["ok"],
            ci_state=d["ci_state"],
            mergeable=d["mergeable"],
            base=d["base"],
            reasons=list(d.get("reasons", [])),
            orphaned=d.get("orphaned", False),
        )


# The fsm-contracts slot descriptor. `verdicts` is an array slot (a list of
# Verdict dicts); the slot version tracks the schema version. Mirrors
# work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
VERDICTS_SLOT = {
    "name": "verdicts",
    "schema": {"type": "array"},
    "version": VERDICT_SCHEMA_VERSION,
}

# The versioned CrossCheck schema (machine-first; bumped on a breaking field-set
# change). Records the CONDITIONAL cross-feature complement run (DESIGN §3.7.6).
CROSS_CHECK_SCHEMA_VERSION = "1.0.0"


@dataclass(eq=True)
class CrossCheck:
    """The result of VERIFY's conditional cross-feature complement run (§3.7.6).

    `ran` is True only when TRIAGE flagged cross_cutting_risk (risk=True) and the
    at-risk features' suites were therefore run; `reason` echoes the triager's
    overlap reason (empty when ran=False). `results` is one entry per named
    at-risk feature: `{feature, passed, returncode, summary}` (mirroring FT-A's
    test-gate verdict shape). `to_dict`/`from_dict` give a machine-first,
    versioned representation for the `cross_check` blackboard slot.
    """

    ran: bool
    reason: str = ""
    results: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": CROSS_CHECK_SCHEMA_VERSION,
            "ran": bool(self.ran),
            "reason": self.reason,
            "results": [dict(r) for r in self.results],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            ran=bool(d["ran"]),
            reason=d.get("reason", ""),
            results=[dict(r) for r in d.get("results", [])],
        )


# The fsm-contracts slot descriptor. `cross_check` is an object slot (a single
# CrossCheck dict); the slot version tracks the schema version. Mirrors
# VERDICTS_SLOT's shape (name/schema/version).
CROSS_CHECK_SLOT = {
    "name": "cross_check",
    "schema": {"type": "object"},
    "version": CROSS_CHECK_SCHEMA_VERSION,
}

# Closed signal set VERIFY emits: OK when any open PRs were found, else EMPTY.
VERIFY_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope contract): reads `cross_cutting_risk` (the
# work-intake CrossCuttingRisk slot — the open-PR set itself is sourced live from
# gh, NOT a slot, per the cross-tick model), writes the `verdicts` and
# `cross_check` slots, emits OK | EMPTY.
VERIFY_MANIFEST = fc.StateManifest(reads=["cross_cutting_risk"],
                                   writes=["verdicts", "cross_check"],
                                   emits=VERIFY_SIGNALS)


# ==========================================================================
# REVIEW — the ADVISORY quality state between VERIFY and INTEGRATE (DESIGN
# §3.7.7). VERIFY is purely deterministic (CI + mergeable + base); REVIEW adds
# the JUDGMENT VERIFY cannot: did the PR build the RIGHT thing (spec-compliance)
# and is it good quality (a code-quality pass over the base..head diff).
#
# REVIEW is a NON-ACTING agent-state: the `auto-maintainer-reviewer` subagent
# reads the actual PR diff and emits MATERIAL quality findings as durable
# `review_findings` records (each conforming EXACTLY to work-intake's
# DiscoveredIssue schema, so REPORT files them unchanged). verify-integrate OWNS
# the SCHEMA + SLOT + MANIFEST (mirroring how `implement` owns HANDOFFS_SLOT
# while the subagent produces the handoffs); the dispatch/collection is the
# agent-dispatch machinery wired in scheduling.
#
# REVIEW is NO LONGER a merge gate (the loop redesign, FT-C). The merge decision
# rests on IMPLEMENT's deterministic run.py gate + VERIFY + merge_guardrails +
# the trust ladder, so a lazy reviewer costs only missed quality notes, never an
# unsafe merge — which structurally defuses the #255 rubber-stamp danger.
#
# The ReviewVerdict schema + the deterministic evidence validators
# (review_evidence_valid / batch_is_untrustworthy) are RETAINED below: scheduling
# and the packaging-config release gate still consume them. They are no longer a
# merge gate.
# ==========================================================================

# The versioned review-finding schema. A review_findings record conforms EXACTLY
# to work-intake's DiscoveredIssue.to_dict field set (a contract-bound
# cross-feature producer relationship), so this version tracks that schema.
REVIEW_FINDINGS_SCHEMA_VERSION = "1.0.0"

# The closed `kind` vocabulary a review finding may carry (matches the
# DiscoveredIssue kinds the tracker accepts).
REVIEW_FINDING_KINDS = ["bug", "enhancement", "chore"]


@dataclass(eq=True)
class ReviewFinding:
    """One MATERIAL advisory finding the reviewer emits for an open loop PR
    (DESIGN §3.7.7).

    The fields mirror work-intake's DiscoveredIssue EXACTLY so a finding's
    `to_dict()` is a DiscoveredIssue-conforming record the downstream REPORT port
    files unchanged. `target` is always `project` (findings go to the project
    tracker); `filed_by` stamps the loop's provenance; `dedup_key` is a STABLE
    key (`review:<pr_ref>:<slug>`) making the filing idempotent.
    """

    title: str
    body: str
    kind: str
    severity: str
    dedup_key: str
    target: str = "project"
    filed_by: str = "autonomous-maintainer"

    def to_dict(self):
        return {
            "schema_version": REVIEW_FINDINGS_SCHEMA_VERSION,
            "title": self.title,
            "body": self.body,
            "kind": self.kind,
            "severity": self.severity,
            "target": self.target,
            "dedup_key": self.dedup_key,
            "filed_by": self.filed_by,
        }


def review_finding_record(pr_ref, title, body, kind, severity, slug):
    """Build a DiscoveredIssue-conforming review_findings record for `pr_ref`.

    `slug` is a short stable identifier for the finding within the PR; the
    `dedup_key` is `review:<pr_ref>:<slug>` so re-filing the same finding on a
    later tick is idempotent. Returns the machine-first record dict (the shape
    REPORT/work-intake consumes unchanged).
    """
    return ReviewFinding(
        title=title,
        body=body,
        kind=kind,
        severity=severity,
        dedup_key=f"review:{pr_ref}:{slug}",
    ).to_dict()


# The fsm-contracts slot descriptor for the advisory review output.
# `review_findings` is an array slot (a list of DiscoveredIssue-conforming
# records); the slot version tracks the schema version. Mirrors VERDICTS_SLOT.
REVIEW_FINDINGS_SLOT = {
    "name": "review_findings",
    "schema": {"type": "array"},
    "version": REVIEW_FINDINGS_SCHEMA_VERSION,
}

# The versioned ReviewVerdict schema (machine-first; bumped on a breaking change
# to the field set). Distinct from the feature version. Mirrors the Verdict shape.
REVIEW_VERDICT_SCHEMA_VERSION = "1.0.0"

# The closed severity vocabulary a ReviewVerdict / finding may carry, ordered
# least-to-most severe. `none` is the no-findings level (an approved verdict);
# `blocker` is the most severe (must block merge).
REVIEW_SEVERITIES = ["none", "low", "medium", "high", "blocker"]


def _empty_evidence():
    """The default (invalid-for-approval) evidence structure: no files examined,
    no rationale. An approval carrying this is a rubber-stamp (#255)."""
    return {"files_examined": [], "rationale": ""}


@dataclass(eq=True)
class ReviewVerdict:
    """One model-backed review verdict per open loop PR (DESIGN §3.7).

    `approved` is the reviewer's overall judgment (did the PR build the right
    thing, nothing more / nothing less, AND is it acceptable quality). `severity`
    is the worst finding severity (one of REVIEW_SEVERITIES; `none` when there
    are no findings). `findings` is a list of {kind, severity, file, line, note}
    dicts the reviewer flagged (empty when approved with nothing to note).
    `evidence` is `{files_examined: [str], rationale: str}` — the concrete proof
    the reviewer actually examined the PR (the files it read from `gh pr diff`
    plus a substantive rationale). Evidence is REQUIRED for a VALID approval
    (#255): an approved verdict with empty/missing evidence is a rubber-stamp and
    is treated as not-approved. `to_dict`/`from_dict` give a machine-first,
    versioned representation for the `review_verdicts` slot.
    """

    pr_ref: str
    approved: bool
    severity: str = "none"
    findings: List[dict] = field(default_factory=list)
    evidence: dict = field(default_factory=_empty_evidence)

    def to_dict(self):
        return {
            "schema_version": REVIEW_VERDICT_SCHEMA_VERSION,
            "pr_ref": self.pr_ref,
            "approved": self.approved,
            "severity": self.severity,
            "findings": [dict(f) for f in self.findings],
            "evidence": {
                "files_examined": list(
                    (self.evidence or {}).get("files_examined", [])),
                "rationale": (self.evidence or {}).get("rationale", ""),
            },
        }

    @classmethod
    def from_dict(cls, d):
        ev = d.get("evidence") or {}
        return cls(
            pr_ref=d["pr_ref"],
            approved=d["approved"],
            severity=d.get("severity", "none"),
            findings=[dict(f) for f in d.get("findings", [])],
            evidence={
                "files_examined": list(ev.get("files_examined", [])),
                "rationale": ev.get("rationale", ""),
            },
        )


# The fsm-contracts slot descriptor. `review_verdicts` is an array slot (a list
# of ReviewVerdict dicts); the slot version tracks the schema version. Mirrors
# VERDICTS_SLOT's shape (name/schema/version).
REVIEW_VERDICTS_SLOT = {
    "name": "review_verdicts",
    "schema": {"type": "array"},
    "version": REVIEW_VERDICT_SCHEMA_VERSION,
}

# Closed signal set REVIEW emits: OK when there were PRs to review, else EMPTY
# (no open PRs this tick). The model never selects control flow — the signal is
# the deterministic OK-when-PRs-else-EMPTY rule. EMPTY does NOT mean "no
# findings"; an advisory tick that reviewed PRs but found nothing material still
# emits OK with an EMPTY review_findings list.
REVIEW_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope): REVIEW reads the verdicts slot (the open-PR
# set VERIFY surfaced — the reviewer reviews the SAME PRs) and writes the
# advisory review_findings slot, emitting OK | EMPTY. The PR diff the reviewer
# reads is fetched LIVE by the subagent (gh pr diff), not a blackboard slot.
REVIEW_MANIFEST = fc.StateManifest(reads=["verdicts"],
                                   writes=["review_findings"],
                                   emits=REVIEW_SIGNALS)


# The minimum number of whitespace-separated words a rationale must carry to be
# "substantive" — a one-word "ok"/"lgtm" is a rubber-stamp, not a rationale.
_MIN_RATIONALE_WORDS = 3


def review_evidence_valid(rv):
    """Whether a ReviewVerdict dict carries VALID evidence (#255) — the
    deterministic backstop that does NOT trust the model's `approved` flag.

    Evidence is valid only when the verdict names at least one concrete file the
    reviewer examined (`evidence.files_examined` non-empty) AND carries a
    substantive (non-blank, at least `_MIN_RATIONALE_WORDS` words) `rationale`.
    An approval lacking either is a contentless rubber-stamp.
    """
    ev = rv.get("evidence") or {}
    files = ev.get("files_examined") or []
    if not files:
        return False
    rationale = (ev.get("rationale") or "").strip()
    return len(rationale.split()) >= _MIN_RATIONALE_WORDS


def batch_is_untrustworthy(review_verdicts):
    """Whether a whole review batch carries the fabricated rubber-stamp signature
    (#255): EVERY verdict approved, ZERO findings across the batch, and NO valid
    evidence anywhere. Such a batch is the hallmark of a reviewer that blanket-
    approved without examining anything — INTEGRATE merges NONE of it.

    A batch that carries valid evidence on any verdict, contains any rejection,
    or records any finding is trustworthy. An empty/None batch is not
    untrustworthy (there is simply nothing to merge).
    """
    rvs = review_verdicts or []
    if not rvs:
        return False
    for rv in rvs:
        if not rv.get("approved"):
            return False
        if rv.get("findings"):
            return False
        if review_evidence_valid(rv):
            return False
    return True


# The gh JSON fields VERIFY needs to derive a verdict.
_GH_JSON_FIELDS = (
    "number,url,headRefName,baseRefName,mergeable,statusCheckRollup")

# The label every loop-opened PR carries (stamped by the IMPLEMENT doer).
LOOP_PR_LABEL = "auto-maintainer"

# Transient-UNKNOWN mergeability resolution (bounded poll). GitHub computes a
# PR's mergeability ASYNCHRONOUSLY, so a PR opened this tick is almost always
# reported mergeable=UNKNOWN the moment VERIFY lists it — indistinguishable at
# the raw-string level from a real CONFLICTING. The open-PR source re-queries a
# transient UNKNOWN up to MERGEABILITY_POLL_ATTEMPTS times, sleeping
# MERGEABILITY_POLL_INTERVAL_S seconds between attempts, until it settles to
# MERGEABLE or CONFLICTING. Small + short by design: mergeability settles in
# seconds and the poll must never dominate a tick.
MERGEABILITY_POLL_ATTEMPTS = 3
MERGEABILITY_POLL_INTERVAL_S = 2


def poll_mergeability(pr_number, repo=None, runner=subprocess.run,
                      sleep=time.sleep):
    """Resolve a transient `mergeable=UNKNOWN` via a BOUNDED poll: re-query gh
    (`gh pr view <n> --json mergeable -q .mergeable`, adding `--repo` when set) up
    to MERGEABILITY_POLL_ATTEMPTS times, sleeping MERGEABILITY_POLL_INTERVAL_S
    between attempts, and return the settled mergeability string. Stops EARLY once
    the value is MERGEABLE or CONFLICTING; returns the last (possibly still
    UNKNOWN) value once attempts are exhausted. Both the subprocess `runner` AND
    the `sleep` fn are INJECTABLE so tests drive the poll deterministically with
    no network and no wall-clock wait. Bounded: never loops unboundedly."""
    cmd = ["gh", "pr", "view", str(pr_number), "--json", "mergeable",
           "-q", ".mergeable"]
    if repo:
        cmd += ["--repo", repo]
    result = "UNKNOWN"
    for attempt in range(MERGEABILITY_POLL_ATTEMPTS):
        out = runner(cmd, capture_output=True, text=True, check=True)
        result = (out.stdout or "").strip().upper()
        if result in ("MERGEABLE", "CONFLICTING"):
            return result
        if attempt < MERGEABILITY_POLL_ATTEMPTS - 1:
            sleep(MERGEABILITY_POLL_INTERVAL_S)
    return result


def gh_open_pr_source(repo=None, label=LOOP_PR_LABEL, runner=subprocess.run,
                      sleep=time.sleep):
    """Production open-PR source: shell the deterministic `gh` CLI for the loop's
    OPEN PRs (filtered by the `auto-maintainer` label) and return the parsed gh
    JSON list. `gh` carries its own auth. When `repo` is given it is passed via
    `--repo`; otherwise gh resolves the repo from the project default.

    A transient `mergeable=UNKNOWN` (a PR whose mergeability GitHub has not yet
    computed) is RESOLVED before the dict is returned: for any listed PR reported
    UNKNOWN, `poll_mergeability` re-queries gh with a bounded retry and overwrites
    that PR dict's `mergeable` with the settled value, so `derive_verdict` sees a
    MERGEABLE/CONFLICTING result the same tick the PR was opened (a still-UNKNOWN
    result becomes a DEFERRED verdict, distinct from a hard CONFLICTING failure).

    The subprocess `runner` and the `sleep` fn are INJECTABLE (defaulting to the
    production impls) so tests assemble/parse the command AND drive the poll with a
    fake — no network, no wall-clock wait, the failure locatable to the fetch
    boundary (mirror of work_intake.gh_issue_source).
    """
    cmd = ["gh", "pr", "list", "--label", label, "--state", "open",
           "--json", _GH_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    prs = json.loads(out.stdout)
    for pr in prs:
        if (pr.get("mergeable") or "").upper() == "UNKNOWN":
            pr["mergeable"] = poll_mergeability(
                pr["number"], repo=repo, runner=runner, sleep=sleep)
    return prs


def gh_default_branch_source(repo=None, runner=subprocess.run):
    """Production default-branch resolver: shell `gh repo view` for the repo's
    default branch name. Injectable `runner` for deterministic tests."""
    cmd = ["gh", "repo", "view", "--json", "defaultBranchRef",
           "-q", ".defaultBranchRef.name"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def feature_run_py_path(feature, features_root):
    """Deterministic resolver of a named feature's `test/run.py`.

    Returns `<features_root>/<feature>/test/run.py`. `features_root` is a
    runtime-injected locator and is REQUIRED — there is no source-tree default:
    the shipped, self-contained plugin lib cannot assume its own on-disk layout,
    so the caller (scheduling) injects the sibling-features root. A None
    `features_root` raises rather than silently joining onto a path that does not
    exist in the plugin tree. Pure: it computes a path string, it does not check
    the filesystem (the runner does)."""
    if features_root is None:
        raise ValueError(
            "feature_run_py_path requires a non-None features_root "
            "(runtime-injected locator; no source-tree default)")
    return os.path.join(features_root, feature, "test", "run.py")


def _summary_line(text):
    """The final non-empty line of run.py output — the conventional
    `N passed, M failed` summary the feature runners emit. Pure."""
    for line in reversed((text or "").splitlines()):
        if line.strip():
            return line.strip()
    return ""


def default_complement_runner(feature, features_root=None,
                              runner=subprocess.run):
    """Production complement-runner: run a named feature's `test/run.py` via
    subprocess and return a machine-checkable result dict
    `{feature, passed, returncode, summary}` (the FT-A test-gate verdict shape).

    Self-contained: it shells the target's OWN run.py (no rabbit-framework runtime
    dependency, no import of the implement feature). A missing run.py is recorded
    as passed=False (never a silent pass). The subprocess `runner` and the
    `features_root` locator are INJECTABLE seams so tests drive a stub with no
    network and no real sibling suite (spec-rules §1)."""
    run_py = feature_run_py_path(feature, features_root)
    if not os.path.isfile(run_py):
        return {
            "feature": feature,
            "passed": False,
            "returncode": 1,
            "summary": f"no test/run.py found for feature {feature!r}",
        }
    proc = runner([sys.executable, run_py],
                  capture_output=True, text=True,
                  cwd=os.path.dirname(os.path.dirname(run_py)))
    return {
        "feature": feature,
        "passed": proc.returncode == 0,
        "returncode": proc.returncode,
        "summary": _summary_line(proc.stdout) or _summary_line(proc.stderr),
    }


def _derive_pr_ref(url, number):
    """Derive a stable `owner/repo#number` ref from a PR URL, falling back to
    `#number` when the URL is not the expected GitHub pull form."""
    marker = "github.com/"
    idx = url.find(marker)
    if idx != -1:
        tail = url[idx + len(marker):]
        parts = tail.split("/")
        if len(parts) >= 2:
            return f"{parts[0]}/{parts[1]}#{number}"
    return f"#{number}"


def _pr_number(pr_ref):
    """The integer PR number parsed off a `pr_ref` (`owner/repo#number` or bare
    `#number`), used to order the GATE's cumulative merges deterministically."""
    return int(pr_ref.split("#")[-1])


def _pr_url(pr_ref, repo):
    """Pure derivation of a PR's web URL from its ref so a merged entry carries a
    real link instead of url:''. `owner/repo#number` ->
    `https://github.com/owner/repo/pull/number`; for a bare `#number` ref the
    `owner/repo` is taken from `repo` when given; if neither yields an owner/repo
    the URL is '' (never raise — URL derivation is best-effort observability).
    """
    head, _, number = pr_ref.rpartition("#")
    owner_repo = head or (repo or "")
    parts = owner_repo.split("/")
    if len(parts) == 2 and parts[0] and parts[1] and number:
        return f"https://github.com/{parts[0]}/{parts[1]}/pull/{number}"
    return ""


def _ci_state(rollup):
    """Map a gh statusCheckRollup list to a ci_state.

    Each entry is a check-run/status with COMPLETED/SUCCESS for a pass. The
    aggregate is conservative: any FAILURE/ERROR -> failing; else any
    pending/in-progress -> pending; an empty/missing rollup -> unknown; only an
    all-SUCCESS rollup -> passing.
    """
    if not rollup:
        return "unknown"
    any_pending = False
    for check in rollup:
        conclusion = (check.get("conclusion") or "").upper()
        status = (check.get("status") or "").upper()
        if conclusion in ("FAILURE", "ERROR", "CANCELLED", "TIMED_OUT"):
            return "failing"
        if status != "COMPLETED" or conclusion not in ("SUCCESS", "NEUTRAL",
                                                        "SKIPPED"):
            any_pending = True
    return "pending" if any_pending else "passing"


# The transient reason a verdict carries when its mergeability is STILL UNKNOWN
# after VERIFY's bounded poll — a DEFERRED verdict (ok=False) the next tick's
# refire re-evaluates, DISTINCT from the permanent CONFLICTING hard failure. The
# exact string is contract-bound (TRIAGE / observers read it), so keep it verbatim.
_DEFERRED_MERGEABILITY_REASON = (
    "mergeability not yet determined (mergeable=UNKNOWN) — deferred to a later "
    "tick")


def derive_verdict(pr_dict, default_branch):
    """Pure derivation of a Verdict from a gh-shaped open-PR dict.

    `ci_state` from the status-check rollup (RECORDED, informational), `mergeable`
    from gh's `mergeable` field (MERGEABLE -> True; CONFLICTING/UNKNOWN/anything
    else -> False, conservative), `base` from `baseRefName`. `ok` is the
    conservative AND of the BLOCKING conditions ONLY: mergeable AND
    base == default_branch (DESIGN §3.7.1/§3.7.2 — CI is recorded but no longer
    gates ok). `reasons` lists each failing BLOCKING condition for a non-ok
    verdict; a non-passing CI does NOT contribute a reason.

    The non-mergeable reason is SPLIT so a transient DEFERRED mergeability is
    distinguishable from a real conflict: a raw `mergeable=UNKNOWN` (still
    unresolved after VERIFY's bounded poll) carries the transient
    `_DEFERRED_MERGEABILITY_REASON` (the next tick's refire re-evaluates it), while
    any other non-MERGEABLE value (CONFLICTING) keeps the permanent
    `not mergeable (mergeable=<X>)` hard-failure reason. `ok` stays False in both
    non-MERGEABLE cases. Deterministic; no I/O.
    """
    number = pr_dict["number"]
    url = pr_dict.get("url", "")
    base = pr_dict.get("baseRefName", "")
    ci_state = _ci_state(pr_dict.get("statusCheckRollup") or [])
    mergeable_raw = (pr_dict.get("mergeable") or "")
    mergeable = mergeable_raw.upper() == "MERGEABLE"

    reasons = []
    if not mergeable:
        if mergeable_raw.upper() == "UNKNOWN":
            reasons.append(_DEFERRED_MERGEABILITY_REASON)
        else:
            reasons.append(
                f"not mergeable (mergeable={pr_dict.get('mergeable')})")
    if base != default_branch:
        reasons.append(
            f"base {base!r} is not the default branch {default_branch!r}")

    ok = not reasons
    return Verdict(
        pr_ref=_derive_pr_ref(url, number),
        url=url,
        ok=ok,
        ci_state=ci_state,
        mergeable=mergeable,
        base=base,
        reasons=reasons,
    )


def _read_optional_slot(ctx, name):
    """Read a slot if it is registered AND written, else None — VERIFY tolerates
    an absent `cross_cutting_risk` slot (runtime seeding is FT-E; until then a tick
    simply has no risk verdict, which is the thin no-complement path). The
    fsm-contracts ctx.read raises on an unregistered/unwritten slot, so this
    guards both cases without coupling VERIFY to the seeding order."""
    try:
        return ctx.read(name)
    except fc.ContractError:
        return None


# The cross-feature-break reason stamped on every verdict when a complement suite
# fails. Names the failing feature + the triager's overlap reason so INTEGRATE's
# skip reason is locatable to the at-risk sibling that broke (spec-rules §1).
def _cross_break_reason(failing_features, triager_reason):
    feats = ", ".join(failing_features)
    return (f"cross-feature break: at-risk sibling suite(s) failed [{feats}] "
            f"(triager overlap reason: {triager_reason})")


# The reason stamped on cross_check + every verdict when a cross-cutting batch is
# flagged (risk=True) but the complement CANNOT run because `features_root` is
# unconfigured. The flagged risk is then UNVERIFIABLE, so VERIFY conservatively
# gates — an unverifiable at-risk batch must never auto-merge (§3.7.1). Loud and
# recorded, never silent.
_UNVERIFIABLE_REASON = (
    "complement run skipped: features_root not configured — "
    "cross-cutting risk unverifiable")

# The reason stamped on a verdict whose driver (closing) issue is CLOSED — an
# orphaned loop PR that will never merge and must be CLOSED by INTEGRATE. VERIFY
# only READS the issue state (never closes); the close is INTEGRATE's act.
_ORPHANED_REASON = (
    "driver issue is closed — orphaned loop PR (will be closed by INTEGRATE)")


def gh_closing_issue_state(pr_ref, repo=None, runner=subprocess.run):
    """Production orphan resolver: resolve the loop PR's closing issue and return
    its state string ('OPEN'/'CLOSED'), or None when the PR closes no issue.

    Resolves the closing-issue ref via `gh_closing_issue_ref` (the same
    injectable `runner`; defined below — invoked at call time), parses its number,
    then shells `gh issue view <n> --json state -q .state` (adding `--repo` when
    given). VERIFY uses this to mark an orphaned verdict when the issue is CLOSED —
    a READ only; VERIFY never mutates GitHub. Injectable `runner` for
    deterministic tests (no network), mirroring the sibling gh resolvers/sinks."""
    ref = gh_closing_issue_ref(pr_ref, repo=repo, runner=runner)
    if ref is None:
        return None
    number = ref.split("#")[-1]
    cmd = ["gh", "issue", "view", number, "--json", "state", "-q", ".state"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return (out.stdout or "").strip()


class Verify:
    """The VERIFY state (thinned by DESIGN §3.7.1/§3.7.2 + §3.7.6).

    Lists the loop's open PRs via the injectable source, derives one Verdict per
    PR (ok = mergeable AND base==default branch; CI recorded but not gating),
    writes the `verdicts` slot, and emits OK if any open PRs were found else EMPTY.

    Cross-feature complement run (§3.7.6): VERIFY reads the `cross_cutting_risk`
    slot (work-intake's CrossCuttingRisk). When risk is True it runs, via the
    injectable `complement_runner`, the run.py of EACH named at-risk feature and
    records the per-feature results in the `cross_check` slot. If ANY complement
    suite fails, every verdict this tick is marked ok=False with a specific
    cross-feature-break reason, so INTEGRATE merges nothing from a batch that
    breaks an at-risk sibling. When risk is False/absent, NO complement runs and
    `cross_check` records ran=False (VERIFY stays thin); verdicts reflect only
    mergeable+base.

    READ-ONLY w.r.t. GitHub: VERIFY never merges, closes, or writes to GitHub. Its
    edges are the read-only PR source, the default-branch resolver, and the
    complement-runner — all injectable (the determinism seam).
    """

    def __init__(self, source=gh_open_pr_source, repo=None,
                 default_branch=None,
                 default_branch_source=gh_default_branch_source,
                 complement_runner=default_complement_runner,
                 features_root=None,
                 orphan_resolver=gh_closing_issue_state):
        self._source = source
        self._repo = repo
        self._default_branch = default_branch
        self._default_branch_source = default_branch_source
        self._complement_runner = complement_runner
        self._features_root = features_root
        self._orphan_resolver = orphan_resolver

    def _resolve_default_branch(self):
        if self._default_branch is not None:
            return self._default_branch
        return self._default_branch_source(self._repo)

    def _run_complement(self, ctx):
        """Run the conditional cross-feature complement (§3.7.6). Returns a
        `(CrossCheck, gate_reason)` pair. `gate_reason` is non-None when every
        verdict this tick must be flipped ok=False:

          - risk=False or an absent slot -> (ran=False, None): VERIFY stays thin.
          - risk=True but `features_root` is unconfigured -> the complement CANNOT
            run, so (ran=False with the unverifiable reason, that reason):
            conservatively GATE — an unverifiable at-risk batch must not merge.
          - risk=True with a configured root -> run each named feature's run.py;
            (ran=True, the cross-break reason) when any complement FAILS, else
            (ran=True, None).
        """
        risk_dict = _read_optional_slot(ctx, "cross_cutting_risk")
        if not risk_dict or not risk_dict.get("risk"):
            return CrossCheck(ran=False, reason="", results=[]), None
        if self._features_root is None:
            return (CrossCheck(ran=False, reason=_UNVERIFIABLE_REASON,
                               results=[]),
                    _UNVERIFIABLE_REASON)
        reason = risk_dict.get("reason", "")
        results = [
            self._complement_runner(feature, features_root=self._features_root)
            for feature in risk_dict.get("features", [])
        ]
        failing = [r["feature"] for r in results if not r.get("passed")]
        gate_reason = _cross_break_reason(failing, reason) if failing else None
        return CrossCheck(ran=True, reason=reason, results=results), gate_reason

    def _resolve_orphan_state(self, pr_ref):
        """Resolve a PR's closing-issue state via the injected resolver, returning
        None on ANY fault (CONSERVATIVE: never orphaned on uncertainty). READ
        only — VERIFY never mutates GitHub."""
        try:
            return self._orphan_resolver(pr_ref, repo=self._repo)
        except Exception:  # noqa: BLE001 — a resolver fault must not orphan a PR
            return None

    def run(self, ctx):
        prs = self._source(repo=self._repo, label=LOOP_PR_LABEL)
        default_branch = self._resolve_default_branch()
        verdicts = [derive_verdict(pr, default_branch).to_dict() for pr in prs]

        # Orphaned-PR detection (convergence, §3.7): a loop PR whose closing issue
        # is CLOSED is forced ok=False + orphaned=True so GATE skips it and
        # INTEGRATE closes it. This is a READ — VERIFY never closes.
        for vd in verdicts:
            if self._resolve_orphan_state(vd["pr_ref"]) == "CLOSED":
                vd["orphaned"] = True
                vd["ok"] = False
                vd["reasons"] = list(vd.get("reasons") or []) + [_ORPHANED_REASON]

        cross_check, gate_reason = self._run_complement(ctx)
        if gate_reason:
            for vd in verdicts:
                vd["ok"] = False
                vd["reasons"] = list(vd.get("reasons") or []) + [gate_reason]

        signal = "OK" if verdicts else "EMPTY"
        return fc.StateResult(signal=signal, writes={
            "verdicts": verdicts,
            "cross_check": cross_check.to_dict(),
        })


# ==========================================================================
# GATE — the cumulative regression gate (DESIGN §2.2 [v2]) between REVIEW and
# INTEGRATE. Deterministic, SCRIPT-TIER: the self-contained regression gate that
# replaces reliance on external CI. Reads `verdicts` (the open loop PRs) +
# `regression_command` from the central config; writes `gate_results` (one
# GateResult per gated PR); emits OK.
#
# No-op when `regression_command` is null (every PR passes — a project that has
# not configured a gate merges exactly as before). Otherwise CUMULATIVE: create a
# DISPOSABLE integration worktree at current `main`, merge each ok verdict PR in
# a deterministic order (PR number) with the same --no-ff strategy INTEGRATE
# uses, and run the regression after each clean merge, so PR k is validated on
# top of `main` + the already-passed 1..k-1. A textual conflict or a nonzero
# regression rolls the PR out, EXCLUDES it, and records the failure. The worktree
# is ALWAYS removed. GATE never merges to `main` and never calls the merge sink.
#
# EVERYTHING external — git (worktree add/fetch/merge/reset/remove), the
# regression subprocess, and gh issue-ref resolution — is behind an INJECTABLE
# callable with a production default, so the cumulative logic is unit-tested with
# a FAKE runner scripting outcomes (no real git, no PRs, no network).
# ==========================================================================

# The versioned GateResult schema (machine-first; bumped on a breaking field-set
# change). Distinct from the feature version.
GATE_RESULT_SCHEMA_VERSION = "1.0.0"

# The bounded cap on a GateResult.failure_summary (a tail of the regression
# output, not the whole multi-thousand-line log).
_FAILURE_SUMMARY_MAX_BYTES = 4096


@dataclass(eq=True)
class GateResult:
    """One GateResult per REVIEW-passed (ok) PR the GATE state gated (§2.2 [v2]).

    `passed` is whether the PR merged cleanly AND its cumulative regression run
    exit-0'd. `reason` is None on pass, else `"regression"` (clean merge, nonzero
    regression), `"conflict"` (textual merge conflict — regression not run),
    `"fetch-failed"` (the PR head `git fetch` returned nonzero — merged nothing,
    excluded so a stale FETCH_HEAD is never merged), or `"load-bearing"` (a
    doc-touched feature dropped a declared load-bearing token from its post-change
    doc surfaces — the #353 doc-survival gate; regression not run). `issue_ref`
    is the PR's closing-issue reference (resolved via the injectable
    gh resolver; None when unresolvable) so INTEGRATE can comment the failure on
    the issue. `failure_summary` is a BOUNDED tail of the regression output
    (empty on pass). `to_dict`/`from_dict` give the machine-first, versioned
    representation for the `gate_results` slot.
    """

    pr_ref: str
    issue_ref: object
    passed: bool
    reason: object = None
    failure_summary: str = ""

    def to_dict(self):
        return {
            "schema_version": GATE_RESULT_SCHEMA_VERSION,
            "pr_ref": self.pr_ref,
            "issue_ref": self.issue_ref,
            "passed": self.passed,
            "reason": self.reason,
            "failure_summary": self.failure_summary,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            pr_ref=d["pr_ref"],
            issue_ref=d.get("issue_ref"),
            passed=d["passed"],
            reason=d.get("reason"),
            failure_summary=d.get("failure_summary", ""),
        )


# The fsm-contracts slot descriptor. `gate_results` is an array slot (a list of
# GateResult dicts); the slot version tracks the schema version.
GATE_RESULTS_SLOT = {
    "name": "gate_results",
    "schema": {"type": "array"},
    "version": GATE_RESULT_SCHEMA_VERSION,
}

# Closed signal set GATE emits: always OK (it reports the gate_results partition;
# a gate failure is recorded per-PR, never signalled).
GATE_SIGNALS = ["OK"]

# Per-state manifest (bounded-scope): reads the verdicts slot (the open loop PRs),
# writes the gate_results slot, emits OK.
GATE_MANIFEST = fc.StateManifest(reads=["verdicts"],
                                 writes=["gate_results"],
                                 emits=GATE_SIGNALS)


def gh_closing_issue_ref(pr_ref, repo=None, runner=subprocess.run):
    """Production issue-ref resolver: shell `gh pr view <n> --json
    closingIssuesReferences` and return the first closing issue's ref
    (`owner/repo#number` when `repo` is given, else `#number`), or None when the
    PR closes no issue. Injectable `runner` for deterministic tests (no network).
    """
    number = pr_ref.split("#")[-1]
    cmd = ["gh", "pr", "view", number, "--json", "closingIssuesReferences",
           "-q", ".closingIssuesReferences"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    refs = json.loads(out.stdout or "[]")
    if not refs:
        return None
    issue_number = refs[0].get("number")
    if issue_number is None:
        return None
    return f"{repo}#{issue_number}" if repo else f"#{issue_number}"


def _bounded_tail(text, max_bytes=_FAILURE_SUMMARY_MAX_BYTES):
    """The last `max_bytes` bytes of `text` (a bounded tail of a regression log).
    Pure; empty text -> ''."""
    text = text or ""
    if len(text) <= max_bytes:
        return text
    return text[-max_bytes:]


# ==========================================================================
# Doc-surface load-bearing-token survival check (issue #353).
#
# A doc-reduction PR (housekeep-style) is guarded today only by a line-count
# baseline + the advisory REVIEW gate; feature test suites do NOT assert doc
# prose, so an over-deletion that drops a load-bearing token (a schema field, a
# script/symbol name, an invariant statement, a cross-reference) can pass the
# line-count gate and auto-merge. This mirrors the rabbit-housekeep skill's
# load-bearing-survival test, but enforced on the loop's OWN auto-merge path:
# GATE, having already built the post-merge integration tree, asserts that every
# token a touched feature DECLARES load-bearing still appears in that feature's
# post-change doc surfaces. A dropped declared token = a "load-bearing" gate
# failure (block the merge). Features that declare no tokens, and PRs that touch
# no doc surface, are unaffected.
# ==========================================================================

# The doc surfaces (feature-relative) the survival check inspects — the same set
# issue #353 names: the spec + contract prose and every skill's SKILL.md.
DOC_SURFACE_FIXED = ("docs/spec.md", "docs/contract.md")
DOC_SURFACE_SKILL_GLOB = os.path.join("skills", "*", "SKILL.md")

# The per-feature declared-token file (a feature-relative path under test/): a
# JSON object `{"tokens": [str, ...]}` listing the tokens that MUST survive any
# reduction of that feature's doc surfaces. Absent or empty ⇒ no tokens declared
# ⇒ the feature is not gated on token survival (opt-in, never mandated).
LOAD_BEARING_TOKENS_FILE = os.path.join("test", "load_bearing_tokens.json")


def _is_doc_surface(rel_path):
    """Whether a feature-relative path is one of the doc surfaces the survival
    check inspects (docs/spec.md, docs/contract.md, or skills/<name>/SKILL.md).
    Pure; path separators are normalized so it matches on any platform."""
    norm = rel_path.replace("\\", "/")
    if norm in ("docs/spec.md", "docs/contract.md"):
        return True
    parts = norm.split("/")
    return len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md"


def _parse_declared_tokens(text):
    """The declared token list parsed from `load_bearing_tokens.json` source
    text (`{"tokens": [...]}`), keeping only non-empty strings in declared order.
    Returns [] when the text is absent, unparseable, or declares no tokens.
    Pure — the shared parse used by both the on-disk and the git-blob readers."""
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    tokens = data.get("tokens") if isinstance(data, dict) else None
    return [t for t in (tokens or []) if isinstance(t, str) and t]


def declared_load_bearing_tokens(feature_dir):
    """The tokens a feature DECLARES load-bearing, read from its
    `test/load_bearing_tokens.json` (`{"tokens": [...]}`). Returns [] when the
    file is absent, unreadable, or declares no tokens (opt-in: a feature that has
    not declared any tokens is never gated on token survival). Pure read."""
    path = os.path.join(feature_dir, LOAD_BEARING_TOKENS_FILE)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r") as f:
            text = f.read()
    except OSError:
        return []
    return _parse_declared_tokens(text)


def _doc_surface_text(feature_dir):
    """The combined text of a feature's PRESENT doc surfaces (spec + contract +
    each skill's SKILL.md). A missing surface contributes nothing (it cannot
    carry a token). Pure read."""
    import glob as _glob
    paths = [os.path.join(feature_dir, rel) for rel in DOC_SURFACE_FIXED]
    paths += sorted(_glob.glob(os.path.join(feature_dir,
                                            DOC_SURFACE_SKILL_GLOB)))
    chunks = []
    for p in paths:
        if os.path.isfile(p):
            try:
                with open(p, "r") as f:
                    chunks.append(f.read())
            except OSError:
                continue
    return "\n".join(chunks)


def _token_survives(token, text):
    """Whether a declared token still appears in `text` as a STANDALONE token
    rather than an incidental substring of unrelated prose. Survival requires a
    word-boundary match: the token's occurrence must not be flanked by a word
    character (letter/digit/underscore) that would fold it into a larger
    identifier. So `Verdict` does NOT survive on `ReviewVerdict`, and `3.7` does
    NOT survive on `13.7`; but `ci_state`, `fsm-contracts`, and `docs/spec.md`
    still match their real occurrences. Pure."""
    esc = re.escape(token)
    # (?<![\w]) / (?![\w]) only bite when the token edge is itself a word char;
    # a token whose edge is punctuation (e.g. "/foo") is matched literally there.
    left = r"(?<!\w)" if token[:1].isalnum() or token[:1] == "_" else ""
    right = r"(?!\w)" if token[-1:].isalnum() or token[-1:] == "_" else ""
    return re.search(left + esc + right, text) is not None


def missing_load_bearing_tokens(feature_dir, tokens=None):
    """The declared load-bearing tokens ABSENT from a feature's post-change doc
    surfaces, in declared order. Empty when no tokens are declared or all declared
    tokens survive. Survival is a WORD-BOUNDARY match (see `_token_survives`), not
    a raw substring test: a token counts as surviving only when it appears as a
    standalone token, so a dropped token cannot pass on an incidental substring of
    unrelated prose (e.g. `Verdict` inside `ReviewVerdict`) and short/common
    tokens can actually fail. Pure: it computes set-membership against the
    feature's doc surfaces (read from `feature_dir`) and does no I/O beyond those
    reads.

    `tokens` is the declared must-survive set to check. When None, it falls back
    to the feature's own `test/load_bearing_tokens.json`. GATE passes the BASE
    (pre-merge) declaration explicitly so a PR cannot bypass the gate by dropping
    a token's declaration alongside the token itself (issue #392)."""
    if tokens is None:
        tokens = declared_load_bearing_tokens(feature_dir)
    if not tokens:
        return []
    text = _doc_surface_text(feature_dir)
    return [tok for tok in tokens if not _token_survives(tok, text)]


def _feature_dir_of(rel_path, features_root):
    """The feature directory owning a repo-relative changed path, or None when
    the path is not under `<features_root>/<feature>/...`. `features_root` is the
    repo-relative root the feature dirs live under (e.g. `features` or a nested
    `<subtree>/features`). Pure path arithmetic."""
    norm = rel_path.replace("\\", "/").lstrip("/")
    root = (features_root or "").replace("\\", "/").strip("/")
    if not root or not norm.startswith(root + "/"):
        return None
    tail = norm[len(root) + 1:].split("/")
    if len(tail) < 2:
        return None
    return "/".join([root, tail[0]])


def features_with_changed_doc_surfaces(changed_paths, features_root):
    """The set of feature dirs (repo-relative) whose DOC SURFACES a PR changed,
    given the PR's changed repo-relative paths + the `features_root`. A path is
    counted only when it is one of a feature's doc surfaces (spec/contract/SKILL);
    a PR that changed no doc surface yields the empty set (the survival check is
    then a no-op — no effect on non-doc PRs). Deterministic, sorted. Pure."""
    feats = set()
    for path in changed_paths or []:
        feat_dir = _feature_dir_of(path, features_root)
        if feat_dir is None:
            continue
        rel_within = path.replace("\\", "/").lstrip("/")[len(feat_dir) + 1:]
        if _is_doc_surface(rel_within):
            feats.add(feat_dir)
    return sorted(feats)


class Gate:
    """The GATE state (cumulative regression gate, DESIGN §2.2 [v2]).

    Reads the `verdicts` slot and, when `regression_command` is configured, runs
    the command CUMULATIVELY against each ok verdict PR in a disposable
    integration worktree at current `main`: merge PR k with --no-ff, run the
    load-bearing-token survival check (issue #353) then the regression, keep the
    merge on pass / roll it back + exclude on a failure, and record a `GateResult`
    per PR. A textual conflict excludes the PR with reason `"conflict"`; a dropped
    declared load-bearing token excludes it with reason `"load-bearing"`. When
    `regression_command` is null GATE is a no-op PASS (every passed=True). Writes
    `gate_results`, emits OK.

    Every external edge — git (via `runner`), the regression subprocess (via the
    same `runner`), and the closing-issue resolver (`issue_resolver`) — is
    injectable so the cumulative logic is unit-tested with a fake. GATE never
    merges to `main` and never calls the INTEGRATE merge sink.

    `features_root` is the repo-relative root the feature dirs live under (e.g.
    `features` or a nested `<subtree>/features`); it is REQUIRED for the
    doc-surface token survival check to map a PR's changed paths to features and
    locate their doc surfaces in the merged worktree. When it is None the token
    check is skipped (the regression gate is unaffected) — the check is opt-in per
    feature anyway.
    """

    def __init__(self, regression_command, runner=subprocess.run, repo=None,
                 default_branch=None, issue_resolver=gh_closing_issue_ref,
                 worktree_dir=None, features_root=None):
        self._regression_command = regression_command
        self._runner = runner
        self._repo = repo
        self._default_branch = default_branch
        self._issue_resolver = issue_resolver
        self._worktree_dir = worktree_dir
        self._features_root = features_root

    def _ok_verdicts_ordered(self, verdicts):
        """The ok verdicts in deterministic PR-number order (a non-ok verdict
        won't merge, so GATE does not gate it)."""
        ok = [v for v in verdicts if v.get("ok")]
        return sorted(ok, key=lambda v: _pr_number(v["pr_ref"]))

    def _resolve_issue_ref(self, pr_ref):
        try:
            return self._issue_resolver(pr_ref, repo=self._repo)
        except Exception:  # noqa: BLE001 — a resolver fault must not wedge GATE
            return None

    def run(self, ctx):
        verdicts = ctx.read("verdicts")
        ordered = self._ok_verdicts_ordered(verdicts)

        if self._regression_command is None:
            # No-op PASS: every ok PR passes; no worktree is created.
            results = [
                GateResult(pr_ref=v["pr_ref"],
                           issue_ref=self._resolve_issue_ref(v["pr_ref"]),
                           passed=True).to_dict()
                for v in ordered
            ]
            return fc.StateResult(signal="OK",
                                  writes={"gate_results": results})

        worktree = self._worktree_dir or os.path.join(
            "/tmp", "am-gate-integration")

        # Setup robustness: the worktree lives at a FIXED path, so a crashed
        # prior tick can leave a stale leftover. Best-effort clear it BEFORE the
        # add (ignore returncodes — purely defensive cleanup) so a leftover
        # never wedges every subsequent tick.
        self._runner(["git", "worktree", "remove", "--force", worktree],
                     capture_output=True, text=True)
        self._runner(["git", "worktree", "prune"],
                     capture_output=True, text=True)

        # Create the disposable integration worktree; CHECK the add returncode.
        # On failure, a setup fault is not any PR's fault: write an EMPTY
        # gate_results list so INTEGRATE merges nothing and posts NO gate-fail
        # marker — the tick converges to idle and retries cleanly next tick,
        # rather than false-failing every PR into the park threshold. Do NOT
        # enter the try/finally (a failed add created nothing to remove).
        add = self._runner(["git", "worktree", "add", "--detach", worktree,
                            self._default_branch],
                           capture_output=True, text=True)
        if add.returncode != 0:
            return fc.StateResult(signal="OK", writes={"gate_results": []})

        results = []
        try:
            for v in ordered:
                results.append(self._gate_one(v, worktree))
        finally:
            self._runner(["git", "worktree", "remove", "--force", worktree],
                         capture_output=True, text=True)

        return fc.StateResult(signal="OK",
                              writes={"gate_results": [r.to_dict()
                                                      for r in results]})

    def _gate_one(self, verdict, worktree):
        """Merge one PR into the integration worktree and run the cumulative
        regression, returning its GateResult. Rolls back on a nonzero regression;
        aborts + excludes on a textual conflict."""
        pr_ref = verdict["pr_ref"]
        issue_ref = self._resolve_issue_ref(pr_ref)
        number = _pr_number(pr_ref)

        # Record the pre-merge HEAD so a regression failure can roll back to it.
        pre = self._runner(["git", "-C", worktree, "rev-parse", "HEAD"],
                           capture_output=True, text=True)
        pre_sha = (pre.stdout or "").strip()

        # Fetch the PR head then merge it with the same --no-ff strategy
        # INTEGRATE uses (the validated tree equals the merged tree). CHECK the
        # fetch returncode BEFORE merging: on a fetch failure return a
        # fetch-failed GateResult WITHOUT merging, so a failed fetch can never
        # silently merge the PREVIOUS PR's stale FETCH_HEAD into the cumulative
        # tree. No rollback is needed (no merge happened).
        fetch = self._runner(["git", "-C", worktree, "fetch", "origin",
                              f"pull/{number}/head"],
                             capture_output=True, text=True)
        if fetch.returncode != 0:
            return GateResult(
                pr_ref=pr_ref, issue_ref=issue_ref, passed=False,
                reason="fetch-failed",
                failure_summary=_bounded_tail(fetch.stderr or fetch.stdout))
        merge = self._runner(
            ["git", "-C", worktree, "merge", "--no-ff", "--no-edit",
             "-m", f"gate-integrate {pr_ref}", "FETCH_HEAD"],
            capture_output=True, text=True)
        if merge.returncode != 0:
            # textual conflict — abort + exclude (not carried forward).
            self._runner(["git", "-C", worktree, "merge", "--abort"],
                         capture_output=True, text=True)
            return GateResult(pr_ref=pr_ref, issue_ref=issue_ref,
                              passed=False, reason="conflict",
                              failure_summary=_bounded_tail(merge.stdout))

        # Doc-surface load-bearing-token survival (issue #353): before the
        # regression (feature suites do NOT assert doc prose), assert every token
        # a doc-touched feature declares load-bearing survived in the merged tree.
        dropped = self._load_bearing_violation(worktree, pre_sha)
        if dropped:
            # roll back the merge; exclude the PR (a dropped token = INVALID).
            self._runner(["git", "-C", worktree, "reset", "--hard", pre_sha],
                         capture_output=True, text=True)
            return GateResult(
                pr_ref=pr_ref, issue_ref=issue_ref, passed=False,
                reason="load-bearing",
                failure_summary=_bounded_tail(dropped))

        reg = self._runner(self._regression_command, shell=True, cwd=worktree,
                           capture_output=True, text=True)
        if reg.returncode != 0:
            # roll back the merge; exclude the PR from the growing tree.
            self._runner(["git", "-C", worktree, "reset", "--hard", pre_sha],
                         capture_output=True, text=True)
            return GateResult(
                pr_ref=pr_ref, issue_ref=issue_ref, passed=False,
                reason="regression",
                failure_summary=_bounded_tail(reg.stdout or reg.stderr))

        # clean merge + passing regression — KEEP as base for the next PR.
        return GateResult(pr_ref=pr_ref, issue_ref=issue_ref, passed=True)

    def _declared_tokens_at_base(self, worktree, feat_rel, pre_sha):
        """The load-bearing tokens a feature DECLARED at the pre-merge base
        (`pre_sha`), read from that revision's `<feat_rel>/test/
        load_bearing_tokens.json` via `git show`. Anchoring the declared set to
        the base — not the PR's own post-merge copy — is what stops a PR from
        weakening the gate that guards it: dropping a token AND its declaration in
        one PR still fails, because the base still declares the token (issue
        #392). Returns [] when the file was absent at base or `git show` failed
        (opt-in: an undeclared feature is never gated)."""
        blob = f"{pre_sha}:{feat_rel}/{LOAD_BEARING_TOKENS_FILE}"
        show = self._runner(
            ["git", "-C", worktree, "show", blob],
            capture_output=True, text=True)
        if show.returncode != 0:
            return []
        return _parse_declared_tokens(show.stdout or "")

    def _load_bearing_violation(self, worktree, pre_sha):
        """Assert the doc-surface load-bearing tokens survived in the merged
        tree (issue #353). Returns a human-readable failure_summary string when a
        doc-touched feature DROPPED a declared token, else '' (survival OK / not
        applicable). Reads the PR's changed doc surfaces via `git diff
        --name-only <pre_sha> HEAD` (the merge just applied), and for each touched
        feature checks the BASE (pre-merge, `pre_sha`) declared token set against
        its post-change doc surfaces IN THE WORKTREE. The declared set is anchored
        to base so a PR cannot bypass the gate by dropping a token's declaration
        together with the token (issue #392). Skipped (returns '') when
        `features_root` is unconfigured or the PR touched no doc surface — no
        effect on non-doc PRs."""
        if self._features_root is None:
            return ""
        diff = self._runner(
            ["git", "-C", worktree, "diff", "--name-only", pre_sha, "HEAD"],
            capture_output=True, text=True)
        changed = [ln.strip() for ln in (diff.stdout or "").splitlines()
                   if ln.strip()]
        feat_dirs = features_with_changed_doc_surfaces(
            changed, self._features_root)
        problems = []
        for feat_rel in feat_dirs:
            base_tokens = self._declared_tokens_at_base(
                worktree, feat_rel, pre_sha)
            missing = missing_load_bearing_tokens(
                os.path.join(worktree, feat_rel), tokens=base_tokens)
            if missing:
                problems.append(
                    f"{feat_rel}: dropped load-bearing token(s): "
                    f"{', '.join(missing)}")
        return "\n".join(problems)


def make_gate(runtime):
    """GATE adapter factory (verify-integrate): the cumulative regression gate
    (DESIGN §2.2 [v2]) between REVIEW and INTEGRATE. Resolves `regression_command`
    from the loaded central config (sg.load_config(project_dir) via the
    regression_command accessor) — null => GATE is a no-op PASS. Binds the
    injectable git+regression runner, the closing-issue resolver, the repo, and
    the resolved default branch. Returns (GATE_MANIFEST, Gate.run) per the
    factory(runtime) -> (StateManifest, run_callable) convention scheduling wires.

    For the doc-surface load-bearing-token survival check (issue #353) it binds a
    REPO-RELATIVE features root, needed to map a PR's `git diff` paths (the
    integration worktree's repo-relative paths) to features. It reads the
    DEDICATED `doc_check_features_root` config key (§3.7) ONLY — deliberately
    kept SEPARATE from `features_root` (which VERIFY's complement runner treats
    as an on-disk locator that may be absolute) so the two semantics never share
    one overloaded key (issue #391): setting `features_root` for VERIFY must
    never silently turn this gate on or off. An absolute `doc_check_features_root`
    cannot match repo-relative diff paths, so it is ignored here; when the key is
    unset or absolute the token check is left off (a conservative no-op; the
    check is opt-in per feature via test/load_bearing_tokens.json anyway).
    """
    project_dir = runtime.get("project_dir") or "."
    cfg = sg.load_config(project_dir)
    regression = sg.regression_command(cfg)
    repo = runtime.get("repo")
    default_branch = runtime.get("default_branch")
    if default_branch is None:
        default_branch = gh_default_branch_source(repo)
    configured_root = sg.doc_check_features_root(cfg)
    doc_features_root = (configured_root
                         if configured_root and not os.path.isabs(configured_root)
                         else None)
    gate = Gate(regression_command=regression, runner=subprocess.run,
                repo=repo, default_branch=default_branch,
                issue_resolver=gh_closing_issue_ref,
                features_root=doc_features_root)
    return GATE_MANIFEST, gate.run


# ==========================================================================
# INTEGRATE (slice 2) — the single highest-stakes act-side state.
# ==========================================================================

# The versioned IntegrationResult schema (machine-first; bumped on a breaking
# change to the field set). Distinct from the feature version.
INTEGRATION_RESULT_SCHEMA_VERSION = "1.2.0"


@dataclass(eq=True)
class IntegrationResult:
    """The outcome of an INTEGRATE run (DESIGN §3.7).

    Partitions each considered PR into exactly one of these lists:
      - `merged`  — [{pr_ref, url}] the PRs the merge sink merged immediately.
      - `auto_merge_enabled` — [{pr_ref, url}] PRs for which GitHub NATIVE
        auto-merge was ENABLED (merge pending on required checks). A PENDING
        SUCCESS, NOT an error: the PR stays OPEN until GitHub merges it once its
        checks pass; the existing acted-ledger opened-lock keeps the loop from
        re-working an OPEN PR.
      - `skipped` — [{pr_ref, reason}] non-ok verdicts, not-permitted modes
        (the dry-run/propose NO-OP), guardrail violations, ok verdicts with
        no matching GateResult (defensive: never merge an un-gated PR), and the
        would-close intent for an orphaned PR at a mode that does not permit merge.
      - `errors`  — [{pr_ref, reason}] PRs whose merge sink or close sink raised.
      - `gate_failed` — [{pr_ref, issue_ref, reason}] PRs INTEGRATE did NOT merge
        because their GATE result failed; a machine-readable marker comment was
        posted on their linked issue instead (the Phase-2 retry/threshold model).
      - `closed_orphaned` — [{pr_ref, issue_ref}] loop PRs INTEGRATE CLOSED because
        their driver issue is closed (orphaned verdicts) — the convergence
        guarantee that a superseded loop PR does not linger open forever.

    Idempotent at the loop level: a merged PR leaves the open set, so a re-run
    never double-merges. `to_dict`/`from_dict` give the machine-first, versioned
    representation for the `integration_result` blackboard slot.
    """

    merged: List[dict] = field(default_factory=list)
    auto_merge_enabled: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)
    gate_failed: List[dict] = field(default_factory=list)
    closed_orphaned: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": INTEGRATION_RESULT_SCHEMA_VERSION,
            "merged": [dict(e) for e in self.merged],
            "auto_merge_enabled": [dict(e) for e in self.auto_merge_enabled],
            "skipped": [dict(e) for e in self.skipped],
            "errors": [dict(e) for e in self.errors],
            "gate_failed": [dict(e) for e in self.gate_failed],
            "closed_orphaned": [dict(e) for e in self.closed_orphaned],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            merged=[dict(e) for e in d.get("merged", [])],
            auto_merge_enabled=[dict(e) for e in d.get("auto_merge_enabled", [])],
            skipped=[dict(e) for e in d.get("skipped", [])],
            errors=[dict(e) for e in d.get("errors", [])],
            gate_failed=[dict(e) for e in d.get("gate_failed", [])],
            closed_orphaned=[dict(e) for e in d.get("closed_orphaned", [])],
        )


# The fsm-contracts slot descriptor. `integration_result` is an object slot (a
# single IntegrationResult dict); the slot version tracks the schema version.
INTEGRATION_RESULT_SLOT = {
    "name": "integration_result",
    "schema": {"type": "object"},
    "version": INTEGRATION_RESULT_SCHEMA_VERSION,
}

# Closed signal set INTEGRATE emits: always OK (it reports the partition; it
# never fails the tick — a merge fault is recorded under `errors`, not signalled).
INTEGRATE_SIGNALS = ["OK"]

# Per-state manifest (bounded-scope): THIN merge — reads the verdicts slot (a PR
# merges only when ok AND its GateResult passed; the review-approval coupling is
# gone, REVIEW is advisory), writes the integration_result slot, emits OK.
#
# COEXISTENCE WINDOW (spec-rules §3, additive-by-default): INTEGRATE CONSULTS the
# `gate_results` slot the cumulative GATE writes, but reads it OPTIONALLY (via
# _read_optional_slot, mirroring how VERIFY reads cross_cutting_risk) and so does
# NOT DECLARE it a hard manifest read yet. GATE is wired into the route by
# scheduling in a LATER cycle (make_gate + GATE_MANIFEST are exported for exactly
# that); until scheduling routes GATE AND seeds gate_results into the initial
# TickContext, the un-wired route must still validate — a hard manifest read of a
# slot no routed state writes would fail data-readiness and break the loop. When
# scheduling wires GATE it will seed gate_results and promote it to a declared
# read. Until then INTEGRATE with an absent gate_results falls back to the
# pre-GATE behaviour (an ok verdict merges on the verdict + guardrail gates).
INTEGRATE_MANIFEST = fc.StateManifest(reads=["verdicts"],
                                      writes=["integration_result"],
                                      emits=INTEGRATE_SIGNALS)

# The FIXED machine-readable marker a gate-fail comment carries so a later tick's
# TRIAGE reads it deterministically (the Phase-2 retry/threshold model).
GATE_FAIL_MARKER = "<!-- auto-maintainer:gate-fail -->"


def gh_issue_comment_sink(issue_ref, body, repo=None, runner=subprocess.run):
    """Production comment sink: shell `gh issue comment <n> --body <body>` (and
    `--repo` when given) to post a comment on the PR's linked issue. Injectable
    `runner` for deterministic tests (no network). Returns None."""
    number = issue_ref.split("#")[-1]
    cmd = ["gh", "issue", "comment", number, "--body", body]
    if repo:
        cmd += ["--repo", repo]
    runner(cmd, capture_output=True, text=True, check=True)


def gate_fail_comment_body(pr_ref, reason, failure_summary):
    """Build the machine-readable gate-fail comment body: the FIXED marker plus a
    JSON block carrying {pr_ref, reason, failure_summary} a later tick's TRIAGE
    parses deterministically. Pure."""
    payload = json.dumps({
        "pr_ref": pr_ref,
        "reason": reason,
        "failure_summary": failure_summary,
    }, sort_keys=True)
    return (f"{GATE_FAIL_MARKER}\n"
            f"Automated regression GATE did not pass for {pr_ref}; "
            f"this PR was NOT merged.\n\n{payload}\n")


def _parse_owner_repo(pr_ref, repo):
    """Resolve (owner, name) from an explicit `repo` ('owner/name') or, failing
    that, from an `owner/name#number` pr_ref. Returns (None, None) when neither
    yields an owner/name — the merge-queue probe then treats it as no-queue."""
    if repo and "/" in repo:
        owner, name = repo.split("/", 1)
        return owner, name
    base = pr_ref.split("#")[0]
    if "/" in base:
        owner, name = base.split("/", 1)
        return owner, name
    return None, None


def _has_merge_queue(pr_ref, repo, base_branch, runner):
    """Probe whether the PR's base branch is governed by a GitHub MERGE QUEUE via
    GraphQL `repository.mergeQueue(branch)` (non-null => queue). TOLERANT by
    design: no base branch, an unparseable owner/name, a non-zero gh exit, or any
    parse error is treated as NO queue — the probe never fails the merge, it only
    selects the correct merge command."""
    if not base_branch:
        return False
    owner, name = _parse_owner_repo(pr_ref, repo)
    if not owner or not name:
        return False
    query = (
        "query($owner:String!,$name:String!,$branch:String!){"
        "repository(owner:$owner,name:$name){"
        "mergeQueue(branch:$branch){id}}}")
    # All three are GraphQL String! variables; pass them with -f (always sends a
    # string) NOT -F (which type-infers, e.g. coercing a numeric branch name to an
    # int and breaking the String! binding -> a spurious no-queue verdict).
    cmd = ["gh", "api", "graphql",
           "-f", "query=" + query,
           "-f", "owner=" + owner,
           "-f", "name=" + name,
           "-f", "branch=" + base_branch]
    try:
        proc = runner(cmd, capture_output=True, text=True)
        if getattr(proc, "returncode", 0) != 0:
            return False
        data = json.loads(proc.stdout or "{}")
        mq = (data.get("data", {}) or {}).get("repository", {}) or {}
        return mq.get("mergeQueue") is not None
    except Exception:  # noqa: BLE001 — tolerant probe: any error => no queue
        return False


def _run_gh_or_raise(cmd, runner):
    """Run a gh command with CAPTURED stderr (NOT check=True) and, on a non-zero
    exit, raise RuntimeError carrying the gh STDERR text so the fault is recorded
    diagnosably (never a bare 'exit status 1'). Returns the completed proc."""
    proc = runner(cmd, capture_output=True, text=True)
    if getattr(proc, "returncode", 0) != 0:
        stderr = (getattr(proc, "stderr", "") or "").strip()
        raise RuntimeError(
            stderr or ("gh " + " ".join(cmd[1:3]) +
                       " failed (exit " + str(proc.returncode) + ")"))
    return proc


def _is_not_yet_mergeable(stderr):
    """Heuristic: does gh's stderr say the PR is not YET mergeable (required
    checks still pending) — the recoverable case where enabling native auto-merge
    is correct — versus a hard failure (403/404/protected branch)?"""
    s = (stderr or "").lower()
    return ("not mergeable" in s or "not yet mergeable" in s
            or "required status check" in s or "required check" in s
            or "checks are still pending" in s or "checks still pending" in s
            or "pending" in s and "check" in s)


def gh_pr_merge_sink(pr_ref, repo=None, base_branch=None, runner=subprocess.run):
    """Production merge sink — MERGE-QUEUE-AWARE. Returns a {pr_ref, url,
    auto_enabled} entry whose `url` is derived from `pr_ref` via `_pr_url`
    (observability: a real link, not url:''). `gh` carries its own auth. The PR
    number is parsed off the `owner/repo#number` ref (gh accepts the bare number
    when `--repo` scopes it). The subprocess `runner` is INJECTABLE (defaulting to
    subprocess.run) so tests exercise every route with no network.

    The sink first DETECTS a merge queue on the PR's base branch
    (`_has_merge_queue`, GraphQL `repository.mergeQueue(branch)`):

    - QUEUE PRESENT -> `gh pr merge <n> --auto` with NO method flag
      (--merge/--squash/--rebase) and NO --delete-branch: a queue branch REJECTS a
      method flag and the queue owns the method + branch deletion. Adds the PR to
      the queue -> `auto_enabled=True` (a pending success).
    - NO QUEUE -> the method-specified path: try an immediate
      `gh pr merge <n> --merge --delete-branch`; on success -> `auto_enabled=False`
      (merged now). If it fails because the PR is not YET mergeable (required
      checks pending), enable native auto-merge with
      `gh pr merge <n> --auto --merge --delete-branch` -> `auto_enabled=True`.

    gh runs with CAPTURED stderr (never check=True); a non-zero exit RAISES
    RuntimeError carrying the gh stderr text so INTEGRATE records a diagnosable
    reason (403/404/queue-conflict), never a bare 'exit status 1'."""
    number = pr_ref.split("#")[-1]
    url = _pr_url(pr_ref, repo)
    if _has_merge_queue(pr_ref, repo, base_branch, runner):
        cmd = ["gh", "pr", "merge", number, "--auto"]
        if repo:
            cmd += ["--repo", repo]
        _run_gh_or_raise(cmd, runner)
        return {"pr_ref": pr_ref, "url": url, "auto_enabled": True}
    # No queue: immediate method-specified merge, capturing stderr.
    cmd = ["gh", "pr", "merge", number, "--merge", "--delete-branch"]
    if repo:
        cmd += ["--repo", repo]
    proc = runner(cmd, capture_output=True, text=True)
    if getattr(proc, "returncode", 0) == 0:
        return {"pr_ref": pr_ref, "url": url, "auto_enabled": False}
    stderr = (getattr(proc, "stderr", "") or "").strip()
    if _is_not_yet_mergeable(stderr):
        cmd = ["gh", "pr", "merge", number, "--auto", "--merge", "--delete-branch"]
        if repo:
            cmd += ["--repo", repo]
        _run_gh_or_raise(cmd, runner)
        return {"pr_ref": pr_ref, "url": url, "auto_enabled": True}
    raise RuntimeError(
        stderr or ("gh pr merge failed (exit " + str(proc.returncode) + ")"))


# The machine+human explanation the orphan-close comment carries.
_ORPHAN_CLOSE_COMMENT = (
    "Closed by auto-maintainer: the driver issue for this PR is closed, so this "
    "loop PR is superseded/abandoned and will not be merged.")


def gh_pr_close_sink(pr_ref, repo=None, runner=subprocess.run):
    """Production close sink: shell `gh pr close <number> --delete-branch
    --comment <body>` (and `--repo` when given) to CLOSE an orphaned loop PR (its
    driver issue is closed) and delete its head branch in one deterministic CLI
    call. `gh` carries its own auth; check=True so a failed close is loud and
    locatable at the close boundary. Injectable `runner` for deterministic tests
    (no network), mirroring gh_pr_merge_sink / gh_issue_comment_sink. Returns
    None."""
    number = pr_ref.split("#")[-1]
    cmd = ["gh", "pr", "close", number, "--delete-branch",
           "--comment", _ORPHAN_CLOSE_COMMENT]
    if repo:
        cmd += ["--repo", repo]
    runner(cmd, capture_output=True, text=True, check=True)


class Integrate:
    """The INTEGRATE state (DESIGN §3.7.3, §3.8.1, §3.8.2) — a THIN merge.

    Reads ONLY the `verdicts` slot and, for each `ok` verdict, merges the PR via
    the injectable `merge_sink` — but ONLY when permits('merge', mode) is True
    (the trust ladder permits merge at auto-merge only) AND merge_guardrails
    passes (the hard backstop below the ladder). REVIEW is now ADVISORY, so
    INTEGRATE does NOT read review_verdicts and does NOT gate merge on a review
    approval: the merge rests on IMPLEMENT's deterministic run.py gate + VERIFY +
    guardrails + the trust ladder. Every non-merged PR goes to `skipped`:

      - a non-ok verdict (its reasons become the skip reason);
      - any verdict when the mode does not permit merge (the dry-run/propose
        NO-OP: the would-merge intent is recorded, a human merges);
      - an ok verdict that violates a guardrail (wrong base / dirty tree).

    A merge sink that raises records the PR under `errors` (the run still
    completes with OK). Writes the `integration_result` slot, emits OK.

    The merge sink, the trust-gate fn, and the guardrails fn are all injectable
    (the determinism seam) so tests drive INTEGRATE with no network.
    """

    def __init__(self, mode, merge_sink=gh_pr_merge_sink, repo=None,
                 default_branch=None, permits_fn=sg.permits,
                 guardrails_fn=sg.merge_guardrails,
                 comment_sink=gh_issue_comment_sink,
                 close_sink=gh_pr_close_sink):
        self._mode = mode
        self._merge_sink = merge_sink
        self._repo = repo
        self._default_branch = default_branch
        self._permits_fn = permits_fn
        self._guardrails_fn = guardrails_fn
        self._comment_sink = comment_sink
        self._close_sink = close_sink

    def run(self, ctx):
        verdicts = ctx.read("verdicts")
        # gate_results is read OPTIONALLY (the coexistence window): when the
        # cumulative GATE is wired by scheduling the slot is present and INTEGRATE
        # merges only gate-passed PRs; until then (slot absent) INTEGRATE falls
        # back to the pre-GATE behaviour and gates only on verdict + guardrails.
        gate_results = _read_optional_slot(ctx, "gate_results")
        gated = gate_results is not None
        gate_by_ref = {g["pr_ref"]: g for g in (gate_results or [])}
        permitted = self._permits_fn("merge", self._mode)
        result = IntegrationResult()

        for vd in verdicts:
            pr_ref = vd["pr_ref"]
            # Orphaned loop PRs are CLOSED, not merged (convergence, §3.7). This
            # is the FIRST disposition — checked before the not-ok/gated/merge
            # logic. Trust-gated on permits('merge', mode) exactly like merge:
            # only at auto-merge does INTEGRATE actually close; at propose/dry-run
            # the would-close intent is recorded under skipped (a human closes).
            # A close-sink fault is recorded under errors (never wedges the tick).
            if vd.get("orphaned"):
                if permitted:
                    try:
                        self._close_sink(pr_ref, repo=self._repo)
                    except Exception as exc:  # noqa: BLE001 — record close fault
                        result.errors.append({"pr_ref": pr_ref,
                                              "reason": str(exc)})
                    else:
                        result.closed_orphaned.append({
                            "pr_ref": pr_ref, "issue_ref": None})
                else:
                    result.skipped.append({
                        "pr_ref": pr_ref,
                        "reason": ("orphaned (driver issue closed) — would close "
                                   "at auto-merge"),
                    })
                continue
            if not vd["ok"]:
                reason = "; ".join(vd.get("reasons") or []) or "verdict not ok"
                result.skipped.append({"pr_ref": pr_ref, "reason": reason})
                continue
            if gated:
                gate = gate_by_ref.get(pr_ref)
                if gate is None:
                    # defensive: an ok verdict with no GateResult is never merged
                    # un-gated once the gate is active.
                    result.skipped.append({
                        "pr_ref": pr_ref,
                        "reason": "no gate result for ok verdict (not merged)",
                    })
                    continue
                if not gate.get("passed"):
                    self._record_gate_failed(result, pr_ref, gate)
                    continue
            if not permitted:
                result.skipped.append({
                    "pr_ref": pr_ref,
                    "reason": (f"merge not permitted at mode={self._mode!r} "
                               f"(auto-merge required)"),
                })
                continue
            guard = self._guardrails_fn(
                {"base": vd["base"], "mergeable": vd["mergeable"]},
                self._default_branch)
            if not guard["ok"]:
                result.skipped.append({
                    "pr_ref": pr_ref,
                    "reason": "; ".join(guard["violations"]),
                })
                continue
            # This branch is only reached when permits('merge', mode) is True
            # (auto-merge mode). The merge-queue-aware sink probes the PR's base
            # branch: a queue -> `gh pr merge --auto` (auto_enabled True ->
            # auto_merge_enabled, a pending success); no queue + already-green ->
            # merged now (auto_enabled False -> merged); no queue + not-yet-
            # mergeable -> native auto-merge (auto_enabled True). A gh fault raises
            # with its stderr text -> errors (diagnosable, never a bare exit-1).
            try:
                outcome = self._merge_sink(pr_ref, repo=self._repo,
                                           base_branch=vd.get("base"))
            except Exception as exc:  # noqa: BLE001 — record any merge fault
                result.errors.append({"pr_ref": pr_ref, "reason": str(exc)})
            else:
                entry = {"pr_ref": outcome["pr_ref"], "url": outcome["url"]}
                if outcome.get("auto_enabled"):
                    result.auto_merge_enabled.append(entry)
                else:
                    result.merged.append(entry)

        return fc.StateResult(
            signal="OK",
            writes={"integration_result": result.to_dict()})

    def _record_gate_failed(self, result, pr_ref, gate):
        """Record a GATE-failed PR under `gate_failed` and post a machine-readable
        marker+JSON comment on its linked issue (when the issue_ref resolved).
        The PR is NEVER merged. A comment-sink fault does not wedge the tick — the
        gate_failed record still lands."""
        issue_ref = gate.get("issue_ref")
        reason = gate.get("reason")
        result.gate_failed.append({
            "pr_ref": pr_ref,
            "issue_ref": issue_ref,
            "reason": reason,
        })
        if issue_ref:
            body = gate_fail_comment_body(
                pr_ref, reason, gate.get("failure_summary", ""))
            try:
                self._comment_sink(issue_ref, body, repo=self._repo)
            except Exception:  # noqa: BLE001 — a comment fault must not wedge tick
                pass


# ==========================================================================
# CLEANUP (slice 2) — v1-thin branch/release hygiene.
# ==========================================================================

# Closed signal set CLEANUP emits: always OK.
CLEANUP_SIGNALS = ["OK"]

# Per-state manifest: reads the integration_result slot, writes nothing, emits OK.
CLEANUP_MANIFEST = fc.StateManifest(reads=["integration_result"], writes=[],
                                    emits=CLEANUP_SIGNALS)


class Cleanup:
    """The CLEANUP state (DESIGN §3.7), v1-thin.

    Reads the `integration_result` slot and emits OK. v1 branch cleanup is
    already done by INTEGRATE's `--delete-branch`, and release/tag is deferred
    (no release config in v1), so CLEANUP is a deterministic pass-through that
    exists for the §2.6 route contract and as the seam for future release/tag
    work. It writes no slot. Reading the slot keeps the manifest's declared
    `reads` honest and gives the future release step its input in place.
    """

    def run(self, ctx):
        ctx.read("integration_result")
        return fc.StateResult(signal="OK")


# ==========================================================================
# RECONCILE-support (cross-tick leftover-PR reconciliation, DESIGN §3.7
# convergence). A deterministic, SCRIPT-TIER class (mirroring Integrate) that
# scheduling's make_reconcile adapter wraps into a route state run BEFORE PULL.
# The state wiring / route edit / durable acted-ledger read are scheduling +
# packaging-config concerns landed in a LATER wave — NOT owned here. This feature
# owns the reconcile LOGIC and the ReconcileResult schema.
#
# It reconciles the PREVIOUS tick's leftover PRs so a merged-but-open issue, or a
# loop PR left CONFLICTING after a sibling merged, never lingers. RECONCILE is
# ADVISORY: its manifest emits ONLY OK; a fault on any single entry is recorded
# under `errors`, never raised — RECONCILE never blocks the tick. Its issue-close
# and comment writes are an OWNED, trust-gated GitHub convergence write, extending
# INTEGRATE's existing issue-comment (gate-fail) and PR-close (orphaned) writes.
# It does NOT file NEW issues (outbound issue FILING remains REPORT/work-intake's).
#
# Everything external — the PR/issue state reads, the issue-close/PR-close/comment
# sinks, and the tier-1 rebase worktree helper — is behind an INJECTABLE seam with
# a production default, so the logic is unit-tested with fakes (no network, no real
# git).
# ==========================================================================

# The versioned ReconcileResult schema (machine-first; bumped on a breaking
# field-set change). Distinct from the feature version. `1.1.0` is ADDITIVE: the
# `deduped` list (same-issue open-PR dedup, C) was added to the `1.0.0` shape.
RECONCILE_RESULT_SCHEMA_VERSION = "1.1.0"


@dataclass(eq=True)
class ReconcileResult:
    """The outcome of a RECONCILE run (DESIGN §3.7 convergence).

    Partitions each reconciled `opened` ledger entry into one of these lists:
      - `closed_issues` — [{issue_ref, pr_ref}] issues CLOSED because their PR
        MERGED but the issue stayed OPEN (the Closes-keyword fallback, branch A).
      - `rebased` — [{pr_ref}] CONFLICTING loop PRs recovered by a TIER-1
        deterministic rebase onto fresh origin/<default> + force-push (branch B).
      - `relanded` — [{pr_ref, issue_ref}] CONFLICTING loop PRs whose rebase hit a
        real textual conflict, so the PR was CLOSED and its issue COMMENTED to
        re-land next tick (TIER-2 fallback, branch B).
      - `deduped` — [{pr_ref, issue_ref, kept_pr_ref}] duplicate loop PRs CLOSED
        because MORE THAN ONE open auto-maintainer PR closed the SAME still-OPEN
        issue (branch C); the highest-numbered PR (`kept_pr_ref`) is kept and each
        lower one is closed. Kept SEPARATE from closed_issues/rebased/relanded.
      - `skipped` — [{ref, reason}] would-act intents recorded at a mode that does
        not permit merge (dry-run/propose): a human acts.
      - `errors` — [{ref, reason}] single-entry faults (never raised; RECONCILE is
        advisory and never blocks the tick).

    Idempotent: a merged-PR issue-close is REPORTED so scheduling records it in the
    ledger and a later tick never re-comments. `to_dict`/`from_dict` give the
    machine-first, versioned representation for the `reconcile_result` slot.
    """

    closed_issues: List[dict] = field(default_factory=list)
    rebased: List[dict] = field(default_factory=list)
    relanded: List[dict] = field(default_factory=list)
    deduped: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": RECONCILE_RESULT_SCHEMA_VERSION,
            "closed_issues": [dict(e) for e in self.closed_issues],
            "rebased": [dict(e) for e in self.rebased],
            "relanded": [dict(e) for e in self.relanded],
            "deduped": [dict(e) for e in self.deduped],
            "skipped": [dict(e) for e in self.skipped],
            "errors": [dict(e) for e in self.errors],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            closed_issues=[dict(e) for e in d.get("closed_issues", [])],
            rebased=[dict(e) for e in d.get("rebased", [])],
            relanded=[dict(e) for e in d.get("relanded", [])],
            deduped=[dict(e) for e in d.get("deduped", [])],
            skipped=[dict(e) for e in d.get("skipped", [])],
            errors=[dict(e) for e in d.get("errors", [])],
        )


# The fsm-contracts slot descriptor. `reconcile_result` is an object slot (a
# single ReconcileResult dict); the slot version tracks the schema version.
RECONCILE_RESULT_SLOT = {
    "name": "reconcile_result",
    "schema": {"type": "object"},
    "version": RECONCILE_RESULT_SCHEMA_VERSION,
}

# Closed signal set RECONCILE emits: always OK (advisory — it never blocks the
# tick; a single-entry fault is recorded under `errors`, never signalled).
RECONCILE_SIGNALS = ["OK"]

# Per-state manifest (bounded-scope): reads the injected `acted_ledger` slot (the
# durable opened-entries scheduling seeds), writes the `reconcile_result` slot,
# emits OK.
RECONCILE_MANIFEST = fc.StateManifest(reads=["acted_ledger"],
                                      writes=["reconcile_result"],
                                      emits=RECONCILE_SIGNALS)


def gh_pr_state_source(pr_ref, repo=None, runner=subprocess.run):
    """Production PR-state read: shell `gh pr view <n> --json state,mergedAt,
    mergeable` and return `{state, merged, mergeable}` — `merged` is True when the
    PR has a `mergedAt` timestamp; `mergeable` is gh's MERGEABLE|CONFLICTING|
    UNKNOWN string. Injectable `runner` for deterministic tests (no network),
    mirroring the sibling gh resolvers/sinks."""
    number = pr_ref.split("#")[-1]
    cmd = ["gh", "pr", "view", number, "--json", "state,mergedAt,mergeable"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    data = json.loads(out.stdout or "{}")
    return {
        "state": data.get("state"),
        "merged": data.get("mergedAt") is not None,
        "mergeable": data.get("mergeable"),
    }


def gh_issue_state_source(issue_ref, repo=None, runner=subprocess.run):
    """Production issue-state read: shell `gh issue view <n> --json state` and
    return `{state}` ('OPEN'/'CLOSED'). Injectable `runner` for deterministic
    tests (no network)."""
    number = issue_ref.split("#")[-1]
    cmd = ["gh", "issue", "view", number, "--json", "state", "-q", ".state"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return {"state": (out.stdout or "").strip()}


def gh_issue_close_sink(issue_ref, repo=None, comment=None,
                        runner=subprocess.run):
    """Production issue-close sink: shell `gh issue close <n> --comment <comment>`
    (and `--repo` when given) to CLOSE the source issue of a MERGED PR whose
    Closes-keyword did not fire. `gh` carries its own auth; check=True so a failed
    close is loud and locatable at the close boundary. Injectable `runner` for
    deterministic tests (no network), mirroring gh_pr_close_sink /
    gh_issue_comment_sink. Returns None."""
    number = issue_ref.split("#")[-1]
    cmd = ["gh", "issue", "close", number]
    if comment:
        cmd += ["--comment", comment]
    if repo:
        cmd += ["--repo", repo]
    runner(cmd, capture_output=True, text=True, check=True)


def gh_open_pr_closing_issue_source(repo=None, runner=subprocess.run):
    """Production same-issue-dedup source (C): shell `gh pr list --label
    auto-maintainer --state open --json number,url,closingIssuesReferences` and
    return the LIVE open loop-PR set as `[{pr_ref, url, issue_ref}]`, each PR
    mapped to its FIRST closing-issue ref (`owner/repo#number` when `repo` is
    given, else `#number`). A PR that closes NO issue is EXCLUDED (it cannot be a
    same-issue duplicate). Injectable `runner` for deterministic tests (no
    network), mirroring the sibling gh sources."""
    cmd = ["gh", "pr", "list", "--label", LOOP_PR_LABEL, "--state", "open",
           "--json", "number,url,closingIssuesReferences"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    prs = json.loads(out.stdout or "[]")
    entries = []
    for pr in prs:
        refs = pr.get("closingIssuesReferences") or []
        if not refs:
            continue
        issue_number = refs[0].get("number")
        if issue_number is None:
            continue
        issue_ref = f"{repo}#{issue_number}" if repo else f"#{issue_number}"
        entries.append({
            "pr_ref": _derive_pr_ref(pr.get("url", ""), pr["number"]),
            "url": pr.get("url", ""),
            "issue_ref": issue_ref,
        })
    return entries


def reconcile_dedup_comment(closed_pr_ref, kept_pr_ref):
    """The machine+human comment the (C) same-issue dedup posts on the source
    issue, naming the KEPT PR as the superseding same-issue re-land so the close
    is attributable. Pure."""
    return (f"auto-maintainer: loop PR {closed_pr_ref} is a duplicate open PR "
            f"for this issue; closing it — {kept_pr_ref} supersedes it as the "
            f"same-issue re-land.")


def reconcile_issue_close_comment(pr_ref):
    """The machine+human comment the merged-PR issue-close fallback posts, naming
    the merged PR so the close is attributable. Pure."""
    return (f"Closed by auto-maintainer: the linked PR {pr_ref} has MERGED; "
            f"closing this issue as resolved (deterministic fallback for a "
            f"Closes-keyword that did not fire).")


def reconcile_reland_comment(pr_ref):
    """The comment the TIER-2 re-land fallback posts on the source issue: the
    CONFLICTING loop PR could not be rebased cleanly, so it was closed and the
    issue is re-opened for a fresh implementer run next tick. Pure."""
    return (f"auto-maintainer: loop PR {pr_ref} could not be rebased cleanly "
            f"(a real textual conflict needs an implementer); it was closed so "
            f"this issue re-lands on a later tick.")


# The fixed disposable worktree path the tier-1 rebase uses (mirrors GATE's fixed
# path; a crashed prior tick can leave it behind, so the helper clears it first).
_RECONCILE_WORKTREE_DIR = os.path.join("/tmp", "am-reconcile-integration")


def _gh_pr_head_ref(pr_ref, repo=None, runner=subprocess.run):
    """The PR's head branch name (`gh pr view <n> --json headRefName`), needed to
    force-push a rebased branch back onto the PR. None when unresolvable."""
    number = pr_ref.split("#")[-1]
    cmd = ["gh", "pr", "view", number, "--json", "headRefName",
           "-q", ".headRefName"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return (out.stdout or "").strip() or None


def reconcile_rebase_worktree(pr_ref, default_branch, repo=None,
                              runner=subprocess.run, worktree_dir=None):
    """Production TIER-1 rebase helper (DESIGN §3.7 convergence): in a DISPOSABLE
    integration worktree, fetch the CONFLICTING loop PR's head, rebase it onto
    fresh `origin/<default_branch>`, and — on a CLEAN rebase — force-push the
    rebased branch back onto the PR so it is mergeable again next tick. Returns
    `{rebased: bool, summary}`: `rebased=True` on a clean rebase + force-push (the
    PR re-enters VERIFY/GATE/INTEGRATE with NO implementer run); `rebased=False`
    on a real textual conflict (the caller falls back to TIER-2 re-land). A setup
    or push fault RAISES so the caller records it under `errors` (never a false
    tier-2 close on an unrelated fault). The subprocess `runner` is INJECTABLE so
    the whole ladder is unit-tested with a fake (no real git)."""
    number = _pr_number(pr_ref)
    worktree = worktree_dir or _RECONCILE_WORKTREE_DIR

    # Best-effort clear a stale leftover from a crashed prior tick (ignore rc).
    runner(["git", "worktree", "remove", "--force", worktree],
           capture_output=True, text=True)
    runner(["git", "worktree", "prune"], capture_output=True, text=True)

    fetch_base = runner(["git", "fetch", "origin", default_branch],
                        capture_output=True, text=True)
    if getattr(fetch_base, "returncode", 0) != 0:
        raise RuntimeError((getattr(fetch_base, "stderr", "") or "").strip()
                           or "git fetch origin <default> failed")
    add = runner(["git", "worktree", "add", "--detach", worktree,
                  f"origin/{default_branch}"], capture_output=True, text=True)
    if getattr(add, "returncode", 0) != 0:
        raise RuntimeError((getattr(add, "stderr", "") or "").strip()
                           or "git worktree add failed")
    try:
        fetch = runner(["git", "-C", worktree, "fetch", "origin",
                        f"pull/{number}/head"], capture_output=True, text=True)
        if getattr(fetch, "returncode", 0) != 0:
            raise RuntimeError((getattr(fetch, "stderr", "") or "").strip()
                               or "git fetch pr head failed")
        runner(["git", "-C", worktree, "checkout", "-B",
                f"reconcile-{number}", "FETCH_HEAD"],
               capture_output=True, text=True)
        rebase = runner(["git", "-C", worktree, "rebase",
                         f"origin/{default_branch}"],
                        capture_output=True, text=True)
        if getattr(rebase, "returncode", 0) != 0:
            runner(["git", "-C", worktree, "rebase", "--abort"],
                   capture_output=True, text=True)
            return {"rebased": False,
                    "summary": _bounded_tail(
                        getattr(rebase, "stdout", "")
                        or getattr(rebase, "stderr", ""))}
        head_ref = _gh_pr_head_ref(pr_ref, repo=repo, runner=runner)
        if not head_ref:
            raise RuntimeError("could not resolve PR head branch for force-push")
        push = runner(["git", "-C", worktree, "push", "--force", "origin",
                       f"HEAD:{head_ref}"], capture_output=True, text=True)
        if getattr(push, "returncode", 0) != 0:
            raise RuntimeError((getattr(push, "stderr", "") or "").strip()
                               or "git push --force failed")
        return {"rebased": True, "summary": ""}
    finally:
        runner(["git", "worktree", "remove", "--force", worktree],
               capture_output=True, text=True)


class Reconcile:
    """The RECONCILE state (DESIGN §3.7 convergence) — an ADVISORY, deterministic
    reconciler of the PREVIOUS tick's leftover loop PRs (mirrors Integrate).

    Reads the injected `acted_ledger` slot (the durable `opened` entries scheduling
    seeds: `{work_order_id, pr_ref, issue_ref, repo}`) and, for each entry, reads
    the PR's live state via the injectable `pr_state_source`:

      - (A) MERGED-PR issue-close fallback — a MERGED PR whose source issue is
        still OPEN (`issue_state_source`) has its issue CLOSED via
        `issue_close_sink` with a comment naming the merged PR; recorded under
        `closed_issues`. NEVER touches a human-closed issue: only a
        MERGED-PR-with-still-OPEN issue is closed.
      - (B) conflict-recovery ladder — an OPEN + CONFLICTING PR (a sibling merged
        and invalidated it) is recovered by TIER-1 `worktree_helper` (rebase onto
        fresh origin/<default> + force-push); a clean rebase is recorded under
        `rebased`. If the rebase hits a real textual conflict (worktree_helper
        returns rebased=False), TIER-2 CLOSES the PR (`pr_close_sink`) and COMMENTS
        the source issue (`comment_sink`) so the acted-ledger re-entry gate re-lands
        it next tick; recorded under `relanded`.

    Trust-gated exactly like INTEGRATE: the mutating acts (issue-close, force-push,
    PR-close) run ONLY at permits('merge', mode) (auto-merge); at dry-run/propose
    the would-act intent is recorded under `skipped` and a human acts. A
    single-entry fault is recorded under `errors` and NEVER raised — RECONCILE is
    advisory and never blocks the tick. Writes `reconcile_result`, emits OK.
    """

    def __init__(self, mode, pr_state_source=gh_pr_state_source,
                 issue_state_source=gh_issue_state_source,
                 issue_close_sink=gh_issue_close_sink,
                 pr_close_sink=gh_pr_close_sink,
                 comment_sink=gh_issue_comment_sink,
                 worktree_helper=reconcile_rebase_worktree,
                 open_pr_closing_issue_source=gh_open_pr_closing_issue_source,
                 permits_fn=sg.permits, default_branch=None, repo=None):
        self._mode = mode
        self._pr_state_source = pr_state_source
        self._issue_state_source = issue_state_source
        self._issue_close_sink = issue_close_sink
        self._pr_close_sink = pr_close_sink
        self._comment_sink = comment_sink
        self._worktree_helper = worktree_helper
        self._open_pr_closing_issue_source = open_pr_closing_issue_source
        self._permits_fn = permits_fn
        self._default_branch = default_branch
        self._repo = repo

    def _resolve_default_branch(self):
        if self._default_branch is not None:
            return self._default_branch
        return gh_default_branch_source(self._repo)

    def run(self, ctx):
        ledger = ctx.read("acted_ledger") or []
        permitted = self._permits_fn("merge", self._mode)
        result = ReconcileResult()

        for entry in ledger:
            pr_ref = entry.get("pr_ref")
            issue_ref = entry.get("issue_ref")
            repo = entry.get("repo") or self._repo
            try:
                self._reconcile_one(result, pr_ref, issue_ref, repo, permitted)
            except Exception as exc:  # noqa: BLE001 — advisory: record, never raise
                result.errors.append({"ref": pr_ref or issue_ref,
                                      "reason": str(exc)})

        # (C) Same-issue open-PR dedup — the deterministic supersede backstop, run
        # AFTER (A)/(B). Sources the LIVE open loop-PR set (NOT the acted_ledger,
        # which would miss a re-dispatch's orphaned first-run PR).
        self._dedup_same_issue(result, permitted)

        return fc.StateResult(
            signal="OK",
            writes={"reconcile_result": result.to_dict()})

    def _dedup_same_issue(self, result, permitted):
        """(C) Close duplicate open loop PRs that close the SAME still-OPEN issue.

        Groups the live open loop-PR set by closing-issue ref; for each group with
        MORE THAN ONE PR whose issue is still OPEN, KEEPS the highest-numbered PR
        (the newest re-land) and CLOSES every other via the EXISTING PR-close sink,
        commenting the issue with the kept PR as the superseding same-issue re-land.
        NEVER closes the sole PR for an issue, NEVER touches a group whose issue is
        CLOSED, NEVER crosses issues. Trust-gated: at dry-run/propose the
        would-close intent is recorded under `skipped`. Each close is wrapped in a
        per-entry try/except that records faults under `errors` and never raises."""
        entries = self._open_pr_closing_issue_source(repo=self._repo)
        groups = {}
        for e in entries:
            groups.setdefault(e["issue_ref"], []).append(e)

        for issue_ref, group in groups.items():
            if len(group) <= 1:
                continue  # sole PR for the issue — never touched.
            issue_state = self._issue_state_source(issue_ref, repo=self._repo)
            if (issue_state or {}).get("state") != "OPEN":
                continue  # closed-issue group — never touched (not (C)'s concern).
            ordered = sorted(group, key=lambda e: _pr_number(e["pr_ref"]),
                             reverse=True)
            kept = ordered[0]
            for dup in ordered[1:]:
                dup_ref = dup["pr_ref"]
                if not permitted:
                    result.skipped.append({
                        "ref": dup_ref,
                        "reason": ("duplicate same-issue loop PR — would close "
                                   "at auto-merge"),
                    })
                    continue
                try:
                    self._pr_close_sink(dup_ref, repo=self._repo)
                    self._comment_sink(
                        issue_ref,
                        reconcile_dedup_comment(dup_ref, kept["pr_ref"]),
                        repo=self._repo)
                    result.deduped.append({
                        "pr_ref": dup_ref,
                        "issue_ref": issue_ref,
                        "kept_pr_ref": kept["pr_ref"],
                    })
                except Exception as exc:  # noqa: BLE001 — record, never raise
                    result.errors.append({"ref": dup_ref, "reason": str(exc)})

    def _reconcile_one(self, result, pr_ref, issue_ref, repo, permitted):
        """Reconcile one `opened` ledger entry into `result` (branch A or B)."""
        pr_state = self._pr_state_source(pr_ref, repo=repo)

        # (A) MERGED-PR issue-close fallback: close a still-OPEN source issue of a
        # MERGED PR (never a human-closed one). Idempotent — reported so a later
        # tick never re-comments.
        if pr_state.get("merged"):
            if not issue_ref:
                return
            issue_state = self._issue_state_source(issue_ref, repo=repo)
            if (issue_state or {}).get("state") != "OPEN":
                return  # already closed / human-closed — never touch it.
            if not permitted:
                result.skipped.append({
                    "ref": issue_ref,
                    "reason": ("merged PR with open issue — would close at "
                               "auto-merge"),
                })
                return
            self._issue_close_sink(
                issue_ref, repo=repo,
                comment=reconcile_issue_close_comment(pr_ref))
            result.closed_issues.append({"issue_ref": issue_ref,
                                         "pr_ref": pr_ref})
            return

        # (B) conflict-recovery ladder: an OPEN + CONFLICTING PR (a sibling merged
        # and invalidated it).
        if (pr_state.get("state") == "OPEN"
                and pr_state.get("mergeable") == "CONFLICTING"):
            if not permitted:
                result.skipped.append({
                    "ref": pr_ref,
                    "reason": ("conflicting loop PR — would recover at "
                               "auto-merge"),
                })
                return
            outcome = self._worktree_helper(
                pr_ref, self._resolve_default_branch(), repo=repo)
            if outcome.get("rebased"):
                # TIER-1: clean rebase + force-push — the PR is mergeable again.
                result.rebased.append({"pr_ref": pr_ref})
                return
            # TIER-2: a real textual conflict — close the PR and comment the issue
            # so the acted-ledger re-entry gate re-lands it next tick.
            self._pr_close_sink(pr_ref, repo=repo)
            if issue_ref:
                self._comment_sink(issue_ref, reconcile_reland_comment(pr_ref),
                                   repo=repo)
            result.relanded.append({"pr_ref": pr_ref, "issue_ref": issue_ref})
        # else: an OPEN mergeable PR, or any other state — left alone (nothing to
        # reconcile this tick).
