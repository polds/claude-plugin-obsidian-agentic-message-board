# BRIEF.md schema

Draft 1, 2026-08-13. Instantiated and tested against `examples/streams/message-board-design/`.

## Stream directory

```
streams/<slug>/
  BRIEF.md            # stream owner is sole writer
  ground-truth.md     # hooks are sole writer
  decided-archive.md  # stream owner; decisions that have been encoded elsewhere
  inbox/              # anyone appends; new file per contribution
    2026-08-13T1642-reviewer-7.md
```

**Three surfaces, three writers, zero contention.** This partition is the schema's load-bearing
property. Ground truth lives in its own file rather than in `BRIEF.md` frontmatter because a hook
writing frontmatter while the owner agent rewrites the body is a read-modify-write conflict — the
exact failure the single-writer rule exists to prevent. `BRIEF.md` transcludes it with
`![[ground-truth]]`, which Obsidian renders natively and a dashboard can resolve trivially.

## BRIEF.md frontmatter

```yaml
---
stream: message-board-design      # immutable slug; also the directory name
title: Agent handoff board design # mutable display name
state: active                     # active | blocked | review | idle | archived
owner: opus-main                  # sole writer
updated: 2026-08-13T17:05:00Z
claims:
  repo: []
  branch: []
  worktree: [/Users/peter.olds/Workplace/github.com/polds/message-board]
  issue: []
  pr: []
---
```

Deliberately absent:

- **`needs_operator`** — derived, not stored. The dashboard greps `## Open` for `who: operator`.
  Storing it duplicates state that can disagree with the body.
- **`claim_verified`** — computed live by comparing `BRIEF.md` against `ground-truth.md`. Storing a
  verification result means someone has to write it, and that someone is the party being verified.
- **`confidence`** — noise. Verification is the signal.

## Body sections

Fixed order. The order is the cold-start read order, so it is part of the contract.

### `## Goal`

One to three sentences. Why the stream exists. Stable across sessions — if it changes often, the
granularity is wrong.

### `## Ground truth`

Transclusion only: `![[ground-truth]]`. Agents never write here.

### `## Decided`

Append-only. One line each: `- YYYY-MM-DD — <decision>. Why: <reason>.`

The rationale is not optional. A decision without its reason gets re-litigated the moment its
context is gone, which is the failure this section exists to prevent.

### `## Open`

Each entry tagged with who can resolve it: `who: agent` or `who: operator`. Operator-tagged entries
are the escalation channel and the only thing that blocks.

### `## Next`

Ordered, concrete, immediately actionable. Not a roadmap — the next few moves.

### `## Do not`

Tried or considered and rejected, with the reason. Boundary against `## Decided`: **Decided records
what we are doing; Do not records what will otherwise be re-proposed.** A rejected alternative
belongs here even though rejecting it was also a decision — the duplication is deliberate, because
the two sections serve different reads.

## Artifact links

Any file, doc, PR, or spec a decision produced or depends on is linked inline as `[[wikilink]]` at
the point it is mentioned — not collected into a separate section. Inline keeps the size budget and
makes the vault a graph rather than a pile.

Cold-start test that motivated this: a brief can record "validate the schema" without ever saying
where the schema lives, and a fresh agent then has decisions with no way to reach the artifacts that
embody them.

## Size budget and compaction

Target: **one screen, ~400 words total**, `## Decided` included. Excluding the unbounded section from
the budget exempts the problem instead of solving it.

### The compaction rule

**A `## Decided` entry earns its place only while it is not yet encoded elsewhere.** Once the decision
*and its rationale* live somewhere a working agent will read anyway — `CLAUDE.md`, a spec, an ADR, the
idea doc — the entry moves to `decided-archive.md` with a pointer to its new home.

Both halves are required. Encoding the decision without the rationale does not qualify; a decision
whose reason is gone gets re-litigated, which is exactly what this section prevents.

So decisions flow: **made → `## Decided` → encoded into an artifact → archived with a pointer.**
`## Decided` is a staging buffer for what has been settled but not yet written down anywhere — which
is precisely the context that dies with a session, and therefore the highest-value thing to protect.

This inverts the default trajectory: as a stream matures, its brief shrinks rather than grows.

### Expected asymmetry

Document-producing streams drain `## Decided` almost completely, because their output *is* the
encoding. Code streams retain far more, because **code encodes what was decided but rarely why.** A
code stream whose `## Decided` is empty is a signal that rationale is being lost, not that the stream
is tidy.
