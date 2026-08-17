---
stream: message-board-design
title: Agent handoff board design
state: active
owner: opus-main
updated: 2026-08-13T17:05:00Z
claims:
  repo: []
  branch: []
  worktree: [/Users/peter.olds/Workplace/github.com/polds/message-board]
  issue: []
  pr: []
---

## Goal

Design a Claude Code plugin that lets one operator hold many parallel agent workstreams without
reading each conversation. Design phase only — no implementation yet. Direction and rejected
alternatives: [[agent-handoff-board]]. Artifact schema: [[brief-schema]]. Repo constraints: [[CLAUDE]].

## Ground truth

![[ground-truth]]

## Decided

*Empty — everything settled here is now written down. See [[decided-archive]].*

*Expected for a document-producing stream: its output* is *the encoding. An empty `## Decided` on a
code stream would mean the opposite — rationale leaking.*

## Open

- Dashboard surface — standalone TUI or compiled into the Claude Code statusline. Statusline is the only option with a negative pane count. `who: operator`
- Trace retention window, and whether the `unassigned/` triage queue gets worked on a schedule. An unworked queue makes strict-bar minting lossy in practice. `who: operator`
- Design-stream verification — nothing mechanical can check judgment claims. Live with it, or find a proxy. `who: agent`

## Next

1. Validate [[brief-schema]] against a code stream with real git and PR data — needs operator to supply a real stream; PLAT-1962 is a candidate. Untested: `claims`, ground-truth verification, worktree resolution.
2. Settle the Honcho boundary — it changes what the brief must carry.
3. Build `brief-read`, `brief-write`, `inbox-append` skills.
4. Wire `SessionStart` (inject best-guess brief) and `Stop`/`SessionEnd` (append ground truth) hooks.

## Do not

- Rebuild agent-to-agent DMs — `ListAgents` and `SendMessage` already do this live.
- Add threads, channels, or a moderator — a status note cannot go astray; a thread can, and the moderator is the tax for choosing conversation.
- Add personas before the substrate is typed — build them first and the artifacts become chat logs, which cannot be retrofitted with a schema.
- Ship self-mutating directives without a fitness signal, versioning, and an operator-approved diff.
- Build the event log and projector now — watch `inbox/` for the justified moment instead.
- Support multi-machine sync — one machine, filesystem is the whole transport.
- Claim `main` as a branch — it would resolve everything.
