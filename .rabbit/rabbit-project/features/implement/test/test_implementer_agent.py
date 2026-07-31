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


def test_shipped_implementer_body_guards_discovered_work_against_known_open():
    """v2.4.0 (auto-maintainer-framework#224): `discovered_work` is for NEW
    problems only. The body must instruct the implementer NOT to emit a discovery
    for dependencies it is blocked on (cited in its own `blocked_reason`) or for
    anything already tracked/open, to prevent REPORT filing duplicate issues."""
    body = _body().lower().replace("*", "")
    assert "discovered_work" in body or "discovered work" in body
    assert "blocked_reason" in body, (
        "guard must reference the blocked_reason dependencies an implementer "
        "must not re-file as discoveries")
    assert "new problems only" in body, (
        "body must state discovered_work is for NEW problems only")


def test_shipped_implementer_body_invokes_deterministic_test_gate():
    """FT-A (DESIGN §3.6.3): IMPLEMENT is the deterministic correctness gate. On
    the accept path, after committing and BEFORE opening the PR, the implementer
    MUST invoke the gate script (test_gate.py) which runs the target's run.py and
    records a machine-checkable verdict — never a model self-assertion (#255).
    The body must name the gate script and tie its use to the accept path."""
    body = _body().lower().replace("*", "")
    assert "test_gate.py" in body, (
        "body must instruct the implementer to invoke the gate script")
    # the gate produces the verdict that is embedded; the pass is the SCRIPT's
    # recorded result, not the model's prose.
    assert "test_verdict" in body, (
        "body must instruct embedding the script-produced test_verdict")


def test_shipped_implementer_invokes_gate_at_deployed_plugin_lib_path():
    """Deployment correctness: the shipped subagent runs inside an INSTALLED
    Claude Code plugin, where the gate lives at ${CLAUDE_PLUGIN_ROOT}/lib/. The
    body MUST invoke the gate via the deployed convention
    `${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py` (mirroring scheduling's shipped
    skills), not via a DEV source-tree path."""
    body = _body()
    assert "${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py" in body, (
        "body must invoke the gate at the deployed "
        "${CLAUDE_PLUGIN_ROOT}/lib/test_gate.py path")


def test_shipped_implementer_body_regenerates_committed_build_tree():
    """v2.7.0 (auto-maintainer-framework#354): when a change touches source that a
    repo mirrors into a committed build/distribution tree, the accept path must
    regenerate that committed tree and include it in the SAME PR, so a
    shipped-source change lands drift-free in one PR (green under the build-drift
    guard) instead of forcing a second regen-only PR. The body must instruct the
    implementer to regenerate the committed build tree on the accept path, and
    tie it to the build-drift concern and the same-PR requirement."""
    body = _body().lower().replace("*", "")
    assert "build-drift" in body or "build drift" in body, (
        "body must reference the build-drift concern the regen step addresses")
    assert "regenerate" in body, (
        "body must instruct the implementer to regenerate the committed build "
        "tree")
    # the regenerated tree must land in the SAME PR as the source change
    assert "same pr" in body, (
        "body must require the regenerated tree in the SAME PR as the source "
        "change")


def test_shipped_implementer_body_has_no_source_tree_leak():
    """The shipped body runs from the installed plugin and must NOT reference the
    dev source tree: neither the `rabbit-project` source path nor the `.rabbit`
    workspace dir may appear (those are non-functional in the installed plugin
    and leak the dev layout)."""
    body = _body()
    assert "rabbit-project" not in body, (
        "shipped body must not reference the dev source path 'rabbit-project'")
    assert ".rabbit" not in body, (
        "shipped body must not reference the '.rabbit' workspace dir")


def test_shipped_implementer_body_opened_only_with_passing_verdict():
    """The accept path may report status:opened ONLY when the gate's recorded
    verdict passes; the body must state the verdict is the SCRIPT's result, never
    the model's claim, and that a failing/missing verdict blocks the open."""
    body = _body().lower().replace("*", "")
    # opened is conditioned on the gate passing
    assert "opened" in body
    assert "test_verdict" in body
    # the pass must come from the script, not a model assertion
    assert "script" in body


