# message-board

> **Operator Cognition Framework** — agents remember, so the operator doesn't have to ask.

An agent handoff board. Every workstream owns one `BRIEF.md` in an external Obsidian vault. Agents
cold-start by reading it, hooks record mechanical ground truth beside it, and a terminal dashboard
replaces asking each running session "what's the status."

## The problem

One operator running many concurrent Claude sessions becomes the bottleneck. Rebuilding context on
any given workstream means asking that session what it's doing, and the answer lives only in a
conversation that dies with the window.

This is not a chat system. Agents share a filesystem, so a typed artifact beats a message on every
axis — cheaper, inspectable, diffable, replayable, and it survives the session. The brief's primary
consumer is *the next agent*, not the operator; the dashboard is a rendering of the same briefs, so
there is one artifact and one write path serving two audiences.

## Setup

```bash
export MESSAGE_BOARD_VAULT=~/vaults/agent-board
mkdir -p "$MESSAGE_BOARD_VAULT/streams" && git -C "$MESSAGE_BOARD_VAULT" init
```

The vault is external and never lives in this repo. `git init` is not optional — it is what makes
brief overwrites non-destructive and trace pruning recoverable, and it is why this design defers
event sourcing entirely.

## Use

```bash
python3 -m plugin.dashboard                          # all streams, operator asks first
python3 -m plugin.lib.brief_read <slug>              # cold-start on one stream
python3 -m plugin.lib.writer --help                  # record a decision (owner only)
python3 -m plugin.lib.inbox --help                   # contribute as a non-owner
python3 -m plugin.lib.traces --help                  # triage the unassigned queue
```

## Stream layout

```
streams/<slug>/
  BRIEF.md            # owner writes
  ground-truth.md     # hooks write
  decided-archive.md  # owner writes
  inbox/              # anyone appends, one file per contribution
unassigned/           # writes and sessions that resolved to no stream
```

One writer per file. Appends are filesystem-atomic, so contention never needs a lock — and write
contention is itself the signal that a stream was scoped too broadly.

## Working the triage queue

A session that resolves to no stream leaves a trace rather than guessing. Strict bar, cheap
recovery — but only if the queue actually gets worked, so triage is grouped by **work item** (one
repository at one branch), never by session. Sessions are what produced the traces; nobody decides
anything per-session.

```bash
ln -s "$PWD/bin/traces" ~/.local/bin/traces   # once; the module path only resolves from the repo root

traces list                            # work items, with the command that resolves each
traces show <selector>                 # everything known about one item
traces adopt <selector>                # mint or join a stream; file every trace into it
traces dismiss <selector> --why "..."  # looked at, not worth a stream
traces prune --days 30                 # sweep what has had its call made
```

The wrapper exists because triage is wanted from wherever you already are — the queue is about work
in *other* repositories, so requiring this repo as the working directory is exactly backwards.

`adopt` reads the repository, branch, working directories, and any ticket key straight off the group
and registers them as claims. Nothing is retyped, and registering those claims is what stops the
refill: the next session on that branch resolves to the stream instead of leaving another trace.

A selector is anything that names one item — the branch, `repo@branch`, a directory for sessions
outside a checkout, or a listing index. **Prefer the selector the listing prints.** Indices renumber
as the queue drains, so a second command copied from the same listing can hit a different item and
succeed.

**A trace carries what its session was about**, not just where it ran. `SessionEnd` reads the
session's own transcript for Claude Code's generated title, falling back to the opening prompt, and
puts that one line in the trace header. Without it a queue entry is a commit hash and a directory —
enough to group, nothing to judge — and an operator who has to open files to triage will not triage.
`show` prints everything known about one item when the compressed listing is not enough.

`dismiss` requires a reason, because "judged not worth a stream" and "silently dropped" are the same
record without one. Dismissed and promoted traces both sweep on `prune`; untriaged ones never sweep
by default, since a backlog disappearing on a timer is how the operator stops learning that triage
has stalled.

## Brief sections

`Goal` · `Ground truth` · `Decided` · `Open` · `Next` · `Do not` — in that order, because the order is
the cold-start read order.

**`Decided` and `Do not` carry the weight.** They hold the reasoning that otherwise dies with the
session. An agent that re-litigates a settled decision is the failure this project exists to prevent,
and every `Decided` entry therefore requires its `Why:`.

Briefs have a ~400-word budget. `Decided` is append-only and drains by **compaction, not truncation**:
an entry archives once the decision *and its rationale* live somewhere a working agent already reads.
Never evict by age or count — that keeps trivia and drops foundations.

## Development

```bash
python3 -m unittest discover -s tests            # everything
python3 -m unittest tests.test_claims -v         # one module
```

Stdlib only, no install step. `examples/` is hand-verified real fixture data — a design stream and a
closed code review — and must stay byte-identical; tests build their vaults in `tempfile`.

Design rationale and explicitly rejected alternatives: `docs/ideas/agent-handoff-board.md`.
Schema: `docs/spec/brief-schema.md`. Working agents should read `CLAUDE.md` first.
