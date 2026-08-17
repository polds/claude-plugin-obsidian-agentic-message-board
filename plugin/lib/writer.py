"""Owner-side writes to a stream's `BRIEF.md` and `decided-archive.md`.

Two rules shape everything here.

**Writes must be certain or fall to `unassigned/`.** Reading the wrong brief is cheap and
self-evident; writing the wrong one silently corrupts two streams and burns the honesty signal. So
this module never infers a stream — an absent, malformed, or unknown slug routes the content to
`unassigned/` for triage instead of guessing at a home for it.

**This module is not the only writer in the stream directory.** Hooks own `ground-truth.md` and
non-owners append to `inbox/`; both are refused here by path, not by convention. Agent judgment and
machine-observed reality are kept apart precisely so they can be compared, which stops being true the
moment one writer produces both.

Stdlib only, like `vault` — a plugin that needs `pip install` has broken its install story.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import date as _date, datetime, timezone
from pathlib import Path

from . import vault

BRIEF_FILE = "BRIEF.md"
ARCHIVE_FILE = "decided-archive.md"

# The only files inside a stream directory this module may write. Everything else in there belongs to
# another writer, and an accidental write is a read-modify-write conflict rather than a lost update.
OWNED_FILES = {BRIEF_FILE, ARCHIVE_FILE}

GROUND_TRUTH_FILE = "ground-truth.md"
GROUND_TRUTH_SECTION = "Ground truth"

# Content-identity markers for `unassigned/`, in a dot-directory for the same reason `.promoted` is:
# Obsidian's file tree should read as waiting work, not work interleaved with bookkeeping.
FINGERPRINT_DIRNAME = ".fingerprints"

# Written when a session's subject could not be observed. A named sentinel rather than an empty
# field, because "we looked and found nothing" and "nobody ever recorded this" are different facts —
# and shared with `traces`, which must treat it as absence rather than print it as if it were the
# summary.
NO_SUMMARY = "no summary recorded"

# `## Ground truth` holds a pointer, never content. Writing the pointer when it is missing restores
# the transclusion the schema mandates; writing anything else would make the agent the author of the
# evidence it is checked against.
GROUND_TRUTH_TRANSCLUSION = "![[ground-truth]]"

DECIDED_SECTION = "Decided"
UNASSIGNED_DIR = "unassigned"

# State a reopened stream returns to. Reopening is its own act rather than a side effect of writing:
# a stream the dashboard lists as closed while its content keeps changing makes the operator surface
# lie, and that surface is the only thing standing between the operator and reading every session.
REOPENED_STATE = "active"

# Rationales that satisfy the format check while carrying no reason. A decision whose "why" is "tbd"
# gets re-litigated exactly like one with no why at all, which is the failure `## Decided` exists to
# prevent — so they are rejected rather than stored.
_NON_REASONS = {
    "",
    "-",
    "--",
    "?",
    ".",
    "n/a",
    "na",
    "tbd",
    "todo",
    "none",
    "unknown",
    "obvious",
    "self-explanatory",
    "see above",
    "as discussed",
}

_HEADING = re.compile(r"^##\s+(.+?)\s*$")

# A slug becomes a path segment. Anything that could climb out of the vault is not a slug at all, and
# treating it as one is how a write ends up outside the directory the operator configured.
_UNSAFE_SLUG = re.compile(r"[/\\]|^\.|\.\.")


class WriteError(vault.VaultError):
    """A write that must not proceed.

    Subclasses `VaultError` so callers already guarding vault access catch refusals with the same
    `except` clause instead of needing to know which module raised.
    """


@dataclass
class WriteOutcome:
    """Where content actually landed. Routing is never silent — the caller must be able to report it."""

    path: Path
    stream: str | None
    unassigned: bool
    reason: str = ""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%SZ")


def _one_line(text: str) -> str:
    """Collapse to a single line. `## Decided` is line-oriented and append-only; an embedded newline
    would silently split one entry into two."""
    return " ".join(str(text).split())


def _terminated(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


def _require_reason(why: str) -> None:
    probe = why.strip().rstrip(".!?").strip().lower()
    if probe in _NON_REASONS:
        raise WriteError(
            "A decision needs its rationale: `Why: <reason>`. The reason is not optional — a "
            "decision without one gets re-litigated the moment its context is gone, which is the "
            "failure ## Decided exists to prevent. See docs/spec/brief-schema.md."
        )


def format_decision(decision: str, why: str, on: _date | None = None) -> str:
    """Build the one canonical `## Decided` entry shape, or refuse.

    Validation happens here rather than at the write site so a malformed decision is rejected before
    any routing choice is made: content with no rationale is wrong wherever it lands.
    """
    decision = _one_line(decision)
    why = _one_line(why)
    if not decision:
        raise WriteError("A decision needs text.")
    if "why:" in decision.lower():
        raise WriteError(
            "Pass the decision and its rationale separately; the `Why:` clause is added for you."
        )
    _require_reason(why)
    stamp = (on or utc_now().date()).isoformat()
    return f"- {stamp} — {_terminated(decision)} Why: {_terminated(why)}"


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter, body) as raw text.

    Deliberately not `vault.parse_frontmatter`: a writer must round-trip fields it does not
    understand, and reserializing parsed values would drop anything the parser's narrow YAML subset
    did not recognize.
    """
    if not text.startswith("---"):
        return "", text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return "", text
    return parts[1], parts[2]


