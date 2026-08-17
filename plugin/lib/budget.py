"""Size-budget guard and compaction candidates for `BRIEF.md`.

A brief has a hard budget (~400 words, one screen) because its whole purpose is cheap cold start. The
budget is enforced by **compaction, not truncation**: this module never removes anything and never
proposes removing anything on the grounds of age or count. Age and count are exactly the wrong
signals — they keep whatever was decided last week and drop the foundational decision from month one.

The only reason an entry may leave `## Decided` is that it is **already encoded elsewhere**:

    made -> ## Decided -> encoded into an artifact -> archived with a pointer

so `## Decided` is a staging buffer for what is settled but not yet written down anywhere, which is
precisely the context that dies with a session. **Both halves are required.** An artifact that states
the decision but not its reason does not qualify: a decision whose reason is gone gets re-litigated,
which is the failure the section exists to prevent.

Detecting "encoded elsewhere" mechanically:

1. The artifacts in play are the ones the brief itself links with `[[wikilinks]]` — inline in `##
   Goal`, in the entry, anywhere. The schema puts links at the point of mention precisely so the
   graph is walkable, and this is the walk.
2. Each artifact is split into heading-delimited passages, because the pointer an operator needs is
   "which section of which doc", not "somewhere in this 3000-word file".
3. A half is encoded when the passage covers enough of its distinctive words. Word coverage, not
   substring matching: an artifact restates a decision in its own prose rather than quoting the
   brief, so a substring test would find nothing and report every mature stream as uncompactable.
4. The rationale half is checked against the *same* passage. A reason two sections away from the
   decision is not where the next agent will find it.

Entries written before the `Why:` clause was enforced carry no rationale to compare. They are not
waved through: the passage that encodes the decision must itself be explanatory prose rather than a
restatement, and the entry is reported as missing its rationale either way.

Output is candidates, never actions. `writer.archive_decision` does the move, and it is a deliberate
act by the stream owner — automatic archiving would let a false positive here silently destroy the
one copy of a rationale.

Stdlib only, like `vault` — a plugin that needs `pip install` has broken its install story.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Sequence

from . import vault

# docs/spec/brief-schema.md: "one screen, ~400 words total, `## Decided` included". Excluding the
# unbounded section from the budget exempts the problem instead of solving it.
WORD_BUDGET = 400

DECIDED_SECTION = "Decided"

# Hook-written and transcluded, so the owner cannot compact it. Counting it against the owner's
# budget would charge them for words they are forbidden to touch; it is reported separately instead,
# because an agent still pays to read it.
EXCLUDED_FROM_BUDGET = {"Ground truth"}

# Claims that only exist for code work. A stream carrying one is a code stream, and the asymmetry in
# docs/spec/brief-schema.md applies: code encodes *what* was decided but rarely *why*, so a drained
# `## Decided` there means rationale is leaking, not that the stream is tidy.
CODE_CLAIM_KINDS = ("repo", "branch", "pr")

KIND_CODE = "code"
KIND_DOCUMENT = "document"

# Share of a half's distinctive words that must appear in the candidate passage. Set by measurement
# against the hand-compacted design fixture rather than taste: at 0.6 the tool reproduces 17 of the
# 18 entries a human archived, and the one it keeps is the entry whose artifact genuinely paraphrases
# it hardest. Raising it drops real encodings; lowering it starts matching on topic rather than
# content, and a false positive here costs a rationale.
COVERAGE = 0.6

# Below this many distinctive words a coverage ratio is noise — three words can co-occur anywhere.
# Such an entry is left in the brief with "too little distinctive text to verify" rather than guessed
# at in either direction.
MIN_SIGNIFICANT = 3

# An encoding passage must say materially more than the entry it encodes. A passage that merely
# restates the decision has not encoded the reasoning, whatever else it contains.
MIN_PASSAGE_RATIO = 2

# Reason-bearing prose. Used only for entries that never recorded a `Why:` — for those there is no
# rationale text to compare against, so the question becomes whether the artifact explains at all.
_REASON_MARKERS = re.compile(
    r"(because|\bwhy\b|rather than|instead of|so that|\breason|otherwise|deliberate|therefore"
    r"|prevent|avoid|\bsince\b|would|cannot|can't|the point|at the cost|trade-?off|matters"
    r"|breaks|fails?\b|risk|expensive|cheaper|beats)",
    re.IGNORECASE,
)

# Words that carry no identifying content. Kept small on purpose: an aggressive stopword list starts
# deleting the domain vocabulary ("state", "claim", "write") that makes a decision distinctive.
_STOPWORDS = frozenset(
    """a an the and or but if then else of to in on at by for with from as is are was were be been
    being this that these those it its we our you your they their he she his her not no nor so than
    too very can will just should now do does did done have has had having i me my which who whom
    what when where why how all any both each few more most other some such only own same don over
    under again further once here there about into through during before after above below up down
    out off""".split()
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*+]\s+")
_WIKILINK = re.compile(r"!?\[\[([^\]]+)\]\]")
_ENTRY = re.compile(r"^\s*[-*+]\s+(?:(\d{4}-\d{2}-\d{2})\s*[—–-]\s*)?(.*)$")
_WHY = re.compile(r"\bwhy\s*:\s*", re.IGNORECASE)
_POINTER_TAIL = re.compile(r"\s*(?:→|->)\s*.*$")


@dataclass
class Encoding:
    """Where one `## Decided` entry was found already written down."""

    artifact: str
    path: Path
    section: str
    decision_coverage: float
    rationale_coverage: float
    explains: bool

    @property
    def pointer(self) -> str:
        """The `[[artifact]] "Section"` string `writer.archive_decision` wants as its pointer."""
        return f"[[{self.artifact}]]" + (f' "{self.section}"' if self.section else "")


@dataclass
class DecidedEntry:
    line: str
    date: str
    decision: str
    rationale: str
    encodings: list[Encoding] = field(default_factory=list)
    match: str = ""
    retained: str = ""

    @property
    def candidate(self) -> bool:
        return bool(self.encodings)

    @property
    def pointer(self) -> str:
        return self.encodings[0].pointer if self.encodings else ""

    @property
    def has_rationale(self) -> bool:
        return bool(self.rationale)


@dataclass
class Report:
    slug: str
    kind: str
    budget: int
    section_words: dict[str, int]
    total_words: int
    transcluded_words: int
    entries: list[DecidedEntry]
    unresolved_links: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def over_budget(self) -> bool:
        return self.total_words > self.budget

    @property
    def overage(self) -> int:
        return max(0, self.total_words - self.budget)

    @property
    def candidates(self) -> list[DecidedEntry]:
        return [entry for entry in self.entries if entry.candidate]

    @property
    def retained(self) -> list[DecidedEntry]:
        return [entry for entry in self.entries if not entry.candidate]

    @property
    def reclaimable_words(self) -> int:
        """Words the brief would shed if every candidate were archived. Advisory: the owner decides."""
        return sum(count_words(entry.line) for entry in self.candidates)


def count_words(text: str) -> int:
    """Count tokens carrying at least one alphanumeric character.

    Markdown punctuation (`-`, `*`, `>`, `|`) is not prose and would inflate a list-heavy section
    against a budget the operator reasons about in words.
    """
    return sum(1 for token in text.split() if any(ch.isalnum() for ch in token))


def significant_words(text: str) -> set[str]:
    """Distinctive words of a passage, for coverage comparison.

    Backticks, brackets and wikilink syntax are stripped so `` `git init` `` in a brief matches
    `git init` in a spec; a decision and the doc encoding it are never byte-identical.
    """
    text = _WIKILINK.sub(lambda m: m.group(1).replace("|", " ").replace("#", " "), text.lower())
    text = re.sub(r"[`\[\](){}]", " ", text)
    text = re.sub(r"[^a-z0-9_./#-]+", " ", text)
    words = set()
    for raw in text.split():
        word = raw.strip("-./#_")
        if len(word) >= 3 and word not in _STOPWORDS:
            words.add(word)
    return words


def coverage(needle: set[str], haystack: set[str]) -> float:
    """Share of `needle` present in `haystack`. Empty needle yields 0.0 — unknown, not perfect."""
    if not needle:
        return 0.0
    return len(needle & haystack) / len(needle)


def split_decision(body: str) -> tuple[str, str]:
    """Split an entry body into (decision, rationale) on its `Why:` clause.

    A trailing `→ [[pointer]]` is dropped from the decision half: it is archive bookkeeping about
    where the decision went, not part of what was decided, and leaving it in would let an entry match
    the artifact on the artifact's own name.
    """
    match = _WHY.search(body)
    if not match:
        return _POINTER_TAIL.sub("", body).strip(), ""
    return _POINTER_TAIL.sub("", body[: match.start()]).strip(), body[match.end() :].strip()


def parse_decided(section: str) -> list[DecidedEntry]:
    """Parse `## Decided` bullets. Prose in the section is commentary, not entries, and is skipped."""
    entries = []
    for line in section.splitlines():
        if not _BULLET.match(line):
            continue
        parsed = _ENTRY.match(line)
        if not parsed:
            continue
        decision, rationale = split_decision(parsed.group(2))
        if not decision:
            continue
        entries.append(
            DecidedEntry(
                line=line.strip(), date=parsed.group(1) or "", decision=decision, rationale=rationale
            )
        )
    return entries


def wikilinks(text: str) -> list[str]:
    """Link targets in document order, deduplicated. `[[name|alias]]` and `[[name#heading]]` reduce
    to `name` — the alias is display text and the heading is a position inside the same file."""
    seen: list[str] = []
    for raw in _WIKILINK.findall(text):
        target = raw.split("|", 1)[0].split("#", 1)[0].strip()
        if target and target not in seen:
            seen.append(target)
    return seen


def _candidate_paths(root: Path, name: str) -> Iterable[Path]:
    direct = root / f"{name}.md"
    if direct.is_file():
        yield direct
    # Obsidian resolves a bare `[[name]]` anywhere in the vault, so a brief may link `[[CLAUDE]]`
    # while the file sits several directories down. Hidden trees are skipped: `.git` alone can hold
    # thousands of files and none of them are artifacts an agent reads.
    for path in sorted(root.rglob(f"{Path(name).name}.md")):
        if not any(part.startswith(".") for part in path.relative_to(root).parts):
            yield path


def resolve_artifact(name: str, roots: Sequence[Path]) -> Path | None:
    """First file matching `[[name]]` across the search roots, or None.

    Roots are supplied by the caller — the vault plus whatever repos hold the docs a brief links.
    Nothing is defaulted or inferred: a hardcoded path would resolve to a file the operator never
    chose, and reporting the wrong artifact as the new home of a decision is how a rationale dies.
    """
    for root in roots:
        if not root.is_dir():
            continue
        for path in _candidate_paths(root, name):
            return path
    return None


def passages(path: Path) -> list[tuple[str, str]]:
    """Split an artifact into (heading, text) pairs on its Markdown headings.

    Heading-sized, because the pointer left behind has to be small enough to be worth following. Text
    before the first heading is kept under an empty heading rather than dropped.
    """
    blocks: list[tuple[str, str]] = []
    heading = ""
    buffer: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        found = _HEADING.match(line)
        if found:
            if buffer:
                blocks.append((heading, "\n".join(buffer)))
            heading = found.group(2)
            buffer = []
        else:
            buffer.append(line)
    if buffer:
        blocks.append((heading, "\n".join(buffer)))
    return blocks


def _explains(passage: str, entry_words: set[str]) -> bool:
    """Does this passage carry reasoning, or does it only restate the decision?

    Only consulted for entries that never recorded a rationale. Both conditions are needed: the
    length test alone passes any long section that happens to mention the decision, and the marker
    test alone passes a one-line bullet containing the word "because".
    """
    passage_words = significant_words(passage)
    if len(passage_words) < MIN_PASSAGE_RATIO * max(len(entry_words), 1):
        return False
    return bool(_REASON_MARKERS.search(passage))


def _encodings(entry: DecidedEntry, artifacts: dict[str, Path]) -> list[Encoding]:
    """Every artifact passage that encodes both halves of this entry, best coverage first."""
    decision_words = significant_words(entry.decision)
    if len(decision_words) < MIN_SIGNIFICANT:
        entry.retained = "too little distinctive text to verify against an artifact"
        return []
    if not artifacts:
        # Distinct from "searched and did not find it": nothing was searched. Common and correct on
        # code streams, whose decisions cite commits and file paths rather than documents.
        entry.retained = "the brief links no artifact, so there is nowhere this could already be encoded"
        return []

    rationale_words = significant_words(entry.rationale)
    found: list[Encoding] = []
    best_decision = 0.0
    for name, path in artifacts.items():
        for heading, text in passages(path):
            words = significant_words(text)
            decision_cov = coverage(decision_words, words)
            best_decision = max(best_decision, decision_cov)
            if decision_cov < COVERAGE:
                continue

            if entry.has_rationale:
                # Same passage, not merely the same file: a reason sitting two sections away from the
                # decision is not where the next agent will look, so archiving against it loses the why.
                rationale_cov = coverage(rationale_words, words)
                explains = rationale_cov >= COVERAGE
            else:
                rationale_cov = 0.0
                explains = _explains(text, decision_words)
            if not explains:
                continue
            found.append(Encoding(name, path, heading, decision_cov, rationale_cov, explains))

    found.sort(key=lambda e: (-e.decision_coverage, e.artifact, e.section))
    if not found:
        entry.retained = (
            "decision not found in any linked artifact"
            if best_decision < COVERAGE
            else (
                "decision is written down but its rationale is not — archiving it would leave a "
                "decision nobody can defend, which is how it gets re-litigated"
            )
        )
    return found


def _match_strings(entries: list[DecidedEntry]) -> None:
    """Give each entry the shortest prefix that identifies it uniquely.

    `writer.archive_decision` refuses an ambiguous match, so a suggestion that cannot be acted on is
    not a suggestion. Computed against the whole section, not just the candidates: a prefix shared
    with a retained entry is exactly the collision that would archive the wrong decision.
    """
    for entry in entries:
        words = entry.decision.split()
        for size in range(4, len(words) + 1):
            probe = " ".join(words[:size])
            if sum(1 for other in entries if probe in other.line) == 1:
                entry.match = probe
                break
        else:
            entry.match = entry.decision


def classify(brief: vault.Brief) -> str:
    """Code stream or document stream, from claims.

    Repo, branch and PR claims exist only for code work. The distinction is not cosmetic: it decides
    whether a drained `## Decided` reads as a healthy stream or as a leak.
    """
    if any(brief.claim(kind) for kind in CODE_CLAIM_KINDS):
        return KIND_CODE
    return KIND_DOCUMENT


def artifact_index(brief: vault.Brief, roots: Sequence[Path]) -> tuple[dict[str, Path], list[str]]:
    """Resolve every artifact the brief links. Returns (resolved, unresolved names).

    Files inside the stream's own directory are excluded. `decided-archive.md` in particular already
    contains the text of archived decisions, so letting it count as an encoding would mean every
    archived decision proves itself — and an entry could be "encoded" by nothing but its own copy.
    """
    resolved: dict[str, Path] = {}
    unresolved: list[str] = []
    stream_dir = brief.path.parent.resolve()
    for name in wikilinks("\n".join(brief.sections.values())):
        path = resolve_artifact(name, roots)
        if path is None:
            unresolved.append(name)
            continue
        if path.resolve().parent == stream_dir:
            continue
        resolved[name] = path
    return resolved, unresolved


def _warnings(report: Report) -> list[str]:
    notes = []
    if report.over_budget:
        notes.append(
            f"OVER BUDGET: {report.total_words} words against a {report.budget}-word budget "
            f"({report.overage} over). Compact by archiving encoded decisions, never by truncating — "
            f"and never by age or count."
        )

    missing = [entry for entry in report.entries if not entry.has_rationale]
    if missing:
        notes.append(
            f"NO RATIONALE: {len(missing)} of {len(report.entries)} ## Decided entries carry no "
            f"`Why:` clause. The rationale is not optional; a decision without its reason gets "
            f"re-litigated the moment its context is gone."
        )

    if report.kind == KIND_CODE:
        if not report.entries:
            notes.append(
                "RATIONALE LOSS: ## Decided is empty on a code stream. That is not a tidy stream — "
                "code encodes what was decided but rarely why, so an empty ## Decided here means the "
                "reasoning is being lost rather than written down. Expect a code stream to retain "
                "most of its entries."
            )
        elif not report.retained:
            notes.append(
                f"RATIONALE LOSS: archiving all {len(report.entries)} entries would empty ## Decided "
                f"on a code stream. Code encodes what was decided but rarely why — re-read each "
                f"pointer and confirm the *reason* survives there, not just the decision."
            )
    elif report.entries and not report.candidates:
        notes.append(
            f"NOT YET ENCODED: {len(report.entries)} entries on a document-producing stream, none "
            f"encoded in a linked artifact. These streams normally drain almost completely, because "
            f"their output is the encoding — so this reads as output not yet written, not as a brief "
            f"that needs trimming."
        )

    if report.unresolved_links:
        notes.append(
            "UNRESOLVED LINKS: "
            + ", ".join(f"[[{name}]]" for name in report.unresolved_links)
            + ". Nothing can be verified as encoded there. Add the root holding them with --root."
        )
    return notes


def audit(
    brief: vault.Brief,
    *,
    roots: Sequence[Path] = (),
    budget: int = WORD_BUDGET,
    kind: str | None = None,
) -> Report:
    """Measure a brief against its budget and find the entries that have earned archiving."""
    section_words = {name: count_words(brief.sections.get(name, "")) for name in vault.SECTIONS}
    for name, content in brief.sections.items():
        section_words.setdefault(name, count_words(content))

    resolved = vault.resolve_transclusions(brief)
    transcluded = sum(
        count_words(resolved.get(name, "")) - section_words.get(name, 0) for name in EXCLUDED_FROM_BUDGET
    )

    entries = parse_decided(brief.sections.get(DECIDED_SECTION, ""))
    # Default search root is the vault the caller's own brief came out of — derived from that brief,
    # never a path this module chose. Docs living outside the vault need an explicit --root.
    search_roots = list(roots) or [brief.path.parent.parent.parent]
    artifacts, unresolved = artifact_index(brief, search_roots)
    for entry in entries:
        entry.encodings = _encodings(entry, artifacts)
    _match_strings(entries)

    report = Report(
        slug=brief.slug,
        kind=kind or classify(brief),
        budget=budget,
        section_words=section_words,
        total_words=sum(
            words for name, words in section_words.items() if name not in EXCLUDED_FROM_BUDGET
        ),
        transcluded_words=max(0, transcluded),
        entries=entries,
        unresolved_links=unresolved,
    )
    report.warnings = _warnings(report)
    return report


def render(report: Report) -> str:
    """Plain-text report. Candidates are proposals — this module has changed nothing."""
    lines = [
        f"# budget: {report.slug} ({report.kind} stream)",
        "",
        f"{report.total_words} / {report.budget} words"
        + (f"  OVER by {report.overage}" if report.over_budget else "  within budget"),
    ]
    if report.transcluded_words:
        lines.append(
            f"(+{report.transcluded_words} transcluded into ## Ground truth — hook-written, not the "
            f"owner's to compact, so not counted against the budget)"
        )

    lines += ["", "## Words per section", ""]
    for name, words in report.section_words.items():
        note = "  (transcluded, excluded)" if name in EXCLUDED_FROM_BUDGET else ""
        lines.append(f"  {name:<14} {words:>4}{note}")

    if report.warnings:
        lines += ["", "## Warnings", ""]
        lines += [f"  - {note}" for note in report.warnings]

    lines += ["", f"## Archive candidates ({len(report.candidates)} of {len(report.entries)})", ""]
    if report.candidates:
        lines.append(
            "  Proposals only — nothing has been archived. Each decision *and* its rationale was"
        )
        lines.append(
            "  found at the pointer below; the stream owner archives with writer.archive_decision."
        )
        lines.append("")
    for entry in report.candidates:
        lines.append(f"  {entry.line}")
        for encoding in entry.encodings:
            lines.append(f"      encoded in {encoding.pointer}  ({encoding.decision_coverage:.0%})")
        if not entry.has_rationale:
            lines.append("      note: entry recorded no `Why:`; verified the passage explains it")
        lines.append(f'      archive: match={entry.match!r} pointer={entry.pointer!r}')
    if not report.candidates:
        lines.append("  (none — every entry is still the only place its reasoning lives)")

    if report.retained:
        lines += ["", f"## Retained ({len(report.retained)})", ""]
        for entry in report.retained:
            lines.append(f"  {entry.line}")
            lines.append(f"      {entry.retained}")

    lines += [
        "",
        "Never evicted by age or count: that keeps trivia and drops foundational decisions.",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m plugin.lib.budget",
        description="Report a brief's word budget and the ## Decided entries already encoded elsewhere.",
    )
    parser.add_argument("slug", help="stream slug; never inferred")
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        dest="roots",
        metavar="PATH",
        help="extra directory to resolve [[wikilinks]] against, e.g. the repo holding the docs a "
        "brief links; repeatable. The vault is always searched.",
    )
    parser.add_argument("--budget", type=int, default=WORD_BUDGET)
    parser.add_argument(
        "--kind",
        choices=[KIND_CODE, KIND_DOCUMENT],
        default=None,
        help="override the code/document classification derived from claims",
    )
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        root = vault.resolve_vault()
        brief = vault.read_stream(root, args.slug)
        roots = [root] + [Path(p).expanduser() for p in args.roots]
        print(render(audit(brief, roots=roots, budget=args.budget, kind=args.kind)))
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
