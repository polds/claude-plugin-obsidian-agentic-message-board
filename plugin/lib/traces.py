"""Traces for sessions below the mint bar, and the triage queue they land in.

The mint bar is deliberately strict — a decision with its rationale, a blocker needing someone else,
an artifact others build on — so most sessions mint nothing. That is only non-lossy if the sessions
that fall short still leave something behind, which is what a trace is: one line saying what happened,
plus enough context (working directory, branch, session reference) to promote it into a real stream
later. Strict bar, cheap recovery.

**The file format is not ours.** `writer.write_unassigned` already creates queue files in
`unassigned/`, so recording a trace calls straight into it rather than re-rendering the same header
here. A second renderer would drift, and a drifted header splits the triage queue into two halves that
need two parsers to read — the exact failure the shared contract exists to prevent. Every file in the
queue therefore carries the same keys (`kind`, `created`, `session`, `repo`, `cwd`, `branch`,
`reason`, `proposed_stream`) and the same `<timestamp>-<session>.md` name. `reason` is what
distinguishes a below-bar trace from a write that could not be routed; both are triage work either
way.

**`unassigned/` is a queue, not a graveyard.** A queue nobody can see is a queue nobody works, and an
unworked queue makes strict-bar minting lossy in practice no matter what git retains — so listing and
triage are first-class here, not an afterthought.

**Triage acts on work items, not on traces.** A trace records what one *session* saw; nothing is ever
decided per session. The decision is "does this work deserve a stream", asked once per (repository,
branch) — so the queue groups on that, `adopt` resolves a whole group at once by building the stream
from the group's own contents, and `dismiss` gives noise a deliberate exit. `promote` remains for the
one case adoption cannot express: a single trace belonging somewhere other than the rest of its group.

**Pruning is opt-in, refuses outside git, and never quietly empties the backlog.** Sweeping is
recoverable *only* because the vault is a git repo, so that is checked rather than assumed. And what
sweeps by default is the *triaged* half of the queue — adopted or dismissed, so the call is already
on record and dropping the queue copy is the queue draining as designed. An untriaged trace is held
no matter how old, because age-sweeping it is exactly how the design's top residual risk hides: the
backlog disappears and the operator never learns that triage stopped happening.

Stdlib only, like the rest of `plugin/lib`.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from . import claims as claims_lib, inbox, mint, vault, writer

# Reused, not restated: one queue directory name across writer.py and this module. A local copy would
# be a second place to change and a silent way for the two writers to stop sharing a queue.
UNASSIGNED_DIR = writer.UNASSIGNED_DIR

# Stamp format `writer` writes into `created`. Kept here so traces can be *dated* (pruning needs an
# age), and pinned by a test against writer's actual output rather than trusted.
ISO_STAMP = "%Y-%m-%dT%H:%M:%SZ"

# Why a trace exists, in the field the queue already uses to say why a file is here. The origin lives
# in `reason` rather than in a new `kind` value so that triage keeps one parser for the whole queue.
BELOW_BAR_REASON = "session ended below the mint bar"

# Promotion markers sit in a dot-directory for the same reason inbox folds do: Obsidian's file tree
# should read as a list of waiting work, not work interleaved with bookkeeping.
PROMOTED_DIRNAME = ".promoted"

PROMOTION_KIND = "promoted-trace"

# Dismissal is the verb the queue was missing. Without it the only exits are promotion and age, so
# noise can never be cleared deliberately — and a backlog number that counts noise is a number the
# operator learns to ignore, which is exactly how triage stops happening.
#
# It is a marker beside `.promoted`, not a deletion, because "nobody ever looked at this" and
# "someone looked and judged it not worth a stream" are different facts and only the second one is
# evidence that the queue is being worked.
DISMISSED_DIRNAME = ".dismissed"

# A default, never a policy. Retention is the operator's call — this is only what `--days` starts at.
DEFAULT_RETENTION_DAYS = 30

# What a work item is: one repository at one branch. Grouping on it is the whole reframe — a session
# is what *produced* a trace, but it is never what the operator wants to act on. Nine traces from one
# branch are one decision ("is this a stream?"), and presenting them as nine decisions is what made
# the queue unworkable.
WORK_ITEM_KEYS = ("repo", "branch")

# Branches that identify a repository rather than a piece of work. The design already refuses to
# *claim* one — "it would resolve everything, and therefore nothing" — and grouping has the same
# defect for the same reason: on a feature branch the branch is the work, on a default branch it is
# just where everybody happens to be standing. Thirteen unrelated sessions were being offered for
# adoption into a single stream named `main`.
#
# So a default branch falls through to the session, exactly as a missing repo falls through to the
# directory. That is per-session triage, which the reframe otherwise rejects — but here it is honest:
# nothing binds these sessions together, and pretending otherwise produces a stream whose contents
# contradict its goal.
DEFAULT_BRANCHES = {"main", "master", "trunk", "develop", "default"}

# Enough of a session id to be unique in a queue while staying typeable.
SESSION_REF_CHARS = 8

# Placeholder goal for a stream minted by adoption. Deliberately conspicuous: minting is justified
# (the operator looked at the queue and judged the work real), but the *goal* is genuinely unwritten,
# and a plausible-sounding invented one is worse than an obviously blank one. `adopt` also files an
# operator ask in `## Open`, so the dashboard reports the gap rather than hiding it.
ADOPTED_GOAL = "TODO — adopted from the triage queue; the objective has not been written yet."

# How many distinct session summaries a listing shows per work item before deferring to `show`.
# Enough to recognize the work, few enough that a wide queue still fits on a screen.
SUMMARY_LINES = 3

ADOPTED_ASK = (
    "Write this stream's `## Goal`. It was adopted from {count} unassigned trace(s) and has only a "
    "placeholder. `who: operator`"
)

# Field order used when a trace is promoted. Explicit labels rather than a dump of the raw header:
# the promoted entry is read by a human and an agent in the target stream, and `parse_promotion`
# below reads it back, which is what makes "promoted without loss" a machine-checkable claim.
PROMOTED_FIELDS = [
    ("summary", "Summary"),
    ("session", "Session"),
    ("repo", "Repo"),
    ("cwd", "Working directory"),
    ("branch", "Branch"),
    ("created", "Recorded"),
    ("reason", "Reason"),
    ("proposed_stream", "Proposed stream"),
    ("trace", "Trace file"),
]

ABSENT = "n/a"

# How this command was invoked, so printed commands are ones the operator can actually paste. The
# wrapper in `bin/traces` sets this; run as a module, the module path is what works. Printing one
# form unconditionally would put a command that does not run in front of whoever used the other —
# the same class of bug as a SKILL.md documenting a flag that does not exist.
PROG_ENV = "MESSAGE_BOARD_CLI"
MODULE_PROG = "python3 -m plugin.lib.traces"


def prog() -> str:
    return os.environ.get(PROG_ENV, "").strip() or MODULE_PROG


class TraceError(vault.VaultError):
    """A refused record, promotion, or prune. Subclasses VaultError so callers keep one except clause."""


@dataclass
class Trace:
    """One queue file, parsed. Fields mirror the shared header exactly — nothing is derived away."""

    path: Path
    kind: str = ""
    created: str = ""
    session: str = ""
    repo: str = ""
    cwd: str = ""
    branch: str = ""
    reason: str = ""
    proposed_stream: str = ""
    summary: str = ""
    details: str = ""
    promoted_stream: str = ""
    promoted_at: str = ""
    promoted_by: str = ""
    promoted_entry: str = ""
    dismissed_at: str = ""
    dismissed_by: str = ""
    dismissed_why: str = ""

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def promoted(self) -> bool:
        return bool(self.promoted_at)

    @property
    def dismissed(self) -> bool:
        return bool(self.dismissed_at)

    @property
    def triaged(self) -> bool:
        """Someone made a call on this one, either way.

        The distinction that matters for the backlog is *looked at* versus *not looked at* — not
        which way the call went. An untriaged count that silently includes deliberately-dismissed
        noise reports a stalled queue that is actually being worked.
        """
        return self.promoted or self.dismissed

    def work_item_key(self) -> tuple[str, str, str]:
        """(repo, branch, discriminator) — the third slot only when the first two identify nothing.

        Sessions outside any checkout have neither repo nor branch, and grouping them all under
        `n/a @ n/a` merges unrelated directories into one undismissable blob whose only handle is a
        listing index that renumbers. A session on a default branch has the same problem for a
        different reason: `main` names the repository, not the work.

        Both fall through to something finer — the working directory in the first case, the session
        in the second — so each stays separately addressable.
        """
        repo = self.repo or ABSENT
        branch = self.branch or ABSENT
        if repo == ABSENT and branch == ABSENT:
            return (repo, branch, self.cwd or ABSENT)
        if branch.lower() in DEFAULT_BRANCHES:
            return (repo, branch, f"session:{self.session or 'unknown'}")
        return (repo, branch, "")

    def created_at(self) -> datetime | None:
        """The `created` stamp as a datetime, or None when it cannot be read.

        Unparseable is a real state, not an error: a hand-edited or future-format file must still
        list. Pruning treats None as un-datable and therefore un-prunable.
        """
        try:
            return datetime.strptime(self.created, ISO_STAMP).replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def age_days(self, now: datetime) -> float | None:
        created = self.created_at()
        if created is None:
            return None
        return (now - created).total_seconds() / 86400


@dataclass
class Promotion:
    """Where a promoted trace landed. Both paths are returned so the caller can report the move."""

    trace: Trace
    stream: str
    entry: Path
    marker: Path


@dataclass
class PruneResult:
    """What a prune did, or would do, split by promotion state.

    The split is the point, not a detail of the report. Promoted traces sweeping is the queue
    draining; unpromoted traces piling up is triage having stalled, and those two numbers moving in
    opposite directions is the only warning the operator gets. So `held_back` and `backlog` are
    computed for every prune — including one that sweeps nothing.
    """

    window_days: int
    applied: bool
    now: datetime
    include_untriaged: bool = False
    swept: list[Trace] = field(default_factory=list)
    kept: list[Trace] = field(default_factory=list)

    @property
    def paths(self) -> list[Path]:
        return [trace.path for trace in self.swept]

    @property
    def swept_triaged(self) -> list[Trace]:
        """Promoted or dismissed. Either way somebody made the call, so the queue copy is redundant."""
        return [trace for trace in self.swept if trace.triaged]

    @property
    def swept_untriaged(self) -> list[Trace]:
        """Only ever non-empty when the caller asked for it explicitly."""
        return [trace for trace in self.swept if not trace.triaged]

    @property
    def held_back(self) -> list[Trace]:
        """Untriaged traces past the window that were kept anyway — the stalled-triage signal."""
        return [
            trace
            for trace in self.kept
            if not trace.triaged and (trace.age_days(self.now) or 0) > self.window_days
        ]

    @property
    def backlog(self) -> list[Trace]:
        """Every untriaged trace still in the queue after this prune, oldest first."""
        return [trace for trace in self.kept if not trace.triaged]

    @property
    def oldest_untriaged(self) -> Trace | None:
        """The front of the queue. Its age is how long triage has been stalled."""
        backlog = self.backlog
        return backlog[0] if backlog else None


def unassigned_dir(root: Path) -> Path:
    return root / UNASSIGNED_DIR


def _one_line(text: str) -> str:
    """A trace is one line by design; an embedded newline would push content below the header's
    first line where a queue listing never shows it."""
    return " ".join(str(text).split())


def record(
    root: Path,
    summary: str,
    *,
    session: str = "unknown",
    cwd: str | None = None,
    branch: str | None = None,
    repo: str | None = None,
    reason: str = BELOW_BAR_REASON,
    proposed_stream: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Drop one below-bar session trace into `unassigned/` and return its path.

    Delegates the actual file creation to `writer.write_unassigned`: same header keys, same
    `<timestamp>-<session>.md` name, same `O_CREAT|O_EXCL` suffix walk, so two sessions ending in the
    same second get two files instead of one overwriting the other. (An `exists()` pre-check would put
    that race straight back — the kernel picking the winner is the whole point.)
    """
    summary = _one_line(summary)
    if not summary:
        raise TraceError(
            "A trace needs a one-line summary of what the session did. A trace with no content is "
            "not cheap recovery, it is a file the triage queue has to open to learn nothing."
        )
    return writer.write_unassigned(
        root,
        summary,
        reason=reason or BELOW_BAR_REASON,
        session=session,
        cwd=cwd,
        branch=branch,
        repo=repo,
        summary=summary,
        proposed_stream=proposed_stream,
        now=now,
    )


