"""Vault resolution and brief parsing.

The vault is external, user-configured, and authoritative. Nothing here writes; readers only.
Stdlib only by design — a plugin that needs `pip install` has broken its install story, so the
frontmatter parser below handles the subset of YAML the schema actually uses rather than taking a
dependency on PyYAML.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

VAULT_ENV = "MESSAGE_BOARD_VAULT"

# Canonical section order from docs/spec/brief-schema.md. The order is the cold-start read order,
# so it is part of the contract, not a formatting preference.
SECTIONS = ["Goal", "Ground truth", "Decided", "Open", "Next", "Do not"]

CLAIM_KINDS = ["repo", "branch", "worktree", "issue", "pr"]

# States that must not participate in session->stream resolution. Archived streams stay *readable*:
# a closed stream's "Do not" is often its most valuable content.
NON_RESOLVABLE_STATES = {"archived"}


class VaultError(Exception):
    """Configuration or lookup failure. Always carries an actionable message."""


@dataclass
class Brief:
    slug: str
    path: Path
    title: str = ""
    state: str = "active"
    owner: str = ""
    updated: str = ""
    claims: dict[str, list[str]] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)

    @property
    def archived(self) -> bool:
        return self.state in NON_RESOLVABLE_STATES

    @property
    def resolvable(self) -> bool:
        """Archived streams are readable but must never be resolved into."""
        return not self.archived

    def claim(self, kind: str) -> list[str]:
        return self.claims.get(kind, [])

    def operator_asks(self) -> list[str]:
        """Entries in ## Open tagged `who: operator`.

        Derived from the body rather than stored in frontmatter: a stored copy is a second source of
        truth that can disagree with the section it summarizes.
        """
        asks = []
        for line in self.sections.get("Open", "").splitlines():
            stripped = line.strip()
            if stripped.startswith("-") and "who: operator" in stripped:
                asks.append(stripped.lstrip("- ").strip())
        return asks


def resolve_vault(env: dict[str, str] | None = None) -> Path:
    """Resolve the vault root, failing loudly rather than defaulting.

    Never invent a path. A silent default would write a user's notes somewhere they did not choose.
    """
    env = os.environ if env is None else env
    raw = env.get(VAULT_ENV, "").strip()
    if not raw:
        raise VaultError(
            f"{VAULT_ENV} is not set. Point it at your Obsidian vault root, "
            f"e.g. export {VAULT_ENV}=~/vaults/agent-board"
        )
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise VaultError(f"{VAULT_ENV} is set to {path}, which does not exist or is not a directory.")
    return path


def streams_dir(vault: Path) -> Path:
    return vault / "streams"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter, body). Absent frontmatter yields ("", text)."""
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _parse_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    return value


def _parse_inline_list(raw: str) -> list[str]:
    inner = raw.strip()[1:-1].strip()
    if not inner:
        return []
    return [_parse_scalar(item) for item in inner.split(",") if item.strip()]


def parse_frontmatter(text: str) -> dict[str, object]:
    """Parse the YAML subset the brief schema uses: scalars, inline lists, one nested level.

    Deliberately narrow. Anything richer than this is a signal the schema drifted, and a silent
    partial parse would be worse than the KeyError a caller gets from a missing field.
    """
    data: dict[str, object] = {}
    nested_key: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in " \t"
        if ":" not in line:
            continue
        key, _, raw = line.partition(":")
        key = key.strip()
        raw = raw.strip()

        if indented and nested_key is not None:
            bucket = data.setdefault(nested_key, {})
            if isinstance(bucket, dict):
                bucket[key] = _parse_inline_list(raw) if raw.startswith("[") else _parse_scalar(raw)
            continue

        if raw == "":
            nested_key = key
            data[key] = {}
        elif raw.startswith("["):
            nested_key = None
            data[key] = _parse_inline_list(raw)
        else:
            nested_key = None
            data[key] = _parse_scalar(raw)
    return data


def parse_sections(body: str) -> dict[str, str]:
    """Split a brief body on `## ` headings, preserving content verbatim."""
    sections: dict[str, str] = {}
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        heading = re.match(r"^##\s+(.+?)\s*$", line)
        if heading:
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = heading.group(1)
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


def parse_brief(path: Path) -> Brief:
    text = path.read_text(encoding="utf-8")
    front_raw, body = _split_frontmatter(text)
    front = parse_frontmatter(front_raw)

    raw_claims = front.get("claims", {})
    claims: dict[str, list[str]] = {}
    if isinstance(raw_claims, dict):
        for kind, value in raw_claims.items():
            claims[kind] = value if isinstance(value, list) else [value]

    slug = str(front.get("stream") or path.parent.name)
    return Brief(
        slug=slug,
        path=path,
        title=str(front.get("title", "")),
        state=str(front.get("state", "active")),
        owner=str(front.get("owner", "")),
        updated=str(front.get("updated", "")),
        claims=claims,
        sections=parse_sections(body),
    )


def list_streams(vault: Path) -> list[Brief]:
    """All streams, archived included. Sorted by slug for stable output."""
    root = streams_dir(vault)
    if not root.is_dir():
        return []
    briefs = [parse_brief(p) for p in sorted(root.glob("*/BRIEF.md"))]
    return sorted(briefs, key=lambda b: b.slug)


def read_stream(vault: Path, slug: str) -> Brief:
    """Read one stream by explicit slug.

    An unknown slug lists what is available rather than guessing: a wrong read is cheap only when the
    reader can tell it was wrong.
    """
    path = streams_dir(vault) / slug / "BRIEF.md"
    if not path.is_file():
        available = ", ".join(b.slug for b in list_streams(vault)) or "(none)"
        raise VaultError(f"No stream '{slug}' in {vault}. Available: {available}")
    return parse_brief(path)


def resolve_transclusions(brief: Brief) -> dict[str, str]:
    """Expand `![[name]]` embeds against sibling files in the stream directory.

    Obsidian renders these natively; every other reader needs them inlined, so callers that must not
    require Obsidian resolve them through here.
    """
    resolved = dict(brief.sections)
    for name, content in brief.sections.items():
        def _expand(match: re.Match[str]) -> str:
            target = brief.path.parent / f"{match.group(1)}.md"
            if not target.is_file():
                return match.group(0)
            _, embedded = _split_frontmatter(target.read_text(encoding="utf-8"))
            return embedded.strip()

        resolved[name] = re.sub(r"!\[\[([^\]]+)\]\]", _expand, content)
    return resolved
