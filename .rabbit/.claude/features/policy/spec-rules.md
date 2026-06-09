# Spec Rules

This guide applies to AI agents that author or modify code or specifications.

---

## 1. Tool-Choice Tier: `script > CLI > spec > prompt`

When choosing how a task gets done, reach for determinism first, AI last.

- **Script** — code you own, version, and control. Fully deterministic.
- **CLI** — a deterministic tool invocation. No AI inside.
- **Spec** — structured directives that tightly constrain what AI does.
  Minimal interpretive freedom.
- **Prompt** — a free-form request to AI. Maximum freedom. Minimum
  predictability.

Determinism means the failure is locatable to a source artifact your team can
read or escalate against — not merely that the function is byte-reproducible.
A script fails reproducibly: the error is locatable and fixable. An LLM fails
silently — it drifts, hallucinates, or returns different output from identical
input.

---

## 2. Schemas and Contracts

Every cross-component handoff uses a fixed-format, declared schema. Never
free-form text. Schema fields are typed, named, and validated at the boundary.

Every component declares its contract: what it reads, what it writes, what it
invokes. Read nothing outside the contract. Generate nothing outside the
contract.

Human-readable views are produced by tools that operate on the schema-formed
artifact — never authored alongside the machine-format one.

**When a task falls outside your declared scope:** stop, emit a structured
handoff (what you intended, what is out-of-scope, what context the scope's
owner needs), and wait. Do not edit across the boundary, even if the change
appears trivial.

---

## 3. Lifecycle and Ownership

At creation time, every artifact records:

- **Owner** — a named individual or team accountable for it. An unowned
  artifact is not a reliable artifact.
  **For repo-level features distributed as part of rabbit-workflow, the owner MUST be `rabbit-workflow team`, not an individual.**
  Individual ownership is reserved for personal scripts, experimental tooling, and out-of-distribution artifacts.
- **Version** — for contracts, schemas, and encodings. Version bumps follow
  semantic conventions appropriate to the artifact.
- **Deprecation criterion** — the condition under which this artifact will be
  superseded (e.g., "when downstream Y migrates to schema v3", "after the
  2026-Q4 platform cutover").

Contract changes are additive by default. Breaking changes require a
coexistence window during which both old and new are honored, and a
documented migration path for consumers.

Every dependency, schema, and encoding will eventually be superseded. Name
the deprecation criterion at creation time, or inherit its failure.

**Where the metadata lives.** Each artifact type has a fixed metadata location:

- **Features** — top-level `name`, `version`, `owner`, `deprecation_criterion`
  fields in the feature's `feature.json`.
- **Specs / contracts** — YAML frontmatter at the top of `docs/spec.md`
  and `docs/contract.md` (`feature:`, `version:`, `owner:`,
  `deprecation_criterion:`). The feature's changelog lives alongside them at
  `docs/CHANGELOG.md`; all three are flat siblings under `docs/` (next to any
  `docs/bugs/` directory).
- **Skills and commands** — YAML frontmatter at the top of the `SKILL.md`
  or command `.md` file (`version:`, `owner:`, `deprecation_criterion:`).
- **Scripts** — module-level docstring, with `Version:`, `Owner:`, and
  `Deprecation criterion:` lines.
- **Schemas and JSON contracts** — top-level `schema_version`, `owner`,
  and `deprecation_criterion` keys.
- **Templates** — `template_version` marker (placement convention defined
  by the contract feature's spec).

An artifact missing any of these fields in its declared location is
considered unowned. Promote it to compliance before extending it.

---

## 4. SKILL.md Authoring Standard

Rules for authoring and changing `SKILL.md` files. Each derives from a
principle above; cite the source, do not restate it.

- **Script-Backed Orchestration** (derives from §1 Tool-Choice Tier). An
  orchestration step that involves a computed value or mode-aware
  branching MUST live in a companion script under `scripts/`; the SKILL.md
  invokes the script and the script owns the logic. SKILL.md bodies MUST
  NOT carry bash blocks with runtime placeholders (e.g. `<feature-name>`,
  `<branch-name>`) that the model assembles at invocation time — that is
  prompt-tier, not script-tier. Exception: read-only informational
  commands (e.g. `git log --oneline -5`) are acceptable inline.

- **Verbatim Policy Embedding** (derives from §2 Schemas and Contracts).
  When a SKILL.md surfaces policy (e.g. the no-session-override rule, the
  bounded-scope rule), it MUST embed the relevant text verbatim from the
  canonical source (`spec-rules.md`, `philosophy.md`, `coding-rules.md`)
  via `@path/to/policy.md` injection or a script that reads and embeds the
  section. It MUST NOT paraphrase policy rules — paraphrases drift; cite
  them.

- **skill-creator Validation** (derives from §1 determinism). A SKILL.md
  change MUST pass through the `skill-creator` tool
  (`skill-creator:skill-creator` / `document-skills:skill-creator`) before
  deployment. The skill-creator workflow validates description accuracy
  (triggering), body correctness, and deployment correctness.

- **No Subagent-Dispatching Skill Inside `Agent()`** (derives from §2
  Schemas and Contracts). A skill whose body dispatches a subagent — ANY
  `Agent(...)` dispatch, whether TYPED (`Agent(subagent_type=...)`) or
  UNTYPED (a default/general-purpose `Agent(prompt=...)` with no
  `subagent_type`) — MUST NOT itself be invoked inside an `Agent()` call.
  Doing so creates illegal two-level nesting
  (`main → Agent level 1 → subagent level 2`), which Claude Code does not
  support. To parallelize work done by such a skill, do NOT wrap the skill
  in parallel `Agent()` calls; instead dispatch the UNDERLYING subagent
  directly at level 1 (`main → N parallel subagents`), reusing the skill's
  mechanics through shared `scripts/` (prompt assembly, result
  persistence). The skill wrapper exists for a single, main-session
  invocation. The known subagent-dispatching skills are `rabbit-feature-touch`
  (dispatches tdd-subagent), `rabbit-feature-scope` (dispatches an untyped
  default-model Agent), and `rabbit-housekeep` (decomposes per-feature
  housekeeping work and dispatches housekeeping subagents); this named set
  is authoritative, and any future subagent-dispatching skill inherits this
  constraint and MUST be added to it.

---
