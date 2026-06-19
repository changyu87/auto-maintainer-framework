# Troubleshooting

Common issues and quick fixes when running the auto-maintainer plugin.

## Plugin does not appear after adding the marketplace

After `/plugin marketplace add ...`, the plugin may not show up until plugins
are reloaded.

- Run `/reload-plugins`.
- If it still does not appear, restart Claude Code and re-check the marketplace
  entry.

## The loop is not ticking

If the maintainer loop seems idle and no ticks are happening:

- Check the current state with `/auto-maintainer:status`.
- Confirm the loop has been started and is not paused.

## `gh` authentication errors

The default GitHub adapters shell out to the `gh` CLI, so it must be installed
and authenticated.

- Run `gh auth login` and complete the prompts.
- Verify with `gh auth status`.
