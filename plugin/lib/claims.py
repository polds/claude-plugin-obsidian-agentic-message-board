"""Claim lookup, session->stream resolution, and join-or-mint.

A stream's identity is its slug; repo, branch, worktree, issue and PR are mutable *claims* it holds.
Everything here reads those claims — nothing in this module writes, and nothing here is authoritative
about intent.

Two rules shape the whole file, and both come from measurement against a real repo rather than from
taste:

1. **Resolution is a read-path hint.** Git state is intent-blind: the same branch serves building,
   reviewing and debugging. So `resolve_for_read` may take a best guess, while `resolve_for_write`
   accepts nothing but an explicit, unambiguous, non-archived declaration. Reading the wrong brief is
   self-evident; writing the wrong one silently corrupts two streams.
2. **Ambiguity is the normal case.** One issue key legitimately spans eight live branches, so every
   lookup returns *all* candidates and refuses to pick when there is more than one.

Stdlib only. The single external dependency is `git`, invoked to validate a worktree claim, and it is
injectable so tests never need a repo.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

from . import vault

# Branches that describe a repo rather than a workstream. A trunk claim would match every session in
# the repo at once, which is indistinguishable from no claim at all — so it is rejected at the door
# rather than filtered at each lookup. `HEAD` is here because detached checkouts report it as a name.
NON_CLAIMABLE_BRANCHES = {"main", "master", "HEAD"}

# Branch naming is inconsistent in the wild — `PLAT-2039`, `plat-1921`, `polds/plat-1870-...`,
# `feat/PLAT-1719/gclb` — so the key is matched anywhere in the string rather than anchored to a
# prefix, and case is discarded. Two leading letters are required because a single-letter "project"
# is nearly always a version fragment (`v1-2-3`). A false positive like `utf-8` is tolerable: it only
# bites if some stream literally claims issue `UTF-8`, and the issue rung is a hint that can never
# authorize a write.
_ISSUE_KEY = re.compile(r"(?<![A-Za-z0-9])([A-Za-z]{2,}[A-Za-z0-9]*)-(\d{1,7})(?![0-9A-Za-z])")

# Dropped from derived slugs. Kept deliberately short: aggressive stopword lists mangle slugs whose
# meaning lives in a preposition, and the slug only has to be recognizable, not descriptive.
_SLUG_STOPWORDS = {
    "a", "an", "the", "to", "for", "of", "in", "on", "and", "or", "with", "from", "at", "by",
    "this", "that", "is", "it", "into",
}


class ClaimError(vault.VaultError):
    """Resolution or derivation refused. Subclasses VaultError so callers keep one except clause."""


@dataclass
class Hints:
    """What a session can observe about itself. Every field is optional and only `stream` is intent.

    The rest are ambient git facts, which is exactly why they are named hints.
    """

    stream: str | None = None
    worktree: str | None = None
    branch: str | None = None
    repo: str | None = None
    issue: str | None = None
    pr: str | None = None


@dataclass
class Resolution:
    """The outcome of the read ladder.

    `stream` is set only when exactly one candidate survives; `candidates` always carries everything
    that matched, so an ambiguous result is inspectable rather than a bare failure.
    """

    via: str
    stream: vault.Brief | None = None
    candidates: list[vault.Brief] = field(default_factory=list)
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return self.stream is not None

    @property
    def ambiguous(self) -> bool:
        return self.stream is None and len(self.candidates) > 1

    @property
    def is_guess(self) -> bool:
        """True when the match came from ambient git state rather than a declaration.

        Callers that inject a brief must label a guess as such: an agent told "this is your stream"
        behaves differently from one told "this might be your stream."
        """
        return self.via != "explicit"

    @property
    def write_safe(self) -> bool:
        return self.resolved and self.via == "explicit"


@dataclass
class MintDecision:
    """Join an existing stream, mint a new one, or refuse because the answer is ambiguous."""

    action: str  # "join" | "mint" | "ambiguous"
    stream: vault.Brief | None = None
    candidates: list[vault.Brief] = field(default_factory=list)
    slug: str = ""
    # Streams matching only the branch or worktree. Surfaced for an operator prompt, never joined
    # automatically: those claims are ephemeral, and a reviewer sharing an author's branch would
    # otherwise land in the author's stream.
    hint_candidates: list[vault.Brief] = field(default_factory=list)
    reason: str = ""


def parse_issue_keys(text: str | None) -> list[str]:
    """Every issue key in a string, uppercased, in order, deduped.

    Returns a list rather than one key because a branch name can carry two (a fix for one issue
    reviewed under another), and dropping the second would hide a legitimate ambiguity.
    """
    if not text:
        return []
    keys: list[str] = []
    for match in _ISSUE_KEY.finditer(text):
        key = f"{match.group(1).upper()}-{match.group(2)}"
        if key not in keys:
            keys.append(key)
    return keys


def _normalize_repo(value: str) -> str | None:
    text = value.strip().rstrip("/")
    if text.endswith(".git"):
        text = text[:-4]
    if "://" in text:
        text = text.split("://", 1)[1]
    if "@" in text and ":" in text:  # git@github.com:Owner/repo
        text = text.split(":", 1)[1]
    segments = [s for s in text.split("/") if s]
    if not segments:
        return None
    return "/".join(segments[-2:]).lower()


def _repo_keys(normalized: str) -> set[str]:
    """A repo matches on its full `owner/name` or on `name` alone.

    Claims are written by whoever happened to be looking at the repo, and `gh` and `git remote`
    disagree about which form they hand back.
    """
    return {normalized, normalized.rsplit("/", 1)[-1]}


def _normalize_pr(value: str) -> str | None:
    text = value.strip().lstrip("#").rstrip("/")
    if "/" in text:  # https://github.com/owner/repo/pull/905
        text = text.rsplit("/", 1)[-1]
    return text if text.isdigit() else None


def _normalize_branch(value: str) -> str | None:
    text = value.strip()
    if text.startswith("refs/heads/"):
        text = text[len("refs/heads/"):]
    if not text or text in NON_CLAIMABLE_BRANCHES:
        return None
    return text


def _normalize_worktree(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    # realpath, not just absolute: worktrees live under symlinked roots (/tmp -> /private/tmp on
    # macOS), and two spellings of one directory must compare equal.
    return str(Path(text).expanduser().resolve())


def normalize_claim(kind: str, value: object) -> str | None:
    """Canonical form of one claim value, or None when the value cannot be a claim at all.

    None is a real answer, not an error: `main` is a branch but never a claim.
    """
    if kind not in vault.CLAIM_KINDS:
        raise ClaimError(f"Unknown claim kind '{kind}'. Known kinds: {', '.join(vault.CLAIM_KINDS)}")
    if value is None:
        return None
    text = str(value)
    if kind == "repo":
        return _normalize_repo(text)
    if kind == "branch":
        return _normalize_branch(text)
    if kind == "worktree":
        return _normalize_worktree(text)
    if kind == "issue":
        keys = parse_issue_keys(text)
        return keys[0] if keys else None
    return _normalize_pr(text)


def claims_match(brief: vault.Brief, kind: str, value: object) -> bool:
    """Does `brief` hold this claim? Both sides are normalized, so stored junk simply never matches."""
    target = normalize_claim(kind, value)
    if target is None:
        return False
    for raw in brief.claim(kind):
        held = normalize_claim(kind, raw)
        if held is None:
            continue
        if kind == "repo":
            if _repo_keys(held) & _repo_keys(target):
                return True
        elif held == target:
            return True
    return False


def find_streams(
    streams: Sequence[vault.Brief],
    kind: str,
    value: object,
    include_archived: bool = False,
) -> list[vault.Brief]:
    """All streams holding a claim. Archived streams are excluded unless explicitly asked for."""
    return [
        brief
        for brief in streams
        if (include_archived or brief.resolvable) and claims_match(brief, kind, value)
    ]


def find_streams_claiming_pr(
    streams: Sequence[vault.Brief],
    number: object,
    repo: object = None,
    include_archived: bool = False,
) -> list[vault.Brief]:
    """PR lookup, narrowed by repo when both sides declare one.

    PR numbers are only unique within a repo, so `905` alone can collide. Streams that declare no
    repo survive the narrowing: unknown is not the same as mismatched, and keeping them preserves an
    ambiguity the caller should see.
    """
    matches = find_streams(streams, "pr", number, include_archived)
    if repo is None or len(matches) < 2:
        return matches
    return [b for b in matches if not b.claim("repo") or claims_match(b, "repo", repo)] or matches


def git_branch_at(path: str) -> str | None:
    """Branch currently checked out at a path, or None if it isn't a working tree."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def worktree_claim_is_live(
    brief: vault.Brief,
    path: object,
    branch_at: Callable[[str], str | None] | None = None,
) -> bool:
    """Validate a worktree claim at the moment of use.

    Measured against a real repo: 53 worktrees across six tools, several prunable, several under
    session-scoped `/private/tmp` paths that vanish. A worktree claim is therefore a hint with a
    shelf life — the path must still exist, and if the stream also claims branches, the checkout
    must still be on one of them. A stream with no branch claim (review and design streams have
    none) can only be checked for existence.
    """
    resolver = git_branch_at if branch_at is None else branch_at
    target = normalize_claim("worktree", path)
    if target is None or not Path(target).is_dir():
        return False
    expected = [b for b in (normalize_claim("branch", raw) for raw in brief.claim("branch")) if b]
    if not expected:
        return True
    current = resolver(target)
    current = normalize_claim("branch", current) if current else None
    return current is not None and current in expected


