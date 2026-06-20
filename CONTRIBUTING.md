# Contributing to auto-maintainer-framework

Thanks for your interest in improving the project. This is an early-stage
Claude Code plugin, so contributions of all sizes — bug reports, docs fixes,
and code — are welcome.

## Where the design lives

Before proposing a change, it helps to understand where the project is headed.
The design and planning docs live under [`docs/`](docs/):

- [`docs/DESIGN.md`](docs/DESIGN.md) — the overall design plan: the tick FSM,
  the ports-and-adapters architecture, and the scope decisions.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — per-feature status and the
  traceability map from design to implementation. Check here to see what is
  planned, in progress, or done.
- [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) — common problems and
  fixes.

See also the [`README.md`](README.md) for what the plugin does and how to
install it.

## Filing issues

Issues are the project's single inbox — please file one before sending a large
change so it can be discussed first.

- Search [existing issues](https://github.com/changyu87/auto-maintainer-framework/issues)
  first to avoid duplicates.
- For a **bug**, include what you did, what you expected, what actually
  happened, and your environment (Claude Code version, `gh` version, OS).
- For a **feature or change**, describe the problem you want solved and, if you
  can, how it fits the design in `docs/DESIGN.md`.
- Keep one issue per topic so each can be tracked on its own.

## Pull request flow

1. **Fork** the repository (or branch, if you have push access).
2. **Branch** off the default branch (`main`) using a descriptive name, e.g.
   `docs/fix-typo` or `feat/pull-adapter`.
3. **Make your change.** Keep each PR focused on a single concern — small,
   reviewable PRs are merged faster.
4. **Check your work.** CI byte-compiles the shipped plugin libraries on every
   PR (see [`.github/workflows/ci.yml`](.github/workflows/ci.yml)); make sure
   your change keeps that green.
5. **Open a pull request** against `main`. Describe what changed and why, and
   link the issue it addresses (e.g. `Fixes #123`).
6. **Address review feedback** by pushing follow-up commits to the same branch.

Maintainers merge once the PR is reviewed and CI is green.

## Questions

If something is unclear, open an issue — questions are a perfectly good reason
to file one.