def _split_file(text: str) -> tuple[str, str]:
    """Return (frontmatter, body). Mirrors inbox's splitter: leading `---`, header, rest."""
    parts = text.split("---", 2)
    if len(parts) < 3 or parts[0].strip():
        return "", text
    return parts[1], parts[2]


def read_trace(
    path: Path,
    markers: dict[str, dict[str, str]] | None = None,
    dismissals: dict[str, dict[str, str]] | None = None,
) -> Trace:
    """Parse one queue file. Missing keys read as empty rather than raising — triage must be able to
    list a malformed entry, since a file nobody can see is a file nobody fixes.

    `repo` is read from the header when present and recovered from the rendered body when it is not,
    so traces written before the header carried it still group correctly instead of collecting in an
    "unknown" bucket that nobody can act on.
    """
    front_raw, body = _split_file(path.read_text(encoding="utf-8"))
    front = vault.parse_frontmatter(front_raw)
    marker = (markers or {}).get(path.name, {})
    dismissal = (dismissals or {}).get(path.name, {})
    repo = str(front.get("repo", "")).strip()
    summary = str(front.get("summary", "")).strip()
    return Trace(
        path=path,
        kind=str(front.get("kind", "")),
        created=str(front.get("created", "")),
        session=str(front.get("session", "")),
        repo=repo if repo and repo != ABSENT else _repo_from_body(body),
        cwd=str(front.get("cwd", "")),
        branch=str(front.get("branch", "")),
        reason=str(front.get("reason", "")),
        proposed_stream=str(front.get("proposed_stream", "")),
        # Header first, body second. The header is where a summary belongs, but traces written
        # before that key existed carry theirs as the whole body, and those must still be readable.
        summary=(summary if summary and summary != ABSENT else _summary_from_body(body)),
        details=body.strip(),
        promoted_stream=str(marker.get("stream", "")),
        promoted_at=str(marker.get("promoted", "")),
        promoted_by=str(marker.get("promoted_by", "")),
        promoted_entry=str(marker.get("entry", "")),
        dismissed_at=str(dismissal.get("dismissed", "")),
        dismissed_by=str(dismissal.get("dismissed_by", "")),
        dismissed_why=str(dismissal.get("why", "")),
    )


# `session_end` renders ground truth as `- **Repo:** owner/name`. Reading it back is a compatibility
# shim for traces written before `repo` was a header key, not a second source of truth: the header
# always wins when it has a value.
_BODY_REPO = re.compile(r"^\s*-\s*\*\*Repo:\*\*\s*(.+?)\s*$", re.MULTILINE)