def _patch_frontmatter(raw: str, updates: dict[str, str]) -> str:
    """Rewrite named top-level scalars in place, leaving every other line byte-identical."""
    lines = raw.split("\n")
    remaining = dict(updates)
    # Nested blocks (`claims:`) must stay last; new keys go after the final scalar so they do not
    # become children of whatever block happens to end the frontmatter.
    last_scalar = 0
    for i, line in enumerate(lines):
        if not line.strip() or line[0] in " \t":
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key = key.strip()
        if key in remaining:
            lines[i] = f"{key}: {remaining.pop(key)}"
            last_scalar = i
        elif value.strip():
            last_scalar = i
    for key, value in remaining.items():
        last_scalar += 1
        lines.insert(last_scalar, f"{key}: {value}")
    return "\n".join(lines)


def split_body(body: str) -> tuple[str, list[tuple[str, str]]]:
    """Split a brief body into (preamble, [(section, content)]) preserving document order."""
    preamble: list[str] = []
    sections: list[tuple[str, str]] = []
    current: str | None = None
    buffer: list[str] = []
    for line in body.splitlines():
        heading = _HEADING.match(line)
        if heading:
            if current is not None:
                sections.append((current, "\n".join(buffer).strip()))
            current = heading.group(1)
            buffer = []
        elif current is None:
            preamble.append(line)
        else:
            buffer.append(line)
    if current is not None:
        sections.append((current, "\n".join(buffer).strip()))
    return "\n".join(preamble).strip(), sections


