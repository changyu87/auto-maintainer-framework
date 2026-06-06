# auto-maintainer-framework

**Autonomous and automatic maintainer loop for AI-backed projects, packaged as a
Claude Code plugin.**

*"auto" stands for both **automatic** and **autonomous**.*

A project drops this in, points it at a work tracker, and gets a self-driving
maintainer: on a schedule it pulls actionable work, triages it, dispatches
isolated coding agents to implement it, verifies the result, integrates it
(PR / merge / release), cleans up, and reschedules — with safety guards,
crash-safe resumable state, and human-delegated authority — until a human
stops it.

The framework is **ports-and-adapters**: the loop core is fixed and
project-agnostic; every project-specific concern (where work comes from, how it
gets implemented, what "safe to merge" means, how integration happens) is a
swappable adapter. Default adapters ship for GitHub Issues + git.

## Status

**Design / draft.** No runnable code yet. The current contents are the design
plan; see [`docs/DESIGN.md`](docs/DESIGN.md).

## Packaging

Distributed as a **Claude Code plugin**, installable via a Claude plugin
marketplace (`/plugin`). The repository layout will follow the Claude
marketplace convention (`.claude-plugin/plugin.json`, etc.) so installation is
a standard `/plugin install` for end users.

## Relationship to rabbit-workflow

This framework generalizes the `rabbit-auto-evolve` feature of the
rabbit-workflow project into a portable, project-agnostic Claude Code plugin.
rabbit-workflow is intended to be the framework's first adapter consumer
(dogfood).
