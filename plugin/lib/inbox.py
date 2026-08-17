"""Non-owner contributions to a stream: one new file per contribution.

`BRIEF.md` has exactly one writer. A reviewer, a sibling session, or a passing agent still needs a
durable channel into it, and creating a *new* file is the only write two parties can perform at the
same instant without coordinating: `O_CREAT|O_EXCL` makes the kernel pick one winner per name, so the
loser learns it lost and retries a different name instead of clobbering someone else's note. That is
what lets an author and a reviewer share one decision unit without sharing a writer.

Nothing here opens `BRIEF.md`, `ground-truth.md`, or another contributor's entry for writing —
including when folding, which records itself in a sidecar marker rather than editing the entry it
folds, so attribution stays exactly as its author wrote it.
"""

from __future__ import annotations

import itertools
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import vault

# Matches the layout in docs/spec/brief-schema.md: streams/<slug>/inbox/2026-08-13T1642-reviewer-7.md
INBOX_DIRNAME = "inbox"
FILENAME_STAMP = "%Y-%m-%dT%H%M"
ISO_STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Fold markers live in a dot-directory so Obsidian's file tree stays a list of contributions rather
# than a list of contributions interleaved with bookkeeping.
FOLD_DIRNAME = ".folded"

DEFAULT_KIND = "note"

USAGE = "\n".join(
    [
        "usage:",
        "  python3 -m plugin.lib.inbox append <slug> --author <name> [--kind <kind>] [--body <text>]",
        "  python3 -m plugin.lib.inbox list <slug> [--all]",
        "  python3 -m plugin.lib.inbox fold <slug> <entry> --owner <name>",
        "",
        "--body defaults to stdin. list shows unfolded entries unless --all is given.",
    ]
)

_BOOL_FLAGS = {"subagent", "all"}


class InboxError(vault.VaultError):
    """A refused append or fold. Subclasses VaultError so callers can catch either alone."""


@dataclass
class Entry:
    path: Path
    slug: str
    author: str
    kind: str
    created: str
    body: str
    folded_by: str = ""
    folded_at: str = ""

    @property
    def folded(self) -> bool:
        return bool(self.folded_at)

    @property
    def name(self) -> str:
        return self.path.name

    def summary(self) -> str:
        """First non-empty body line — enough for the owner to triage without opening the file."""
        for line in self.body.splitlines():
            if line.strip():
                return line.strip()
        return ""


def inbox_dir(vault_root: Path, slug: str) -> Path:
    return vault.streams_dir(vault_root) / slug / INBOX_DIRNAME


def append(
    vault_root: Path,
    slug: str,
    *,
    author: str,
    body: str,
    kind: str = DEFAULT_KIND,
    is_subagent: bool = False,
    now: datetime | None = None,
) -> Path:
    """Create one new inbox entry and return its path.

    Every refusal below is preferred to a silent partial write: an entry nobody can attribute or act
    on costs the owner more to triage than it saves the contributor.
    """
    if is_subagent:
        raise InboxError(
            "Subagents do not write to the vault. Report this to your parent agent — the parent is "
            "the only party that may append, which is what keeps one writer per file."
        )

    author = (author or "").strip()
    if not author:
        raise InboxError(
            "An inbox entry needs an author. Folding preserves attribution, so an entry that arrives "
            "without any cannot be followed up later."
        )

    body = (body or "").strip()
    if not body:
        raise InboxError(f"Refusing to append an empty entry to '{slug}'.")

    # Validates the slug and, on a miss, names the streams that do exist. Writes must be certain:
    # appending to a guessed stream corrupts two of them silently.
    brief = vault.read_stream(vault_root, slug)

    directory = inbox_dir(vault_root, slug)
    directory.mkdir(parents=True, exist_ok=True)

    created = _utc(now)
    path, fd = _create_unique(directory, created, author)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(_render_entry(brief.slug, author, kind.strip() or DEFAULT_KIND, created, body))
    return path


def list_entries(vault_root: Path, slug: str) -> list[Entry]:
    """Every entry, folded included. Sorted by creation stamp, then filename, for stable output."""
    directory = inbox_dir(vault_root, slug)
    if not directory.is_dir():
        return []
    markers = _fold_markers(directory)
    entries = [_read_entry(path, markers) for path in sorted(directory.glob("*.md")) if path.is_file()]
    return sorted(entries, key=lambda e: (e.created, e.path.name))


def list_unfolded(vault_root: Path, slug: str) -> list[Entry]:
    """The owner's work queue: what has not yet been folded into the brief."""
    return [entry for entry in list_entries(vault_root, slug) if not entry.folded]


def fold(
    vault_root: Path,
    slug: str,
    entry_name: str,
    *,
    folded_by: str,
    now: datetime | None = None,
) -> Path:
    """Mark one entry as folded into the brief and return the marker path.

    Folding never deletes or rewrites the entry. Deleting would drop the attribution the owner may
    need weeks later to ask a follow-up question, and rewriting would make the owner a second writer
    of a file the contributor already wrote.
    """
    folded_by = (folded_by or "").strip()
    if not folded_by:
        raise InboxError("Folding needs the owner's name — the fold is itself an attributed act.")

    directory = inbox_dir(vault_root, slug)
    entry_path = directory / (entry_name if entry_name.endswith(".md") else f"{entry_name}.md")
    if not entry_path.is_file():
        available = ", ".join(e.name for e in list_unfolded(vault_root, slug)) or "(none unfolded)"
        raise InboxError(f"No inbox entry '{entry_name}' in '{slug}'. Unfolded: {available}")

    marker_dir = directory / FOLD_DIRNAME
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / entry_path.name
    folded_at = _utc(now)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        existing = _read_marker(marker)
        raise InboxError(
            f"'{entry_path.name}' was already folded by {existing.get('folded_by', 'unknown')} "
            f"at {existing.get('folded', 'unknown')}."
        ) from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            "---\n"
            f"entry: {entry_path.name}\n"
            f"folded_by: {folded_by}\n"
            f"folded: {folded_at.strftime(ISO_STAMP)}\n"
            "---\n"
        )
    return marker


