"""The operator surface: every stream at a glance, without opening a session.

Written for the operator who has been away for hours while many agents ran in parallel. The ordering
is the argument: what needs a human decision is at the top, everything else is context beneath it.

Two properties are load-bearing and easy to lose in a refactor:

1. Operator escalations are derived from `## Open` on every render. Nothing here caches or stores
   them, because a stored copy is a second source of truth that can disagree with the body.
2. Brief-vs-ground-truth disagreements render as *adjudication items*, never as failures. Hooks
   observe mechanics; they cannot observe intent, so a disagreement is an ambiguous signal and the
   operator is the one who resolves it. Painting these red teaches the operator to ignore red, which
   costs the system the one signal it depends on.

Read-only, stdlib only.
"""

from __future__ import annotations

import re
import shutil
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .lib import vault

# Bullet forms the schema allows in a section body: "- x", "* x", "1. x".
_ITEM = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$")

# ground-truth.md is a flat list of `- **Key:** value` facts written by hooks.
_FACT = re.compile(r"^\s*[-*]\s*\*\*(?P<key>[^*]+?):?\*\*\s*(?P<value>.*?)\s*$")

# Hook phrasings that mean "the thing you claim exists, I could not see".
_ABSENT = re.compile(r"does not exist|never created|not found|not registered|^none\b|^n/?a$", re.I)

_MIN_WIDTH, _MAX_WIDTH = 64, 110


def _terminal_width() -> int:
    return max(_MIN_WIDTH, min(_MAX_WIDTH, shutil.get_terminal_size(fallback=(96, 24)).columns))


def _items(text: str) -> list[str]:
    return [m.group(1) for m in (_ITEM.match(line) for line in text.splitlines()) if m]


def _parse_ts(raw: str) -> datetime | None:
    value = raw.strip()
    if not value:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _ago(stamp: datetime | None, now: datetime) -> str:
    """Elapsed time in the coarsest useful unit.

    The absolute timestamp is already in the brief; what the returning operator actually needs to
    know is which streams went quiet and which just moved.
    """
    if stamp is None:
        return "age unknown"
    seconds = (now - stamp).total_seconds()
    if seconds < 0:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit} ago"
    return "just now"


class GroundTruth:
    """The hook-written facts for one stream, addressable by key."""

    def __init__(self, text: str):
        self.text = text
        self.facts: list[tuple[str, str]] = []
        for line in text.splitlines():
            match = _FACT.match(line)
            if match:
                self.facts.append((match.group("key").strip(), match.group("value").strip()))

    def fact(self, key_pattern: str) -> tuple[str, str] | None:
        for key, value in self.facts:
            if re.search(key_pattern, key, re.I):
                return key, value
        return None


@dataclass(frozen=True)
class Adjudication:
    """One place the brief and the hooks describe the same event differently.

    `benign` is mandatory and not decoration: it carries the reading under which the disagreement is
    fine. A signal presented without its innocent explanation is a verdict wearing a question mark.
    """

    headline: str
    brief_side: str
    hooks_side: str
    benign: str


def _probe_review_disposition(brief: vault.Brief, gt: GroundTruth) -> Adjudication | None:
    """A closed review stream against a PR with zero formal reviews submitted.

    Measured on PLAT-1962: the disposition was recorded as a closing comment rather than a submitted
    review, which the workflow permits. The reviews API cannot see that, so this is exactly the shape
    of disagreement that must never be rendered as a failure.
    """
    if not brief.archived or not brief.claim("pr"):
        return None
    if not re.search(r"\breview", f"{brief.title} {brief.sections.get('Goal', '')}", re.I):
        return None
    fact = gt.fact(r"formal review")
    if fact is None or fact[1].strip() != "0":
        return None

    comments = gt.fact(r"comment")
    benign = "a closing comment in place of a submitted review is a permitted disposition"
    if comments:
        benign += f"; hooks did record {comments[0].lower()}: {comments[1]}"
    return Adjudication(
        headline="review stream closed; hooks saw no submitted review",
        brief_side=f"stream concluded a review of pr {', '.join(brief.claim('pr'))} (state {brief.state})",
        hooks_side=f"{fact[0]}: {fact[1]}",
        benign=benign,
    )


def _probe_absent_claim(kind: str):
    """Build a probe for a claim the hooks looked for and did not find."""

    def probe(brief: vault.Brief, gt: GroundTruth) -> Adjudication | None:
        claimed = brief.claim(kind)
        if not claimed:
            return None
        fact = gt.fact(rf"\b{kind}\b")
        if fact is None or not _ABSENT.search(fact[1]):
            return None
        return Adjudication(
            headline=f"{kind} claim not witnessed by hooks",
            brief_side=f"claims {kind}={', '.join(claimed)}",
            hooks_side=f"{fact[0]}: {fact[1]}",
            benign=f"the {kind} may be gone, renamed, or on another machine — claims outlive the "
            f"things they point at, which is why they are not identity",
        )

    return probe


