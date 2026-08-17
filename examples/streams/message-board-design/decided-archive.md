---
stream: message-board-design
note: Decisions encoded elsewhere. Archived from BRIEF.md; each carries a pointer to its new home.
---

- 2026-08-13 — Handoff document first, event ledger second, task queue only if routing hurts. → [[agent-handoff-board]] "Recommended Direction"
- 2026-08-13 — Vault is external, at a configured path, and `git init`'d. → [[CLAUDE]] constraint 1, [[agent-handoff-board]] "Recommended Direction"
- 2026-08-13 — Obsidian is storage; the operator reads a rendered dashboard. → [[CLAUDE]] constraint 4
- 2026-08-13 — The brief's primary consumer is the next agent, not the operator. → [[CLAUDE]] constraint 5
- 2026-08-13 — Cut the agent-society layer: personas, threads, DMs, moderator, self-mutating directives. → [[agent-handoff-board]] "Why not the agent-society design"
- 2026-08-13 — Hooks write mechanical ground truth; skills write judgment. → [[CLAUDE]] constraint 6
- 2026-08-13 — A stream is a durable immutable slug holding mutable claims. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Granularity is one stream per independently-decidable unit of work. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Session-to-stream mapping is explicit-first; git inference is a convenience. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Reads may guess the stream; writes must be certain or fall to `unassigned/`. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Owner is sole writer of `BRIEF.md`; others append to `inbox/`; subagents never write. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Ground truth lives in its own file with the hook as sole writer. → [[brief-schema]] "Stream directory"
- 2026-08-13 — `## Decided` entries archive once the decision *and its rationale* are encoded elsewhere; never evict by age or count. → [[brief-schema]] "Size budget and compaction"
- 2026-08-13 — The claim-vs-ground-truth honesty probe only functions on code streams; design streams have no mechanical evidence. → [[agent-handoff-board]] "Key Assumptions to Validate"
- 2026-08-13 — Ground-truth verification yields ambiguous signals, not verdicts, and needs an adjudication step. → [[agent-handoff-board]] "Key Assumptions to Validate"
- 2026-08-13 — Mint lazily at first durable write; auto for structured dispatch, prompt for ad-hoc; strict bar plus traces to `unassigned/`. → [[agent-handoff-board]] "Minting" and "Traces", [[CLAUDE]] "Minting"
- 2026-08-13 — Git-derived resolution is a read-path hint only, never a write path. → [[agent-handoff-board]] "Stream Identity"
- 2026-08-13 — Honcho owns the operator, the brief owns the work; one-way wall, no reads of Honcho for project state. Settled by auditing Honcho's own record of this session: conclusions frozen at turn one, five cut features still stored as live desires, two fabricated facts. → [[agent-handoff-board]] "Open Questions", [[CLAUDE]] "Do not rebuild what the harness already provides"
