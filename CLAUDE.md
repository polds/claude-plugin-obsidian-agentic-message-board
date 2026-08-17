# OpenWolf

@.wolf/OPENWOLF.md

This project uses OpenWolf for context management. Read and follow .wolf/OPENWOLF.md every session. Check .wolf/cerebrum.md before generating code. Check .wolf/anatomy.md before reading files.


# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A Claude Code **plugin** that gives parallel agent workstreams a shared handoff surface, persisted as
Markdown notes in an **Obsidian vault**.

The problem it exists to solve: one operator running many concurrent Claude sessions becomes the
bottleneck, because rebuilding context on any given workstream requires asking that session "what's
the status." The plugin's job is to make that context reconstructable without reading the
conversation.

The design direction is settled. Read `docs/ideas/agent-handoff-board.md` before proposing
architecture — it records not just what was chosen but what was explicitly rejected and why, so those
decisions don't get relitigated.

Implementation is underway against `tasks/plan.md`. The read path, write path, claim resolution, inbox,
and dashboard exist in `plugin/`; hooks, minting, and traces do not yet. When reality and this file
disagree, reality wins — fix this file.

Two consumers, one codebase:

- **The plugin** (`.claude-plugin/` + `skills/`, `commands/`, `agents/`, `hooks/`) — the shippable
  artifact other people install.
- **`.claude/`** — this repo dogfooding its own plugin during development. Exercise changes here
  before shipping them.

## Non-negotiable design constraints

Decisions already made. Do not relitigate them in code:

1. **The vault is external.** It lives at a user-configured path, not in this repo. Never hardcode a
   vault path, never write into the repo as if it were the vault, and never assume the vault exists —
   resolve the configured path and fail loudly with an actionable message if unset or missing.
2. **The vault is the source of truth.** No sidecar database, no cache that can drift into being
   authoritative. Any index is derived and safe to delete and rebuild.
3. **Obsidian-native, not Obsidian-dependent.** Notes are plain Markdown with YAML frontmatter,
   readable and writable with `Read`/`Write`/`Grep` alone. Use Obsidian conventions — `[[wikilinks]]`,
   tags, frontmatter — but never require Obsidian, a plugin API, or a running app.
4. **Obsidian is storage, not the reading surface.** The operator reads a rendered dashboard. Obsidian
   is durability, human archaeology, and graph view. Do not optimize the note format for Obsidian
   skimmability at the cost of agent cold-start compression.
5. **The primary consumer of a brief is the next agent, not the operator.** The dashboard is a
   rendering of the same briefs. One artifact, two audiences, one write path — never a separate
   operator-facing document that agents must maintain in parallel.
6. **Agents never write the ground-truth section.** Mechanical facts come from hooks. Agent judgment
   and hook-observed reality are kept in separate fields precisely so they can be compared.

## Architecture

### The brief is the whole design

Every workstream has one `BRIEF.md`. Its frontmatter is the schema and the wire format
simultaneously — what makes a folder of Markdown queryable by agents *and* by Obsidian Dataview.
Getting this shape right matters more than any code in the repo; changing it later invalidates every
existing vault.

Section priority is deliberate and load-bearing:

- **Decided** and **Do not** are the highest-value sections and the ones comparable tools omit. They
  carry the implicit reasoning that otherwise dies with the session — the exact context-fragmentation
  failure that makes parallel agents produce work that doesn't compose. An agent that re-litigates a
  settled decision is the bug this project exists to prevent.
- **Ground truth** is hook-written only. The gap between what an agent claimed and what actually
  happened is a first-class signal, not an inconsistency to paper over.

Briefs overwrite in place. History comes from the vault being a git repo, which defers event sourcing
entirely — do not add an event log or projector without revisiting the idea doc.

A brief has a hard size budget (~400 words, one screen) because its whole purpose is cheap cold start.
`## Decided` is append-only, so it drains by **compaction, not truncation**: an entry archives once the
decision *and its rationale* live somewhere a working agent already reads. Never evict by age or
count — that keeps trivia and drops foundational decisions. Expect document-producing streams to drain
almost completely and code streams to retain much more, since code encodes what was decided but rarely
why; an empty `## Decided` on a code stream means rationale is being lost, not that the stream is tidy.
Schema and layout: `docs/spec/brief-schema.md`.

### Skills are the interface, code is an implementation detail

Agents interact through skills, not by being told the file format. A skill's `SKILL.md` owns the
*contract*; any bundled script is an optimization behind that contract.

The bias: **do it with `Read`/`Write`/`Grep` until that demonstrably breaks.** Reach for a script when
markdown-only instructions produce inconsistent output or the work is genuinely mechanical. Prefer
stdlib-only and keep scripts invocable standalone — a script needing a build step, a package install,
or a running server has broken the plugin's install story.

Split skills by *operation*, not by *entity*: an agent cold-starting needs the read contract only, and
loading write instructions alongside it is context spent for nothing.

### Minting

Streams are minted **lazily, at first durable write** — never at session start, because most sessions
produce nothing worth keeping. The bar is deliberately strict: a decision with its rationale, a blocker
needing someone else, or an artifact others build on. Not: ran tests, read files, answered a question.

Slug derivation already exists — `start-task` derives `plat-192-fix-erpc-scrape` from the task at
branch-naming time, which is exactly a stream slug. Reuse it rather than inventing a second scheme.
Structured dispatch (start-task, Linear, PR review, scheduled) mints silently; only a bare ad-hoc
session prompts, once, with a proposed slug. **Always check `claims` before minting** — a stream
already claiming that PR or issue is joined, not duplicated.

