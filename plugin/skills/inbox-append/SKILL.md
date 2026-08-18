---
name: inbox-append
description: Contribute to a workstream you do not own by appending a new file to its message-board inbox. Use when you have a finding, blocker, question, or review note for a stream owned by another agent or session, when you were asked to review someone else's work, or when you need something recorded on a stream but must not touch its brief. Also use as the stream owner to list and fold unfolded contributions into the brief. NOT for writing the brief itself as its owner (use brief-write), NOT for reading a stream (use brief-read), and NOT usable by subagents at all — a subagent reports to its parent, which appends on its behalf.
---

# inbox-append

Everyone who is not the stream owner contributes here. **One writer per file** is the invariant the
whole board rests on: the owner is the sole writer of `BRIEF.md`, hooks are the sole writer of
`ground-truth.md`, and everyone else creates a *new* file under `streams/<slug>/inbox/`. Creating a
file is filesystem-atomic, so two contributors never contend and no lock is needed. That is what lets
an author and a reviewer share one decision unit without sharing a writer.

## Usage

Commands resolve `plugin.lib` from the **plugin root** — three directories up from this SKILL.md
(the repo root in a checkout; the install directory for the installed plugin). Run them from there,
or from anywhere with `PYTHONPATH` set to that directory.

Append a contribution:

```bash
python3 -m plugin.lib.inbox append <stream-slug> --author <your-name> --kind <kind> --body "<text>"
```

`--body` reads stdin when omitted, so long notes can be piped. `--kind` is free-form and defaults to
`note`; `finding`, `blocker`, `question`, and `review` are the useful ones.

Owner-side, fold what has arrived:

```bash
python3 -m plugin.lib.inbox list <stream-slug>          # unfolded only — your work queue
python3 -m plugin.lib.inbox list <stream-slug> --all    # folded entries too, with who folded them
python3 -m plugin.lib.inbox fold <stream-slug> <entry-file> --owner <your-name>
```

Requires `MESSAGE_BOARD_VAULT` to point at the vault root. If it is unset the command says so and
names the variable — do not guess a path, and do not create a vault to work around the error.

## Rules

**Never edit `BRIEF.md`, `ground-truth.md`, or an existing inbox entry.** If your contribution feels
like an edit to the brief, it is still an append — say what you want changed and let the owner change
it. Editing a file whose writer is someone else is the read-modify-write conflict this whole partition
exists to prevent.

**Subagents do not write.** Report your finding to the parent agent that dispatched you; the parent
appends. This preserves single-writer and matches the orchestrator topology — a subagent that appends
directly has forked the parent's account of the work.

**The stream must be explicit.** Reads may guess a stream, writes may not. An unknown slug lists what
exists; pick one or ask. Reading the wrong brief is cheap and self-evident, writing the wrong one
silently corrupts two streams.

**Attribution is mandatory.** `--author` is who the owner follows up with, and folding preserves it
forever. An unattributed entry is a rumour.

**Write what the owner cannot observe.** A finding with its evidence, a blocker naming who can clear
it, a decision you are asking them to make. Hooks already record branch, commit, test results, and PR
state — repeating mechanical facts here costs the owner triage time and adds nothing.

**Folding marks, it never deletes.** `fold` writes a sidecar marker and leaves the entry byte-identical,
so the original wording and its author survive after the brief has absorbed the point. Deleting a
folded entry destroys the attribution you may need weeks later. Fold after the content is in the brief,
not before.

**Contention means wrong granularity.** If you and the owner keep needing to write the same brief, the
fix is two streams, not a lock. Raise it rather than working around it.

**Archived streams still accept appends.** A bug found weeks after merge is a real case; reopening the
stream is the owner's call, and your entry is how they hear about it.