def _utc(moment: datetime | None) -> datetime:
    """Naive input is read as UTC rather than local time: vault stamps are compared across machines."""
    if moment is None:
        return datetime.now(timezone.utc)
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "anon"


def _create_unique(directory: Path, created: datetime, author: str) -> tuple[Path, int]:
    """Claim a filename atomically, walking the suffix until the kernel hands us an unused one.

    Two contributors appending within the same minute produce the same prefix, so the suffix is the
    only thing separating them. Checking `exists()` first would reintroduce the race this avoids —
    `O_EXCL` is what makes losing the race harmless rather than destructive.
    """
    prefix = f"{created.strftime(FILENAME_STAMP)}-{_slugify(author)}"
    for suffix in itertools.count(1):
        path = directory / f"{prefix}-{suffix}.md"
        try:
            return path, os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
    raise AssertionError("unreachable: itertools.count is infinite")


def _render_entry(slug: str, author: str, kind: str, created: datetime, body: str) -> str:
    return (
        "---\n"
        f"stream: {slug}\n"
        f"author: {author}\n"
        f"kind: {kind}\n"
        f"created: {created.strftime(ISO_STAMP)}\n"
        "---\n"
        "\n"
        f"{body}\n"
    )


def _split_entry(text: str) -> tuple[str, str]:
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return "", text
    return parts[1], parts[2]


def _read_entry(path: Path, markers: dict[str, dict[str, str]]) -> Entry:
    front_raw, body = _split_entry(path.read_text(encoding="utf-8"))
    front = vault.parse_frontmatter(front_raw)
    marker = markers.get(path.name, {})
    return Entry(
        path=path,
        slug=str(front.get("stream", "")),
        author=str(front.get("author", "")),
        kind=str(front.get("kind", DEFAULT_KIND)),
        created=str(front.get("created", "")),
        body=body.strip(),
        folded_by=str(marker.get("folded_by", "")),
        folded_at=str(marker.get("folded", "")),
    )


def _read_marker(path: Path) -> dict[str, str]:
    front_raw, _ = _split_entry(path.read_text(encoding="utf-8"))
    return {key: str(value) for key, value in vault.parse_frontmatter(front_raw).items()}


def _fold_markers(directory: Path) -> dict[str, dict[str, str]]:
    marker_dir = directory / FOLD_DIRNAME
    if not marker_dir.is_dir():
        return {}
    return {path.name: _read_marker(path) for path in marker_dir.glob("*.md") if path.is_file()}


def _parse_argv(args: list[str]) -> tuple[list[str], dict[str, str]]:
    positional: list[str] = []
    options: dict[str, str] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            positional.append(token)
            index += 1
            continue
        key = token[2:]
        if key in _BOOL_FLAGS:
            options[key] = "true"
            index += 1
            continue
        if index + 1 >= len(args):
            raise InboxError(f"--{key} needs a value.")
        options[key] = args[index + 1]
        index += 2
    return positional, options


def _render_listing(slug: str, entries: list[Entry], folded_shown: bool) -> str:
    label = "entries" if folded_shown else "unfolded entries"
    if not entries:
        return f"0 {label} in {slug}."
    lines = [f"{len(entries)} {label} in {slug}:"]
    for entry in entries:
        state = f" [folded by {entry.folded_by} at {entry.folded_at}]" if entry.folded else ""
        lines.append(f"- {entry.name} — {entry.author} ({entry.kind}, {entry.created}){state}")
        summary = entry.summary()
        if summary:
            lines.append(f"    {summary}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(USAGE, file=sys.stderr)
        return 2
    command = argv[0]
    try:
        positional, options = _parse_argv(argv[1:])
        root = vault.resolve_vault()

        if command == "append":
            if len(positional) != 1:
                print(USAGE, file=sys.stderr)
                return 2
            body = options.get("body")
            if body is None:
                body = sys.stdin.read()
            path = append(
                root,
                positional[0],
                author=options.get("author", ""),
                body=body,
                kind=options.get("kind", DEFAULT_KIND),
                is_subagent="subagent" in options,
            )
            print(path)
        elif command == "list":
            if len(positional) != 1:
                print(USAGE, file=sys.stderr)
                return 2
            show_all = "all" in options
            slug = positional[0]
            entries = list_entries(root, slug) if show_all else list_unfolded(root, slug)
            print(_render_listing(slug, entries, show_all))
        elif command == "fold":
            if len(positional) != 2:
                print(USAGE, file=sys.stderr)
                return 2
            print(fold(root, positional[0], positional[1], folded_by=options.get("owner", "")))
        else:
            print(USAGE, file=sys.stderr)
            return 2
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