# `- **Commit:** abc123 subject` — an observed fact, not a description of the work.
_FACT_LINE = re.compile(r"^\s*-\s*\*\*[^*]+:\*\*")


def _summary_from_body(body: str) -> str:
    """A pre-header trace's summary, or "" when its body is a fact list rather than a description.

    Old `record` traces are a single descriptive line and should still read as a summary. Old
    hook-written traces are a block of observed facts, and showing the first bullet of one as the
    summary is worse than admitting nothing was captured — it looks like an answer.
    """
    body = body.strip()
    if not body or "\n" in body or _FACT_LINE.match(body):
        return ""
    return body


def _repo_from_body(body: str) -> str:
    match = _BODY_REPO.search(body)
    if not match:
        return ""
    value = match.group(1).strip()
    return "" if value in (ABSENT, "unknown", "not a git repository") else value


def _markers(directory: Path, dirname: str = PROMOTED_DIRNAME) -> dict[str, dict[str, str]]:
    marker_dir = directory / dirname
    if not marker_dir.is_dir():
        return {}
    markers = {}
    for path in marker_dir.glob("*.md"):
        if path.is_file():
            front_raw, _ = _split_file(path.read_text(encoding="utf-8"))
            markers[path.name] = {k: str(v) for k, v in vault.parse_frontmatter(front_raw).items()}
    return markers


def list_traces(root: Path) -> list[Trace]:
    """Everything waiting in `unassigned/`, oldest first.

    Oldest first because the queue is worked from the front: the trace most likely to have lost its
    context is the one that has been sitting longest, and it is also the one closest to being pruned.
    Undatable entries sort first for the same reason — they are the ones needing a human.

    Length breaks ties before the name does, because `writer`'s collision walk appends `-2`, `-3`, …
    *before* the extension and those sort ahead of the unsuffixed original under a plain string
    comparison ('-' < '.'). Within a single second that silently reverses creation order — and
    adoption reads working directories in listing order, so the reversal would land in a stream's
    claims.
    """
    directory = unassigned_dir(root)
    if not directory.is_dir():
        return []
    markers = _markers(directory, PROMOTED_DIRNAME)
    dismissals = _markers(directory, DISMISSED_DIRNAME)
    traces = [read_trace(p, markers, dismissals) for p in directory.glob("*.md") if p.is_file()]
    return sorted(traces, key=lambda t: (t.created, len(t.name), t.name))


def pending(root: Path) -> list[Trace]:
    """The actual work queue: what nobody has made a call on yet."""
    return [trace for trace in list_traces(root) if not trace.triaged]


@dataclass
class WorkItem:
    """One repository at one branch, and every trace that observed it.

    This is the unit triage acts on. A trace records what a *session* saw; nobody decides anything
    per-session. The decision is "does this work deserve a stream", and that question is asked once
    per work item no matter how many sessions touched it.
    """

    repo: str
    branch: str
    # Identity of last resort for a session that ran outside any checkout. Never a claim — a
    # directory is a worktree, and `adopt` registers it as one rather than as a branch.
    where: str = ""
    # Set when the branch is a default branch and therefore identifies the repo, not the work. Such
    # items are shown clustered under their repo so the listing does not become a wall, but they are
    # separate items because nothing binds them together.
    session: str = ""
    traces: list[Trace] = field(default_factory=list)
    claimed_by: str = ""

    @property
    def count(self) -> int:
        return len(self.traces)

    @property
    def label(self) -> str:
        if self.session:
            subject = self.summaries[0] if self.summaries else "(no subject recorded)"
            return f"{subject}  [{self.session_ref}]"
        return f"{self.repo} @ {self.branch or ABSENT}" + (f" ({self.where})" if self.where else "")

    @property
    def worktrees(self) -> list[str]:
        """Every distinct working directory that produced a trace here, first-seen order.

        Plural on purpose: one branch is routinely checked out in a main clone and in one or more
        tool-managed worktrees, and a stream that claims only the first will not resolve from the
        others.
        """
        seen: list[str] = []
        for trace in self.traces:
            if trace.cwd and trace.cwd != ABSENT and trace.cwd not in seen:
                seen.append(trace.cwd)
        return seen

    @property
    def key(self) -> str:
        tail = self.where or self.session
        return f"{self.repo}@{self.branch}" + (f"@{tail}" if tail else "")

    @property
    def cluster(self) -> str:
        """The `repo@branch` these session-scoped items share, or "" for an ordinary item.

        A cluster is a display grouping and a bulk-dismiss handle, never an adoptable thing: the
        whole reason these are separate items is that they are separate work.
        """
        return f"{self.repo}@{self.branch}" if self.session else ""

    @property
    def session_ref(self) -> str:
        return self.session[:SESSION_REF_CHARS] if self.session else ""

    def selector(self, among: Sequence["WorkItem"] = (), index: int = 0) -> str:
        """The shortest identifier that still names this item after the queue changes.

        Not cosmetic. Indices renumber the moment anything is adopted or dismissed, so a second
        command copied from the same listing targets a different work item than the operator read —
        and unlike an out-of-range index, a shifted-but-valid one fails silently by succeeding.
        """
        if self.session:
            return self.session_ref
        if self.where:
            return self.where
        if self.branch != ABSENT:
            same = [item for item in among if item.branch == self.branch]
            return self.branch if len(same) <= 1 else self.key
        if self.repo != ABSENT:
            return self.key
        return str(index)

    @property
    def summaries(self) -> list[str]:
        """What the sessions here were about, deduplicated, in the order first seen.

        Deduplicated because the common case is one session ending several times with the same
        title, and repeating it makes the item look like more distinct work than it is.
        """
        seen: list[str] = []
        for trace in self.traces:
            line = " ".join(trace.summary.split())
            if line and line not in (ABSENT, writer.NO_SUMMARY) and line not in seen:
                seen.append(line)
        return seen

    @property
    def unsummarized(self) -> int:
        """Traces whose session subject was never captured. Counted, never printed as a summary."""
        return sum(
            1
            for trace in self.traces
            if " ".join(trace.summary.split()) in ("", ABSENT, writer.NO_SUMMARY)
        )

    @property
    def sessions(self) -> list[str]:
        seen: list[str] = []
        for trace in self.traces:
            if trace.session and trace.session not in seen:
                seen.append(trace.session)
        return seen

    def age_span(self, now: datetime) -> tuple[float | None, float | None]:
        ages = [t.age_days(now) for t in self.traces]
        known = [a for a in ages if a is not None]
        return (max(known), min(known)) if known else (None, None)

    def proposed_slug(self, taken: Sequence[str] = ()) -> str:
        """A slug derived from the branch, or "" when nothing usable can be derived.

        Derivation is `claims.derive_slug` — the same one `start-task` uses at branch-naming time —
        so a stream adopted from the queue and a stream minted at dispatch get the same name for the
        same work instead of two names the operator has to reconcile.
        """
        for candidate in (self.branch, self.repo):
            if not candidate or candidate == ABSENT:
                continue
            try:
                return claims_lib.ensure_unique_slug(
                    claims_lib.derive_slug(_slug_source(candidate)), taken
                )
            except claims_lib.ClaimError:
                continue
        return ""


