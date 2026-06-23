#!/usr/bin/env python3
"""verify-integrate — slice 1: the read-only VERIFY gate.

The act-side CLOSE of the loop (DESIGN §3.7): after IMPLEMENT opens a PR, VERIFY
gates it on CI + mergeability + base. VERIFY is READ-ONLY and always safe — it
lists the loop's open PRs and derives one Verdict per PR; it never merges,
closes, or writes to GitHub.

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
  - VERIFY_MANIFEST   — the VERIFY state's {reads, writes, emits} manifest.
  - VERIFY_SIGNALS    — the closed signal set VERIFY may emit (OK | EMPTY).
  - gh_open_pr_source()       — the production source: shells the gh CLI.
  - gh_default_branch_source() — the production default-branch resolver.
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

Public surface (slice 2 — INTEGRATE + CLEANUP):
  - INTEGRATION_RESULT_SCHEMA_VERSION + IntegrationResult — the typed,
    versioned {merged, skipped, errors} integration-result schema.
  - INTEGRATION_RESULT_SLOT — the fsm-contracts slot registration descriptor.
  - INTEGRATE_MANIFEST / INTEGRATE_SIGNALS — INTEGRATE's manifest (reads ONLY
    verdicts — thin merge, no review coupling) + signal set.
  - gh_pr_merge_sink(pr_ref, repo) — the production merge sink: shells
    `gh pr merge <pr> --merge --delete-branch` (the determinism seam).
  - Integrate — the INTEGRATE state; merges only at auto-merge, guardrail-gated.
  - CLEANUP_MANIFEST / CLEANUP_SIGNALS — CLEANUP's manifest + signal set.
  - Cleanup — the CLEANUP state (v1-thin pass-through; run -> OK).

Version: 0.5.0
Owner: changyu87
Deprecation criterion: Superseded when the loop adopts a non-git VCS backend,
  or a model-backed verify/integrate policy replaces the deterministic gh-based
  gates, or when the Verdict / IntegrationResult schemas reach a breaking major
  version. See docs/spec.md.
"""

import json
import subprocess
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
VERDICT_SCHEMA_VERSION = "1.0.0"


@dataclass(eq=True)
class Verdict:
    """One verdict per open loop PR (DESIGN §3.7).

    `ok` is the conservative AND: CI passing AND mergeable AND base == default
    branch. `ci_state` is one of passing|pending|failing|unknown. `reasons`
    explains a non-ok verdict (empty when ok). `to_dict`/`from_dict` give a
    machine-first, versioned representation for the `verdicts` blackboard slot.
    """

    pr_ref: str
    url: str
    ok: bool
    ci_state: str
    mergeable: bool
    base: str
    reasons: List[str] = field(default_factory=list)

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
        )


# The fsm-contracts slot descriptor. `verdicts` is an array slot (a list of
# Verdict dicts); the slot version tracks the schema version. Mirrors
# work-intake's WORK_ORDERS_SLOT shape (name/schema/version).
VERDICTS_SLOT = {
    "name": "verdicts",
    "schema": {"type": "array"},
    "version": VERDICT_SCHEMA_VERSION,
}

# Closed signal set VERIFY emits: OK when any open PRs were found, else EMPTY.
VERIFY_SIGNALS = ["OK", "EMPTY"]