def _probe_tests(brief: vault.Brief, gt: GroundTruth) -> Adjudication | None:
    body = " ".join(brief.sections.get(name, "") for name in vault.SECTIONS)
    match = re.search(r"tests?\s+(?:all\s+)?(?:pass\w*|green)", body, re.I)
    if not match:
        return None
    fact = gt.fact(r"\btests?\b")
    if fact is None or not re.search(r"not run|none defined|fail", fact[1], re.I):
        return None
    return Adjudication(
        headline="test claim and test observation differ",
        brief_side=f"brief states \"{match.group(0)}\"",
        hooks_side=f"{fact[0]}: {fact[1]}",
        benign="hooks watch this session only; the run may have happened elsewhere or in another suite",
    )


PROBES = [
    _probe_review_disposition,
    _probe_absent_claim("branch"),
    _probe_absent_claim("worktree"),
    _probe_tests,
]


def adjudications(brief: vault.Brief, gt: GroundTruth) -> list[Adjudication]:
    found = [probe(brief, gt) for probe in PROBES]
    return [item for item in found if item is not None]


def inbox_entries(brief: vault.Brief) -> list[str]:
    """Filenames appended to this stream by other agents, newest last.

    Counted rather than parsed: the inbox is a contention-free append surface, and the operator only
    needs to know something is waiting and who left it.
    """
    inbox = brief.path.parent / "inbox"
    if not inbox.is_dir():
        return []
    return sorted(p.name for p in inbox.iterdir() if p.is_file() and not p.name.startswith("."))


def unassigned_traces(vault_root: Path) -> list[str]:
    traces = vault_root / "unassigned"
    if not traces.is_dir():
        return []
    return sorted(p.name for p in traces.iterdir() if p.is_file() and not p.name.startswith("."))


@dataclass
class StreamView:
    brief: vault.Brief
    asks: list[str] = field(default_factory=list)
    agent_open: int = 0
    decided: int = 0
    do_not: int = 0
    next_steps: list[str] = field(default_factory=list)
    inbox: list[str] = field(default_factory=list)
    signals: list[Adjudication] = field(default_factory=list)
    last_session: str = ""


def _clean_ask(ask: str) -> str:
    """Drop the `who: operator` tag — under a NEEDS OPERATOR heading it is noise."""
    return re.sub(r"[.\s]*`?\s*who:\s*operator\s*`?[.\s]*$", "", ask).strip()


def _last_session(gt: GroundTruth) -> str:
    """The hook-recorded summary of the most recent session on this stream, if any."""
    session = gt.fact(r"^session$")
    if not session or not session[1]:
        return ""
    turns = gt.fact(r"^turns$")
    suffix = f" ({turns[1]} turns)" if turns and turns[1] not in ("", "0") else ""
    return session[1] + suffix


def build_view(brief: vault.Brief) -> StreamView:
    resolved = vault.resolve_transclusions(brief)
    gt = GroundTruth(resolved.get("Ground truth", ""))
    open_items = _items(brief.sections.get("Open", ""))
    return StreamView(
        brief=brief,
        asks=[_clean_ask(a) for a in brief.operator_asks()],
        agent_open=sum(1 for item in open_items if "who: agent" in item),
        decided=len(_items(brief.sections.get("Decided", ""))),
        do_not=len(_items(brief.sections.get("Do not", ""))),
        next_steps=_items(brief.sections.get("Next", "")),
        inbox=inbox_entries(brief),
        signals=adjudications(brief, gt),
        last_session=_last_session(gt),
    )


def _wrap(text: str, width: int, indent: str, hang: str | None = None) -> list[str]:
    return textwrap.wrap(
        text,
        width=width,
        initial_indent=indent,
        subsequent_indent=hang if hang is not None else indent,
    ) or [indent + text]


def sort_key(view: StreamView) -> tuple:
    """Operator escalations first, then most recently touched.

    Recency second because a stream that moved while the operator was away is the one whose context
    has changed under them.
    """
    stamp = _parse_ts(view.brief.updated)
    return (0 if view.asks else 1, -(stamp.timestamp() if stamp else 0.0), view.brief.slug)


