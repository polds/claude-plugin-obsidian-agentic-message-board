---
name: brief-write
description: Record a decision, blocker, or next step into a workstream's handoff brief in the message-board vault, as that stream's owner. Use when a decision has been settled and its rationale would otherwise die with the session, when work is blocked on someone else, when the next moves change, or when an approach is rejected and should not be re-proposed. Also use to archive a decided entry once it has been written into a spec, ADR, or CLAUDE.md. NOT for cold-starting or checking what was decided (use brief-read), NOT for contributing to a stream you do not own (use inbox-append), NOT for recording branches, commits, test results, or PR state — hooks write those — and NOT usable by a subagent, which reports to its parent instead.
---

# brief-write

Owner-side updates to one stream's `BRIEF.md`. You are writing for the **next agent**, not for the
operator and not for yourself later — the test of every entry is whether someone with no memory of
this session can act on it.

## Usage

Commands resolve `plugin.lib` from the **plugin root** — three directories up from this SKILL.md
(the repo root in a checkout; the install directory for the installed plugin). Run them from there,
or from anywhere with `PYTHONPATH` set to that directory.

```bash
python3 -m plugin.lib.writer decide  <slug> "<decision>" "<why>"
python3 -m plugin.lib.writer entry   <slug> "Open|Next|Do not" "<text>"
python3 -m plugin.lib.writer section <slug> "Open|Next|Do not|Goal" "<content>"   # '-' reads stdin
python3 -m plugin.lib.writer archive <slug> "<match>" "<pointer>"
```

Add `--reopen` to any of these to write to an archived stream. See the reopen rule below.

Requires `MESSAGE_BOARD_VAULT`. If it is unset the command says so and names the variable — do not
guess a path and do not create a vault to work around the error.

## The bar for writing at all

Write when the session produced something **expensive to reconstruct**:

- a decision *with its rationale*
- a blocker that needs someone else
- an artifact others will build on

Not: ran tests, read files, answered a question. Those are below the bar and belong nowhere — the
brief stays cheap to read precisely because most sessions add nothing to it.

## Rules

**You must name the stream. Never infer one.** Reading the wrong brief is cheap and self-evident;
writing the wrong one silently corrupts two streams and burns the honesty signal. If you cannot name
the slug with certainty, pass an empty slug — the content is filed in `unassigned/` for triage with
enough context to promote later. Report that it was filed there; do not retry with a guess.

**Every decision carries `Why:`.** The command rejects a decision without a rationale, and it rejects
placeholders like "n/a" or "tbd" for the same reason: a decision whose reason is gone gets
re-litigated the moment its context is. If you cannot state the reason, you have not finished making
the decision.

**Never write `## Ground truth`.** Branch, commit, test result, and PR state come from hooks. The gap
between what you claimed and what actually happened is the honesty probe this whole system runs on,
and it disappears the moment the same writer produces both sides. The command refuses; do not route
around it by editing `ground-truth.md` with `Write`.

**Archived streams refuse writes. Reopening is a separate act.** Naming the slug proves you know
which stream, not that you meant to revive a closed one. A write that landed without flipping the
state would leave the dashboard reporting a closed stream whose content is actively changing, and an
operator surface that lies is worse than no surface. Pass `--reopen` to flip it back to `active` —
which is recorded in `## Decided` — then write. If reopening is not what you meant, the content
probably belongs to a new stream.

**`## Decided` is append-only.** Entries leave only through `archive`, which requires a pointer to
where the decision *and its rationale* now live. Compaction, not truncation — never drop entries by
age or count, which keeps trivia and loses foundations.

**Decided is what we are doing; Do not is what will otherwise be re-proposed.** A rejected
alternative goes in `## Do not` even though rejecting it was also a decision. The duplication is
deliberate: the two sections serve different reads.

**Link the artifact inline.** Any file, spec, PR, or doc a decision produced or depends on is
`[[wikilinked]]` where it is mentioned. A decision recorded without a way to reach what embodies it
leaves the next agent with a conclusion and no evidence.

**One writer per file.** You write `BRIEF.md` and `decided-archive.md` for streams you own. Hooks own
`ground-truth.md`; non-owners append to `inbox/` via `inbox-append`. Subagents never write — report to
your parent and let the parent decide.

**Keep it to one screen.** ~400 words total, `## Decided` included. If a write pushes it over, the fix
is archiving what is already encoded elsewhere, not trimming the newest entry.

## Contention is a signal

If two agents need to write the same brief at the same time, the stream should have been two streams.
Do not coordinate around it — say so, and split the stream.
