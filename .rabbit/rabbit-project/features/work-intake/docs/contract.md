---
feature: work-intake
version: 0.9.0
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
      "gh_issue_source(repo=None, runner=..., issue_filter=None) -> [WorkItem]: injectable production issue source; with a non-empty labels DNF runs one gh issue list --label query per AND-group and unions by number, applies title_pattern post-fetch, then enriches with comments",
      "WorkOrder slot schema (versioned, machine-first; decision-carrying; carries target_feature: the TRIAGE-stamped blast-radius feature key(s), #258)",
      "target_features_for(labels, body, title) -> [str]: pure detection of a WorkOrder's blast-radius target feature(s) from authoritative signals (prefixed labels, a Component:/Feature: body line, a conventional title prefix; sorted, empty when none provable) — TRIAGE stamps the result so PRIORITIZE reads an authoritative field (#258)",
      "TRIAGE state: run(TickContext) -> StateResult, reads work_items, writes work_orders + cross_cutting_risk, emits OK|EMPTY (deterministic validity gate; stamps each order's target_feature)",
      "CrossCuttingRisk slot schema (versioned, machine-first; {risk, features, reason}) + CROSS_CUTTING_RISK_SLOT descriptor; TRIAGE ALWAYS writes cross_cutting_risk (default no-risk) for VERIFY (DESIGN §3.5.9)",
      "normalize_cross_cutting_risk(annotation) -> CrossCuttingRisk: pure normalizer/validator (risk=true only on >=2 distinct features + non-empty reason; rejects malformed input)",
      "DiscoveredIssue slot schema (versioned, machine-first; the outbound discovery shape)",
      "ReportResult schema (machine-first; the {filed, skipped_existing, errors} filing-batch outcome)",
      "file_discoveries(discoveries, sink, known_dedup_keys) -> ReportResult: pure REPORT orchestrator (out-of-band, not a routed state; scheduling.run_tick flushes through it)",
      "gh_issue_file_sink(discovery, repo=None, runner=...) -> {tracker_ref, url}: the production GitHub filing sink (injectable runner)",
      "is_loop_filed(item) -> bool: the §3.11.5 loopback/provenance recognizer (LOOP_FILED_LABEL or am-dedup body marker); PULL applies it as an EXCLUSION only under the work_own_filings=False opt-out (default True includes loop-filed items)"
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
    "external": ["gh issue list --state open --json number,title,body,url,state,labels,author,createdAt,updatedAt [--repo <repo>] [--label <l> …] (one such query per issue_filter AND-group; results unioned + deduped by number)", "gh issue view <number> --json comments [--repo <repo>]", "gh issue create --title <title> --body <body> --label filed-by:autonomous-maintainer [--repo <repo>] (REPORT filing sink)", "gh label create filed-by:autonomous-maintainer --description <desc> [--repo <repo>] (REPORT idempotent label ensure; non-zero 'already exists' tolerated)"]
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