def _render_stream(view: StreamView, now: datetime, width: int) -> list[str]:
    brief = view.brief
    stamp = _parse_ts(brief.updated)
    lines = [
        f"  {brief.slug}  [{brief.state}] {brief.owner or 'unowned'} · "
        f"{_ago(stamp, now)} · {brief.updated or 'no timestamp'}"
    ]
    if brief.title:
        lines += _wrap(brief.title, width, "      ")

    counts = [
        f"open {len(view.asks)} operator / {view.agent_open} agent",
        f"decided {view.decided}",
        f"do-not {view.do_not}",
        f"inbox {len(view.inbox)}",
    ]
    lines.append("      " + " · ".join(counts))

    claims = [f"{k}={', '.join(brief.claim(k))}" for k in vault.CLAIM_KINDS if brief.claim(k)]
    if claims:
        lines += _wrap("claims: " + " · ".join(claims), width, "      ", "              ")

    # One line each here — the full text already led the render. Repeating a paragraph inside the
    # stream block costs a third of the screen and tells the operator nothing new.
    for ask in view.asks:
        lines.append("      " + textwrap.shorten(f"operator: {ask}", width=width - 6))

    # Absence is rendered, not skipped. A dropped line makes the operator guess whether the data is
    # missing or the surface is hiding it — and guessing is the exact failure this board exists to
    # end. Archived streams are exempt: nothing further is expected of them.
    if not brief.archived:
        lines += _wrap(
            f"last session: {view.last_session or 'none recorded — hook writes this on session end'}",
            width, "      ", "            ",
        )

    if view.next_steps:
        lines += _wrap(f"next: {view.next_steps[0]}", width, "      ", "            ")
        if len(view.next_steps) > 1:
            lines.append(f"      ...and {len(view.next_steps) - 1} more in ## Next")
    elif not brief.archived:
        lines += _wrap(
            "next: none recorded — owner has not written ## Next",
            width, "      ", "            ",
        )

    for name in view.inbox:
        lines.append(f"      inbox: {name}")

    for signal in view.signals:
        lines += _wrap(f"needs adjudication: {signal.headline}", width, "      ", "        ")
        lines += _wrap(f"brief: {signal.brief_side}", width, "        ", "               ")
        lines += _wrap(f"hooks: {signal.hooks_side}", width, "        ", "               ")
        lines += _wrap(f"also:  {signal.benign}", width, "        ", "               ")
    return lines


def render(vault_root: Path, now: datetime | None = None, width: int | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    width = width or _terminal_width()

    views = [build_view(brief) for brief in vault.list_streams(vault_root)]
    active = sorted((v for v in views if not v.brief.archived), key=sort_key)
    archived = sorted((v for v in views if v.brief.archived), key=sort_key)
    asks = [(v.brief.slug, ask) for v in views for ask in v.asks]
    inbox_total = sum(len(v.inbox) for v in views)
    signal_total = sum(len(v.signals) for v in views)

    lines = _wrap(
        f"BOARD  {len(views)} streams · {len(active)} active · {len(archived)} archived · "
        f"{len(asks)} need operator · {inbox_total} inbox · {signal_total} to adjudicate",
        width,
        "",
        "       ",
    )
    # Left unwrapped even when it overruns: a path split across lines is a path the operator
    # cannot copy.
    lines.append(f"vault  {vault_root}  ·  as of {now.strftime('%Y-%m-%dT%H:%MZ')}")

    # Escalations lead, before any per-stream detail: they are the only thing that blocks, and an
    # operator who reads nothing else must still read these.
    lines += ["", f"NEEDS OPERATOR — {len(asks)}"]
    if asks:
        for slug, ask in asks:
            lines += _wrap(f"{slug}: {ask}", width, "  ", "    ")
    else:
        lines.append("  (none — no stream is waiting on you)")

    lines += ["", f"ACTIVE — {len(active)}"]
    for view in active:
        lines += _render_stream(view, now, width)
    if not active:
        lines.append("  (none)")

    # Archived streams stay listed: a closed stream's `## Do not` is often its most valuable content,
    # and hiding it is how a settled decision gets re-litigated.
    lines += ["", f"ARCHIVED — {len(archived)}  (readable; never resolved into)"]
    for view in archived:
        lines += _render_stream(view, now, width)
    if not archived:
        lines.append("  (none)")

    traces = unassigned_traces(vault_root)
    if traces:
        lines += ["", f"UNASSIGNED TRACES — {len(traces)}  (sessions below the minting bar)"]

    lines.append("")
    lines += _wrap(
        "adjudication items are ambiguous signals, not verdicts: hooks observe mechanics, never "
        "intent. Judge each one on the evidence shown.",
        width,
        "",
    )
    lines.append("full brief: python3 -m plugin.lib.brief_read <slug>")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if argv:
        print("usage: python3 -m plugin.dashboard", file=sys.stderr)
        return 2
    try:
        print(render(vault.resolve_vault()))
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