def _slug_source(branch: str) -> str:
    """The part of a branch name that describes the work.

    `start-task` names branches `<user>/<slug>`, so the slug is already sitting in there — but a
    literal read also picks up the author prefix and turns `polds/plat-2020-secret-delete-mode` into
    `plat-2020-polds-secret-delete-mode`, which is the branch and the stream disagreeing about what
    the work is called.

    The ticket key is the anchor: everything before the segment carrying it is routing (`polds/`,
    `feat/`), not description. A branch with no ticket key has no anchor, so it is read whole rather
    than guessed at.
    """
    segments = branch.split("/")
    for index, segment in enumerate(segments):
        if claims_lib.parse_issue_keys(segment):
            segments = segments[index:]
            break
    return " ".join(segments)


def work_items(traces: list[Trace], streams: Sequence[vault.Brief] = ()) -> list[WorkItem]:
    """Group traces into work items, oldest work first, and note any stream already claiming one.

    The claim check is what stops adoption from duplicating a stream that already exists: a branch
    can start producing traces before a stream claims it and keep producing them after, and the
    operator needs to see "join this" rather than "mint another".
    """
    grouped: dict[tuple[str, str, str], WorkItem] = {}
    for trace in traces:
        key = trace.work_item_key()
        item = grouped.get(key)
        if item is None:
            tail = key[2]
            item = grouped[key] = WorkItem(
                repo=key[0],
                branch=key[1],
                where="" if tail.startswith("session:") else tail,
                session=tail[len("session:"):] if tail.startswith("session:") else "",
            )
        item.traces.append(trace)

    for item in grouped.values():
        item.claimed_by = _claiming_stream(item, streams)
    return list(grouped.values())


def _claiming_stream(item: WorkItem, streams: Sequence[vault.Brief]) -> str:
    """The stream that already owns this work, by the resolver's rules and not a looser set of them.

    A naive path intersection was wrong here in the direction that costs the most. The main clone of
    a repository is the working directory of *everything* in that repository, so a stream claiming it
    matched every work item in the repo — and the listing then invited the operator to file a dozen
    unrelated sessions into one stream. That is `main`-as-a-claim wearing a different hat.

    `claims.worktree_claim_is_live` is the rule the read path already applies: a worktree claim only
    counts while that checkout is still on one of the stream's claimed branches. Reusing it is the
    point — a listing that says "claimed by X" while resolution would never choose X is worse than
    one that says nothing, because the operator acts on it.
    """
    for brief in streams:
        if item.branch != ABSENT:
            claimed = [claims_lib.normalize_claim("branch", b) for b in brief.claim("branch")]
            if claims_lib.normalize_claim("branch", item.branch) in [c for c in claimed if c]:
                return brief.slug
        for worktree in item.worktrees:
            if worktree in brief.claim("worktree") and claims_lib.worktree_claim_is_live(
                brief, worktree
            ):
                return brief.slug
    return ""


def _age_text(age: float | None) -> str:
    return f"{age:.0f}d" if age is not None else "undated"


def render_queue(
    traces: list[Trace],
    streams: Sequence[vault.Brief] = (),
    *,
    now: datetime | None = None,
    cli: str | None = None,
) -> str:
    """The triage surface: one block per work item, each with the command that resolves it.

    Printing the exact next command is not decoration. The old listing named a file and left the
    operator to assemble an eight-flag mint invocation out of fields the trace already held, which is
    data re-entry the queue can do itself — and a triage step nobody performs is a queue that only
    grows.
    """
    if not traces:
        return "0 traces waiting in unassigned/."
    now = now or datetime.now(timezone.utc)
    cli = prog() if cli is None else cli
    items = work_items(traces, streams)
    taken = [brief.slug for brief in streams]

    lines = [f"{len(items)} work item(s) waiting ({len(traces)} traces):", ""]
    printed_cluster = ""
    for index, item in enumerate(items, start=1):
        oldest, newest = item.age_span(now)
        span = _age_text(oldest) if oldest == newest else f"{_age_text(oldest)}–{_age_text(newest)}"
        picked = item.selector(items, index)
        ref = picked if picked.isdigit() else f"'{picked}'"

        # Session-scoped items share a heading so a busy default branch does not become a wall of
        # near-identical rows. The heading is also the bulk-dismiss handle, which is the common case:
        # most of what accumulates on a default branch is noise, and clearing it should cost one
        # command rather than one per session.
        if item.cluster and item.cluster != printed_cluster:
            printed_cluster = item.cluster
            siblings = [other for other in items if other.cluster == item.cluster]
            lines.append(f"{item.repo} @ {item.branch} — {len(siblings)} session(s), nothing shared")
            lines.append(
                f"    a default branch names the repo, not the work; each session stands alone"
            )
            lines.append(
                f"    dismiss all: {cli} dismiss '{item.cluster}' --why \"<reason>\""
            )
            lines.append("")
        elif not item.cluster:
            printed_cluster = ""

        indent = "  " if item.cluster else ""
        lines.append(f"{indent}[{index}] {item.label}")
        sessions = len(item.sessions)
        lines.append(
            f"{indent}    {item.count} trace(s) from {sessions} session(s), {span} old"
            if sessions
            else f"{indent}    {item.count} trace(s), {span} old"
        )

        # What the work was, before what to do about it. Judging an item without this means opening
        # files, and an operator who has to open files to triage will not triage. A clustered item
        # already carries its subject in the label, so repeating it here is noise.
        summaries = item.summaries
        if not item.cluster:
            for line in summaries[:SUMMARY_LINES]:
                lines.append(f"    · {line}")
            if len(summaries) > SUMMARY_LINES:
                lines.append(f"    · … and {len(summaries) - SUMMARY_LINES} more")
            blank = item.unsummarized
            if blank:
                lines.append(
                    f"    · ({blank} session(s) with no subject recorded — written before summaries "
                    "were captured)"
                )
            if not summaries:
                lines.append(f"    · nothing here says what the work was — {cli} show {ref}")

        if item.claimed_by:
            lines.append(f"{indent}    claimed by: {item.claimed_by} — adopt files these traces into it")
            lines.append(f"{indent}    adopt:      {cli} adopt {ref}")
        else:
            slug = item.proposed_slug(taken)
            if not item.cluster:
                lines.append("    no stream claims this")
            if slug:
                taken = [*taken, slug]
                if not item.cluster:
                    lines.append(f"    proposed:   {slug}  (pass a different one as a second argument)")
                lines.append(f"{indent}    adopt:      {cli} adopt {ref}" + (
                    " <slug>" if item.cluster else ""
                ))
            else:
                lines.append(
                    f"{indent}    adopt:      {cli} adopt {ref} <slug>"
                    if item.cluster
                    else f"    proposed:   (none — no slug derivable; pass one: {cli} adopt {ref} <slug>)"
                )
        if not item.cluster:
            lines.append(f'    dismiss:    {cli} dismiss {ref} --why "<reason>"')

        for worktree in item.worktrees if not item.cluster else []:
            lines.append(f"    worktree:   {worktree}")
        lines.append("")

    lines.append("adopt   = mint or join a stream, and file every trace in the group into its inbox.")
    lines.append("dismiss = record that this was looked at and judged not worth a stream.")
    lines.append(f"Either one ends the item. {cli} <verb> --help for more.")
    return "\n".join(lines).rstrip() + "\n"


def _find_trace(root: Path, name: str) -> Trace:
    directory = unassigned_dir(root)
    path = directory / (name if name.endswith(".md") else f"{name}.md")
    # Resolved to keep a name like `../streams/x/BRIEF.md` from reaching outside the queue: promotion
    # reads a file and then marks it, and neither belongs anywhere but `unassigned/`.
    if path.parent.resolve() != directory.resolve() or not path.is_file():
        waiting = ", ".join(t.name for t in pending(root)) or "(none waiting)"
        raise TraceError(f"No trace '{name}' in {directory}. Waiting: {waiting}")
    return read_trace(path, _markers(directory))


