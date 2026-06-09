---
name: rabbit-feature-scope
description: Resolve a natural-language request to the list of rabbit features whose files it will modify. Emits a prompt for a default-model Agent; caller parses the JSON response. General-purpose shared skill — no assumptions about callers.
version: 1.2.0
owner: rabbit-workflow team
deprecation_criterion: when feature-scope resolution is automated natively by the dispatch infrastructure.
---

## Overview

`rabbit-feature-scope` resolves a natural-language request to the set of features
it will modify. It is a general-purpose shared skill — callers and use cases are
not prescribed.

## Usage

Step 1 — generate the prompt. Run the companion `resolve-scope.py` script in
your shell, passing the caller's natural-language request verbatim as its
single argument and capturing the emitted prompt:

<!-- example: invocation synopsis of resolve-scope.py; the request is passed verbatim -->
```bash
PROMPT=$(.claude/features/rabbit-feature/scripts/resolve-scope.py "<request-description>")
```

Step 2 — dispatch the Agent. The following is a **Claude tool call**, not a
shell command. Do NOT shell-execute it; invoke the Agent tool from Claude with
the default model (no Opus override):

```text
Agent(prompt: PROMPT)
# Agent responds with JSON:
# {"features": ["feature-name-1"], "rationale": "one sentence"}
```

## Response Schema

```json
{"features": ["name1", "name2"], "rationale": "one sentence"}
```

- `features`: list of feature names matching `find-feature.py list`. May be empty `[]`.
- `rationale`: one sentence explaining the selection.
- The feature list is authoritative — caller does not second-guess it.

## Notes

- `resolve-scope.py` emits a prompt to stdout only; it does not call Agent itself.
- Uses `find-feature.py list-json` (subcommand, not flag) — never reads `registry.json`.
- Default model (no Opus override).
- Prompt instructs Agent to respond with ONLY valid JSON on a single line.
