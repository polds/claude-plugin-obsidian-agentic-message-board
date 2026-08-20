# Contributing

Design first, then code. The design direction is settled and recorded — read
`docs/ideas/agent-handoff-board.md` before proposing architecture; it records what was rejected and
why, precisely so those decisions don't get relitigated. `CLAUDE.md` carries the non-negotiable
constraints. A PR that relitigates a settled decision will be closed with a pointer to the record,
which is this project working as intended.

## Setup

```bash
git clone https://github.com/polds/claude-plugin-obsidian-agentic-message-board
cd claude-plugin-obsidian-agentic-message-board
python3 -m unittest discover -s tests
```

Python 3.10+ and nothing else. **Stdlib only is a design constraint, not a preference** — a plugin
that needs `pip install`, a build step, or a running server has broken its install story. Scripts
stay invocable standalone. Run tests from the repo root so `plugin.lib` imports resolve.

Dogfood wiring (machine-local settings, scratch vault): see
[docs/getting-started.md](docs/getting-started.md#for-contributors).

## The rules that are actually enforced

CI enforces these on every PR; save yourself the round trip:

- **The suite passes** on Python 3.10–3.14, Linux and macOS.
- **`examples/` is byte-identical** after the test run. It is read-only fixture input transcribed
  from real streams — its untidiness (a review with no branch, a stale base, unreachable citations)
  is exactly what the parsers must survive, so never "clean it up" and never test against anything
  but a `tempfile` vault.
- **`claude plugin validate .` passes.**
- **Packaging invariants hold**: manifests parse, the manifest version has a `CHANGELOG.md`
  section, hook commands resolve inside the plugin, and every skill description states what the
  skill is NOT for.

## Testing standards

Two qualities this suite has and keeps:

- **Mutation-check anything concurrency- or safety-critical.** Swap the safe primitive for the
  naive one and confirm the test actually fails. A safety test that passes both ways is decoration.
- **Pair every absence assertion with a presence assertion.** "Renders no error vocabulary" passes
  trivially on a renderer that outputs nothing.

And one rule for prose: **skills are documentation that nothing executes, so doc bugs survive a
green suite.** Every command a `SKILL.md` prints must be pasted into a shell and run before it
ships. This repo has already shipped documented commands that did not exist; the suite stayed
green the whole time.

A change is not done when its tests pass. It is done when it has also been exercised through
`.claude/` in a real session against a scratch vault, and — for anything touching note structure —
checked in Obsidian: frontmatter parses, wikilinks resolve.

## Releases

A release is a `v<version>` tag on `main`:

1. Bump `version` in `.claude-plugin/plugin.json`. Installed plugins only update when this string
   changes, so an unbumped release reaches nobody.
2. Move the changes into a `## [<version>] - <date>` section in `CHANGELOG.md`.
3. Tag and push: `git tag v<version> && git push origin v<version>`.

The Release workflow re-runs the suite, refuses a tag that disagrees with the manifest, and
publishes the changelog section as the GitHub release notes. If it refuses, fix the mismatch —
don't force it.

## Reporting problems

Bugs and design discussion: GitHub issues. Vulnerabilities: see [SECURITY.md](SECURITY.md) —
privately, please.