def promotion_body(trace: Trace) -> str:
    """Render a trace as a stream contribution, every header field labeled and carried.

    Nothing is summarized away. A trace is promoted precisely when someone decided it mattered after
    all, and the fields that make it promotable — which directory, which branch, which session — are
    the first things a reader needs and the first things a lossy render would drop.
    """
    values = {
        "summary": trace.summary,
        "session": trace.session,
        "repo": trace.repo,
        "cwd": trace.cwd,
        "branch": trace.branch,
        "created": trace.created,
        "reason": trace.reason,
        "proposed_stream": trace.proposed_stream,
        "trace": trace.name,
    }
    lines = [f"Promoted from {UNASSIGNED_DIR}/{trace.name}.", ""]
    lines += [f"- {label}: {values[key] or ABSENT}" for key, label in PROMOTED_FIELDS]
    return "\n".join(lines)


def parse_promotion(body: str) -> dict[str, str]:
    """Read a promoted entry back into its fields. The inverse of `promotion_body`.

    Public because "promotion is lossless" is only a claim until something can check it — this is what
    a test, or a later triage pass, uses to compare the entry against the trace it came from.
    """
    labels = {label: key for key, label in PROMOTED_FIELDS}
    fields: dict[str, str] = {}
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        label, sep, value = stripped[2:].partition(":")
        if sep and label.strip() in labels:
            fields[labels[label.strip()]] = value.strip()
    return fields


def promote(
    root: Path,
    name: str,
    slug: str,
    *,
    promoted_by: str = "",
    now: datetime | None = None,
) -> Promotion:
    """Promote one trace into `slug`'s inbox and mark it promoted.

    The inbox is the target rather than `BRIEF.md` because promotion is a non-owner write — the
    session that dropped the trace is usually not the stream owner, and the owner folds what matters
    into the brief. A trace also has no rationale by construction (it was below the bar), so it could
    not become a `## Decided` entry without inventing one.

    The trace file itself is never rewritten. Promotion records itself in a sidecar marker instead, so
    the queue file stays exactly as the session that produced it wrote it, and a second promotion
    loses the race to `O_EXCL` rather than quietly duplicating the entry into another stream.
    """
    trace = _find_trace(root, name)
    if trace.promoted:
        raise TraceError(
            f"'{trace.name}' was already promoted into '{trace.promoted_stream}' at "
            f"{trace.promoted_at}. Promoting twice files the same content in two streams."
        )

    # Validates the slug and, on a miss, names the streams that exist. Promotion is a write, and a
    # write to a guessed stream is the failure `unassigned/` exists to absorb in the first place.
    brief = vault.read_stream(root, slug)
    if brief.archived:
        raise TraceError(
            f"Stream '{brief.slug}' is {brief.state}. Promoting into a closed stream makes the "
            f"dashboard report it closed while its content changes; reopen it first, or pick an "
            f"active stream."
        )

    entry = inbox.append(
        root,
        brief.slug,
        author=trace.session or "unknown",
        body=promotion_body(trace),
        kind=PROMOTION_KIND,
        now=now,
    )

    marker_dir = unassigned_dir(root) / PROMOTED_DIRNAME
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = marker_dir / trace.name
    stamp = (now or writer.utc_now()).strftime(ISO_STAMP)
    try:
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise TraceError(f"'{trace.name}' is already marked promoted.") from None
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(
            "---\n"
            f"trace: {trace.name}\n"
            f"stream: {brief.slug}\n"
            f"entry: {entry.name}\n"
            f"promoted_by: {_one_line(promoted_by) or 'unknown'}\n"
            f"promoted: {stamp}\n"
            "---\n"
        )
    # Promotion supersedes an earlier dismissal rather than coexisting with it. Leaving both would
    # make the trace read as simultaneously discarded and filed, and the filed half is the true one.
    (unassigned_dir(root) / DISMISSED_DIRNAME / trace.name).unlink(missing_ok=True)
    return Promotion(trace=trace, stream=brief.slug, entry=entry, marker=marker)


def render_item(item: WorkItem, *, now: datetime | None = None, cli: str | None = None) -> str:
    """Everything known about one work item, so a judgment call never requires opening a file.

    `list` is deliberately compressed and will hide detail on a wide queue; this is where the detail
    goes. If the operator still cannot decide after reading this, the missing information was never
    captured — and that is a defect in what the hook observes, not in how it is displayed.
    """
    now = now or datetime.now(timezone.utc)
    cli = prog() if cli is None else cli
    ref = item.selector([item], 1)
    quoted = ref if ref.isdigit() else f"'{ref}'"

    lines = [item.label, ""]
    lines.append(f"{item.count} trace(s) from {len(item.sessions)} session(s)")
    if item.claimed_by:
        lines.append(f"already claimed by stream: {item.claimed_by}")
    for worktree in item.worktrees:
        lines.append(f"worktree: {worktree}")
    lines.append("")

    for trace in item.traces:
        age = trace.age_days(now)
        stamp = trace.created or "undated"
        state = ""
        if trace.promoted:
            state = f"  [adopted into {trace.promoted_stream}]"
        elif trace.dismissed:
            state = f"  [dismissed: {trace.dismissed_why}]"
        lines.append(f"── {stamp} ({_age_text(age)} old){state}")
        lines.append(f"   session: {trace.session or 'unknown'}")
        subject = " ".join(trace.summary.split())
        if subject in ("", ABSENT, writer.NO_SUMMARY):
            subject = "(never recorded — this trace predates summary capture)"
        lines.append(f"   summary: {subject}")
        lines.append(f"   why here: {trace.reason}")
        if trace.details and trace.details != trace.summary:
            for detail in trace.details.splitlines():
                if detail.strip():
                    lines.append(f"   {detail.strip()}")
        lines.append(f"   file: {UNASSIGNED_DIR}/{trace.name}")
        lines.append("")

    if not item.summaries:
        lines.append(
            "No summaries recorded. These traces predate summary capture, so what the sessions were"
        )
        lines.append(
            "about was never written down — the commit and directory above are all there is. Newer"
        )
        lines.append("traces carry the session title and opening prompt.")
        lines.append("")

    lines.append(f"adopt:   {cli} adopt {quoted}")
    lines.append(f'dismiss: {cli} dismiss {quoted} --why "<reason>"')
    return "\n".join(lines)


@dataclass
class Adoption:
    """What `adopt` did. `minted` separates a stream that was created from one that was joined."""

    item: WorkItem
    stream: str
    minted: bool
    promotions: list[Promotion] = field(default_factory=list)
    # Whether the caller supplied a real goal. Without this the renderer cannot tell a written goal
    # from `ADOPTED_GOAL`, and told every mint its goal was a placeholder — including the ones that
    # had just written one, which reads as `--goal` being ignored.
    goal_supplied: bool = False


