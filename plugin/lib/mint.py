"""Lazy minting: a stream is created at the first durable write, never at session start.

Three rules shape this module, and each is a decision already made rather than a preference:

1. **Minting is lazy.** Most sessions produce nothing worth keeping, so minting up front floods the
   board with empty streams. Nothing here creates anything until a caller presents content that
   clears the bar — `plan()` is deliberately pure and takes no vault root, so "did this session mint
   anything" stays a question about one call rather than an audit of the whole module.

2. **The bar is data.** `MINT_BAR` is the entire rule set: three kinds that mint and three that
   explicitly do not. It is a table so it can be printed, tested, and disagreed with; buried in an
   if-chain the bar becomes prose that the code may or may not still implement.

3. **Joining beats minting, always.** Every mint runs through `claims.join_or_mint` first. A stream
   already claiming that PR or issue is joined, not duplicated — except an archived one, whose
   conclusions are final and which must never be revived by new work landing on the same PR.

Posture, settled in the idea doc: structured dispatch (start-task, Linear, PR review, scheduled)
mints **silently**, because the slug already exists at dispatch time. Only a bare ad-hoc session with
no derivable slug prompts, once, at first durable write — by which point the agent has enough context
to *propose* a slug rather than ask blind.

Stdlib only, like the rest of `plugin/lib`. Writing is delegated to `writer`, resolution to `claims`;
this module owns the decision of *whether* and *where*, not the mechanics of either.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Sequence

from . import claims, vault, writer

# Dispatch modes, and whether each one may mint without asking. Structured paths carry a slug their
# own tooling already derived, so a prompt there is friction with nothing to resolve. `ad-hoc` is the
# only path where inference can genuinely fail, and even it stays silent when the session carries a
# durable identifier to derive from.
DISPATCH_AD_HOC = "ad-hoc"
DISPATCH_START_TASK = "start-task"
DISPATCH_LINEAR = "linear"
DISPATCH_PR_REVIEW = "pr-review"
DISPATCH_SCHEDULED = "scheduled"

DISPATCH_MODES = {
    DISPATCH_START_TASK: True,
    DISPATCH_LINEAR: True,
    DISPATCH_PR_REVIEW: True,
    DISPATCH_SCHEDULED: True,
    DISPATCH_AD_HOC: False,
}

# Outcomes of a mint attempt. Every one is reportable: a caller must always be able to say what
# happened to the content it handed over, including when the answer is "nothing was created".
ACTION_MINT = "mint"
ACTION_JOIN = "join"
ACTION_PROMPT = "prompt"
ACTION_SKIPPED = "skipped"
ACTION_BELOW_BAR = "below-bar"
ACTION_AMBIGUOUS = "ambiguous"

# Why a contribution failed the bar. Coded rather than message-matched so callers can branch on the
# distinction that matters: an unknown or below-bar kind is *filed*, a missing rationale is *refused*
# — the fix for the first is triage, the fix for the second is finishing the decision.
CODE_CLEARS = "clears"
CODE_NO_SUMMARY = "no-summary"
CODE_UNKNOWN_KIND = "unknown-kind"
CODE_BELOW_BAR = "below-bar"
CODE_NO_RATIONALE = "no-rationale"

DEFAULT_OWNER = "unknown"
DEFAULT_WHO = "operator"

OPEN_SECTION = "Open"


class MintError(vault.VaultError):
    """A mint that must not proceed. Subclasses VaultError so callers keep one except clause."""


@dataclass(frozen=True)
class BarRule:
    """One row of the mint bar.

    `section` is where a clearing contribution lands, and it is part of the rule rather than a
    caller's choice: the bar and the destination are the same judgment. Below-bar rules carry no
    section because they never reach a brief.
    """

    kind: str
    clears: bool
    description: str
    why: str
    section: str | None = None
    requires_rationale: bool = False


# The bar, in full. Both halves are load-bearing: the three that mint are the content that is
# expensive to reconstruct, and the three that do not are the ones agents most often mistake for
# progress. Naming the rejections explicitly is what keeps the board from filling with streams whose
# whole content is "ran the tests, they passed".
MINT_BAR: tuple[BarRule, ...] = (
    BarRule(
        kind="decision",
        clears=True,
        description="a decision with its rationale",
        why="the reasoning dies with the session and gets re-litigated without it",
        section=writer.DECIDED_SECTION,
        requires_rationale=True,
    ),
    BarRule(
        kind="blocker",
        clears=True,
        description="a blocker that needs someone else",
        why="it cannot be resolved inside this session, so it has to outlive it",
        section=OPEN_SECTION,
    ),
    BarRule(
        kind="artifact",
        clears=True,
        description="an artifact others will build on",
        why="the next agent needs to reach it, and why it matters is not in the file itself",
        section=writer.DECIDED_SECTION,
        requires_rationale=True,
    ),
    BarRule(
        kind="ran-tests",
        clears=False,
        description="ran tests",
        why="the result is reproducible in one command, and hooks record it as ground truth anyway",
    ),
    BarRule(
        kind="read-files",
        clears=False,
        description="read files",
        why="reading produced no state; the repo still says exactly what it said before",
    ),
    BarRule(
        kind="answered-question",
        clears=False,
        description="answered a question",
        why="an answer nobody has to act on is not something the next agent needs handed to them",
    ),
)

_BAR_BY_KIND = {rule.kind: rule for rule in MINT_BAR}


@dataclass(frozen=True)
class Choice:
    """One option on the ad-hoc prompt. Data so the prompt text and the handler cannot drift apart."""

    name: str
    effect: str


CHOICE_ACCEPT = "accept"
CHOICE_EDIT = "edit"
CHOICE_SKIP = "skip"

PROMPT_CHOICES: tuple[Choice, ...] = (
    Choice(CHOICE_ACCEPT, "mint the proposed slug and record this write there"),
    Choice(CHOICE_EDIT, "supply a different slug; that one is minted instead"),
    Choice(CHOICE_SKIP, "mint nothing; the write is filed in unassigned/ for triage"),
)


@dataclass
class Contribution:
    """What the session is trying to write, and what kind of thing it is.

    `kind` is checked against the bar rather than inferred from the text: an agent describing its own
    output is exactly the party with an incentive to over-report, and a free-text classifier would
    make the bar unfalsifiable.
    """

    kind: str
    summary: str
    why: str = ""
    who: str = DEFAULT_WHO  # only meaningful for a blocker: who can unblock it


@dataclass
class BarVerdict:
    """Whether a contribution clears the bar, which rule said so, and why."""

    clears: bool
    code: str
    reason: str
    rule: BarRule | None = None


@dataclass
class SlugProposal:
    """A slug offered to a human, with the options that may be taken on it."""

    slug: str
    reason: str = ""
    hint_candidates: list[vault.Brief] = field(default_factory=list)

    def prompt_text(self) -> str:
        lines = [
            "No stream covers this work yet, and nothing in this session derives a slug.",
            f"Proposed slug: {self.slug}",
            "",
        ]
        lines += [f"  {choice.name:<7}- {choice.effect}" for choice in PROMPT_CHOICES]
        if self.hint_candidates:
            named = ", ".join(b.slug for b in self.hint_candidates)
            lines += [
                "",
                f"Claiming your branch or worktree, not joined for you ({named}): those claims are "
                "ephemeral, and a reviewer sharing an author's branch would land in the author's "
                "stream. Name one explicitly to join it.",
            ]
        return "\n".join(lines)


@dataclass
class MintPlan:
    """What would happen, computed without touching the filesystem.

    Pure by construction — it takes streams, not a vault root — so a caller can inspect the decision
    and still be certain nothing was created. That is the whole laziness guarantee in one property.
    """

    action: str
    verdict: BarVerdict
    slug: str = ""
    stream: vault.Brief | None = None
    decision: claims.MintDecision | None = None
    proposal: SlugProposal | None = None
    reason: str = ""

    @property
    def mints(self) -> bool:
        return self.action == ACTION_MINT


@dataclass
class MintOutcome:
    """Where the content landed, and whether this call created a stream.

    `minted` is true only when *this* call created the directory. A racing loser reports
    `raced=True` and `action=join`, because from the vault's point of view a second stream for one
    slug is the duplicate the whole module exists to prevent.
    """

    action: str
    slug: str = ""
    path: Path | None = None
    minted: bool = False
    raced: bool = False
    unassigned: bool = False
    proposal: SlugProposal | None = None
    plan: MintPlan | None = None
    reason: str = ""


def bar_rule(kind: str) -> BarRule | None:
    """The rule for a kind, or None when the kind is not on the bar at all."""
    return _BAR_BY_KIND.get(str(kind).strip().lower())


def bar_kinds(clears: bool | None = None) -> list[str]:
    """Kinds on the bar, optionally filtered to the ones that mint or the ones that do not."""
    return [rule.kind for rule in MINT_BAR if clears is None or rule.clears is clears]


def evaluate(contribution: Contribution) -> BarVerdict:
    """Apply the bar. A table lookup plus one rationale check — no judgment lives here.

    An unknown kind does *not* silently clear: content whose kind nobody wrote a rule for is filed
    for triage, which loses nothing, rather than minting a stream nobody asked for.
    """
    summary = " ".join(str(contribution.summary or "").split())
    if not summary:
        return BarVerdict(False, CODE_NO_SUMMARY, "a write needs a one-line summary")

    rule = bar_rule(contribution.kind)
    if rule is None:
        return BarVerdict(
            False,
            CODE_UNKNOWN_KIND,
            f"'{contribution.kind}' is not on the mint bar; only {', '.join(bar_kinds(True))} "
            f"mint a stream",
        )
    if not rule.clears:
        return BarVerdict(
            False, CODE_BELOW_BAR, f"{rule.description} is below the bar — {rule.why}", rule
        )
    if rule.requires_rationale and not str(contribution.why or "").strip():
        return BarVerdict(
            False,
            CODE_NO_RATIONALE,
            f"{rule.description}: the rationale is the half that dies with the session, so "
            f"{contribution.kind} without a `why` does not clear the bar",
            rule,
        )
    return BarVerdict(True, CODE_CLEARS, f"{rule.description} — {rule.why}", rule)


def _is_silent(mode: str) -> bool:
    if mode not in DISPATCH_MODES:
        raise MintError(
            f"Unknown dispatch mode '{mode}'. Known modes: {', '.join(sorted(DISPATCH_MODES))}."
        )
    return DISPATCH_MODES[mode]


def has_derivable_slug(hints: claims.Hints) -> bool:
    """Does the session carry a durable identifier a slug can be derived from?

    Branch and worktree are excluded on purpose. They are ephemeral and intent-blind, so a slug
    derived from them names the checkout rather than the work — which is the same reason
    `join_or_mint` refuses to join on them.
    """
    return bool(
        hints.stream
        or hints.pr
        or claims.parse_issue_keys(hints.issue)
        or claims.parse_issue_keys(hints.branch)
    )


def plan(
    streams: Sequence[vault.Brief],
    contribution: Contribution,
    hints: claims.Hints | None = None,
    *,
    task: str = "",
    mode: str = DISPATCH_AD_HOC,
) -> MintPlan:
    """Decide join / mint / prompt / file, creating nothing.

    Order matters: the bar is checked before the claims lookup, so a below-bar write never even asks
    which stream it would have belonged to. Asking is how "lazy" quietly becomes "eager but polite".
    """
    hints = hints or claims.Hints()
    silent = _is_silent(mode)
    verdict = evaluate(contribution)
    if not verdict.clears:
        return MintPlan(action=ACTION_BELOW_BAR, verdict=verdict, reason=verdict.reason)

    decision = claims.join_or_mint(streams, hints, task=task or contribution.summary)

    if decision.action == "join":
        return MintPlan(
            action=ACTION_JOIN,
            verdict=verdict,
            slug=decision.slug,
            stream=decision.stream,
            decision=decision,
            reason=decision.reason,
        )
    if decision.action == "ambiguous":
        return MintPlan(
            action=ACTION_AMBIGUOUS, verdict=verdict, decision=decision, reason=decision.reason
        )

    if silent or has_derivable_slug(hints):
        return MintPlan(
            action=ACTION_MINT,
            verdict=verdict,
            slug=decision.slug,
            decision=decision,
            reason=decision.reason,
        )
    return MintPlan(
        action=ACTION_PROMPT,
        verdict=verdict,
        slug=decision.slug,
        decision=decision,
        proposal=SlugProposal(
            slug=decision.slug,
            reason=decision.reason,
            hint_candidates=list(decision.hint_candidates),
        ),
        reason="ad-hoc session with no derivable slug; propose once, then mint",
    )


def resolve_choice(
    proposal: SlugProposal,
    choice: str,
    slug: str | None = None,
    taken: Sequence[str] = (),
) -> str | None:
    """Turn an answer to the prompt into a slug, or None for skip."""
    answer = str(choice or "").strip().lower()
    if answer == CHOICE_ACCEPT:
        return proposal.slug
    if answer == CHOICE_SKIP:
        return None
    if answer == CHOICE_EDIT:
        edited = str(slug or "").strip()
        if not edited:
            raise MintError("Editing the proposed slug requires the replacement slug.")
        _require_slug(edited)
        return claims.ensure_unique_slug(edited, list(taken))
    raise MintError(
        f"Unknown choice '{choice}'. Options: {', '.join(c.name for c in PROMPT_CHOICES)}."
    )


def _require_slug(slug: str) -> str:
    """A slug must be its own kebab-case normal form.

    Checked against `claims.slugify` rather than a bespoke regex so there is one definition of a
    slug, and so anything that could climb out of the vault — `../x`, an absolute path — fails by
    simply not being a slug.
    """
    text = str(slug or "").strip()
    if not text or claims.slugify(text) != text:
        raise MintError(
            f"'{slug}' is not a stream slug. Slugs are lowercase kebab-case and become a directory "
            f"name; try '{claims.slugify(text) or 'a-short-name'}'."
        )
    return text


def _stamp(now: datetime) -> str:
    # Same wire format `writer` patches into `updated`. A brief is readable the instant it exists, so
    # the field is written at creation rather than left for the first update to fill in.
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_brief(
    slug: str,
    *,
    goal: str,
    title: str = "",
    owner: str = "",
    claim_block: dict[str, list[str]] | None = None,
    now: datetime | None = None,
) -> str:
    """The text of a newly minted `BRIEF.md`: schema frontmatter, six sections, canonical order.

    Sections come from `writer.canonical_order`, so a minted brief and an updated one are the same
    shape by construction. `## Ground truth` gets the transclusion and nothing else — hooks are its
    only writer, and even an empty `ground-truth.md` created here would make minting the author of
    the evidence the brief is checked against.
    """
    slug = _require_slug(slug)
    goal = str(goal or "").strip()
    if not goal:
        raise MintError(
            f"Minting '{slug}' needs a goal. A brief whose purpose is unstated cannot cold-start "
            f"anyone, which is the only reason the stream exists."
        )
    now = now or writer.utc_now()
    block = claim_block or {}
    front = [
        f"stream: {slug}",
        f"title: {' '.join((title or slug).split())}",
        "state: active",
        f"owner: {owner or DEFAULT_OWNER}",
        f"updated: {_stamp(now)}",
        "claims:",
    ]
    front += [
        f"  {kind}: [{', '.join(str(v) for v in block.get(kind, []))}]" for kind in vault.CLAIM_KINDS
    ]
    body = writer.render_body("", writer.canonical_order([("Goal", goal)]))
    return "---\n" + "\n".join(front) + "\n---\n\n" + body


def create_stream(
    root: Path,
    slug: str,
    *,
    goal: str,
    title: str = "",
    owner: str = "",
    claim_block: dict[str, list[str]] | None = None,
    now: datetime | None = None,
) -> tuple[Path, bool]:
    """Create `streams/<slug>/BRIEF.md`. Returns (path, created_by_this_call).

    The exclusive create is the whole concurrency story: two sessions racing on one slug both call
    this, exactly one gets the file, and the loser is told so instead of overwriting a brief it never
    read. Deliberately *not* `writer._atomic_write` — a temp file plus `os.replace` is atomic against
    a torn read but not against a competing creator, and here the second writer is the hazard.
    """
    slug = _require_slug(slug)
    text = render_brief(
        slug, goal=goal, title=title, owner=owner, claim_block=claim_block, now=now
    )
    directory = vault.streams_dir(root) / slug
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / writer.BRIEF_FILE
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return path, False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path, True


def _record(
    root: Path,
    slug: str,
    contribution: Contribution,
    rule: BarRule,
    *,
    now: datetime | None = None,
) -> Path:
    """Write the contribution into the section its bar rule names."""
    if rule.section == writer.DECIDED_SECTION:
        return writer.append_decision(
            root, slug, contribution.summary, contribution.why, now=now
        )
    text = " ".join(str(contribution.summary).split())
    if rule.section == OPEN_SECTION and "who:" not in text.lower():
        # `## Open` entries carry who can resolve them; an untagged blocker is invisible to the
        # dashboard's operator queue, which is the only thing that escalates.
        text = f"{text} `who: {contribution.who or DEFAULT_WHO}`"
    return writer.append_entry(root, slug, rule.section, text, now=now)


def _file_for_triage(
    root: Path,
    contribution: Contribution,
    plan_: MintPlan,
    *,
    proposed: str = "",
    session: str = "unknown",
    cwd: str | None = None,
    branch: str | None = None,
    now: datetime | None = None,
) -> Path:
    """Send content that did not mint to `unassigned/`.

    Strict bar, cheap recovery: the bar stays strict precisely because nothing is lost when content
    falls below it. Format and filename are `writer.write_unassigned`'s, shared with the triage
    queue's other writers so triage never has to know which module produced a file.
    """
    body = " ".join(str(contribution.summary).split())
    if contribution.why.strip():
        body += f"\nWhy: {' '.join(contribution.why.split())}"
    return writer.write_unassigned(
        root,
        body,
        reason=plan_.reason or plan_.verdict.reason,
        session=session,
        cwd=cwd,
        branch=branch,
        proposed_stream=proposed,
        now=now,
    )


def durable_write(
    root: Path,
    contribution: Contribution,
    hints: claims.Hints | None = None,
    *,
    task: str = "",
    mode: str = DISPATCH_AD_HOC,
    choice: str | None = None,
    slug: str | None = None,
    owner: str = "",
    title: str = "",
    goal: str = "",
    session: str = "unknown",
    cwd: str | None = None,
    branch: str | None = None,
    now: datetime | None = None,
    streams: Sequence[vault.Brief] | None = None,
) -> MintOutcome:
    """The single entry point that may create a stream. Nothing else in the plugin mints.

    Two-phase on the ad-hoc path and stateless across the phases: called without `choice` it returns
    the proposal and creates nothing, and the caller calls again with the human's answer. A callback
    would have made "did anything get minted" depend on what the callback did.
    """
    hints = hints or claims.Hints()
    now = now or writer.utc_now()
    known = list(vault.list_streams(root)) if streams is None else list(streams)
    plan_ = plan(known, contribution, hints, task=task, mode=mode)

    if plan_.action == ACTION_BELOW_BAR:
        # A malformed contribution is refused rather than filed: triage cannot supply a rationale
        # the author never had, so filing it would only move the problem where nobody is looking.
        if plan_.verdict.code in (CODE_NO_SUMMARY, CODE_NO_RATIONALE):
            raise MintError(plan_.verdict.reason)
        return MintOutcome(
            action=ACTION_BELOW_BAR,
            path=_file_for_triage(
                root, contribution, plan_, session=session, cwd=cwd, branch=branch, now=now
            ),
            unassigned=True,
            plan=plan_,
            reason=plan_.reason,
        )

    if plan_.action == ACTION_AMBIGUOUS:
        return MintOutcome(
            action=ACTION_AMBIGUOUS,
            path=_file_for_triage(
                root, contribution, plan_, session=session, cwd=cwd, branch=branch, now=now
            ),
            unassigned=True,
            plan=plan_,
            reason=plan_.reason,
        )

    if plan_.action == ACTION_JOIN:
        return MintOutcome(
            action=ACTION_JOIN,
            slug=plan_.slug,
            path=_record(root, plan_.slug, contribution, plan_.verdict.rule, now=now),
            plan=plan_,
            reason=plan_.reason,
        )

    if plan_.action == ACTION_PROMPT:
        if choice is None:
            return MintOutcome(
                action=ACTION_PROMPT,
                slug=plan_.slug,
                proposal=plan_.proposal,
                plan=plan_,
                reason=plan_.reason,
            )
        chosen = resolve_choice(
            plan_.proposal, choice, slug=slug, taken=[b.slug for b in known]
        )
        if chosen is None:
            return MintOutcome(
                action=ACTION_SKIPPED,
                path=_file_for_triage(
                    root,
                    contribution,
                    plan_,
                    proposed=plan_.slug,
                    session=session,
                    cwd=cwd,
                    branch=branch,
                    now=now,
                ),
                unassigned=True,
                proposal=plan_.proposal,
                plan=plan_,
                reason="operator skipped minting",
            )
    else:
        chosen = plan_.slug

    rule = plan_.verdict.rule
    if rule.requires_rationale:
        # Validated before anything is created. `writer` rejects placeholder rationales ("tbd",
        # "n/a") as well as absent ones, and discovering that after the mint would leave an empty
        # stream on the board.
        writer.format_decision(contribution.summary, contribution.why)

    path, created = create_stream(
        root,
        chosen,
        goal=goal or task or contribution.summary,
        title=title or task or chosen,
        owner=owner or session,
        claim_block=claims.claims_from_hints(hints),
        now=now,
    )
    return MintOutcome(
        action=ACTION_MINT if created else ACTION_JOIN,
        slug=chosen,
        path=_record(root, chosen, contribution, rule, now=now),
        minted=created,
        raced=not created,
        plan=plan_,
        reason=plan_.reason if created else f"another session minted '{chosen}' first; joined it",
    )


def bar_table() -> str:
    """The bar, rendered. Printable so the rule set can be read without reading the code."""
    lines = []
    for rule in MINT_BAR:
        verb = "mints" if rule.clears else "below"
        where = f"-> ## {rule.section}" if rule.section else ""
        lines.append(f"{verb}  {rule.kind:<18}{rule.description:<40}{where}".rstrip())
        lines.append(f"       {rule.why}")
    return "\n".join(lines)


def _hints_from_args(args: argparse.Namespace) -> claims.Hints:
    return claims.Hints(
        stream=args.stream,
        worktree=args.worktree,
        branch=args.branch,
        repo=args.repo,
        issue=args.issue,
        pr=args.pr,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m plugin.lib.mint")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("bar", help="print the mint bar")

    write = sub.add_parser("write", help="record a durable write, minting or joining as needed")
    write.add_argument("--kind", required=True, help=f"one of: {', '.join(bar_kinds())}")
    write.add_argument("--summary", required=True)
    write.add_argument("--why", default="")
    write.add_argument("--who", default=DEFAULT_WHO, help="for a blocker: who can unblock it")
    write.add_argument("--task", default="", help="task text; slug derivation reads this")
    write.add_argument("--mode", default=DISPATCH_AD_HOC, choices=sorted(DISPATCH_MODES))
    write.add_argument("--choice", default=None, choices=[c.name for c in PROMPT_CHOICES])
    write.add_argument("--slug", default=None, help="replacement slug, with --choice edit")
    write.add_argument("--goal", default="", help="## Goal for a newly minted stream")
    write.add_argument("--title", default="")
    write.add_argument("--owner", default="")
    write.add_argument("--session", default="unknown")
    write.add_argument("--cwd", default=None)
    write.add_argument("--stream", default=None)
    write.add_argument("--worktree", default=None)
    write.add_argument("--branch", default=None)
    write.add_argument("--repo", default=None)
    write.add_argument("--issue", default=None)
    write.add_argument("--pr", default=None)
    write.add_argument(
        "--dry-run", action="store_true", help="print the plan and create nothing at all"
    )
    return parser


def main(argv: list[str]) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "bar":
            print(bar_table())
            return 0

        root = vault.resolve_vault()
        contribution = Contribution(
            kind=args.kind, summary=args.summary, why=args.why, who=args.who
        )
        hints = _hints_from_args(args)
        if args.dry_run:
            plan_ = plan(
                vault.list_streams(root), contribution, hints, task=args.task, mode=args.mode
            )
            print(f"{plan_.action}: {plan_.reason or plan_.verdict.reason}")
            if plan_.proposal:
                print(plan_.proposal.prompt_text())
            return 0

        outcome = durable_write(
            root,
            contribution,
            hints,
            task=args.task,
            mode=args.mode,
            choice=args.choice,
            slug=args.slug,
            owner=args.owner,
            title=args.title,
            goal=args.goal,
            session=args.session,
            cwd=args.cwd,
            branch=args.branch,
        )
        if outcome.action == ACTION_PROMPT:
            print(outcome.proposal.prompt_text())
        elif outcome.unassigned:
            print(f"{outcome.action}: nothing minted; filed at {outcome.path} ({outcome.reason})")
        else:
            print(f"{outcome.action} {outcome.slug}: wrote {outcome.path}")
    except vault.VaultError as err:
        print(f"error: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
