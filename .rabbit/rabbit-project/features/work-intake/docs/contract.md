---
feature: work-intake
version: 0.11.0
owner: changyu87
deprecation_criterion: Superseded when the tracker-read model changes incompatibly (e.g. multi-tracker support, or the WorkItem schema reaches a breaking major version). See spec.md / feature.json.
---

# work-intake — Contract

```json
{
  "provides": {
    "files": [
      "WorkItem slot schema (versioned, machine-first)",
      "PULL state: run(TickContext) -> StateResult, writes work_items, emits OK|EMPTY; accepts an optional issue_filter (Pull(issue_filter=...)) narrowing pulled issues by DNF labels (server-side per-AND-group gh --label union) + a post-fetch title_pattern regex; default (empty labels + null pattern) pulls all open issues (non-breaking)",
      "gh_issue_source(repo=None, runner=..., issue_filter=None) -> [WorkItem]: injectable production issue source; with a non-empty labels DNF runs one gh issue list --label query per AND-group and unions by number, applies title_pattern post-fetch, drops any issue carrying a listed exclude_labels label post-fetch (NEGATIVE term; empty = no-op), then enriches with comments",
      "WorkOrder slot schema (versioned, machine-first; decision-carrying; carries target_feature: the TRIAGE-stamped blast-radius feature key(s), #258)",
      "target_features_for(labels, body, title) -> [str]: pure detection of a WorkOrder's blast-radius target feature(s) from authoritative signals (prefixed labels, a Component:/Feature: body line, a conventional title prefix; sorted, empty when none provable) — TRIAGE stamps the result so PRIORITIZE reads an authoritative field (#258)",
      "TRIAGE state: run(TickContext) -> StateResult, reads work_items, writes work_orders + cross_cutting_risk, emits OK|EMPTY (deterministic validity gate; stamps each order's target_feature)",
      "CrossCuttingRisk slot schema (versioned, machine-first; {risk, features, reason}) + CROSS_CUTTING_RISK_SLOT descriptor; TRIAGE ALWAYS writes cross_cutting_risk (default no-risk) for VERIFY (DESIGN §3.5.9)",
      "normalize_cross_cutting_risk(annotation) -> CrossCuttingRisk: pure normalizer/validator (risk=true only on >=2 distinct features + non-empty reason; rejects malformed input)",
      "DiscoveredIssue slot schema (versioned, machine-first; the outbound discovery shape)",
      "ReportResult schema (machine-first; the {filed, skipped_existing, errors} filing-batch outcome)",
      "file_discoveries(discoveries, sink, known_dedup_keys, known_open, apply_labels=None) -> ReportResult: pure REPORT orchestrator (out-of-band; scheduling.run_tick flushes through it). Forwards apply_labels (the active issue_filter PULL-visibility labels) to the sink ONLY for project-target discoveries; maintainer-self filings get []",
      "gh_issue_file_sink(discovery, repo=None, apply_labels=None, runner=...) -> {tracker_ref, url}: the production GitHub filing sink (injectable runner). Stamps filed-by:autonomous-maintainer PLUS each label in apply_labels (the issue_filter PULL-visibility labels so a later PULL re-pulls the loop's own filing), ensuring each label exists first via idempotent gh label create; apply_labels None/[] = provenance label only (unchanged)",
      "is_loop_filed(item) -> bool: the §3.11.5 loopback/provenance recognizer (LOOP_FILED_LABEL or am-dedup body marker); PULL applies it as an EXCLUSION only under the work_own_filings=False opt-out (default True includes loop-filed items)",
      "REJECTED_LABEL ('auto-maintainer-rejected') + REJECT_MARKER ('<!-- auto-maintainer:rejected -->'): the fixed reject-disposition label + comment-marker literals (work-intake owns tracker labels; safety-governance/packaging-config reference REJECTED_LABEL as the issue_filter exclude_labels negative term)",
      "reject_dispositions(work_orders) -> [{work_item_id, issue_ref, reason}]: pure selector of the disposition payload for every decision=rejected order (accepts WorkOrder objects or their machine-first dicts); accepted orders dropped",
      "gh_issue_reject_sink(issue_ref, repo=None, reason='', label=REJECTED_LABEL, runner=...) -> None: injectable tracker sink enacting a SEMANTIC reject at TRIAGE-time — ensures the label (idempotent gh label create), posts ONE gh issue comment carrying the reason behind REJECT_MARKER, applies the label via gh issue edit --add-label; NEVER closes; idempotent no-op when the item already carries REJECTED_LABEL (reads gh issue view --json labels first). Enactment + triage_memory recording are scheduling's",
      "auto-maintainer-triager (shipped subagent) stamps an authoritative target_feature on each accepted order (real analysis, #258; target_features_for is fallback-only) and EMITS rejected orders (decision=rejected + reason + work_item_id/issue ref) so the deterministic reject disposition can be enacted"
    ],
    "scripts": [],
    "skills": [],
    "agents": [
      "ship/agents/auto-maintainer-triager (the read-only TRIAGE judge subagent; protocol-free, prompt-contracted; collected into the plugin's agents/ by the build's ship/ pass)"
    ]
  },
  "reads": {"files": [], "external": []},
  "invokes": {
    "scripts": [],
    "agents": [],
    "external": ["gh issue list --state open --json number,title,body,url,state,labels,author,createdAt,updatedAt [--repo <repo>] [--label <l> …] (one such query per issue_filter AND-group; results unioned + deduped by number)", "gh issue view <number> --json comments [--repo <repo>]", "gh issue create --title <title> --body <body> --label filed-by:autonomous-maintainer [--label <apply_label> …] [--repo <repo>] (REPORT filing sink; apply_labels = the issue_filter PULL-visibility labels stamped on project-target filings)", "gh label create <filed-by:autonomous-maintainer | each apply_label | auto-maintainer-rejected> --description <desc> [--repo <repo>] (idempotent label ensure for the provenance label, each apply_label, AND the reject label; non-zero 'already exists' tolerated)", "gh issue view <issue_ref> --json labels [--repo <repo>] (reject sink idempotency probe)", "gh issue comment <issue_ref> --body <REJECT_MARKER + reason> [--repo <repo>] (reject disposition; NEVER gh issue close)", "gh issue edit <issue_ref> --add-label auto-maintainer-rejected [--repo <repo>] (reject disposition label apply)"]
  },
  "never": [
    "performs dedup-vs-closed / 1-level decompose / dependency ordering / WHAT-generation seam (slice 3+)",
    "implements or executes maintainer work",
    "edits files in other features",
    "calls the live gh CLI from tests (the issue source is injectable; tests use a stub)",
    "calls the wall clock implicitly in TRIAGE (the staleness reference time is injectable; tests pin it)"
  ]
}
```