Sessions below the bar still drop a one-line trace into `unassigned/` (`<timestamp>-<session>.md`, one
file per session so appends stay atomic). Strict bar, cheap recovery: nothing is lost, nothing pollutes
the dashboard. Traces prune after a retention window, which is non-lossy because the vault is a git
repo.

### Adoption is structural, not encouraged

The mechanism that keeps briefs from decaying is that **reading one is the cheapest way for an agent
to start working**. Writing follows reading. Any change that makes reading a brief more expensive than
re-deriving context from the repo has broken the core loop, regardless of what else it improves.

Splitting the write path accordingly: hooks own mechanical facts (deterministic, agent cannot forget,
but blind to what mattered); skills own judgment (blockers, decisions, asks). Neither substitutes for
the other.

### Do not rebuild what the harness already provides

- Agent-to-agent messaging exists — `ListAgents` and `SendMessage` reach other local sessions live.
  Wrap it if needed; never reimplement it in the vault.
- **Honcho owns the operator; the brief owns the work. One-way wall.** Never read Honcho as authority
  on project state, and never write stream-scoped state into it. Anything stream-scoped — decisions,
  blockers, status, claims — lives only in the vault. Honcho keeps stable cross-project preferences
  about *how* to work.

  This is not caution, it is measured: an audit of Honcho's record of this project found all
  conclusions frozen at the first turn, five already-cut features still stored as live desires, and
  two fabricated facts inferred from an illustrative code example. Honcho models the person and does
  not revise when a decision reverses. Treat any Honcho conclusion about project state as stale and
  verify against the repo.

### Streams, ownership, and concurrency

A **stream** is the unit that owns one `BRIEF.md`: a durable immutable slug holding mutable *claims*
(repo, branch, worktree, issue, PR). Never make a claim the identity — branches die on merge, stacks
span several, and review means checking out someone else's. Granularity is one stream per
independently-decidable unit of work, not per unit of code.

**One writer per file, three surfaces:** the owner writes `BRIEF.md` and `decided-archive.md`, hooks
write `ground-truth.md`, everyone else appends a new file to `streams/<id>/inbox/`. Appends are
filesystem-atomic and therefore contention-free. Subagents never write; they report to their parent.
Any design requiring a read-modify-write of a file another agent may be holding needs to justify
itself against that partition first — including putting hook-written fields in `BRIEF.md` frontmatter,
which is how this rule got broken the first time.

Write contention is not just a race to avoid — it is the **granularity signal**. Two agents needing to
write one brief simultaneously means it should have been two streams.

Session-to-stream mapping is **explicit-only for writes**. Git state is intent-blind (the same branch
serves building, reviewing, and debugging), and measurement against a real repo showed inference is
weaker still: review streams often have no branch or worktree at all, one issue can span eight
branches, and a busy repo carries dozens of worktrees across half a dozen tools, many ephemeral. So
git-derived resolution is a **read-path hint**, never a write path. Reads may guess; **writes must be
certain or fall to `unassigned/`** — reading the wrong brief is cheap and self-evident, writing the
wrong one silently corrupts two streams and burns the honesty signal.

Full model, resolution ladder, and lifecycle rules: `docs/ideas/agent-handoff-board.md`.

## Development

### Working on the plugin

Validate structure after touching `plugin.json`, skills, commands, or hooks — the `plugin-dev` plugin
provides `plugin-validator` and `skill-reviewer` agents for exactly this.

Skill frontmatter `description` is not documentation — it is the retrieval key deciding whether the
skill fires at all. Write it as trigger conditions, and include what it is *not* for.

**Skills are prose and nothing executes them, so doc bugs survive a green suite.** Every command a
`SKILL.md` prints must be run, not read: `start-task-integration` documented two commands that did
not exist and the whole suite stayed green. Treat any command in a skill as untested until it has
been pasted into a shell.

### Testing changes

```bash
python3 -m unittest discover -s tests            # everything
python3 -m unittest tests.test_claims -v         # one module
python3 -m unittest tests.test_vault.TestResolve.test_missing_vault_env_names_the_variable
```

Stdlib `unittest`, no install step — a plugin that needs `pip install` has broken its install story.
Run from the repo root so `plugin.lib` imports resolve.

**Tests build their vaults in `tempfile`. `examples/` is read-only fixture input and must stay
byte-identical** — a design stream and a closed code review, both transcribed from real streams and
then scrubbed of the originating organization's identifiers before this repo was made public. What
makes them worth more than invented data is their *shape*: a review with no branch and no worktree, a
stale base, superseded scope, and citations pointing at commits a reader cannot reach. Synthetic
fixtures are tidy, and tidiness is exactly what these parsers must not assume. Never test against the
operator's working vault.

Beyond unit tests, a change is not done until it has been exercised through `.claude/` in a real
session against a scratch vault. A change that has only been reasoned about, not run, is not done.

Two test qualities this suite already has and should keep: **mutation-check anything concurrency- or
safety-critical** (swap the safe primitive for the naive one and confirm the test actually fails), and
**pair every absence assertion with a presence assertion** — "renders no error vocabulary" passes
trivially on a renderer that outputs nothing.

### Verifying against Obsidian

A change correct for agents but rendering badly in Obsidian is a regression. Frontmatter parsing
cleanly and wikilinks resolving are part of the acceptance bar for anything touching note structure.

## Open decisions

Tracked in `docs/ideas/agent-handoff-board.md` under "Open Questions". Record answers there and reflect
any that change the constraints above. Stream identity and session mapping are resolved; the live ones
are the Honcho boundary, the dashboard surface, and whether explicit stream declaration actually gets
used in practice.
