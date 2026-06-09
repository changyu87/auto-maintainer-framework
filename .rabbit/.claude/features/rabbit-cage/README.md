# rabbit-workflow

Structured Claude Code workflow enforcing Machine First, Bounded Scope, and Designed Deprecation.

Policy is auto-injected every 20 prompts (configurable). Slash commands and skills included.

---

## Modes

Rabbit runs in two modes, detected automatically from the directory `claude` is launched from:

| Mode | Launch from | Who uses it |
|------|-------------|-------------|
| **Standalone** | repo root (`rabbit-workflow/`) | Developing rabbit itself |
| **Vendored** | `<your-project>/.rabbit/` | Applying rabbit to your project |

---

## Standalone mode (developing rabbit)

Clone the repo and launch `claude` from the root:

```bash
git clone https://github.com/changyu87/rabbit-workflow
cd rabbit-workflow
claude
```

Feature work lives in `.claude/features/`. The scope-guard enforces feature boundaries and the TDD subagent drives each feature through the full spec → test-red → impl → test-green cycle.

---

## Vendored mode (installing into your project)

Rabbit installs as a vendored `.rabbit/` directory committed to your project. No submodules; no external dependencies at runtime.

> **Note:** rabbit's vendored `.rabbit/` install (historically called "plugin mode") is **unrelated** to Claude Code's native `/plugin` marketplace plugins — they are distinct, independent mechanisms that happen to share the word "plugin."

### Install

The one-liner fetches the latest published release and runs `install.py --src ... --target .rabbit`:

```bash
cd /path/to/your/project
curl -sSL https://raw.githubusercontent.com/changyu87/rabbit-workflow/dev/install.sh | bash
```

The pipe to `bash` is explicit — this works from any shell including csh and tcsh.

The `install.sh` bootstrap is fetched from `dev` (small, stable shell wrapper);
the install itself resolves GitHub's latest release dynamically — `install.sh`
queries the `releases/latest` API and installs that tag, so first-install tracks
the latest release with no per-release bump. If the lookup is unavailable
(offline / API outage) it falls back to a hardcoded last-known-good release tag.

If you prefer to download first:

```bash
curl -fsSLO https://raw.githubusercontent.com/changyu87/rabbit-workflow/dev/install.sh
bash install.sh      # must be bash — install.sh is not csh/tcsh compatible
```

Requires: `python3`, `curl`, `tar`.

### Commit the install

```bash
git add .rabbit/
git commit -m "install rabbit"
```

Collaborators get a complete rabbit install via `git clone` — no per-developer install step.

### Start a session

**bash / zsh:**
```bash
cd .rabbit/ && claude
```

**csh / tcsh:**
```tcsh
cd .rabbit/ && claude
```

The `&&` operator works in both. Once inside the Claude session, all hooks are Python — no further shell dependency.

### Declare a feature (map a code slice)

```bash
python3 .rabbit/.claude/features/rabbit-feature/scripts/scaffold-feature.py \
    my-feature ../src/auth/**
```

This scaffolds `.rabbit/rabbit-project/features/my-feature/` and registers the glob in `.rabbit/rabbit-project/project-map.json`. From then on, scope-guard blocks unsanctioned edits to `../src/auth/**`.

### Scope bypass (one-shot)

To make a single change without ceremony:

```bash
touch .rabbit/.runtime/scope-bypass-once
```

The marker is consumed on the next write — single-use only.

### Update

In-place refresh (preserves `.rabbit/.runtime/` and any custom entries in
`.rabbit/.claude/settings.json`). Updates go through `install.py` directly —
`install.sh` is for first-time installs only:

```bash
cd <project-root>
python3 .rabbit/install.py --update
git add .rabbit/
git commit -m "chore(rabbit): update to latest"
```

The default `--update` resolves GitHub's latest published release — the same
release the update-check banner advertises — and installs it. It NEVER
downgrades: when the latest release is not newer than what is installed,
`--update` reports "already up to date" and changes nothing. To install a
specific ref regardless (including an intentional downgrade), name it
explicitly with `--version <ref>` (or `--ref <ref>`).

### Pin to a specific version

Setting `RABBIT_REF` short-circuits the dynamic latest-release lookup and installs the exact ref you name:

```bash
RABBIT_REF=v9.0.26 curl -sSL https://raw.githubusercontent.com/changyu87/rabbit-workflow/dev/install.sh | bash
RABBIT_REF=v1.14.0 curl -sSL https://raw.githubusercontent.com/changyu87/rabbit-workflow/dev/install.sh | bash
```

`RABBIT_REF` accepts any branch, tag, or commit SHA. `RABBIT_REPO` overrides the default repo.

### Bleeding edge (developers — opt-in only)

```bash
RABBIT_REF=dev curl -sSL https://raw.githubusercontent.com/changyu87/rabbit-workflow/dev/install.sh | bash
```

The default install resolves GitHub's latest release dynamically; `dev` is opt-in only.

---

## Commands

| Command | Description |
|---|---|
| `/rabbit-refresh` | Re-inject policy into context |
| `/rabbit-cage-config prompt-threshold N` | Set auto-refresh interval to N prompts |
| `/rabbit-cage-config prompt-threshold` | Restore default (20 prompts) |
| `/rabbit-cage-config allowed-tools add\|remove <tool>` | Manage Claude Code tool permissions |
| `/rabbit-cage-config bash-allow add\|remove <cmd>` | Manage Bash command permissions |
| `/rabbit-cage-config scope-guard on` | Revoke an active scope-guard override (re-arm default-deny) |

> rabbit-cage's settings (`scope-guard`, `bypass-permissions`, `allowed-tools`, `bash-allow`, `prompt-threshold`) are managed via the per-feature `/rabbit-cage-config` command.

---

## Uninstall

**Vendored mode:** `rm -rf .rabbit/` and remove `.rabbit` from `.gitignore` if present.

**Standalone mode:** delete the cloned repo.
