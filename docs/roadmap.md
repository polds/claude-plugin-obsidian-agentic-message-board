# Roadmap

## North Star

**Operator Cognition Framework: agents remember, so the operator doesn't have to ask.**

Concretely: one operator holds every parallel workstream in their head at any moment, without
reading a single conversation. The memory belongs to the agents — briefs written for the next
agent — and the operator's not-having-to-ask is the payoff, rendered from those same briefs. Any
feature that strengthens one half by weakening the other is off-axis regardless of its merits
(see `CLAUDE.md`).

The North Star is *measured*, not asserted, by the four assumptions recorded in
[agent-handoff-board.md](ideas/agent-handoff-board.md#key-assumptions-to-validate):

1. Agents are honest narrators about their own state — probed by the claim-vs-ground-truth gap.
2. The bottleneck is context rebuild, not decision-making — probed by logging what each status
   check actually led to.
3. A written brief can substitute for a live session's context — probed by cold-starting an agent
   on a brief alone and counting re-litigated decisions.
4. The operator will actually open it — probed by unprompted use in week one against the free,
   always-available incumbent: just asking the session.

Every phase below exists to get one or more of these measured.

## Now — v0.1.x: prove it live

The MVP is built and packaged; what it has not had is contact with reality. Nothing new ships
until these run, because each is designed to invalidate work before more is stacked on it.

- **Checkpoint C — live run.** One real workstream end-to-end against a scratch vault:
  `ground-truth.md` populated with no agent involvement, then a second session cold-starts from
  the brief alone and continues without contradicting it. (Probes assumptions 1 and 3.)
- **Installed-plugin run.** The same, but through `/plugin install message-board@polds` in a
  consumer project rather than this repo's dogfood symlinks. The install path shipped two
  would-be-silent defects already (see `CHANGELOG.md` 0.1.0 "Fixed"); only a real installed run
  proves there is no third. Known gap to close on the way: skills print commands without saying
  they must run from the plugin root, which the dogfood layout masks.
- **Checkpoint D — a week of real use** before anything beyond MVP scope, logging every status
  check the board did or did not absorb. (Probes assumptions 2 and 4.)

## Next — v0.2: the open decisions

Deliberately open questions, to be answered with Checkpoint D's data rather than by preference.
Recorded in [agent-handoff-board.md](ideas/agent-handoff-board.md#open-questions); answers land
there first.

- **Dashboard surface.** Standalone TUI vs. compiled into the Claude Code statusline. The
  statusline is the only option with a negative pane-of-glass count, which bears directly on
  assumption 4 — a surface that is not already on screen loses to just asking.
- **Trace retention window.** How long an untriaged trace survives, and whether the queue is
  worked on a schedule or opportunistically. An unworked queue makes strict-bar minting quietly
  lossy regardless of git retention.
- **Untrusted content path.** Briefs shape agent behavior, and today every writer is trusted. One
  design line before agents ever write web/issue/repo content into a brief beats a retrofit after.
  (See `SECURITY.md`.)

## Later — watchpoints, not commitments

Each of these was explicitly cut, with its revisit condition recorded. The condition, not
enthusiasm, is what reopens it:

| Cut feature | Reopens when |
|---|---|
| Event log + projector | Folding `streams/<id>/inbox/` by hand stops scaling — the inbox is already an append-only log in miniature, and that pressure is the justified moment |
| Task queue and claiming | Parallel work exceeds available agents; note it is the only design with structural adoption |
| Personas, profiles | A typed substrate exists to layer them onto as *views*; never as architecture |
| Multi-machine sync | The operator genuinely runs from more than one machine; until then the filesystem is the entire transport |
| Agent-to-agent DMs | Never — the harness already provides messaging (`ListAgents`/`SendMessage`); wrap, don't rebuild |

## Non-goals, permanently

The social-board design (threads, channels, moderator, self-mutating directives) stays rejected
for the reasons recorded in
[agent-handoff-board.md](ideas/agent-handoff-board.md#why-not-the-agent-society-design).
Conversation is the most expensive serialization format available; this project exists because a
typed artifact beats it on every axis.