def test_shipped_implementer_body_supersedes_prior_same_issue_pr():
    """v2.8.0: on the accept path, BEFORE `gh pr create`, the implementer closes
    an EXISTING open `auto-maintainer`-labelled PR that resolves the SAME source
    issue (a prior superseded attempt), so a stale duplicate never lingers to
    conflict or generate un-executable 'close PR X' work (spec: Supersede-on-retry).
    The body must:
      - query open auto-maintainer PRs by closingIssuesReferences,
      - close a prior same-issue PR via `gh pr close`,
      - constrain the close to the SAME issue only (never an unrelated PR),
      - do it BEFORE `gh pr create`, and be a no-op when none exists."""
    body = _body()
    lower = body.lower().replace("*", "")
    # queries open auto-maintainer PRs and their closing-issue references
    assert "closingissuesreferences" in lower, (
        "body must query open auto-maintainer PRs' closingIssuesReferences to "
        "find a prior same-issue attempt")
    # closes the prior PR (never merges)
    assert "gh pr close" in lower, (
        "body must close the prior same-issue PR via `gh pr close`")
    # the supersede concept is named
    assert "supersede" in lower, (
        "body must describe the supersede-on-retry behaviour")
    # constrained to the SAME issue only — never an unrelated PR
    assert "same issue" in lower or "same source issue" in lower, (
        "body must constrain the close to the SAME issue only")


def test_shipped_implementer_frontmatter_version_is_2_11_0():
    """v2.11.0 moves the worktree-setup + PR-open from prompt-tier git/gh prose
    into the deterministic companion script open_pr.py (spec-rules §4), fixing
    the wrong-base STACKED PR bug (#844/#846)."""
    fm = _frontmatter()
    assert fm["version"] == "2.11.0"


def test_shipped_implementer_invokes_open_pr_script_at_deployed_lib_path():
    """v2.11.0: the order-critical git sequence (resolve default, fetch, worktree
    add from origin/<default>, gh pr create --base <default>) is now a
    DETERMINISTIC companion script invoked at the deployed
    ${CLAUDE_PLUGIN_ROOT}/lib/open_pr.py path (mirroring test_gate.py), NOT
    hand-run git/gh prose. The body must invoke the script at that deployed
    path."""
    body = _body()
    assert "${CLAUDE_PLUGIN_ROOT}/lib/open_pr.py" in body, (
        "body must invoke the worktree/PR script at the deployed "
        "${CLAUDE_PLUGIN_ROOT}/lib/open_pr.py path")


def test_shipped_implementer_no_raw_worktree_add_base_prose():
    """v2.11.0: the raw `git worktree add ... origin/<default>` and the raw
    `gh pr create --base <default>` prose is REPLACED by the script invocation —
    the deterministic base/start-point resolution no longer lives as model-filled
    git/gh prose the model assembles per-invocation (spec-rules §4). The body
    must not carry the raw `git worktree add` command nor a hand-run
    `gh pr create --base` command line."""
    body = _body()
    assert "git worktree add" not in body, (
        "raw `git worktree add` prose must be replaced by the open_pr.py "
        "script invocation")
    assert "gh pr create --base" not in body, (
        "raw `gh pr create --base` prose must be replaced by the open_pr.py "
        "script invocation")


def test_shipped_implementer_body_never_reports_planned():
    """v2.9.0: `planned` is the DRY-RUN adapter's status, NOT the agent's. The
    shipped implementer reports only `opened` or `blocked` — never `planned`
    (and no longer `closed`). The body must state it never reports `planned`."""
    body = _body().lower().replace("*", "")
    assert "planned" in body, "body must name the planned status it must never use"
    assert "never" in body and "planned" in body
    # the never-planned instruction must be explicit
    assert "never report" in body and "planned" in body, (
        "body must explicitly instruct the implementer to never report planned")


def test_shipped_implementer_body_treats_orders_as_accepted_only():
    """v2.10.0: PRIORITIZE fans out ACCEPTED-ONLY orders to IMPLEMENT, so the
    doer only ever sees an accepted order and its action is implement->open PR.
    The body must state IMPLEMENT receives accepted-only orders."""
    body = _body().lower().replace("*", "")
    assert "accepted-only" in body or "accepted only" in body, (
        "body must state IMPLEMENT receives accepted-only orders")


def test_shipped_implementer_body_has_no_reject_close_branch():
    """v2.10.0: the rejected->close branch is REMOVED. Reject disposition
    (comment + rejected label, no close) is enacted DETERMINISTICALLY at TRIAGE
    (work-intake gh_issue_reject_sink, wired by scheduling), never by the doer.
    The doer never closes a source issue: the body must not instruct
    `gh issue close`, must not emit a `status: closed` handoff, and must not
    carry the removed `defensive` reject-branch note."""
    body = _body()
    lower = body.lower().replace("*", "")
    assert "gh issue close" not in lower, (
        "body must not instruct the implementer to close the source issue")
    assert "status: closed" not in lower, (
        "the doer no longer emits a closed handoff (reject moved to TRIAGE)")
    assert "defensive" not in lower, (
        "the defensive reject-branch note must be removed")
    # the reject disposition now lives at TRIAGE, not here
    assert "triage" in lower, (
        "body must note reject disposition is enacted at TRIAGE, not the doer")


