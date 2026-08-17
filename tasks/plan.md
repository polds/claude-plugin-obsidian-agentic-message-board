# Implementation Plan: Agent Handoff Board MVP

## Overview

Build the MVP described in `docs/ideas/agent-handoff-board.md` against the schema in
`docs/spec/brief-schema.md`: a Claude Code plugin where each workstream owns a `BRIEF.md` in an
external Obsidian vault, agents cold-start by reading it, hooks record mechanical ground truth, and
the operator reads a rendered dashboard instead of asking each session for status.

Two fixture streams already exist at `examples/streams/` — one design stream, one code-review stream.
They are real, hand-verified, and serve as the test corpus from Task 1 onward. No task needs to invent
test data.

## Architecture Decisions

- **Markdown-first, code where determinism is required.** Skills own contracts; a small stdlib-only
  Python library handles frontmatter parsing, claim resolution, verification, and rendering — the four
  places where inconsistent agent output would be a correctness bug rather than a style issue.
- **Stdlib only, `unittest`, zero install.** A plugin that needs `pip install` has broken its install
  story. Test command: `python3 -m unittest discover -s tests -v`.
- **One writer per file** — owner writes `BRIEF.md` and `decided-archive.md`, hooks write
  `ground-truth.md`, everyone else appends to `inbox/`. Every task preserves this partition.
- **Read path before write path.** Adoption is structural: reading must be the cheapest way for an
  agent to start. Tasks are ordered so reading works before anything writes.
- **Scratch vault only.** No task, test, or manual check touches the operator's working vault.

## Dependency Graph

```
Task 1  vault resolution + frontmatter parsing
   │
   ├── Task 2  brief-read ──────────► CHECKPOINT A (cold-start proof)
   │
   ├── Task 3  brief-write ──┬─────── Task 4  size-budget guard
   │                         │
   │                         └─────── CHECKPOINT B (round-trip)
   │
   ├── Task 5  claims lookup + join-or-mint
   │      └── Task 6  ad-hoc mint prompt
   │      └── Task 7  traces to unassigned/
   │
   ├── Task 8  SessionEnd hook → ground-truth.md
   │      └── Task 9  SessionStart hook → inject brief   ► CHECKPOINT C (live run)
   │
   ├── Task 10 inbox-append            (independent after Task 1)
   ├── Task 11 dashboard renderer      (independent after Task 1)
   │
   └── Task 12 start-task wiring ──── Task 13 plugin packaging ► CHECKPOINT D
```

## Phase 1: Read Path

### Task 1: Vault resolution and brief parsing library

**Description:** Resolve the external vault path from configuration and parse stream files into
structured data. Foundation for every later task, but delivered as a vertical slice — after this task
you can list streams and read one from the existing fixtures.

**Acceptance criteria:**
- [ ] Vault path resolves from `MESSAGE_BOARD_VAULT`; unset or missing path fails with an actionable
      message naming the variable and the path tried — never a silent default
- [ ] Parses `BRIEF.md` frontmatter including nested `claims`, and splits the body into its six
      canonical sections
- [ ] Lists streams with state; archived streams are returned as readable but flagged non-resolvable

**Verification:**
- [ ] `python3 -m unittest tests.test_vault -v`
- [ ] Manual: parse both fixtures in `examples/streams/`, confirm section and claim extraction

**Dependencies:** None
**Files:** `plugin/lib/vault.py`, `tests/test_vault.py`
**Scope:** M

---

### Task 2: `brief-read` skill

**Description:** The cold-start contract. Given an explicit stream slug, return the brief with the
`![[ground-truth]]` transclusion resolved inline, in the canonical section order.

**Acceptance criteria:**
- [ ] Given only a slug, returns Goal, Ground truth, Decided, Open, Next, Do not
- [ ] Unknown slug fails with the list of available streams, never a guess
- [ ] Archived streams are readable — a closed stream's `Do not` must remain reachable

