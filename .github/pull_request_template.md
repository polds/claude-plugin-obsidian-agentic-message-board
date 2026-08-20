## What

<!-- What changed, in a sentence or two. -->

## Why

<!-- The problem or decision behind it. Link the relevant section of
     docs/ideas/agent-handoff-board.md if this touches settled design. -->

## Verification

- [ ] `python3 -m unittest discover -s tests` passes from the repo root
- [ ] `examples/` is byte-identical (read-only fixtures; CI enforces this too)
- [ ] Every command printed by a touched `SKILL.md` was pasted into a shell and run, not just read
- [ ] Anything touching note structure was checked against Obsidian rendering (frontmatter parses, wikilinks resolve)
