# Agent Handoff Board

> Status: direction agreed 2026-08-13. Not yet implemented.

## Problem Statement

**How might we let one operator hold 10 parallel agent workstreams in their head at any
moment, without reading 10 conversations?**

*Framing adopted 2026-08-18:* **"Operator Cognition Framework: agents remember, so the operator
doesn't have to ask."** The memory is the agents'; the operator's freedom from asking is the
outcome. An earlier phrasing — "an agentic memory system *for operators*" — was rejected because it
inverts the brief's primary audience (the next agent, not the operator; see Recommended Direction)
and lands on the wrong side of the Honcho wall, where operator-modeling memory lives and
stream-scoped memory must never go.

## Recommended Direction

**Handoff-document first, ledger second, queue only if routing hurts.**

The canonical artifact is a `BRIEF.md` per workstream — a compressed, machine-first statement of
what is true, what was decided, what is blocked, and what is next. Its primary consumer is *the
next agent*, not the operator. An agent cold-starting reads the brief and begins; the operator's
dashboard is a rendering *of the same briefs*, not a separate thing agents maintain for the
operator's benefit. One artifact, two audiences, no duplicate write path.

This inverts the normal CQRS build order deliberately. People build the event log first and
discover the read model later; here the read model **is** the product and the log is an
implementation detail. Designing events before knowing what the projector must emit means guessing
granularity blind — and event granularity is a one-way door. So: make the brief genuinely good,
then define events that can regenerate it.

Adoption is structural rather than encouraged: **reading the board is the cheapest way for an agent
to start working.** Writing follows reading — an agent that was briefed well understands the format
and has reason to maintain it. No moderator, no personas, no social layer; those are views, and
views come after schemas.

History comes free without building the ledger: **`git init` the vault.** Briefs overwrite in
place; git keeps every prior version. This defers the entire event-sourcing question without
losing the past.

### Why not the agent-society design

The original proposal was a social board: usernames, profiles, DMs, threads, categories, a
moderator. The reasoning for cutting it, recorded so it isn't relitigated:

Conversation between agents is the most expensive serialization format available — every message
compresses a context window into prose another model must re-parse and re-infer intent from.
Human orgs are shaped by minds being opaque and non-copyable; neither constraint holds for agents
sharing a filesystem. Writing a typed artifact to shared storage beats telling another agent about
it on every axis: cheaper, inspectable, diffable, replayable, and it survives the session.

