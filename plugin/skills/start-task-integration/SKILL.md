---
name: start-task-integration
description: Register a message-board stream when starting isolated work via /start-task or any worktree-based dispatch. Use immediately after creating a worktree and branch for a task, so the stream claims that branch and worktree from the outset. Also use when picking up a Linear issue or a PR review that will run in its own checkout. NOT for ad-hoc sessions with no worktree (minting there is lazy and happens at first durable write), NOT for reading a brief (use brief-read), and NOT for recording decisions (use brief-write).
---

# start-task-integration

Bridges `/start-task` to the message board. The point is that **structured dispatch costs the operator
nothing**: `start-task` already derives the slug, so the stream can be registered without anyone
typing it twice.

## Why this exists

Explicit stream declaration is the only path permitted for writes, which raises the obvious risk that
declaration decays in practice and the whole board degrades to `unassigned/`. It doesn't, because
every structured dispatch path already carries a slug at dispatch time. This skill is what collects it.

Measured against a real repo, inference cannot be relied on instead: review streams often have no
branch and no worktree at all, one issue can span eight branches, and a busy checkout carries dozens
of worktrees across half a dozen tools, many of them ephemeral. Registering the claim at creation is
cheap and exact; deriving it later is neither.

## The slug is already derived — reuse it

`start-task` step 2 produces a kebab-case slug leading with the ticket ID when present, e.g.
`plat-192-fix-erpc-scrape`, and names the branch `polds/<slug>`. **That slug is the stream slug.**
Do not invent a second naming scheme; a divergence here means the branch and the stream disagree
about what the work is called.

## Usage

Registration is still **lazy** — creating a worktree is not itself durable work, and a stream minted
at dispatch for a task that produces nothing is exactly the empty-stream flooding the bar exists to
prevent. So do not mint here. Instead, carry the dispatch context into the first durable write.
(As in every skill here, `python3 -m plugin.lib.…` resolves from the **plugin root** — three
directories up from this SKILL.md; run from there or set `PYTHONPATH` to it, which matters in this
skill especially, because the command below runs from the task's new worktree, not from a
message-board checkout.)

```bash
python3 -m plugin.lib.mint write --kind decision \
  --mode start-task \
  --stream "<the slug start-task derived>" \
  --branch "polds/<slug>" --worktree "$PWD" --repo "<owner/name>" \
  --summary "<what was decided>" --why "<why>" \
  --goal "<one sentence: why this stream exists>"
```

**Pass the slug as `--stream`, not `--slug`.** `--slug` only supplies the edited value on the ad-hoc
prompt path; in structured dispatch it is ignored and the slug gets re-derived from `--task` or the
summary — which is precisely the branch/stream divergence this skill exists to prevent. `--stream`
names the stream exactly, minting it if absent and joining it if present.

`--mode start-task` mints **silently** when that first write lands — no prompt, because the slug is
already known. Only a bare ad-hoc session with no derivable slug asks, and it asks once.

`python3 -m plugin.lib.mint bar` prints the bar as the code enforces it.

## Rules

**Claims are registered at creation, not inferred later.** The worktree path and branch belong on the
stream the moment they exist. Worktree paths are ephemeral hints that go stale as tooling recreates
them, so capturing them while they are certainly correct is the only time it is free.

**Never claim `main`.** It would resolve every stream in the repo, and therefore none.

**An existing stream claiming the same issue or PR is joined, not duplicated.** `mint.durable_write`
checks claims before creating anything. One exception matters: a stream that is *archived* is never
joined, even on an exact PR match — reviving a stream whose conclusions are final is worse than
starting a fresh one.

**If the vault is unconfigured, do nothing and say nothing.** `start-task` must work unchanged for
anyone who has not set `MESSAGE_BOARD_VAULT`. The board is additive; it never gates the work.

**Granularity is one stream per independently-decidable unit of work**, not per branch. A stack of
three mechanical splits is one stream. Two related tickets are two streams even when stacked, because
their decisions are independent. If two agents end up needing to write one brief at once, that is the
signal it should have been two streams.