def _rung(via: str, candidates: list[vault.Brief]) -> Resolution | None:
    """One step of the read ladder. None means "no match, keep descending"."""
    if not candidates:
        return None
    if len(candidates) == 1:
        return Resolution(via=via, stream=candidates[0], candidates=candidates)
    return Resolution(
        via=via,
        candidates=candidates,
        reason=(
            f"{len(candidates)} streams match on {via} "
            f"({', '.join(b.slug for b in candidates)}); declare one explicitly"
        ),
    )


def resolve_for_read(
    streams: Sequence[vault.Brief],
    hints: Hints,
    branch_at: Callable[[str], str | None] | None = None,
) -> Resolution:
    """Best-guess stream for a read. Never authorizes a write.

    Ladder: explicit -> validated worktree -> branch -> issue key -> stop. An ambiguous rung stops
    the descent rather than falling through: the lower rungs are strictly weaker evidence, so
    continuing past a genuine multi-match would trade a visible ambiguity for an invisible guess.
    """
    live = [b for b in streams if b.resolvable]

    if hints.stream:
        named = [b for b in streams if b.slug == hints.stream]
        if not named:
            return Resolution(
                via="explicit",
                reason=f"no stream '{hints.stream}' in the vault",
            )
        if not named[0].resolvable:
            return Resolution(
                via="explicit",
                candidates=named,
                reason=(
                    f"stream '{hints.stream}' is {named[0].state}; archived streams stay readable "
                    "by slug but are never resolved into"
                ),
            )
        return Resolution(via="explicit", stream=named[0], candidates=named)

    if hints.worktree:
        matched = [
            b
            for b in find_streams(live, "worktree", hints.worktree)
            if worktree_claim_is_live(b, hints.worktree, branch_at=branch_at)
        ]
        rung = _rung("worktree", matched)
        if rung:
            return rung

    if hints.branch:
        rung = _rung("branch", find_streams(live, "branch", hints.branch))
        if rung:
            return rung

    keys = parse_issue_keys(hints.issue) or parse_issue_keys(hints.branch)
    if keys:
        matched: list[vault.Brief] = []
        for key in keys:
            for brief in find_streams(live, "issue", key):
                if brief not in matched:
                    matched.append(brief)
        rung = _rung("issue", matched)
        if rung:
            return rung

    return Resolution(via="none", reason="no explicit stream and no hint matched")