What agent societies genuinely win — adversarial separation (a critic sharing the author's context
inherits the author's blind spots), context isolation as memory management, and legibility to a
human operator — are all obtainable without a messaging topology. **Personas are a good interface
and a bad architecture.** They can be layered as a view later, but only onto a typed substrate;
build them first and the artifacts become chat logs, which cannot be retrofitted with a schema.

## Stream Identity

*Resolved 2026-08-13 after pressure-testing against stacked PRs, cross-author review, worktree reuse,
non-git workstreams, mid-session pivots, and subagent fan-out.*

### A stream is durable; everything else is a claim

Repo, branch, worktree, issue, and PR all churn during a workstream's life — branches die on merge,
stacks span several, review means checking out someone else's. So none of them can be the identity.

**A stream has a stable slug ID and holds mutable claims.** This is the same split as
`ServiceIdentity` vs `CloudRunService` in a prior infrastructure project: identity is its own thing, runtime references
it, so migrating runtime never churns identity.

```yaml
stream: plat-1962-cloudsql-review   # slug, human-typeable, unique, IMMUTABLE
state: active
owner: agent-3                       # sole writer of BRIEF.md
claims:
  repo: [platform]
  branch: [peterolds/plat-1962-review-platform905-featcrossplane-cloudsqlinstance]
  worktree: [/Users/peter.olds/wt/plat-1962]
  issue: [PLAT-1962]
  pr: [905]
```

Slug not UUID — the operator has to type it. Slugs are immutable because the slug is in the path and
renaming breaks `[[wikilinks]]`; a mutable display name lives in frontmatter. Claims live in `BRIEF.md`
frontmatter, never in a separate index — grep across a few dozen files is instant, and an index would
violate "the vault is the source of truth."

### Granularity: one stream per independently-decidable unit of work

Not per unit of code. Three mechanical splits of one change is one stream; two related Linear issues
are two streams even when stacked, because their decisions are independent.

**Write contention is the granularity signal.** Two agents needing to write one brief simultaneously is
evidence it should have been two streams. The single-writer constraint self-diagnoses the modeling
error instead of requiring it to be right up front.

### Explicit declaration is the mechanism; inference is a convenience

Git state is **intent-blind**. The same worktree and branch serve "I'm building this," "I'm reviewing
this," and "I'm debugging this" — different streams with different decisions. Claims are not exclusive;
a reviewer checking out an author's branch would otherwise resolve straight into the author's stream
and write review decisions into someone else's workstream.

Design-review and ideation streams settle it: they have **no git identity at all**, and they are a
named target workflow. The explicit path has to work standalone.

So intent arrives with the dispatch (`stream: <slug>`). Git-derived resolution is a **read-path hint
only** — never a write path.

1. Explicit declaration — always wins, and is the only path valid for writes
2. *(hint)* Worktree path match, validated: does that path still have that branch checked out?
3. *(hint)* Branch exact match
4. *(hint)* Issue key parsed from the branch name
5. No match or ambiguous → **stop**

Steps 2–4 were demoted from "convenience" to "hint" after measuring against `acme/platform`
on 2026-08-13. The environment is far more hostile to inference than assumed:

- **Review streams have no branch and no worktree.** PLAT-1962 had a Linear `gitBranchName` that was
  never created — the whole review ran through `gh api`. So two of the three named target workflows
  (design review, code review) have no local git identity at all, not just design.
- **53 worktrees on one repo**, spread across `.sculptor`, `.cursor`, `.mngr`, `.claude/worktrees`,
  `/private/tmp` scratchpads, and sibling checkouts. Several are `prunable`; the `/private/tmp` ones
  are session-scoped and vanish. Worktree paths are ephemeral hints, never durable claims.
- **Issue key to stream is not 1:1.** PLAT-1719 spans eight live worktree branches. Parsing an issue
  key resolves to many streams, not one.
- **Branch naming is inconsistent** — `PLAT-2039`, `plat-1921`, `polds/plat-1870`,
  `feat/PLAT-1719/gclb`. Any parser must be case-insensitive across at least three prefix conventions.
- A "repo, if exactly one active stream claims it" step was dropped entirely. With this much
  parallelism it would never fire.

**Read and write get different cutoffs.** Reading the wrong brief is cheap — the agent notices
immediately. Writing the wrong brief is corrupting: it silently poisons two streams and burns the
honesty signal. So reads may take a best guess (injected at `SessionStart`); writes require certainty
or refuse. A session touches multiple streams over its life, so mapping is per-write, not per-session.

Never guess on write. Unresolved writes go to `unassigned/` for triage.

### Minting a stream

Streams are minted **lazily, at first durable write** — never at session start. Most sessions produce
nothing worth keeping, and minting up front would flood the board with empty streams. By first write
the agent has enough context to *propose* a slug rather than ask blind.

**The bar:** mint when the session produces something expensive to reconstruct — a decision with its
rationale, a blocker that needs someone else, or an artifact others will build on. Not: ran tests,
read files, answered a question.

**Slug derivation is already solved.** The `start-task` skill derives a kebab-case slug from the task
and leads with the ticket ID when present (`plat-192-fix-erpc-scrape`) — identical in shape to a
stream slug. Minting hooks into that step at zero operator cost. The same derivation serves Linear
issues (identifier plus `gitBranchName`), PR reviews (repo plus PR number), and mngr or scheduled
dispatch (the task string).

**Posture: auto for structured paths, ask for ad-hoc.** Anything with a derivable slug mints silently.
A bare session in a repo — the one path where nothing can be inferred — asks once, at first durable
write, with a proposed slug to accept, edit, or skip. Friction lands only where inference genuinely
fails.

**Mint checks claims first.** A stream already claiming `pr: 905` is joined, not duplicated. This is
where `claims` earns its cost; without it, lazy minting spawns a fresh stream every time anyone
touches the same work.

### Traces: strict bar, cheap recovery

The bar is deliberately strict, so sessions that fall below it would otherwise vanish. They don't:
every session that doesn't clear the bar drops a **one-line trace** into `unassigned/` —
`<timestamp>-<session>.md`, one file per session so appends stay atomic. A trace carries just enough
to promote it later: working directory, branch, what happened, session reference.

So nothing is lost and nothing pollutes the dashboard. The cost is a triage queue that has to actually
be worked.

Traces are pruned after a retention window unless promoted into a stream. Pruning is not lossy —
**the vault is a git repo**, so swept traces remain recoverable in history. `unassigned/` is a queue,
not a graveyard: an unresolved write is still a file with content, and triage re-homes it.

**The queue's unit is the work item, not the session.** Settled 2026-08-14 after first real use, which
produced fourteen near-identical traces for four work items and a triage flow nobody would run twice.
A trace records what a *session* saw, but nothing is ever decided per session — the decision is "does
this work deserve a stream", asked once per (repository, branch). Three consequences, each fixing a
way the session-shaped queue failed:

- **Byte-identical traces are written once.** Sessions end, resume, and end again against the same
  commit, so the same observation arrives repeatedly. Only the timestamp differs, so only the
  timestamp is excluded from the content fingerprint.
- **`adopt` mints or joins from the group's own contents.** Repository, branch, every working
  directory the branch was seen in, and any ticket key become claims without being retyped. Those
  claims are also what stops the refill — the next session there resolves instead of tracing. The
  mint bar is not weakened: it exists to stop *sessions* minting automatically, and adoption is an
  operator judging a work item real.
- **`dismiss` is a first-class exit.** Without it the only ways out are promotion and age, so noise
  can never be cleared deliberately and the backlog count becomes a number the operator ignores —
  which is precisely how triage stops happening. A reason is mandatory; "dismissed" with no reason is
  indistinguishable from "deleted". Dismissed and promoted traces sweep on the same terms.
- **A trace says what its session was about.** The original definition promised "one line saying what
  happened, plus enough context to promote it later" — but the hook only ever wrote the context half,
  so a queue entry could show five negatives and a directory path and give the operator no way to
  judge it. `SessionEnd` now reads the session's own transcript for Claude Code's generated title,
  falling back to the opening prompt. Both are observations, not inference, which keeps the hook
  inside its remit. Where nothing was captured the display says so rather than showing a sentinel:
  "never recorded" and "recorded and empty" are different facts, and only the first one is a defect
  in what the hook observes.

Selectors are stable identifiers (branch, `repo@branch`, or directory), not listing indices. Indices
renumber as the queue drains, and a shifted-but-still-valid index fails by succeeding on the wrong
work item.

### Ownership and contention

**One writer per file, three surfaces:** the stream owner writes `BRIEF.md` and `decided-archive.md`,
hooks write `ground-truth.md`, and everyone else appends a new file to `streams/<id>/inbox/`.
New-file-per-contribution is filesystem-atomic, so there is no contention. The owner folds the inbox
into the brief.

Ground truth is a separate file rather than a `BRIEF.md` frontmatter block for exactly this reason — a
hook writing frontmatter while the owner rewrites the body is the read-modify-write conflict that
single-writer exists to prevent. Full layout: [[brief-schema]].

This is what lets an author and a reviewer share one decision unit without sharing a writer.

**Subagents never write.** They report to their parent; only the parent writes. Preserves single-writer
and matches the orchestrator topology.

### Lifecycle rules

- Archived streams do not participate in resolution — otherwise a reused branch name resolves new work
  into a closed stream. Reopening is explicit (a bug found weeks after merge is a real case).
- **Archived streams stay readable.** Not resolvable is not the same as not readable: a closed
  stream's `## Do not` is often its most valuable content ("don't revive this branch — stale base,
  superseded scope"), and losing it forfeits the protection against re-proposing settled work.
- `main` is never a valid branch claim. It would resolve everything.
- Claims are registered on write, not inferred on read. The `start-task` worktree flow is the natural
  registration point.

Degradation is graceful: no claims registered means work lands in `unassigned/`. Annoying, not wrong —
the correct failure direction.

## Key Assumptions to Validate

- [ ] **Agents are honest narrators about their own state.** *Test:* the MVP itself is the probe —
      agents write a claimed state, hooks independently write ground truth (branch, commit, test
      exit code, PR existence), dashboard flags mismatches. If claims prove unreliable, agent
      judgment cannot be the substrate and hooks must carry more weight than planned.
      *First real run (PLAT-1962, 2026-08-13) fired and was benign:* the issue checklist claimed
      "left a GitHub review" while `pulls/905/reviews` was empty — a closing comment had been posted
      instead, which that checklist item permits. **Verification produces ambiguous signals, not
      verdicts.** It needs an adjudication step; a dashboard that paints this red teaches the
      operator to ignore red.
- [ ] **The bottleneck is context rebuild, not decision-making.** *Test:* for one week, log every
      status check — was the follow-on action a decision only the operator could make, or dispatch
      that could have been delegated? If mostly decisions, the winning feature is decision-queuing
      with pre-computed options, not a status board.
- [ ] **A written brief can substitute for a live session's context.** *Test:* cold-start an agent
      on a stream using only its brief. Does it redo work, re-litigate settled decisions, or
      contradict prior choices? That failure rate is the measured value of the "Decided" and
      "Do not" sections.
- [ ] **The operator will actually open it.** *Test:* the incumbent is asking a live session, and
      it is free and always available. If the surface isn't already on screen for other reasons, it
      loses. Watch for unprompted use in week one.

## MVP Scope

### The artifact — `vault/streams/<stream-id>/BRIEF.md`

Frontmatter: `stream` (immutable slug), `title`, `state` (active|blocked|review|idle|archived),
`owner`, `claims` (see Stream Identity), `updated`, `needs_operator`, `claim_verified`.

Body, in priority order:

- **Goal** — why this stream exists. Stable across sessions.
- **Ground truth** — hook-written only. Branch, last commit, test status, PR. Agents never write here.
- **Decided** — append-only, one line each: decision plus rationale. *The highest-value section.*
  This is precisely the implicit context that gets lost between parallel agents.
- **Open** — blockers and questions, each tagged with who can answer (agent vs operator).
- **Next** — ordered, concrete, immediately actionable.
- **Do not** — tried and rejected, with reason. Prevents re-litigation.

### In scope

- Vault at a configured external path, `git init`'d for free history
- Lazy stream minting at first durable write: auto for structured dispatch, prompt for ad-hoc;
  claims checked before minting and registered on write
- `start-task` wiring — reuse the slug it already derives at branch-naming time
- Traces to `unassigned/` for sessions below the mint bar, one file per session
- Three skills: `brief-read` (cold start), `brief-write` (owner updates at decisions and exit), and
  `inbox-append` (non-owners contribute without contention)
- `SessionStart` hook injects the best-guess stream's brief; `Stop`/`SessionEnd` hook appends ground
  truth only. Writes require an explicit stream or fall to `unassigned/`
- Minimal terminal renderer: all streams, state, `needs_operator` first, claim-vs-truth mismatches
  flagged, unfolded inbox counts visible
- Escalation: `needs_operator` is the only operator-blocking channel

### Out of scope for MVP

Everything else.

## Not Doing (and Why)

- **Agent-to-agent DMs** — `ListAgents` + `SendMessage` already do this live in the harness. Wrap
  later; never rebuild.
- **Threads and channels** — a status note cannot go astray. Threads can, which is why they would
  need a moderator. Not paying that tax.
- **Moderator workflow** — treats a symptom of having chosen conversation as the primitive.
  Removed along with threads.
- **Usernames, profiles, personas** — good interface, bad architecture. Layer as a view once the
  substrate is typed.
- **Self-mutating directives via self-improvement loops** — worst tail risk in the original design.
  Without a fitness signal, self-improvement optimizes for verbosity and self-consistency;
  degradation is silent and correlated across every agent simultaneously. Requires versioned
  directives, a measured signal, and an operator-approved diff before reconsidering.
- **Event log plus projector** — the right long-term spine, the wrong first move. Git on the vault
  buys history now. Watch `streams/<id>/inbox/` for the pressure to return: it is an append-only log
  in miniature, and when folding it by hand stops scaling, that is the justified moment to build the
  projector rather than the speculative one.
- **Task queue and claiming** — routing is not the current constraint. Revisit when parallel work
  exceeds available agents. Note its one real advantage when revisiting: it is the only design with
  structural adoption, since an agent must write to the board to get work.
- **Multi-machine sync** — one machine. The filesystem is the entire transport.

## Open Questions

- **Stream identity and session mapping** — *resolved, see above.*
- **Dispatch friction** — *resolved, see Minting.* Structured paths auto-mint from slugs their tooling
  already derives; only bare ad-hoc sessions prompt, and only at first durable write.
- **Trace retention window.** How long an untriaged trace survives before pruning, and whether the
  triage queue is worked on a schedule or opportunistically. If the queue isn't worked, strict-bar
  minting quietly becomes lossy in practice even though git retains everything.

  *Narrowed 2026-08-14 by first real use.* The cost of working it was the live half of this question,
  and that part is answered: the queue groups by work item, `adopt` builds the stream from the
  traces' own contents, and `dismiss` gives noise a deliberate exit — fourteen traces cleared in four
  commands. What remains genuinely open is the *window*, and it is now a smaller question, because
  what ages out is only what nobody made a call on. See "Traces: strict bar, cheap recovery".
- **Honcho overlap** — *resolved 2026-08-13. **Honcho owns the operator; the brief owns the work.
  One-way wall.*** Briefs never read Honcho as authority on project state; anything stream-scoped
  lives only in the vault. Honcho keeps stable cross-project preferences that shape *how* an agent
  works (drafts-by-default PRs, branch naming, parallelize where feasible).

  Settled by measurement, not preference. Auditing Honcho's record of this very design session found:
  every project conclusion timestamped to **turn one**, with none of the ~15 subsequent decisions
  recorded; five cut features (DMs, threads, usernames, moderator, self-improving profiles) still
  stored as live desires and re-injected every turn; and two **fabricated** facts — a branch that was
  never created, and a stream slug lifted from an illustrative YAML example and asserted as real.

  The mechanism is not fixable by configuration: Honcho models the *person* and treats everything said
  as evidence about them, including hypotheticals, without revising when a decision reverses. The
  contrast that proves the point is the same user's `[openwolf:decision-log]` entries — dated,
  rationale-bearing, artifact-linked, and excellent, because they were **deliberately authored rather
  than inferred**. Honcho is a good store and an unreliable inferrer, which is exactly why decisions
  must be written to a brief and never inferred from conversation.
- **Dashboard surface** — *resolved 2026-08-20. **The Claude Code statusline, plus the existing
  one-shot dashboard for detail. No background TUI.***

  The statusline is the only surface the operator never has to choose to look at, and an escalation
  nobody looks at is an escalation that did not happen. `plugin/statusline.py` prints one line —
  needs-you count, the stream the dashboard would list first, inbox, adjudications, untriaged traces
  — computed from the same `build_view` and the same `sort_key` the dashboard uses, so the two
  surfaces can never disagree about what is most urgent. Everything the line cannot fit is one
  `python3 -m plugin.dashboard` away, and that render already existed.

  The TUI was rejected on cost, not on capability. It buys navigation of a read-only view and pays a
  pane, a refresh loop, a key map, and a process that can die without saying so. Three segments are
  load-bearing and should survive any rewrite: the needs-you segment renders even at zero (every
  other segment is dropped when empty, so a silent line would be ambiguous between "clear" and "not
  running"); segments drop whole rather than truncate (a shortened slug is indistinguishable from a
  real one); and adjudications are counted as "to adjudicate", never as failures — the statusline is
  the surface most likely to train a reflex, and that reflex must not be to dismiss.

  Reopen only if glancing at the line stops being enough to know what to open.
- **Untrusted content path.** Brief content shapes agent behavior. Low risk today; becomes the
  primary attack surface the moment an agent writes web, issue, or repo content into a brief. One
  design line now beats a retrofit later.
