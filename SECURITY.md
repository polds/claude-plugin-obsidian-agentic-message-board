# Security

## Reporting a vulnerability

Report vulnerabilities privately via
[GitHub security advisories](https://github.com/polds/claude-plugin-obsidian-agentic-message-board/security/advisories/new)
rather than a public issue. You should receive a response within a week. Only the latest release is
supported with fixes.

## Trust model

Worth knowing before reporting:

- **The vault is trusted local data.** The plugin reads and writes plain Markdown in a directory
  the operator configured. It runs no code from the vault, makes no network calls, and depends on
  nothing outside the Python standard library.
- **Brief content shapes agent behavior.** Whatever a brief says, the next agent will tend to do —
  that is the product. Today every writer is one of the operator's own agents, so briefs are as
  trusted as the operator's own prompts. The moment an agent writes *external* content (web pages,
  issues, third-party repos) into a brief, briefs become an injection surface. This is a known,
  open design line — tracked in `docs/ideas/agent-handoff-board.md` under "Open Questions" — and
  hardening proposals for it are welcome.
- **Hooks execute with the session's privileges.** They are registered by the plugin manifest and
  run `plugin/hooks/*.py` from the installed plugin only. Anything that could cause a hook to
  execute content sourced from the vault would be a vulnerability; report it.