**Verification:**
- [ ] `python3 -m unittest tests.test_brief_read -v`
- [ ] Manual: read `plat-1962-review-pr905`, confirm the stale-base warning surfaces

**Dependencies:** Task 1
**Files:** `plugin/skills/brief-read/SKILL.md`, `tests/test_brief_read.py`
**Scope:** S

---

### Checkpoint A: Cold-start proof

Tests assumption 3 — *a written brief can substitute for a live session's context.* Highest-risk
assumption in the design, deliberately tested before anything is built on top of it.

- [ ] `python3 -m unittest discover -s tests -v` passes
- [ ] Dispatch a fresh agent given **only** the slug `plat-1962-review-pr905`. It must reach "closed,
      superseded by `Datastore`, do not revive" without reading this repo's docs
- [ ] The agent does not re-litigate any `## Do not` entry
- [ ] **Stop and review with the operator.** If cold start fails here, the schema is wrong and every
      later task is built on sand

## Phase 2: Write Path

### Task 3: `brief-write` skill

**Description:** Owner-only updates to `BRIEF.md`. Requires an explicit stream; never guesses.

**Acceptance criteria:**
- [ ] Refuses to write without an explicit stream and routes the content to `unassigned/` instead
- [ ] Appends to `## Decided` with the mandatory `Why:` clause; rejects a decision lacking rationale
- [ ] Preserves section order and never writes `## Ground truth`

**Verification:**
- [ ] `python3 -m unittest tests.test_brief_write -v`
- [ ] Manual: write a decision to a scratch-vault copy of a fixture, confirm `ground-truth.md`
      is byte-identical afterward

**Dependencies:** Task 1
**Files:** `plugin/skills/brief-write/SKILL.md`, `plugin/lib/writer.py`, `tests/test_brief_write.py`
**Scope:** M

---

### Task 4: Size-budget guard and compaction candidates

**Description:** Enforce the ~400-word budget by surfacing archivable entries rather than truncating.

**Acceptance criteria:**
- [ ] Reports word count per section and flags briefs over budget
- [ ] Lists `## Decided` entries whose decision *and rationale* appear in a linked artifact, as
      archive candidates — never archives automatically
- [ ] Never proposes eviction by age or count

**Verification:**
- [ ] `python3 -m unittest tests.test_budget -v`
- [ ] Manual: run against the design fixture, confirm it reproduces the 512→365 compaction already
      performed by hand

**Dependencies:** Task 3
**Files:** `plugin/lib/budget.py`, `tests/test_budget.py`
**Scope:** S

---

### Checkpoint B: Round-trip

- [ ] Write then read reproduces content exactly, section order intact
- [ ] Concurrent writes to `BRIEF.md` and `inbox/` do not corrupt either

## Phase 3: Minting

### Task 5: Claims lookup and join-or-mint

**Description:** Resolve a claim (repo, issue, PR, branch, worktree) to an existing stream, or mint a
new one with a derived slug. Prevents duplicate streams for the same work.

**Acceptance criteria:**
- [ ] An existing stream claiming a given PR or issue is joined, never duplicated
- [ ] Slug derivation matches `start-task` conventions — ticket ID first, kebab-case
- [ ] Issue keys parse case-insensitively across `PLAT-2039`, `plat-1921`, `polds/plat-1870`,
      `feat/PLAT-1719/gclb`
- [ ] Ambiguous resolution (one issue, many branches) returns all candidates and refuses to pick

**Verification:**
- [ ] `python3 -m unittest tests.test_claims -v`
- [ ] Manual: `PLAT-1719` must return multiple candidates, not one

**Dependencies:** Task 1
**Files:** `plugin/lib/claims.py`, `tests/test_claims.py`
**Scope:** M

---

### Task 6: Lazy mint at first durable write

**Description:** Mint at first durable write, never at session start. Structured dispatch mints
silently; ad-hoc prompts once with a proposed slug.

