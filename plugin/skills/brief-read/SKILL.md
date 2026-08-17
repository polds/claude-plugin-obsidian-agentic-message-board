---
name: brief-read
description: Read a workstream's handoff brief from the message-board vault to cold-start on that work. Use when resuming a workstream, when told to continue an existing stream, when a stream slug is mentioned, or before making changes to work someone else started. Also use to check what was already decided or rejected on a stream. NOT for writing or updating a brief (use brief-write), NOT for contributing as a non-owner (use inbox-append), and NOT for browsing all streams (use the dashboard).
---

# brief-read

Cold-start on a workstream by reading its brief. **This should be the cheapest possible way to start
working** — if reading a brief costs more than re-deriving context from the repo, the loop is broken
and that is a bug worth reporting, not working around.

## Usage

```bash
python3 -m plugin.lib.brief_read <stream-slug>
```

Requires `MESSAGE_BOARD_VAULT` to point at the vault root. If it is unset the command says so and
names the variable — do not guess a path, and do not create a vault to work around the error.

## What you get

The brief's six sections in canonical order, with `![[ground-truth]]` expanded inline:

| Section | What it is | How to treat it |
|---|---|---|
| **Goal** | Why the stream exists | Stable. If your task contradicts it, stop and ask. |
| **Ground truth** | Branch, commit, tests, PR — written by hooks | Machine-observed. Trust over any prose claim. |
| **Decided** | Settled decisions, each with its rationale | Do not relitigate. Build on these. |
| **Open** | Blockers and questions, tagged `who:` | `who: operator` entries block; surface, never guess. |
| **Next** | Ordered, immediately actionable | Your likely starting point. |
| **Do not** | Tried and rejected, with reasons | Check before proposing anything. |

## Rules

**Read `## Do not` before proposing anything.** It exists specifically to stop rediscovery of dead
ends. Proposing something listed there is the failure this whole system was built to prevent.

**Ground truth beats claims.** When the prose says one thing and `## Ground truth` says another,
believe ground truth and flag the gap — that mismatch is a signal, not noise to reconcile silently.

**Archived streams are readable.** A closed stream still answers "why didn't this land?" and its
`## Do not` is often its most valuable content. Reading one is fine; resuming it needs an explicit
reopen.

**An unknown slug is not a prompt to guess.** The command lists available streams. Pick one or ask.

**Empty `## Decided` means different things.** On a document-producing stream it is expected — the
output *is* the encoding, and the archive holds the history. On a code stream it means rationale is
being lost, which is worth flagging.
