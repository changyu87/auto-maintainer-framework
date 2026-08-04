# Coding Rules

*Adapted from Andrej Karpathy's CLAUDE.md — battle-tested for reducing common LLM coding mistakes.*

---

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

**Read before editing.**
Before editing an existing file, Read it. Before writing alongside existing code, Read the surrounding module. Edits made without reading are speculative.

---

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes,
simplify.

---

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused in the
  current (uncommitted) edit. This is "cleaning up your own mess."
- "Uncommitted" includes BOTH staged and unstaged work from the current
  agent session — if you ran `git add` earlier in this session and a
  later edit orphans something in those staged hunks, clean it up now;
  staged-but-not-committed work is still yours to clean.
- The exception does NOT extend to previously committed artifacts. Once
  code is committed (even within the same session) it is pre-existing —
  mention it, don't delete it.

The test: every changed line should trace directly to the user's request.

---

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:

    1. [Step] → verify: [check]
    2. [Step] → verify: [check]
    3. [Step] → verify: [check]

Strong success criteria let you loop independently. Weak criteria ("make it
work") require constant clarification.

For non-trivial feature edits, the TDD discipline defined in the repo
`CLAUDE.md` (Rabbit Workflow) supersedes the lighter loop above: go through
the full spec → test-red → impl → test-green cycle via
`/rabbit-feature-touch`. Trivial edits may use the session/one-time
override, but only when explicitly justified.

---

## 5. Output Hygiene

**Do not produce artifacts the user did not ask for.**

- Do NOT create documentation files (`*.md`, `README*`, design notes,
  findings, summaries) unless the user explicitly requested one. Return
  findings as your reply, not as a written file.
- Do NOT add emojis to code, comments, commit messages, or generated files
  unless the user explicitly requested them. Keep prose plain.
- Do NOT add speculative scaffolding (placeholder configs, draft schemas,
  empty test files) "for later use." Add the file when the work demands it.

The test: every file you create should trace directly to the user's request
or to a clearly named follow-up they approved.

---

## 6. Cleanup: Prove It Dead or Flag It

**A cleanup pass removes dead-but-plausible content, not just syntactically
tagged historical burden — and never silently keeps the uncertain.**

A cleanup pass is done only when every claim in scope has been resolved by a
deterministic VERIFICATION check, not by judgment. For each claim, run the
matching check:

- **path reference** → `find` it across the repo; zero matches = dead.
- **function / flag / script / symbol** → `grep` for callers/usages; none =
  dead.
- **described behavior** → a reachable code path and/or a test exercising it;
  neither = dead.
- **cross-feature claim** → inspect the other feature directly.

Apply the action table to each result:

| Verification result            | Action                                      |
| ------------------------------ | ------------------------------------------- |
| **Proven dead** (check empty)  | DELETE with confidence.                     |
| **Proven live** (check finds it)| KEEP.                                       |
| **Unverifiable** (no cheap check)| FLAG: file a `housekeeping`-tagged sub-issue naming the file, the sentence, and why it could not be verified. |

**Annotate-and-continue.** An unverifiable sentence is flagged as a separate
sub-issue and the pass CONTINUES. One uncertain sentence never stalls a
feature's cleanup.

---

## 7. Parenthetical Clarity

**Prefer declarative sentences over parenthetical asides.**

For each parenthetical aside, decide:

- **Load-bearing** → fold it into the sentence as a clause, or promote it to
  its own sentence.
- **Redundant or decorative** → drop it.

This is a clarity guideline, not an absolute ban. A load-bearing parenthetical
— a precise clarifying term, a citation like `(Inv 49)`, a code token — is
fine to keep inline. Apply this when authoring or editing prose; it does not
mandate a sweep of existing text.