def select_work_items(root: Path, selector: str) -> list[WorkItem]:
    """Every work item a selector names. One for most selectors; many for a cluster.

    A cluster selector (`repo@main`) deliberately resolves to all of its sessions, because clearing
    a default branch's accumulated noise is one judgment and should cost one command. Adoption still
    demands exactly one — see `select_work_item`.

    Ambiguity refuses rather than picking. A write to a guessed target is the failure `unassigned/`
    exists to absorb, reintroduced at the step meant to drain it.
    """
    items = work_items(pending(root), vault.list_streams(root))
    if not items:
        raise TraceError("Nothing is waiting in unassigned/.")

    selector = selector.strip()
    if selector.isdigit():
        index = int(selector)
        if not 1 <= index <= len(items):
            raise TraceError(f"No work item [{index}]. The queue has {len(items)}; run `list`.")
        return [items[index - 1]]

    # Exact forms first, so a branch that happens to be a substring of another one still resolves to
    # itself rather than being reported ambiguous.
    normalized = selector.replace(" @ ", "@")
    for item in items:
        if normalized == item.key or selector in (item.session, item.session_ref, item.where):
            return [item]
    cluster = [item for item in items if item.cluster and normalized == item.cluster]
    if cluster:
        return cluster
    exact_branch = [item for item in items if selector == item.branch]
    if exact_branch:
        return exact_branch

    needle = selector.lower()
    matches = [
        item
        for item in items
        if needle in item.branch.lower()
        or needle in item.repo.lower()
        or needle in item.where.lower()
        or needle in item.session.lower()
        or needle in item.label.lower()
    ]
    if not matches:
        available = ", ".join(i.selector(items) for i in items)
        raise TraceError(f"No work item matches {selector!r}. Waiting: {available}")
    clusters = {item.cluster for item in matches}
    if len(matches) > 1 and not (len(clusters) == 1 and "" not in clusters):
        names = ", ".join(i.label for i in matches)
        raise TraceError(
            f"{selector!r} matches {len(matches)} work items ({names}). Narrow it, or use the "
            f"selector `list` prints — acting on the wrong group files unrelated work into one stream."
        )
    return matches


def select_work_item(root: Path, selector: str) -> WorkItem:
    """Exactly one work item, or a refusal naming what to do instead."""
    found = select_work_items(root, selector)
    if len(found) == 1:
        return found[0]
    item = found[0]
    raise TraceError(
        f"{selector!r} names {len(found)} sessions on {item.repo} @ {item.branch}, not one piece of "
        f"work. A default branch is where everyone stands, so nothing binds these together and one "
        f"stream cannot honestly hold them. Adopt a single session by its reference from `list` "
        f"(e.g. `{found[0].session_ref}`), or dismiss the whole set with "
        f"`dismiss '{item.cluster}' --why \"...\"`."
    )


def adopt(
    root: Path,
    selector: str,
    slug: str = "",
    *,
    goal: str = "",
    owner: str = "",
    promoted_by: str = "",
    now: datetime | None = None,
) -> Adoption:
    """Mint or join the stream for one work item, then promote every trace in it.

    This is the whole triage workflow in one call, and it exists because the previous one asked the
    operator to retype what the traces already contained: the repository, the branch, and every
    working directory are read straight off the group and registered as claims. Registering them is
    also what stops the refill — the next session on that branch resolves to the stream instead of
    dropping another trace.

    Minting here does not weaken the mint bar. The bar keeps *sessions* from creating streams
    automatically; adoption is an operator looking at a work item and judging it real, which is
    exactly the judgment the bar wants a human to make.
    """
    item = select_work_item(root, selector)
    now = now or writer.utc_now()
    goal_supplied = bool(goal.strip())

    if item.claimed_by:
        target, minted = item.claimed_by, False
    else:
        target = (slug or "").strip() or item.proposed_slug([b.slug for b in vault.list_streams(root)])
        if not target:
            raise TraceError(
                f"No slug given and none derivable from {item.repo!r} / {item.branch!r}. Pass one: "
                f"`traces adopt {selector} <slug>`."
            )
        claim_block = {kind: [] for kind in vault.CLAIM_KINDS}
        if item.repo != ABSENT:
            claim_block["repo"] = [item.repo]
        # A default branch is never claimed: it would resolve every stream in the repo, and
        # therefore none of them.
        if item.branch != ABSENT and item.branch.lower() not in DEFAULT_BRANCHES:
            claim_block["branch"] = [item.branch]
        # A session-scoped item stood on a default branch, so its working directory is the repo's
        # main clone — shared by everything in that repository. Claiming it would match every work
        # item there, which is `main`-as-a-claim by another route: a worktree claim with no branch
        # claim beside it is live whenever the directory exists. Work with no durable git handle
        # gets none, and is reached by explicit declaration instead.
        claim_block["worktree"] = [] if item.session else item.worktrees
        claim_block["issue"] = claims_lib.parse_issue_keys(item.branch) or claims_lib.parse_issue_keys(
            " ".join(item.summaries)
        )

        _, minted = mint.create_stream(
            root,
            target,
            goal=goal.strip() or ADOPTED_GOAL,
            owner=owner,
            claim_block=claim_block,
            now=now,
        )
        if minted and not goal_supplied:
            # Filed as an operator ask rather than left implicit, so the placeholder goal surfaces on
            # the dashboard instead of quietly becoming permanent.
            writer.append_entry(
                root, target, "Open", ADOPTED_ASK.format(count=item.count), now=now
            )

    promotions = []
    for trace in item.traces:
        if trace.triaged:
            continue
        promotions.append(promote(root, trace.name, target, promoted_by=promoted_by, now=now))
    return Adoption(
        item=item,
        stream=target,
        minted=minted,
        promotions=promotions,
        goal_supplied=goal_supplied,
    )


def render_adoption(adoption: Adoption) -> str:
    verb = "minted" if adoption.minted else "joined"
    item = adoption.item
    lines = [
        f"{verb} streams/{adoption.stream} — {item.label}",
        f"promoted {len(adoption.promotions)} trace(s) into streams/{adoption.stream}/inbox/",
    ]
    for worktree in (item.worktrees if not item.session else []):
        lines.append(f"  claimed worktree: {worktree}")
    if adoption.minted and item.session:
        lines.append(
            "  No branch or worktree claimed: this ran on a default branch, so it has no git handle"
        )
        lines.append(
            "  that would not also match everything else in the repo. Reach it explicitly:"
        )
        lines.append(f"    MESSAGE_BOARD_STREAM={adoption.stream}")
    if adoption.minted and not adoption.goal_supplied:
        lines.append(
            f"Goal is a placeholder. Write it: python3 -m plugin.lib.writer section "
            f"{adoption.stream} Goal '<why this stream exists>'"
        )
    return "\n".join(lines)


def dismiss(
    root: Path,
    selector: str,
    *,
    why: str,
    dismissed_by: str = "",
    now: datetime | None = None,
) -> list[Trace]:
    """Record that a work item was looked at and judged not worth a stream.

    A reason is required. "Dismissed" with no reason is indistinguishable from "deleted", and the
    only thing separating a worked queue from an abandoned one is that its exits are accounted for.

    Nothing is deleted here. The trace stays exactly as written and a marker records the judgment, so
    a dismissal can be read back, argued with, and reversed — and `prune` can then sweep it on the
    same terms as a promoted trace, because both have had their call made.
    """
    why = _one_line(why)
    if not why:
        raise TraceError(
            "Dismissing needs a reason. Without one the queue cannot tell 'judged not worth a "
            "stream' from 'silently dropped', and that distinction is the only evidence that triage "
            "is happening at all."
        )

    # Plural on purpose: a cluster selector names every session on a default branch, and clearing
    # that accumulated noise is one judgment, not thirteen.
    items = select_work_items(root, selector)
    marker_dir = unassigned_dir(root) / DISMISSED_DIRNAME
    marker_dir.mkdir(parents=True, exist_ok=True)
    stamp = (now or writer.utc_now()).strftime(ISO_STAMP)

    dismissed = []
    for trace in [trace for item in items for trace in item.traces]:
        if trace.triaged:
            continue
        marker = marker_dir / trace.name
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                "---\n"
                f"trace: {trace.name}\n"
                f"why: {why}\n"
                f"dismissed_by: {_one_line(dismissed_by) or 'unknown'}\n"
                f"dismissed: {stamp}\n"
                "---\n"
            )
        dismissed.append(trace)
    return dismissed