**Acceptance criteria:**
- [ ] Nothing is minted for a session that never produces a durable write
- [ ] The mint bar is stated in the skill: a decision with rationale, a blocker needing someone else,
      or an artifact others build on — explicitly not ran-tests, read-files, answered-a-question
- [ ] Ad-hoc path proposes a slug with accept / edit / skip

**Verification:**
- [ ] `python3 -m unittest tests.test_mint -v`
- [ ] Manual: a read-only session creates no stream

**Dependencies:** Task 5
**Files:** `plugin/skills/brief-write/SKILL.md`, `plugin/lib/mint.py`, `tests/test_mint.py`
**Scope:** S

---

### Task 7: Traces for below-bar sessions

**Description:** Sessions below the mint bar drop a one-line trace to `unassigned/`, one file per
session so appends stay atomic.

**Acceptance criteria:**
- [ ] Trace filename is `<timestamp>-<session>.md`; concurrent sessions never collide
- [ ] Carries working directory, branch, one-line summary, session reference — enough to promote later
- [ ] A trace can be promoted into a stream without loss

**Verification:**
- [ ] `python3 -m unittest tests.test_traces -v`
- [ ] Manual: promote a trace, confirm content survives

**Dependencies:** Task 6
**Files:** `plugin/lib/traces.py`, `tests/test_traces.py`
**Scope:** S

## Phase 4: Hooks

### Task 8: `SessionEnd` hook writes ground truth

**Description:** Hooks are the sole writer of `ground-truth.md`. Mechanical facts only — the
independent evidence the honesty probe compares against.

**Acceptance criteria:**
- [ ] Writes branch, last commit, test result, PR state, files touched
- [ ] Records absence explicitly for non-git streams ("not a git repository") rather than omitting
- [ ] Never writes `BRIEF.md`, including its frontmatter
- [ ] Runs without a resolvable stream, writing a trace instead

**Verification:**
- [ ] `python3 -m unittest tests.test_ground_truth -v`
- [ ] Manual: run in `platform` and in a non-git directory; both produce sane output

**Dependencies:** Task 1, Task 7
**Files:** `plugin/hooks/session_end.py`, `tests/test_ground_truth.py`
**Scope:** M

---

### Task 9: `SessionStart` hook injects the brief

**Description:** Read-path resolution — the only place inference is permitted. Injects a best-guess
brief so an agent starts briefed rather than cold.

**Acceptance criteria:**
- [ ] Ladder is read-only: explicit → worktree (validated) → branch → issue key → stop
- [ ] Injected content is labeled as a guess when it came from a hint rather than a declaration
- [ ] Ambiguity injects nothing rather than guessing
- [ ] Never writes anything

**Verification:**
- [ ] `python3 -m unittest tests.test_session_start -v`
- [ ] Manual: in a worktree whose branch has no stream, confirm nothing is injected

**Dependencies:** Task 5
**Files:** `plugin/hooks/session_start.py`, `tests/test_session_start.py`
**Scope:** M

---

### Checkpoint C: Live run

- [ ] Run one real workstream end-to-end against a scratch vault
- [ ] `ground-truth.md` populated without agent involvement
- [ ] A second session cold-starts from the brief alone and continues correctly
- [ ] **Review with the operator before Phase 5**

## Phase 5: Multi-Writer and Operator Surface

### Task 10: `inbox-append` skill

**Description:** Non-owner contributions as separate files. Filesystem-atomic, contention-free.

**Acceptance criteria:**
- [ ] Appends a new file; never modifies `BRIEF.md`
- [ ] Subagents are refused — they report to their parent
- [ ] Owner-side folding lists unfolded entries and preserves attribution

**Verification:**
- [ ] `python3 -m unittest tests.test_inbox -v`
- [ ] Manual: two concurrent appends both survive

**Dependencies:** Task 1
**Files:** `plugin/skills/inbox-append/SKILL.md`, `plugin/lib/inbox.py`, `tests/test_inbox.py`
**Scope:** S

---

### Task 11: Dashboard renderer

