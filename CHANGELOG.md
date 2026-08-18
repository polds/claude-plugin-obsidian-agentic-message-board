# Changelog

All notable changes to the message-board plugin are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org). The authoritative version lives in
`.claude-plugin/plugin.json`, and a release is a `v<version>` tag on `main` — the Release workflow
refuses tags that do not match the manifest, and publishes this file's matching section as the
release notes. Bump the manifest version on every release: installed plugins update only when the
version string changes.

## [Unreleased]

Nothing yet.

## [0.1.0] - 2026-08-18

Initial release: the full MVP from `tasks/plan.md`.

### Added

- **Briefs**: one `BRIEF.md` per workstream in an external, git-tracked Obsidian vault — Goal,
  Ground truth, Decided, Open, Next, Do not, in cold-start read order, under a ~400-word budget
  with compaction (never truncation) via `plugin.lib.budget`.
- **Skills**: `brief-read` (cold start), `brief-write` (owner-only decisions with mandatory
  rationale), `inbox-append` (contention-free non-owner contributions), and
  `start-task-integration` (streams claim their branch and worktree at dispatch).
- **Claims and minting**: durable stream slugs with mutable claims, join-or-mint resolution, and
  lazy minting at first durable write via `plugin.lib.claims` and `plugin.lib.mint`.
- **Traces**: below-bar sessions drop atomic one-line traces to `unassigned/`; the `traces` CLI
  triages them by work item — `list`, `show`, `adopt`, `dismiss`, `prune`.
- **Hooks**: `SessionStart` injects the resolved brief (read-path hints only); `SessionEnd` writes
  mechanical ground truth to `ground-truth.md`, the honesty signal agents never touch.
- **Dashboard**: `plugin.dashboard` renders every stream at a glance — operator asks first,
  claim-vs-truth mismatches surfaced for adjudication, never as verdicts.
- **Doctor**: `plugin.lib.doctor` reports whether the board is actually wired up, because an
  unconfigured board and an empty board are silent in the same way.
- **CI/CD**: test matrix across Python 3.10–3.14 on Linux and macOS, fixture-immutability guard,
  packaging structure tests, and a tag-driven release pipeline publishing notes from this file.
- Two real-world fixture streams under `examples/` and a stdlib-only `unittest` suite.
