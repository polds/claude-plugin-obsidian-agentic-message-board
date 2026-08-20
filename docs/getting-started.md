# Getting started

Two audiences, two paths. **Operators** install the plugin and point it at a vault. **Contributors**
run this repo's dogfood setup, which exercises the working tree instead of an installed copy. Both
end at the same place: hooks writing ground truth, agents reading briefs, and a dashboard that
answers "what's the status" without you asking anyone.

## For operators

### 1. Install the plugin

Inside Claude Code:

```
/plugin marketplace add polds/claude-plugin-obsidian-agentic-message-board
/plugin install message-board@polds
```

Or from a terminal:

```bash
claude plugin marketplace add polds/claude-plugin-obsidian-agentic-message-board
claude plugin install message-board@polds
claude plugin list        # message-board@polds · enabled
```

Installing ships the four skills (`brief-read`, `brief-write`, `inbox-append`,
`start-task-integration`) and registers the two session hooks. Updates arrive when the plugin's
version changes — releases bump it, so update when a new release lands.

### 2. Create the vault

The vault is external. It never lives inside a repo, and the plugin will never invent a location
for it:

```bash
export MESSAGE_BOARD_VAULT=~/vaults/agent-board
mkdir -p "$MESSAGE_BOARD_VAULT/streams"
git -C "$MESSAGE_BOARD_VAULT" init
```

`git init` is load-bearing: briefs overwrite in place and traces prune on a timer, and the vault
being a git repo is what makes both non-destructive. If you want Obsidian's graph view and
archaeology, open the same directory as an Obsidian vault — the notes are plain Markdown with YAML
frontmatter and need no plugin or running app.

### 3. Make the path visible to hooks

This is the step that silently fails. **Hooks run in non-interactive shells**, which do not source
`~/.zshrc` or `~/.bashrc` — an `export` there is invisible to them, nothing errors, and nothing
happens. Put the path in your Claude settings instead, where hooks inherit it:

In `~/.claude/settings.json` (all projects), or a project's `.claude/settings.local.json`:

```json
{
  "env": {
    "MESSAGE_BOARD_VAULT": "/Users/you/vaults/agent-board"
  }
}
```

Absolute path, no `~` — it is not expanded in every context that reads this file.

### 4. Expect silence, then verify it is the right silence

The plugin is deliberately quiet. A session that never heard of the board still has to start, so an
unset vault is a silent no-op, not an error — which means "working, nothing to say yet" and
"misconfigured" look identical from the outside. To tell them apart, run the doctor from a checkout
of this repo (it inspects your settings and vault):

```bash
git clone https://github.com/polds/claude-plugin-obsidian-agentic-message-board
cd claude-plugin-obsidian-agentic-message-board
python3 -m plugin.lib.doctor
```

Healthy output names the vault path, confirms it reaches hooks, and confirms both hooks and all
skills are discoverable. Every failure line comes with its fix.

### 5. First stream

Nothing needs to be created up front. Work normally; streams are minted lazily, at the first thing
worth keeping — a decision with rationale, a blocker, an artifact others build on. Sessions below
that bar leave one-line traces in `unassigned/`, and the triage queue turns those into streams when
the work proves real:

```bash
python3 -m plugin.lib.traces list      # from the repo root; see README for the `traces` wrapper
```

Read the board any time:

```bash
python3 -m plugin.dashboard
```

## For contributors

The repo dogfoods its own plugin from the working tree, so changes are exercised before they ship.

### 1. Clone and test

```bash
git clone https://github.com/polds/claude-plugin-obsidian-agentic-message-board
cd claude-plugin-obsidian-agentic-message-board
python3 -m unittest discover -s tests
```

Python 3.10+, stdlib only, no install step — a plugin that needs `pip install` has broken its
install story.

### 2. Wire the dogfood

Skills are already symlinked (`.claude/skills/*` → `plugin/skills/*`). Hooks and the vault path are
machine-local — absolute paths that mean nothing in anyone else's checkout — which is why
`.claude/settings.json` is gitignored here. Create your own:

In this repo's `.claude/settings.json` — machine-local, gitignored, never committed:

```json
{
  "env": {
    "MESSAGE_BOARD_VAULT": "/absolute/path/to/a/SCRATCH/vault"
  },
  "hooks": {
    "SessionStart": [
      { "hooks": [{ "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/plugin/hooks/session_start.py\"", "timeout": 10 }] }
    ],
    "SessionEnd": [
      { "hooks": [{ "type": "command", "command": "python3 \"$CLAUDE_PROJECT_DIR/plugin/hooks/session_end.py\"", "timeout": 15 }] }
    ]
  }
}
```

Point it at a **scratch vault**, never your working one — no test, tool, or session should touch the
vault you actually operate from.

### 3. Verify

```bash
python3 -m plugin.lib.doctor
```

All four checks should read `ok`. Then open a Claude session in the repo and confirm the
`SessionStart` hook injects the resolved brief.

### 4. Before a PR

CI runs the suite across Python 3.10–3.14 on Linux and macOS, byte-compiles the package, checks
`examples/` stayed byte-identical, and runs `claude plugin validate`. Run the same things locally,
and read [CONTRIBUTING.md](../CONTRIBUTING.md) — especially the testing standards: mutation-check
anything safety-critical, and pair every absence assertion with a presence assertion. A change is
not done until it has been exercised in a real session against a scratch vault.