**Description:** The operator surface. All streams, `needs_operator` first, verification mismatches
surfaced for adjudication rather than as verdicts.

**Acceptance criteria:**
- [ ] Operator-tagged `## Open` entries sort first, derived from body — no stored `needs_operator`
- [ ] Claim-vs-ground-truth mismatches render as "needs adjudication", not as failures. The PLAT-1962
      case (claimed review, empty reviews API, closing comment posted) must not render as an error
- [ ] Shows unfolded inbox counts and archived streams separately
- [ ] Runs in under a second across the fixture vault

**Verification:**
- [ ] `python3 -m unittest tests.test_dashboard -v`
- [ ] Manual: render the fixture vault, confirm the PLAT-1962 mismatch reads as ambiguous

**Dependencies:** Task 1
**Files:** `plugin/dashboard.py`, `tests/test_dashboard.py`
**Scope:** M

---

### Task 12: `start-task` wiring

**Description:** Reuse the slug `start-task` already derives at branch-naming time, so structured
dispatch costs the operator nothing.

**Acceptance criteria:**
- [ ] Slug is reused, not re-derived by a second scheme
- [ ] Registers `worktree` and `branch` claims at creation
- [ ] Existing `start-task` behavior is unchanged when the vault is unconfigured

**Verification:**
- [ ] Manual: `/start-task` in a scratch repo mints exactly one stream with matching slug and claims

**Dependencies:** Task 5, Task 6
**Files:** `plugin/skills/start-task-integration/SKILL.md`
**Scope:** S

---

### Task 13: Plugin packaging and dogfood

**Description:** Package as an installable plugin and wire `.claude/` to consume it in-repo.

**Acceptance criteria:**
- [ ] `plugin-validator` passes
- [ ] Skill descriptions are written as trigger conditions and state what they are *not* for
- [ ] `.claude/` consumes the plugin; this repo's own design stream is maintained through it

**Verification:**
- [ ] `plugin-validator` agent reports clean
- [ ] Manual: fresh session in this repo resolves and reads `message-board-design`

**Dependencies:** Tasks 2, 3, 10, 11
**Files:** `.claude-plugin/plugin.json`, `.claude/settings.json`, `README.md`
**Scope:** M

---

### Checkpoint D: Complete

- [ ] All acceptance criteria met
- [ ] Design stream for this project maintained by the plugin itself
- [ ] One week of real use before adding anything beyond MVP scope

## Risks and Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Brief can't substitute for live context | High — invalidates the design | Checkpoint A tests it before anything is built on top |
| Agent self-report unreliable | High | Tasks 8 + 11; hooks carry more weight if claims prove unreliable |
| Verification noise trains operator to ignore it | High | Task 11 renders mismatches for adjudication, never as verdicts |
| Explicit declaration decays in practice | High | Tasks 5, 6, 12 auto-mint every structured path; only ad-hoc prompts |
| Dashboard never gets opened | Med | Keep Task 11 minimal; statusline alternative still open |
| `unassigned/` queue never worked | Med | Trace retention undecided — watch during Checkpoint C |
| Scope creep back to threads/personas | Med | `CLAUDE.md` "Do not"; reject at review |
| Write contention | Low | Single-writer partition; contention signals wrong granularity |

## Parallelization

- **Safe now, against existing fixtures:** Task 11 (dashboard) needs only Task 1 and the fixture vault
- **Safe after Task 1:** Tasks 2, 3, 5, 10 are independent of each other
- **Must be sequential:** 5 → 6 → 7 (minting chain), 8 → 9 (hook pair), 12 after 6
- **Needs coordination:** Tasks 3 and 10 share the write partition — settle the file-ownership contract
  before parallelizing them

## Open Questions

- Dashboard surface: standalone TUI or Claude Code statusline. Task 11 assumes standalone; the
  statusline is the only option with a negative pane count and would change the task materially.
- Trace retention window, and whether the `unassigned/` queue is worked on a schedule. An unworked
  queue makes strict-bar minting lossy in practice regardless of git retention.