def _git_root(root: Path) -> Path | None:
    """The repo containing the vault, or None. `.git` may be a directory or a worktree pointer file."""
    start = root.resolve()
    for candidate in [start, *start.parents]:
        if (candidate / ".git").exists():
            return candidate
    return None


def prune(
    root: Path,
    *,
    older_than_days: int = DEFAULT_RETENTION_DAYS,
    now: datetime | None = None,
    apply: bool = False,
    include_untriaged: bool = False,
) -> PruneResult:
    """Sweep aged *promoted* traces. Dry run unless `apply=True`; refuses outside a git repo.

    Three deliberate refusals:

    **Never automatic.** Nothing else in this module calls `prune`, and it does nothing without
    `apply`. A queue that empties itself on a timer teaches the operator it is not worth working,
    which is precisely how strict-bar minting turns lossy.

    **Never the backlog, unless asked.** Only promoted traces sweep by default. Their content already
    lives in a stream inbox, so the queue entry is redundant and dropping it *is* the queue draining.
    An unpromoted trace is held however old it gets: age-sweeping it deletes the one visible evidence
    that triage stopped happening, so the backlog would vanish exactly when it most needed attention.
    `include_untriaged=True` is the operator saying they have seen the count and want it gone anyway.

    **Never outside git.** The entire argument that pruning is non-lossy is that a swept trace stays
    recoverable in the vault's history. Without a repo that argument is gone and this is deletion, so
    the property is checked rather than assumed.

    Undatable traces are kept: something whose age cannot be established cannot be established as
    expired either.
    """
    if older_than_days < 0:
        raise TraceError("A retention window cannot be negative.")
    if _git_root(root) is None:
        raise TraceError(
            f"Refusing to prune: {root} is not inside a git repository. Pruning is non-lossy only "
            f"because the vault is version-controlled — swept traces stay recoverable in history. "
            f"Without git this is deletion, not pruning. Run `git init` in the vault, or promote the "
            f"traces worth keeping."
        )

    now = now or writer.utc_now()
    cutoff = now - timedelta(days=older_than_days)
    result = PruneResult(
        window_days=older_than_days, applied=apply, now=now, include_untriaged=include_untriaged
    )
    for trace in list_traces(root):
        created = trace.created_at()
        expired = created is not None and created < cutoff
        if expired and (trace.triaged or include_untriaged):
            result.swept.append(trace)
        else:
            result.kept.append(trace)

    if apply:
        directory = unassigned_dir(root)
        for trace in result.swept:
            trace.path.unlink(missing_ok=True)
            # Markers outlive nothing: a promotion or dismissal record pointing at a file that is
            # gone is bookkeeping the next triage pass has to reason about for no gain.
            (directory / PROMOTED_DIRNAME / trace.name).unlink(missing_ok=True)
            (directory / DISMISSED_DIRNAME / trace.name).unlink(missing_ok=True)
        _sweep_fingerprints(directory)
    return result


def _sweep_fingerprints(directory: Path) -> None:
    """Drop content markers whose trace is gone.

    Not required for correctness — `writer` already treats a marker with no file as stale and writes
    fresh — but without it the marker directory grows without bound for the life of the vault.
    """
    marker_dir = directory / writer.FINGERPRINT_DIRNAME
    if not marker_dir.is_dir():
        return
    for marker in marker_dir.glob("*.md"):
        try:
            owner = marker.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not owner or not (directory / owner).is_file():
            marker.unlink(missing_ok=True)