def canonical_order(sections: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """Reorder to `vault.SECTIONS`, keeping unrecognized sections at the end.

    Section order is the cold-start read order and therefore part of the contract, so it is enforced
    on every write rather than trusted from whatever the file happened to contain. Unknown sections
    are carried rather than dropped — silently deleting a human's prose is worse than a schema drift
    the next reader can see.
    """
    known = dict(sections)
    ordered = []
    for name in vault.SECTIONS:
        content = known.get(name, "")
        if name == GROUND_TRUTH_SECTION and not content.strip():
            content = GROUND_TRUTH_TRANSCLUSION
        ordered.append((name, content))
    ordered.extend((name, content) for name, content in sections if name not in vault.SECTIONS)
    return ordered


def render_body(preamble: str, sections: list[tuple[str, str]]) -> str:
    blocks = [preamble] if preamble else []
    blocks += [f"## {name}\n\n{content}".rstrip() for name, content in sections]
    return "\n\n".join(blocks) + "\n"


def _stream_file(root: Path, slug: str, name: str) -> Path:
    if name not in OWNED_FILES:
        raise WriteError(
            f"{name} is not writable by this module. `{GROUND_TRUTH_FILE}` belongs to hooks and "
            f"`inbox/` to non-owners; one writer per file is what keeps them contention-free."
        )
    return vault.streams_dir(root) / slug / name


def _atomic_write(path: Path, text: str) -> None:
    """Replace via a temp file in the same directory.

    A brief is read by other sessions at arbitrary moments; a partially written one is worse than a
    stale one because nothing about it looks wrong.
    """
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".brief-write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _guard_archived(slug: str, front: str, reopen: bool) -> bool:
    """Refuse a write to an archived stream unless it is explicitly reopened. Returns True on reopen.

    Naming the slug proves the writer is certain *which* stream; it does not prove they meant to
    revive a closed one. Those are separate intents, and collapsing them leaves the vault in a state
    no reader can trust: archived on the dashboard, changing on disk.
    """
    state = str(vault.parse_frontmatter(front).get("state", REOPENED_STATE))
    if state not in vault.NON_RESOLVABLE_STATES:
        return False
    if not reopen:
        raise WriteError(
            f"Stream '{slug}' is {state}. Reopening is its own act, not a side effect of writing — "
            f"a stream the dashboard reports as closed while its content changes makes the operator "
            f"surface lie. Pass reopen=True (CLI: --reopen) to reopen it and then write, or pick an "
            f"active stream."
        )
    return True


def _reopen_entry(slug: str, state: str, on: _date) -> str:
    return (
        f"- {on.isoformat()} — Reopened this stream; it was {state}. Why: durable content arrived "
        f"after close, and leaving the state {state} would report a closed stream to the operator "
        f"while its brief keeps changing."
    )


def _rewrite_brief(
    root: Path, slug: str, mutate, *, now: datetime | None = None, reopen: bool = False
) -> Path:
    """Read, apply `mutate` to the section list, write back in canonical order."""
    path = _stream_file(root, slug, BRIEF_FILE)
    if not path.is_file():
        raise WriteError(f"No {BRIEF_FILE} at {path}.")

    front, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    if not front.strip():
        raise WriteError(
            f"{path} has no frontmatter. The frontmatter is the schema and the wire format; a brief "
            f"without it is malformed and rewriting it would guess at fields that were never there."
        )

    now = now or utc_now()
    updates = {"updated": _stamp(now)}
    preamble, sections = split_body(body)

    was = str(vault.parse_frontmatter(front).get("state", REOPENED_STATE))
    if _guard_archived(slug, front, reopen):
        # The reopen is recorded before the caller's content so the brief reads in the order the
        # events happened, and so the state change is never silent.
        updates["state"] = REOPENED_STATE
        sections = _replace(
            sections,
            DECIDED_SECTION,
            _append_bullet(_get(sections, DECIDED_SECTION), _reopen_entry(slug, was, now.date())),
        )

    sections = mutate(sections)
    front = _patch_frontmatter(front, updates)
    _atomic_write(
        path,
        "---\n" + front.strip("\n") + "\n---\n\n" + render_body(preamble, canonical_order(sections)),
    )
    return path


def _replace(sections: list[tuple[str, str]], name: str, content: str) -> list[tuple[str, str]]:
    if any(existing == name for existing, _ in sections):
        return [(existing, content if existing == name else body) for existing, body in sections]
    return sections + [(name, content)]


def _get(sections: list[tuple[str, str]], name: str) -> str:
    return dict(sections).get(name, "")


def _append_bullet(existing: str, bullet: str) -> str:
    """Append a list item, blank-separating it from any preceding prose.

    A bullet placed directly under a paragraph line renders as a continuation of that paragraph in
    Obsidian, which quietly hides the entry from the human reading the vault.
    """
    existing = existing.rstrip()
    if not existing:
        return bullet
    tail = existing.splitlines()[-1].lstrip()
    separator = "\n" if tail.startswith(("-", "*", "+")) or re.match(r"^\d+[.)]\s", tail) else "\n\n"
    return existing + separator + bullet


def _guard_section(name: str) -> None:
    if name == GROUND_TRUTH_SECTION:
        raise WriteError(
            f"## {GROUND_TRUTH_SECTION} is hook-written only. Mechanical facts come from hooks so "
            f"that agent claims and observed reality can be compared; an agent writing both erases "
            f"the signal. Record the claim in ## Decided or ## Open instead."
        )
    if name not in vault.SECTIONS:
        raise WriteError(f"'{name}' is not a brief section. Canonical: {', '.join(vault.SECTIONS)}.")


def append_decision(
    root: Path,
    slug: str,
    decision: str,
    why: str,
    *,
    on: _date | None = None,
    now: datetime | None = None,
    reopen: bool = False,
) -> Path:
    """Append one entry to `## Decided`. Requires an existing stream; see `record_decision` to route."""
    entry = format_decision(decision, why, on)

    def mutate(sections):
        return _replace(
            sections, DECIDED_SECTION, _append_bullet(_get(sections, DECIDED_SECTION), entry)
        )

    return _rewrite_brief(root, slug, mutate, now=now, reopen=reopen)


def update_section(
    root: Path,
    slug: str,
    name: str,
    content: str,
    *,
    now: datetime | None = None,
    reopen: bool = False,
) -> Path:
    """Replace a section wholesale. `## Decided` is excluded — it is append-only by design."""
    _guard_section(name)
    if name == DECIDED_SECTION:
        raise WriteError(
            f"## {DECIDED_SECTION} is append-only. Add with append_decision(); remove only via "
            f"archive_decision(), which requires the decision to already live somewhere else."
        )

    def mutate(sections):
        return _replace(sections, name, content.strip())

    return _rewrite_brief(root, slug, mutate, now=now, reopen=reopen)


def append_entry(
    root: Path,
    slug: str,
    name: str,
    text: str,
    *,
    now: datetime | None = None,
    reopen: bool = False,
) -> Path:
    """Append a single bullet to `## Open`, `## Next`, or `## Do not`."""
    _guard_section(name)
    if name == DECIDED_SECTION:
        raise WriteError(
            f"Use append_decision() for ## {DECIDED_SECTION}; its entries need a `Why:` clause."
        )
    bullet = "- " + _one_line(text)
    if not bullet.strip("- "):
        raise WriteError("Refusing to append an empty entry.")

    def mutate(sections):
        return _replace(sections, name, _append_bullet(_get(sections, name), bullet))

    return _rewrite_brief(root, slug, mutate, now=now, reopen=reopen)


def unwritable_reason(root: Path, slug: str | None) -> str:
    """Why this slug cannot be written to, or "" if it can.

    Certainty is the bar: anything short of an existing stream named outright is a guess, and a guess
    on the write path is the one failure mode `unassigned/` exists to absorb.
    """
    if not slug or not slug.strip():
        return "no explicit stream declared"
    slug = slug.strip()
    if _UNSAFE_SLUG.search(slug):
        return f"'{slug}' is not a valid stream slug"
    if not (vault.streams_dir(root) / slug / BRIEF_FILE).is_file():
        available = ", ".join(b.slug for b in vault.list_streams(root)) or "(none)"
        return f"no stream '{slug}' in the vault (available: {available})"
    return ""


def _fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _claim_fingerprint(directory: Path, digest: str, name: str) -> Path | None:
    """Claim `digest` for a file about to be written, or return the existing file that owns it.

    `O_EXCL` rather than an `exists()` check for the same reason the filename walk below uses it: two
    sessions ending in the same second must not both conclude they are the original. The kernel picks
    the winner and the loser is told which file already holds this content.

    A marker whose trace is gone (pruned, or hand-deleted from the vault) is stale, not authoritative.
    It is removed and the caller writes fresh — otherwise pruning a trace would permanently suppress
    the only record that the same situation recurred.
    """
    marker_dir = directory / FINGERPRINT_DIRNAME
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / f"{digest}.md"
    while True:
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            owner = marker.read_text(encoding="utf-8").strip()
            if owner and (directory / owner).is_file():
                return directory / owner
            marker.unlink(missing_ok=True)
            continue
        # Written through the exclusive descriptor, not reopened: a marker that exists but is still
        # empty would read as stale to a concurrent caller, which would delete a claim that had just
        # been legitimately made.
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(name)
        return None


def write_unassigned(
    root: Path,
    content: str,
    *,
    reason: str,
    session: str = "unknown",
    cwd: str | None = None,
    branch: str | None = None,
    repo: str | None = None,
    summary: str | None = None,
    proposed_stream: str | None = None,
    now: datetime | None = None,
    dedupe: bool = True,
) -> Path:
    """Drop unrouted content into `unassigned/` as a new file.

    One file per write, created exclusively, so concurrent sessions never contend and nothing is
    overwritten. `unassigned/` is a triage queue, not a graveyard — the metadata below is what makes
    an entry promotable into a real stream later.

    The header keys and the `<timestamp>-<session>.md` filename are a **shared contract with
    `traces.py`**, which writes below-bar session traces into the same directory. A second format
    there would mean triage has to know which writer produced a file before it can read it.

    **Byte-identical content is written once.** A session that ends, resumes, and ends again observes
    the same repository at the same commit every time, so without this one work item arrives as a
    dozen indistinguishable files and the queue stops being readable — which is the same as not
    having one. Only `created` is excluded from the identity, because it is the sole field that
    differs between those repeats. Anything a triage pass could act on differently produces a
    different fingerprint and therefore a new file.
    """
    now = now or utc_now()
    directory = root / UNASSIGNED_DIR
    directory.mkdir(parents=True, exist_ok=True)

    body = content.rstrip() + "\n"
    # Everything that identifies the content. `created` is deliberately absent — it is the one field
    # that differs between repeats of the same observation, so including it would defeat the dedupe.
    fields = [
        ("kind", "unresolved-write"),
        ("session", session),
        # One line saying what happened. First in the header after the identity fields because it is
        # the first thing triage reads and the only one that answers "is this worth a stream".
        ("summary", _one_line(summary or "") or "n/a"),
        ("repo", (repo or "").strip() or "n/a"),
        ("cwd", cwd or "n/a"),
        ("branch", branch or "n/a"),
        ("reason", _one_line(reason)),
        ("proposed_stream", (proposed_stream or "").strip() or "n/a"),
    ]
    fields.insert(1, ("created", _stamp(now)))
    text = "---\n" + "\n".join(f"{key}: {value}" for key, value in fields) + "\n---\n" + body

    digest = _fingerprint(
        "\0".join(f"{k}={v}" for k, v in fields if k != "created") + "\0" + body
    )

    stem = f"{_file_stamp(now)}-{re.sub(r'[^A-Za-z0-9_-]+', '-', session).strip('-') or 'unknown'}"
    for attempt in range(1, 1000):
        candidate = directory / (f"{stem}.md" if attempt == 1 else f"{stem}-{attempt}.md")
        if dedupe:
            existing = _claim_fingerprint(directory, digest, candidate.name)
            if existing is not None:
                return existing
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            # The fingerprint now points at a name this call lost the race for. Release it so the
            # retry can claim the digest for the name it actually writes.
            if dedupe:
                (directory / FINGERPRINT_DIRNAME / f"{digest}.md").unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        return candidate
    raise WriteError(f"Could not create a unique trace file in {directory}.")


def record_decision(
    root: Path,
    slug: str | None,
    decision: str,
    why: str,
    *,
    session: str = "unknown",
    cwd: str | None = None,
    branch: str | None = None,
    on: _date | None = None,
    now: datetime | None = None,
    reopen: bool = False,
) -> WriteOutcome:
    """Record a decision on `slug`, or route it to `unassigned/` when `slug` is not certain.

    Rationale is validated before routing: a decision missing its `Why:` is malformed, and dumping it
    into the triage queue would only move the problem somewhere nobody is looking.

    An archived stream raises rather than routing. Routing exists for *uncertainty* about which
    stream; here the stream is known and the lifecycle is the objection, so the caller has to answer
    it rather than have the content quietly filed somewhere else.
    """
    entry = format_decision(decision, why, on)
    now = now or utc_now()
    reason = unwritable_reason(root, slug)
    if reason:
        path = write_unassigned(
            root,
            entry,
            reason=reason,
            session=session,
            cwd=cwd,
            branch=branch,
            proposed_stream=slug,
            now=now,
        )
        return WriteOutcome(path=path, stream=None, unassigned=True, reason=reason)
    slug = str(slug).strip()
    return WriteOutcome(
        path=append_decision(root, slug, decision, why, on=on, now=now, reopen=reopen),
        stream=slug,
        unassigned=False,
    )


def archive_decision(
    root: Path,
    slug: str,
    match: str,
    pointer: str,
    *,
    now: datetime | None = None,
    reopen: bool = False,
) -> Path:
    """Move one `## Decided` entry to `decided-archive.md` with a pointer to its new home.

    Compaction, not truncation: an entry leaves the brief only once the decision *and* its rationale
    live somewhere a working agent already reads, which is why `pointer` is mandatory. Never evict by
    age or count — that keeps trivia and drops foundational decisions.
    """
    pointer = _one_line(pointer)
    if not pointer:
        raise WriteError(
            "Archiving needs a pointer to where the decision and its rationale now live. Without "
            "one this is truncation, not compaction."
        )

    brief = _stream_file(root, slug, BRIEF_FILE)
    if not brief.is_file():
        raise WriteError(f"No {BRIEF_FILE} at {brief}.")

    front, body = _split_frontmatter(brief.read_text(encoding="utf-8"))
    # Checked here as well as in _rewrite_brief because the archive file is appended first: a refusal
    # discovered after that append would leave the decision in both places.
    _guard_archived(slug, front, reopen)

    decided = _get(split_body(body)[1], DECIDED_SECTION)
    hits = [line for line in decided.splitlines() if line.strip().startswith("-") and match in line]
    if not hits:
        raise WriteError(f"No ## {DECIDED_SECTION} entry in '{slug}' contains {match!r}.")
    if len(hits) > 1:
        raise WriteError(
            f"{len(hits)} ## {DECIDED_SECTION} entries in '{slug}' contain {match!r}. Narrow the "
            f"match — archiving the wrong decision loses the rationale it was protecting."
        )
    entry = hits[0].strip()

    # Archive first, then remove. A crash between the two duplicates a decision, which a reader can
    # see and fix; the other order loses it silently.
    archive = _stream_file(root, slug, ARCHIVE_FILE)
    if archive.is_file():
        body = archive.read_text(encoding="utf-8").rstrip("\n")
    else:
        body = (
            "---\n"
            f"stream: {slug}\n"
            "note: Decisions encoded elsewhere. Archived from BRIEF.md; each carries a pointer to "
            "its new home.\n"
            "---\n"
        )
    _atomic_write(archive, f"{body}\n{entry} → {pointer}\n")

    def mutate(sections):
        kept = [ln for ln in _get(sections, DECIDED_SECTION).splitlines() if ln.strip() != entry]
        return _replace(sections, DECIDED_SECTION, "\n".join(kept).strip())

    _rewrite_brief(root, slug, mutate, now=now, reopen=reopen)
    return archive


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m plugin.lib.writer")
    parser.add_argument("--session", default="unknown", help="session reference, carried on traces")
    parser.add_argument("--cwd", default=None)
    parser.add_argument("--branch", default=None)
    parser.add_argument(
        "--reopen",
        action="store_true",
        help="reopen an archived stream before writing; flips state back to active and records it",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    decide = sub.add_parser("decide", help="append a decision, or route it to unassigned/")
    decide.add_argument("slug")
    decide.add_argument("decision")
    decide.add_argument("why")

    section = sub.add_parser("section", help="replace a section (never ## Ground truth or ## Decided)")
    section.add_argument("slug")
    section.add_argument("name")
    section.add_argument("content", help="'-' reads from stdin")

    entry = sub.add_parser("entry", help="append a bullet to ## Open, ## Next, or ## Do not")
    entry.add_argument("slug")
    entry.add_argument("name")
    entry.add_argument("text")

    archive = sub.add_parser("archive", help="move a decided entry to decided-archive.md")
    archive.add_argument("slug")
    archive.add_argument("match")
    archive.add_argument("pointer")
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        root = vault.resolve_vault()
        if args.command == "decide":
            # An empty slug is a legitimate argument, not a usage error: it is how a caller says
            # "I do not know the stream", and the answer is unassigned/ rather than a refusal.
            outcome = record_decision(
                root,
                args.slug,
                args.decision,
                args.why,
                session=args.session,
                cwd=args.cwd,
                branch=args.branch,
                reopen=args.reopen,
            )
            if outcome.unassigned:
                print(f"routed to {outcome.path} ({outcome.reason})")
            else:
                print(f"wrote {outcome.path}")
        elif args.command == "section":
            content = sys.stdin.read() if args.content == "-" else args.content
            print(f"wrote {update_section(root, args.slug, args.name, content, reopen=args.reopen)}")
        elif args.command == "entry":
            print(f"wrote {append_entry(root, args.slug, args.name, args.text, reopen=args.reopen)}")
        elif args.command == "archive":
            print(
                "archived to "
                f"{archive_decision(root, args.slug, args.match, args.pointer, reopen=args.reopen)}"
            )
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