def test_shipped_implementer_description_drops_reject_close():
    """v2.10.0: the frontmatter description must no longer advertise a
    rejected->close-the-source-issue capability — the doer only implements
    accepted orders (open PR) or reports blocked."""
    fm = _frontmatter()
    desc = fm["description"].lower()
    assert "gh issue close" not in desc
    assert "close the source issue" not in desc


def test_shipped_implementer_body_fetches_issue_when_envelope_underfilled():
    """v2.9.0 ROBUSTNESS: if the `## Inputs` work order lacks the source issue's
    title/body (an under-filled envelope), the agent FETCHES it from the work
    order's ref/url before enacting rather than bailing to a silent no-op. The
    body must instruct the fetch-fallback via `gh issue view` with the
    title,body,comments JSON fields."""
    body = _body()
    lower = body.lower().replace("*", "")
    assert "gh issue view" in lower, (
        "body must instruct fetching the issue via `gh issue view`")
    assert "title,body,comments" in body, (
        "body must fetch the title,body,comments JSON fields")
    # the fetch is conditioned on an under-filled envelope (missing title/body)
    assert "title" in lower and "body" in lower


def test_shipped_implementer_body_still_supersedes_prior_pr():
    """v2.8.0 supersede-on-retry must remain intact after the v2.9.0 bump."""
    lower = _body().lower().replace("*", "")
    assert "supersede" in lower
    assert "gh pr close" in lower


def test_shipped_implementer_body_pr_closes_source_issue_on_merge():
    """PR closes its source issue on merge (`Closes #<n>` in the body). The
    accept-path `gh pr create` MUST include a `--body` embedding the GitHub
    closing keyword `Closes #<source-issue-number>` so GitHub auto-closes the
    source issue on merge (propose = stays open, auto-merge = closes) and the
    PR's closingIssuesReferences is populated (repairing supersede-on-retry +
    verify-integrate orphan-detection). The label must still be present, and the
    keyword is omitted only when no source issue number is resolvable.

    This is an e2e assertion over the shipped agent definition's accept-path
    PR-open instruction — the sole surface enacting this behaviour."""
    body = _body()
    lower = body.lower().replace("*", "")
    # the closing keyword must be embedded in the PR body
    assert "closes #" in lower, (
        "accept-path PR body must embed the `Closes #<source-issue-number>` "
        "closing keyword so GitHub auto-closes the issue on merge")
    # it must be carried via the PR body, not merely mentioned
    assert "--body" in body, (
        "accept-path `gh pr create` must pass the closing keyword via --body")
    # the provenance label must remain on the same accept-path PR-create
    assert "--label auto-maintainer" in body, (
        "accept-path PR must still be stamped with --label auto-maintainer")
    # it must be tied to closing on MERGE and to populating closingIssuesReferences
    assert "merge" in lower, (
        "body must tie the closing keyword to closing the issue ON MERGE")
    assert "closingissuesreferences" in lower, (
        "body must note the keyword populates closingIssuesReferences")
    # omit only when unresolvable (never guess a number)
    assert "omit" in lower or "unresolvable" in lower, (
        "body must state the keyword is omitted when no source issue number is "
        "resolvable")


def test_shipped_implementer_body_instructs_emitting_concerns():
    """v2.5.0 (auto-maintainer-framework#212): the implementer is the PRODUCER of
    the Handoff's `concerns[]` — residual doubts on an opened handoff for the
    REVIEW gate / REPORT to surface. The body must instruct it to populate
    `concerns[]` on the accept path and distinguish a concern (a doubt about THIS
    change) from a `discovered_work[]` item (a separate new problem to file)."""
    body = _body().lower().replace("*", "")
    assert "concerns" in body, (
        "body must instruct the implementer to emit concerns on an opened "
        "handoff")
    # a concern is tied to the opened (accept) path, not a substitute for a block
    assert "opened" in body
    # the concern-vs-discovered_work distinction must be drawn so the implementer
    # does not conflate the two
    assert "discovered_work" in body or "discovered work" in body