# Per-state manifest (bounded-scope contract): reads NOTHING (the open-PR set is
# sourced live from gh, not a slot — the cross-tick model), writes the verdicts
# slot, emits OK | EMPTY.
VERIFY_MANIFEST = fc.StateManifest(reads=[], writes=["verdicts"],
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


def gh_open_pr_source(repo=None, label=LOOP_PR_LABEL, runner=subprocess.run):
    """Production open-PR source: shell the deterministic `gh` CLI for the loop's
    OPEN PRs (filtered by the `auto-maintainer` label) and return the parsed gh
    JSON list. `gh` carries its own auth. When `repo` is given it is passed via
    `--repo`; otherwise gh resolves the repo from the project default.

    The subprocess `runner` is INJECTABLE (defaulting to subprocess.run) so
    tests assemble/parse the command with a fake — no network, the failure
    locatable to the fetch boundary (mirror of work_intake.gh_issue_source).
    """
    cmd = ["gh", "pr", "list", "--label", label, "--state", "open",
           "--json", _GH_JSON_FIELDS]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def gh_default_branch_source(repo=None, runner=subprocess.run):
    """Production default-branch resolver: shell `gh repo view` for the repo's
    default branch name. Injectable `runner` for deterministic tests."""
    cmd = ["gh", "repo", "view", "--json", "defaultBranchRef",
           "-q", ".defaultBranchRef.name"]
    if repo:
        cmd += ["--repo", repo]
    out = runner(cmd, capture_output=True, text=True, check=True)
    return out.stdout.strip()


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


def derive_verdict(pr_dict, default_branch):
    """Pure derivation of a Verdict from a gh-shaped open-PR dict.

    `ci_state` from the status-check rollup, `mergeable` from gh's `mergeable`
    field (MERGEABLE -> True; CONFLICTING/UNKNOWN/anything else -> False,
    conservative), `base` from `baseRefName`. `ok` is the conservative AND:
    ci_state == "passing" AND mergeable AND base == default_branch. `reasons`
    lists each failing condition for a non-ok verdict. Deterministic; no I/O.
    """
    number = pr_dict["number"]
    url = pr_dict.get("url", "")
    base = pr_dict.get("baseRefName", "")
    ci_state = _ci_state(pr_dict.get("statusCheckRollup") or [])
    mergeable = (pr_dict.get("mergeable") or "").upper() == "MERGEABLE"

    reasons = []
    if ci_state != "passing":
        reasons.append(f"CI not passing (ci_state={ci_state})")
    if not mergeable:
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


class Verify:
    """The VERIFY state. Lists the loop's open PRs via the injectable source,
    derives one Verdict per PR, writes the `verdicts` slot, and emits OK if any
    open PRs were found else EMPTY.

    READ-ONLY: VERIFY never merges, closes, or writes to GitHub. The only edges
    it touches are the read-only PR source and the default-branch resolver, both
    injectable (the determinism seam). The default branch is resolved once per
    run via `default_branch_source` unless `default_branch` is supplied directly.
    """

    def __init__(self, source=gh_open_pr_source, repo=None,
                 default_branch=None,
                 default_branch_source=gh_default_branch_source):
        self._source = source
        self._repo = repo
        self._default_branch = default_branch
        self._default_branch_source = default_branch_source

    def _resolve_default_branch(self):
        if self._default_branch is not None:
            return self._default_branch
        return self._default_branch_source(self._repo)

    def run(self, ctx):  # noqa: ARG002 — ctx is the fsm-contracts TickContext
        prs = self._source(repo=self._repo, label=LOOP_PR_LABEL)
        default_branch = self._resolve_default_branch()
        verdicts = [derive_verdict(pr, default_branch).to_dict() for pr in prs]
        signal = "OK" if verdicts else "EMPTY"
        return fc.StateResult(signal=signal, writes={"verdicts": verdicts})


# ==========================================================================
# INTEGRATE (slice 2) — the single highest-stakes act-side state.
# ==========================================================================

# The versioned IntegrationResult schema (machine-first; bumped on a breaking
# change to the field set). Distinct from the feature version.
INTEGRATION_RESULT_SCHEMA_VERSION = "1.0.0"


@dataclass(eq=True)
class IntegrationResult:
    """The outcome of an INTEGRATE run (DESIGN §3.7).

    Partitions each considered PR into exactly one of three lists:
      - `merged`  — [{pr_ref, url}] the PRs the merge sink merged.
      - `skipped` — [{pr_ref, reason}] non-ok verdicts, not-permitted modes
        (the dry-run/propose NO-OP), and guardrail violations.
      - `errors`  — [{pr_ref, reason}] PRs whose merge sink raised.

    Idempotent at the loop level: a merged PR leaves the open set, so a re-run
    never double-merges. `to_dict`/`from_dict` give the machine-first, versioned
    representation for the `integration_result` blackboard slot.
    """

    merged: List[dict] = field(default_factory=list)
    skipped: List[dict] = field(default_factory=list)
    errors: List[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            "schema_version": INTEGRATION_RESULT_SCHEMA_VERSION,
            "merged": [dict(e) for e in self.merged],
            "skipped": [dict(e) for e in self.skipped],
            "errors": [dict(e) for e in self.errors],
        }

    @classmethod
    def from_dict(cls, d):
        return cls(
            merged=[dict(e) for e in d.get("merged", [])],
            skipped=[dict(e) for e in d.get("skipped", [])],
            errors=[dict(e) for e in d.get("errors", [])],
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

# Per-state manifest (bounded-scope): THIN merge — reads ONLY the verdicts slot
# (the review-approval coupling is gone; REVIEW is advisory), writes the
# integration_result slot, emits OK.
INTEGRATE_MANIFEST = fc.StateManifest(reads=["verdicts"],
                                      writes=["integration_result"],
                                      emits=INTEGRATE_SIGNALS)


def gh_pr_merge_sink(pr_ref, repo=None, runner=subprocess.run):
    """Production merge sink: shell `gh pr merge <number> --merge
    --delete-branch` (and `--repo` when given) to merge the PR and delete its
    head branch in one deterministic CLI call. Returns a {pr_ref, url} merged
    entry. `gh` carries its own auth.

    The PR number is parsed off the `owner/repo#number` ref (gh accepts the bare
    number when `--repo` scopes it). The subprocess `runner` is INJECTABLE
    (defaulting to subprocess.run) so tests assemble the command with a fake —
    no network, the failure locatable to the merge boundary (mirror of
    gh_open_pr_source).
    """
    number = pr_ref.split("#")[-1]
    cmd = ["gh", "pr", "merge", number, "--merge", "--delete-branch"]
    if repo:
        cmd += ["--repo", repo]
    runner(cmd, capture_output=True, text=True, check=True)
    return {"pr_ref": pr_ref, "url": ""}


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
                 guardrails_fn=sg.merge_guardrails):
        self._mode = mode
        self._merge_sink = merge_sink
        self._repo = repo
        self._default_branch = default_branch
        self._permits_fn = permits_fn
        self._guardrails_fn = guardrails_fn

    def run(self, ctx):
        verdicts = ctx.read("verdicts")
        permitted = self._permits_fn("merge", self._mode)
        result = IntegrationResult()

        for vd in verdicts:
            pr_ref = vd["pr_ref"]
            if not vd["ok"]:
                reason = "; ".join(vd.get("reasons") or []) or "verdict not ok"
                result.skipped.append({"pr_ref": pr_ref, "reason": reason})
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
            try:
                merged = self._merge_sink(pr_ref, repo=self._repo)
            except Exception as exc:  # noqa: BLE001 — record any merge fault
                result.errors.append({"pr_ref": pr_ref, "reason": str(exc)})
            else:
                result.merged.append(merged)

        return fc.StateResult(
            signal="OK",
            writes={"integration_result": result.to_dict()})


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
