"""Render a brief for agent cold start.

Output is optimized for an agent reading it once, not for human skimming: canonical section order,
transclusions expanded, no decoration.
"""

from __future__ import annotations

import sys

from . import refs, vault


def render(brief: vault.Brief) -> str:
    resolved = vault.resolve_transclusions(brief)
    # Claims and body citations are expanded to URLs here because a cold-start reader has no repo
    # access to resolve `a1b2c3d4e` or `#1025` by hand. Guessed links never reach the body — a wrong
    # link costs more than an unlinked reference.
    ctx = refs.reference_context(brief)
    lines = [
        f"# {brief.title or brief.slug}",
        "",
        f"stream: {brief.slug}",
        f"state: {brief.state}" + ("  (ARCHIVED — readable, not resumable without explicit reopen)" if brief.archived else ""),
        f"owner: {brief.owner}",
        f"updated: {brief.updated}",
    ]

    claim_lines = refs.claim_lines(refs.expand_claims(brief, ctx))
    if claim_lines:
        lines += ["claims:"] + [f"  {line}" for line in claim_lines]
    else:
        lines.append("claims: (none)")

    asks = brief.operator_asks()
    if asks:
        lines += ["", f"** {len(asks)} item(s) need the operator — see ## Open **"]

    for name in vault.SECTIONS:
        body = refs.link_text(resolved.get(name, "").strip(), ctx)
        lines += ["", f"## {name}", "", body if body else "(empty)"]

    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python3 -m plugin.lib.brief_read <stream-slug>", file=sys.stderr)
        return 2
    try:
        root = vault.resolve_vault()
        print(render(vault.read_stream(root, argv[0])))
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
