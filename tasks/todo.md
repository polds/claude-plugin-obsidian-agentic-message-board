# Agent Handoff Board MVP — Task List

Plan: `plan.md` · Design: `../docs/ideas/agent-handoff-board.md` · Schema: `../docs/spec/brief-schema.md`

Test command: `python3 -m unittest discover -s tests -v`
Single test: `python3 -m unittest tests.test_vault.TestResolve.test_missing_vault -v`

## Phase 1: Read Path

- [x] **Task 1** — Vault resolution + brief parsing library · M · 19 tests
- [x] **Task 2** — `brief-read` skill · S · 8 tests

- [x] **CHECKPOINT A — Cold-start proof. PASSED.** A fresh agent restricted to the stream directory
      identified the closed state, explained the supersession, **refused a rebase-and-merge proposal**
      citing `## Do not`, and routed a hypothetical bug to `datastore.xrd.yaml` rather than the dead
      branch. Assumption 3 validated.
      *Finding:* external references (`a1b2c3d4e`, `#1025`, "nine live branches", "all four env
      projects") are unresolvable — cited by ID with no link. The artifact-link rule covers internal
      `[[wikilinks]]` only. See Task 14.

## Phase 2: Write Path

- [x] **Task 3** — `brief-write` skill (owner-only, explicit stream required) · M · 56 tests
      *Follow-up in flight:* refuse writes to archived streams without an explicit `reopen` —
      writing without flipping state makes the dashboard's ARCHIVED section lie.
- [ ] **Task 4** — Size-budget guard + compaction candidates · S · deps: 3

- [ ] **CHECKPOINT B — Round-trip.** Write then read reproduces exactly; concurrent
      `BRIEF.md` and `inbox/` writes don't corrupt either.

## Phase 3: Minting

- [x] **Task 5** — Claims lookup + join-or-mint · M · 62 tests
- [ ] **Task 6** — Lazy mint at first durable write · S · deps: 5
- [ ] **Task 7** — Traces to `unassigned/` for below-bar sessions · S · deps: 6
      **Trace header format is already set** by `writer.py` (kind / created / session / cwd / branch /
      reason / proposed_stream) and tested. Adopt it as the contract; do not invent a second one.

## Phase 4: Hooks

- [ ] **Task 8** — `SessionEnd` hook writes `ground-truth.md` · M · deps: 1, 7
- [ ] **Task 9** — `SessionStart` hook injects best-guess brief · M · deps: 5

- [ ] **CHECKPOINT C — Live run.** One real workstream end-to-end on a scratch vault;
      second session cold-starts from the brief alone. **Review with operator.**

## Phase 5: Multi-Writer and Operator Surface

- [x] **Task 10** — `inbox-append` skill · S · 32 tests
- [x] **Task 11** — Dashboard renderer · M · 41 tests · renders fixture vault in 1.2ms
- [ ] **Task 12** — `start-task` wiring · S · deps: 5, 6
- [ ] **Task 13** — Plugin packaging + `.claude/` dogfood · M · deps: 2, 3, 10, 11
- [ ] **Task 14** — External reference resolution · S · deps: 3
      From Checkpoint A. A brief cites `a1b2c3d4e`, `#1025`, `PLAT-1719` with no way to reach any of
      them. Expand `claims` (repo + pr + issue) into resolvable URLs at render time, and require a
      `## Decided` entry that cites evidence to link where that evidence lives. The brief is a
      conclusion summary; it must at least say where the proof is.

- [ ] **CHECKPOINT D — Complete.** All criteria met; this project's own design stream
      maintained by the plugin. One week of real use before extending scope.

## Standing bar (every task)

- [ ] Scratch vault only — never the operator's working vault
- [ ] One writer per file: owner → `BRIEF.md` + `decided-archive.md`, hooks → `ground-truth.md`,
      others → `inbox/`
- [ ] Stdlib only, no install step
- [ ] Vault path never hardcoded; missing config fails loudly with the variable name and path tried
- [ ] No threads, personas, DMs, moderator, or self-mutating directives (see `CLAUDE.md` "Do not")