def render_prune(result: PruneResult) -> str:
    """The split, always. Reported whether the prune ran or was a dry run, and whether it swept
    anything or nothing.

    A prune that says only "removed 4" reads as progress no matter what is rotting behind it. The
    numbers that matter are how much drained *because it was promoted*, how much is being held
    because nobody triaged it, and how old the front of that queue is — that last one is the operator
    learning triage has stalled, so it prints even on a no-op run.
    """
    verb = "removed" if result.applied else "would remove"
    triaged = len(result.swept_triaged)
    untriaged = len(result.swept_untriaged)
    lines = [
        f"{verb} {len(result.swept)} trace(s) older than {result.window_days}d: "
        f"{triaged} triaged, {untriaged} untriaged."
    ]
    lines += [f"- {trace.name}" for trace in result.swept]

    held = len(result.held_back)
    backlog = result.backlog
    lines.append(
        f"kept {len(result.kept)}: {len(backlog)} untriaged ({held} past the window, held back), "
        f"{len(result.kept) - len(backlog)} triaged."
    )
    oldest = result.oldest_untriaged
    if oldest is None:
        lines.append("No untriaged traces waiting. The queue is worked.")
    else:
        age = oldest.age_days(result.now)
        age_text = f"{age:.0f}d old" if age is not None else "undated"
        lines.append(f"Oldest untriaged trace: {oldest.name} ({age_text}) — {oldest.summary}")
        if held:
            lines.append(
                f"{held} untriaged trace(s) are past the {result.window_days}d window and were not "
                f"swept. Triage has stalled; adopt or dismiss them, or pass --include-untriaged to "
                f"discard them deliberately."
            )
    if not result.applied and result.swept:
        lines.append("Nothing was deleted. Re-run with --apply.")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    cli = prog()
    parser = argparse.ArgumentParser(
        prog=cli,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Triage the unassigned queue.\n"
            "\n"
            "A session that resolves to no stream leaves a trace here rather than guessing at a\n"
            "home for it. Nothing is lost, but the queue only stays useful if it gets worked.\n"
            "\n"
            "Triage acts on WORK ITEMS, not on traces. A work item is one repository at one branch\n"
            "(or one directory, for sessions outside any checkout), and every trace that observed\n"
            "it. Sessions are what produced the traces; nothing is ever decided per session."
        ),
        epilog=(
            "TYPICAL USE\n"
            f"  {cli} list                                  see what is waiting\n"
            f"  {cli} adopt <selector>                      this is real work -> give it a stream\n"
            f'  {cli} dismiss <selector> --why "..."        this is noise -> record the call\n'
            "\n"
            "  Every item ends one of those two ways. `list` prints the exact command for each.\n"
            "\n"
            "SELECTORS\n"
            "  A branch ('feat/thing'), a repo@branch pair, a directory, or a listing index.\n"
            "  Prefer what `list` prints: indices renumber as the queue drains, so an index copied\n"
            "  from an earlier listing can act on a different item and still succeed.\n"
            "\n"
            "OCCASIONAL\n"
            f"  {cli} promote <trace-file> <slug>           one trace into one EXISTING stream\n"
            f"  {cli} prune --days 30                       sweep traces that have had their call made\n"
            "\n"
            "  `promote` is the escape hatch for when a single trace in a group belongs somewhere\n"
            "  other than the rest of it. Adopting is the normal path and promotes the whole group.\n"
            "\n"
            "WRITTEN BY HOOKS\n"
            f"  {cli} record <summary>                      you should not need to run this\n"
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    list_cmd = sub.add_parser(
        "list",
        help="what is waiting, grouped into work items",
        description=(
            "Work items waiting in unassigned/, oldest first, each with the command that resolves "
            "it. Traces are grouped by (repository, branch) — or by directory for sessions that "
            "ran outside any checkout."
        ),
    )
    list_cmd.add_argument(
        "--all", action="store_true", help="also show items already adopted or dismissed"
    )

    show_cmd = sub.add_parser(
        "show",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="everything known about one work item, so you can judge it",
        description=(
            "Every trace in one work item, in full: when each session ran, what it was about, what\n"
            "was observed, and which file it lives in.\n"
            "\n"
            "`list` is compressed on purpose and hides detail once a queue gets wide. This is where\n"
            "that detail went. If you still cannot decide after reading this, the information was\n"
            "never captured — that is a gap in what the hook observes, not in the display."
        ),
    )
    show_cmd.add_argument("selector", help="what `list` prints — a branch, repo@branch, or index")

    adopt_cmd = sub.add_parser(
        "adopt",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="this is real work: give the item a stream and file its traces into it",
        description=(
            "Mint or join a stream for one work item, then file every trace in it into that\n"
            "stream's inbox.\n"
            "\n"
            "The claims come from the traces themselves — repository, branch, every working\n"
            "directory the branch was seen in, and any ticket key in the branch name. Nothing is\n"
            "retyped. Registering those claims is also what stops the queue refilling: the next\n"
            "session on that branch resolves to the stream instead of leaving another trace.\n"
            "\n"
            "If a stream already claims the branch, it is joined rather than duplicated.\n"
            "\n"
            "Traces land in the inbox, not in ## Decided: a trace was below the mint bar by\n"
            "construction, so it has no rationale and could not become a decision without one\n"
            "being invented. The stream owner folds up whatever matters."
        ),
    )
    adopt_cmd.add_argument("selector", help="what `list` prints — a branch, repo@branch, or index")
    adopt_cmd.add_argument(
        "slug", nargs="?", default="", help="stream slug; derived from the branch when omitted"
    )
    adopt_cmd.add_argument(
        "--goal",
        default="",
        help="why this stream exists. Omit and a placeholder is filed plus an operator ask, "
             "because an invented goal that reads well is worse than an obviously blank one",
    )
    adopt_cmd.add_argument("--owner", default="", help="stream owner; defaults to unknown")
    adopt_cmd.add_argument("--by", dest="promoted_by", default="", help="who adopted it")

    dismiss_cmd = sub.add_parser(
        "dismiss",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="this is noise: record that it was looked at and is not worth a stream",
        description=(
            "Record that a work item was looked at and judged not worth a stream.\n"
            "\n"
            "Nothing is deleted — the traces stay exactly as written and a marker records the\n"
            "judgment, so a dismissal can be read back, argued with, and reversed by promoting.\n"
            "\n"
            "--why is required. Without a reason the queue cannot tell 'judged not worth a stream'\n"
            "from 'silently dropped', and that difference is the only evidence that triage is\n"
            "happening at all."
        ),
    )
    dismiss_cmd.add_argument("selector", help="what `list` prints — a branch, repo@branch, or index")
    dismiss_cmd.add_argument("--why", required=True, help="why it is not worth a stream")
    dismiss_cmd.add_argument("--by", dest="dismissed_by", default="", help="who dismissed it")

    promote_cmd = sub.add_parser(
        "promote",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="(rare) file ONE trace into ONE existing stream — see `adopt` for the normal path",
        description=(
            "File a single trace into an existing stream's inbox.\n"
            "\n"
            "You usually want `adopt` instead. Adopt works on a whole work item: it creates the\n"
            "stream if needed and promotes every trace in the group in one step. This command is\n"
            "the escape hatch for the case adopt cannot express — one trace in a group belongs to\n"
            "a different stream than the rest of it.\n"
            "\n"
            "The stream must already exist; promotion is a write, and a write to a guessed stream\n"
            "is the failure unassigned/ exists to absorb. An archived stream is refused: reviving\n"
            "one whose conclusions are final is worse than starting fresh.\n"
            "\n"
            "Promoting a dismissed trace reverses the dismissal."
        ),
    )
    promote_cmd.add_argument(
        "trace", help="a trace FILENAME from `list --all`, with or without .md — not a selector"
    )
    promote_cmd.add_argument("slug", help="an existing stream to file it into")
    promote_cmd.add_argument("--by", dest="promoted_by", default="", help="who promoted it")

    prune_cmd = sub.add_parser(
        "prune",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        help="sweep traces that have had their call made; dry run unless --apply",
        description=(
            "Sweep traces older than the window that have already been adopted or dismissed.\n"
            "Their content lives in a stream, or the call to drop them is on record, so removing\n"
            "the queue copy is the queue draining as designed.\n"
            "\n"
            "Untriaged traces are held however old they get. Age-sweeping them would delete the\n"
            "one visible piece of evidence that triage stopped happening — the backlog would\n"
            "vanish exactly when it most needed attention.\n"
            "\n"
            "Refuses outside a git repository: the whole argument that pruning is non-lossy is\n"
            "that swept traces stay recoverable in the vault's history."
        ),
    )
    prune_cmd.add_argument(
        "--days",
        type=int,
        default=DEFAULT_RETENTION_DAYS,
        help=f"retention window in days (default: {DEFAULT_RETENTION_DAYS})",
    )
    prune_cmd.add_argument(
        "--apply",
        action="store_true",
        help="actually delete; without it nothing is removed. Requires the vault to be a git repo.",
    )
    prune_cmd.add_argument(
        "--include-untriaged",
        "--include-unpromoted",  # the older name, kept working
        dest="include_untriaged",
        action="store_true",
        help="also discard aged traces nobody ever made a call on. Off by default: sweeping them "
             "hides the backlog that proves triage stopped.",
    )
    record_cmd = sub.add_parser(
        "record",
        help="(hooks) drop a below-bar session trace into unassigned/",
        description=(
            "Drop a below-bar session trace into unassigned/. SessionEnd calls this; you should "
            "not normally need to."
        ),
    )
    record_cmd.add_argument("summary", help="one line: what this session did")
    record_cmd.add_argument("--session", default="unknown", help="session reference")
    record_cmd.add_argument("--cwd", default=None)
    record_cmd.add_argument("--branch", default=None)
    record_cmd.add_argument("--repo", default=None, help="owner/name; groups the triage queue")
    record_cmd.add_argument("--reason", default=BELOW_BAR_REASON)
    record_cmd.add_argument("--proposed-stream", dest="proposed_stream", default=None)

    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        root = vault.resolve_vault()
        if args.command == "record":
            print(
                record(
                    root,
                    args.summary,
                    session=args.session,
                    cwd=args.cwd,
                    branch=args.branch,
                    repo=args.repo,
                    reason=args.reason,
                    proposed_stream=args.proposed_stream,
                )
            )
        elif args.command == "list":
            print(
                render_queue(
                    list_traces(root) if args.all else pending(root), vault.list_streams(root)
                )
            )
        elif args.command == "show":
            print(render_item(select_work_item(root, args.selector)))
        elif args.command == "adopt":
            print(
                render_adoption(
                    adopt(
                        root,
                        args.selector,
                        args.slug,
                        goal=args.goal,
                        owner=args.owner,
                        promoted_by=args.promoted_by,
                    )
                )
            )
        elif args.command == "dismiss":
            dismissed = dismiss(
                root, args.selector, why=args.why, dismissed_by=args.dismissed_by
            )
            print(f"dismissed {len(dismissed)} trace(s): {args.why}")
        elif args.command == "promote":
            promotion = promote(root, args.trace, args.slug, promoted_by=args.promoted_by)
            print(f"promoted {promotion.trace.name} → {promotion.entry}")
        elif args.command == "prune":
            print(
                render_prune(
                    prune(
                        root,
                        older_than_days=args.days,
                        apply=args.apply,
                        include_untriaged=args.include_untriaged,
                    )
                )
            )
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