def resolve_for_write(streams: Sequence[vault.Brief], hints: Hints) -> vault.Brief:
    """The write path. Explicit, existing, non-archived — or it fails.

    Git hints are deliberately ignored here even when they are unambiguous. The same branch serves
    building, reviewing and debugging, so an unambiguous branch match is still an ambiguous
    *intent*, and a wrong write poisons two streams at once.
    """
    if not hints.stream:
        raise ClaimError(
            "Writes require an explicit stream. Git state is intent-blind, so branch and worktree "
            "hints are never sufficient — declare `stream: <slug>` or route the content to "
            "unassigned/ for triage."
        )
    named = [b for b in streams if b.slug == hints.stream]
    if not named:
        available = ", ".join(b.slug for b in streams) or "(none)"
        raise ClaimError(f"No stream '{hints.stream}'. Available: {available}")
    if not named[0].resolvable:
        raise ClaimError(
            f"Stream '{hints.stream}' is {named[0].state}. Reopen it explicitly or mint a new "
            "stream; archived streams are readable but never written to."
        )
    return named[0]


def slugify(text: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def derive_slug(task: str = "", issue: object = None, max_words: int = 5) -> str:
    """Kebab-case slug, ticket ID leading when present.

    Deliberately the same derivation `start-task` already performs at branch-naming time
    (`plat-192-fix-erpc-scrape`). A second scheme would produce two slugs for one piece of work,
    which is the duplicate-stream bug wearing a different hat.
    """
    keys = parse_issue_keys(str(issue)) if issue else parse_issue_keys(task)
    lead = keys[0].lower() if keys else ""

    # Strip every key from the descriptive text so a ticket mentioned inline is not repeated.
    words = [w for w in slugify(_ISSUE_KEY.sub(" ", task or "")).split("-") if w]
    words = [w for w in words if w not in _SLUG_STOPWORDS]
    words = words[: max(max_words - (1 if lead else 0), 0)]

    slug = "-".join(([lead] if lead else []) + words)
    if not slug:
        raise ClaimError("Cannot derive a slug: no ticket ID and no usable words in the task.")
    return slug


def ensure_unique_slug(slug: str, taken: Sequence[str]) -> str:
    """Append a numeric discriminator on collision, matching `start-task`'s branch-collision rule."""
    existing = set(taken)
    if slug not in existing:
        return slug
    n = 2
    while f"{slug}-{n}" in existing:
        n += 1
    return f"{slug}-{n}"


def claims_from_hints(hints: Hints) -> dict[str, list[str]]:
    """Claims to register on a newly minted stream.

    Every kind is present even when empty, matching the schema — an absent key and an empty list
    read differently to a human, and the schema promises all five.
    """
    claims: dict[str, list[str]] = {kind: [] for kind in vault.CLAIM_KINDS}
    for kind, raw in (
        ("repo", hints.repo),
        ("branch", hints.branch),
        ("worktree", hints.worktree),
        ("pr", hints.pr),
    ):
        value = normalize_claim(kind, raw) if raw else None
        if value:
            claims[kind] = [value]
    claims["issue"] = parse_issue_keys(hints.issue) or parse_issue_keys(hints.branch)
    return claims


def join_or_mint(
    streams: Sequence[vault.Brief],
    hints: Hints,
    task: str = "",
    max_words: int = 5,
) -> MintDecision:
    """Join the stream already covering this work, or propose a slug for a new one.

    Only durable claims — an explicit slug, a PR, an issue key — can trigger a join. Branch and
    worktree matches are returned as `hint_candidates` for a prompt instead: they are ephemeral, and
    joining on them would silently merge a reviewer's stream into the author's.

    Archived streams never join. A closed review of PR 905 must not swallow a fresh session on the
    same PR — that stream's conclusions are final.
    """
    live = [b for b in streams if b.resolvable]
    hint_candidates: list[vault.Brief] = []
    for kind, value in (("branch", hints.branch), ("worktree", hints.worktree)):
        for brief in find_streams(live, kind, value) if value else []:
            if brief not in hint_candidates:
                hint_candidates.append(brief)

    if hints.stream:
        named = [b for b in live if b.slug == hints.stream]
        if named:
            return MintDecision(
                action="join",
                stream=named[0],
                candidates=named,
                slug=named[0].slug,
                hint_candidates=hint_candidates,
                reason="explicit stream declaration",
            )
        return MintDecision(
            action="mint",
            slug=ensure_unique_slug(hints.stream, [b.slug for b in streams]),
            hint_candidates=hint_candidates,
            reason="explicit slug, no existing stream",
        )

    if hints.pr:
        matched = find_streams_claiming_pr(live, hints.pr, hints.repo)
        if len(matched) == 1:
            return MintDecision(
                action="join",
                stream=matched[0],
                candidates=matched,
                slug=matched[0].slug,
                hint_candidates=hint_candidates,
                reason=f"an active stream already claims PR {hints.pr}",
            )
        if matched:
            return MintDecision(
                action="ambiguous",
                candidates=matched,
                hint_candidates=hint_candidates,
                reason=(
                    f"{len(matched)} streams claim PR {hints.pr} "
                    f"({', '.join(b.slug for b in matched)}); pick one explicitly"
                ),
            )

    keys = parse_issue_keys(hints.issue) or parse_issue_keys(hints.branch)
    matched = []
    for key in keys:
        for brief in find_streams(live, "issue", key):
            if brief not in matched:
                matched.append(brief)
    if len(matched) == 1:
        return MintDecision(
            action="join",
            stream=matched[0],
            candidates=matched,
            slug=matched[0].slug,
            hint_candidates=hint_candidates,
            reason=f"an active stream already claims {keys[0]}",
        )
    if matched:
        # One issue spanning many streams is normal, not broken: PLAT-1719 has eight live branches.
        # Minting anyway would duplicate; picking one would guess. So refuse and hand back all of them.
        return MintDecision(
            action="ambiguous",
            candidates=matched,
            hint_candidates=hint_candidates,
            reason=(
                f"{len(matched)} streams claim {', '.join(keys)} "
                f"({', '.join(b.slug for b in matched)}); join one explicitly or mint with an "
                "explicit slug"
            ),
        )

    proposed = derive_slug(task=task, issue=hints.issue or hints.branch, max_words=max_words)
    return MintDecision(
        action="mint",
        slug=ensure_unique_slug(proposed, [b.slug for b in streams]),
        hint_candidates=hint_candidates,
        reason="no durable claim matched an existing stream",
    )
