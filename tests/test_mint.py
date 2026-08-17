"""Task 6 verification.

The claim under test is a negative one — *most sessions mint nothing* — so most of what follows is
an absence assertion, and every one of them is paired with a presence assertion: a suite that proves
"no stream was created" passes trivially against a module that cannot create streams at all.

The instrument for the absence half is `tree_digest`: a byte-level fingerprint of the whole scratch
vault. A test that mints nothing must leave that digest untouched, which catches a stray file the way
counting stream directories cannot.

Every test runs against a `tempfile` copy of the hand-verified fixtures in `examples/`, including the
archived `plat-1962-review-pr905` — the stream that still claims `pr: 905` and must never be revived
by new work on that PR.
"""

import hashlib
import io
import contextlib
import os
import shutil
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import claims, mint, vault, writer  # noqa: E402

FIXTURE_VAULT = REPO / "examples"
DESIGN = "message-board-design"
REVIEW = "plat-1962-review-pr905"

FIXED_NOW = datetime(2026, 8, 14, 9, 30, 0, tzinfo=timezone.utc)

DECISION = mint.Contribution(
    kind="decision",
    summary="Resolve claims before minting",
    why="a second stream for one PR splits the handoff in half",
)
RAN_TESTS = mint.Contribution(kind="ran-tests", summary="Ran the suite; 62 passed")


def tree_digest(root: Path) -> str:
    """Fingerprint every file under `root`, path and bytes both."""
    digest = hashlib.sha256()
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_brief(root: Path, slug: str, state="active", claim_block=None, title=""):
    """Materialize a stream in a scratch vault, shaped like examples/streams/*/BRIEF.md.

    Written by hand rather than through `mint.render_brief` so the fixtures a test asserts against do
    not come from the code under test.
    """
    directory = vault.streams_dir(root) / slug
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"stream: {slug}",
        f"title: {title or slug}",
        f"state: {state}",
        "owner: test-agent",
        "updated: 2026-08-13T00:00:00Z",
        "claims:",
    ]
    block = claim_block or {}
    for kind in vault.CLAIM_KINDS:
        lines.append(f"  {kind}: [{', '.join(str(v) for v in block.get(kind, []))}]")
    lines += ["---", "", "## Goal", "", "test stream", "", "## Decided", "", "## Do not", "", "- x", ""]
    (directory / writer.BRIEF_FILE).write_text("\n".join(lines), encoding="utf-8")
    return directory


class VaultCase(unittest.TestCase):
    """Base: a disposable copy of the fixtures. Nothing here touches a real vault."""

    def setUp(self):
        scratch = Path(tempfile.mkdtemp(prefix="mb-mint-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        shutil.copytree(FIXTURE_VAULT / "streams", scratch / "streams")
        self.vault = scratch

    def digest(self):
        return tree_digest(self.vault)

    def slugs(self):
        return [b.slug for b in vault.list_streams(self.vault)]

    def brief_text(self, slug):
        return (vault.streams_dir(self.vault) / slug / writer.BRIEF_FILE).read_text(encoding="utf-8")

    def unassigned(self):
        directory = self.vault / writer.UNASSIGNED_DIR
        return sorted(directory.glob("*.md")) if directory.is_dir() else []


class BarIsData(VaultCase):
    """Acceptance 2: the bar is inspectable and testable, not an if-chain."""

    def test_bar_names_exactly_the_three_durable_kinds(self):
        self.assertEqual(set(mint.bar_kinds(True)), {"decision", "blocker", "artifact"})

    def test_bar_names_the_rejections_explicitly(self):
        self.assertEqual(
            set(mint.bar_kinds(False)), {"ran-tests", "read-files", "answered-question"}
        )

    def test_every_clearing_rule_names_a_canonical_section_and_never_ground_truth(self):
        for rule in mint.MINT_BAR:
            if rule.clears:
                self.assertIn(rule.section, vault.SECTIONS, rule.kind)
                self.assertNotEqual(rule.section, writer.GROUND_TRUTH_SECTION, rule.kind)
            else:
                self.assertIsNone(rule.section, rule.kind)

    def test_every_rule_states_why(self):
        for rule in mint.MINT_BAR:
            self.assertTrue(rule.why.strip(), rule.kind)
            self.assertTrue(rule.description.strip(), rule.kind)

    def test_rules_are_immutable(self):
        with self.assertRaises(Exception):
            mint.MINT_BAR[0].clears = False

    def test_below_bar_kinds_do_not_clear_and_say_why(self):
        for kind in mint.bar_kinds(False):
            verdict = mint.evaluate(mint.Contribution(kind=kind, summary="did a thing"))
            self.assertFalse(verdict.clears, kind)
            self.assertEqual(verdict.code, mint.CODE_BELOW_BAR)
            self.assertTrue(verdict.reason.strip())

    def test_unknown_kind_does_not_clear_and_lists_the_ones_that_do(self):
        verdict = mint.evaluate(mint.Contribution(kind="refactored-something", summary="moved code"))
        self.assertFalse(verdict.clears)
        self.assertEqual(verdict.code, mint.CODE_UNKNOWN_KIND)
        for kind in mint.bar_kinds(True):
            self.assertIn(kind, verdict.reason)

    def test_decision_without_rationale_does_not_clear(self):
        verdict = mint.evaluate(mint.Contribution(kind="decision", summary="use claims first"))
        self.assertFalse(verdict.clears)
        self.assertEqual(verdict.code, mint.CODE_NO_RATIONALE)

    def test_artifact_without_rationale_does_not_clear(self):
        verdict = mint.evaluate(mint.Contribution(kind="artifact", summary="wrote [[brief-schema]]"))
        self.assertFalse(verdict.clears)
        self.assertEqual(verdict.code, mint.CODE_NO_RATIONALE)

    def test_blocker_clears_without_a_rationale(self):
        verdict = mint.evaluate(mint.Contribution(kind="blocker", summary="needs a prod credential"))
        self.assertTrue(verdict.clears)
        self.assertEqual(verdict.rule.section, mint.OPEN_SECTION)

    def test_empty_summary_never_clears(self):
        verdict = mint.evaluate(mint.Contribution(kind="decision", summary="   ", why="because"))
        self.assertFalse(verdict.clears)
        self.assertEqual(verdict.code, mint.CODE_NO_SUMMARY)

    def test_decision_with_rationale_clears(self):
        verdict = mint.evaluate(DECISION)
        self.assertTrue(verdict.clears)
        self.assertEqual(verdict.rule.section, writer.DECIDED_SECTION)

    def test_bar_table_prints_both_halves(self):
        table = mint.bar_table()
        for kind in mint.bar_kinds():
            self.assertIn(kind, table)
        self.assertIn("below", table)
        self.assertIn("mints", table)


class NothingMintsBelowTheBar(VaultCase):
    """Acceptance 1: a session that never produces a durable write creates nothing."""

    def test_plan_touches_nothing(self):
        before = self.digest()
        streams = vault.list_streams(self.vault)
        for contribution in (DECISION, RAN_TESTS):
            mint.plan(streams, contribution, claims.Hints(branch="polds/plat-1870-speed"))
        self.assertEqual(self.digest(), before)

    def test_read_only_session_leaves_the_vault_byte_identical(self):
        before = self.digest()
        streams = vault.list_streams(self.vault)
        for kind in mint.bar_kinds(False):
            contribution = mint.Contribution(kind=kind, summary="session activity")
            self.assertFalse(mint.evaluate(contribution).clears)
            self.assertEqual(
                mint.plan(streams, contribution, claims.Hints()).action, mint.ACTION_BELOW_BAR
            )
        self.assertEqual(self.digest(), before)
        # Paired presence assertion: the same vault does mint when the bar is cleared, so the
        # digest above is evidence about the bar rather than about an inert module.
        outcome = mint.durable_write(
            self.vault, DECISION, claims.Hints(), task="claims before minting", mode="start-task"
        )
        self.assertTrue(outcome.minted)
        self.assertNotEqual(self.digest(), before)

    def test_below_bar_write_mints_no_stream_but_keeps_the_content(self):
        before = set(self.slugs())
        outcome = mint.durable_write(self.vault, RAN_TESTS, claims.Hints(), now=FIXED_NOW)
        self.assertEqual(outcome.action, mint.ACTION_BELOW_BAR)
        self.assertFalse(outcome.minted)
        self.assertEqual(set(self.slugs()), before)
        filed = self.unassigned()
        self.assertEqual(len(filed), 1)
        text = filed[0].read_text(encoding="utf-8")
        self.assertIn("Ran the suite", text)
        self.assertIn("below the bar", text)

    def test_unknown_kind_is_filed_not_minted(self):
        before = set(self.slugs())
        outcome = mint.durable_write(
            self.vault,
            mint.Contribution(kind="refactor", summary="split the resolver"),
            claims.Hints(),
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_BELOW_BAR)
        self.assertEqual(set(self.slugs()), before)
        self.assertEqual(len(self.unassigned()), 1)

    def test_rationale_free_decision_is_refused_not_filed(self):
        before = self.digest()
        with self.assertRaises(mint.MintError):
            mint.durable_write(
                self.vault,
                mint.Contribution(kind="decision", summary="use claims first"),
                claims.Hints(),
            )
        self.assertEqual(self.digest(), before)

    def test_placeholder_rationale_is_refused_before_anything_is_created(self):
        before = self.digest()
        with self.assertRaises(writer.WriteError):
            mint.durable_write(
                self.vault,
                mint.Contribution(kind="decision", summary="use claims first", why="tbd"),
                claims.Hints(),
                task="claims first",
                mode="start-task",
            )
        self.assertEqual(self.digest(), before)


class AdHocPrompt(VaultCase):
    """Acceptance 3: the ad-hoc path proposes a slug with accept / edit / skip."""

    def bare(self):
        """A bare ad-hoc session: a branch, nothing durable to derive a slug from."""
        return claims.Hints(branch="polds/scratch-work")

    def test_prompt_phase_creates_nothing(self):
        before = self.digest()
        outcome = mint.durable_write(
            self.vault, DECISION, self.bare(), task="resolve claims before minting"
        )
        self.assertEqual(outcome.action, mint.ACTION_PROMPT)
        self.assertFalse(outcome.minted)
        self.assertEqual(self.digest(), before)

    def test_proposal_offers_accept_edit_skip(self):
        outcome = mint.durable_write(
            self.vault, DECISION, self.bare(), task="resolve claims before minting"
        )
        text = outcome.proposal.prompt_text()
        for choice in ("accept", "edit", "skip"):
            self.assertIn(choice, text)
        self.assertIn(outcome.proposal.slug, text)

    def test_proposed_slug_is_derived_not_invented(self):
        """The proposal is `claims.derive_slug`'s output verbatim — never a second scheme.

        Both shapes are asserted because they differ: with no branch hint the ticket in the task
        text leads the slug, and with one it does not (`derive_slug` reads keys from the hint it was
        given rather than falling back to the task). Pinning both keeps the divergence visible if
        `claims` ever changes.
        """
        task = "PLAT-2100 resolve claims before minting"
        ticketed = mint.durable_write(self.vault, DECISION, claims.Hints(), task=task)
        self.assertEqual(ticketed.proposal.slug, claims.derive_slug(task=task))
        self.assertTrue(ticketed.proposal.slug.startswith("plat-2100"))

        branched = mint.durable_write(self.vault, DECISION, self.bare(), task=task)
        self.assertEqual(
            branched.proposal.slug, claims.derive_slug(task=task, issue=self.bare().branch)
        )

    def test_accept_mints_the_proposed_slug(self):
        proposal = mint.durable_write(
            self.vault, DECISION, self.bare(), task="resolve claims before minting"
        ).proposal
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            self.bare(),
            task="resolve claims before minting",
            choice="accept",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_MINT)
        self.assertTrue(outcome.minted)
        self.assertEqual(outcome.slug, proposal.slug)
        self.assertIn(proposal.slug, self.slugs())

    def test_edit_mints_the_supplied_slug(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            self.bare(),
            task="resolve claims before minting",
            choice="edit",
            slug="mint-bar-rework",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.slug, "mint-bar-rework")
        self.assertIn("mint-bar-rework", self.slugs())

    def test_edit_refuses_a_slug_that_is_a_path(self):
        before = self.digest()
        with self.assertRaises(mint.MintError):
            mint.durable_write(
                self.vault,
                DECISION,
                self.bare(),
                task="resolve claims",
                choice="edit",
                slug="../escape",
            )
        self.assertEqual(self.digest(), before)

    def test_edit_onto_an_existing_slug_gets_a_discriminator(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            self.bare(),
            task="resolve claims",
            choice="edit",
            slug=DESIGN,
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.slug, f"{DESIGN}-2")
        self.assertEqual(vault.read_stream(self.vault, DESIGN).state, "active")

    def test_skip_mints_nothing_and_files_the_content(self):
        before = set(self.slugs())
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            self.bare(),
            task="resolve claims before minting",
            choice="skip",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_SKIPPED)
        self.assertFalse(outcome.minted)
        self.assertEqual(set(self.slugs()), before)
        filed = self.unassigned()
        self.assertEqual(len(filed), 1)
        self.assertIn("Resolve claims before minting", filed[0].read_text(encoding="utf-8"))

    def test_unknown_choice_is_refused(self):
        with self.assertRaises(mint.MintError):
            mint.durable_write(
                self.vault, DECISION, self.bare(), task="resolve claims", choice="maybe"
            )

    def test_prompt_happens_once_per_stream(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            self.bare(),
            task="resolve claims before minting",
            choice="accept",
            now=FIXED_NOW,
        )
        # The session now knows its slug, so the second durable write declares it and joins.
        again = mint.durable_write(
            self.vault,
            mint.Contribution(
                kind="blocker", summary="needs the operator to pick a dashboard surface"
            ),
            claims.Hints(stream=outcome.slug, branch="polds/scratch-work"),
            now=FIXED_NOW,
        )
        self.assertEqual(again.action, mint.ACTION_JOIN)
        self.assertIsNone(again.proposal)


class StructuredDispatchIsSilent(VaultCase):
    """Posture: anything with a slug at dispatch time mints without asking."""

    def test_each_structured_mode_mints_silently(self):
        for i, mode in enumerate(sorted(m for m, silent in mint.DISPATCH_MODES.items() if silent)):
            with self.subTest(mode=mode):
                outcome = mint.durable_write(
                    self.vault,
                    DECISION,
                    claims.Hints(branch=f"polds/scratch-{i}"),
                    task=f"scratch task {i} for dispatch",
                    mode=mode,
                    now=FIXED_NOW,
                )
                self.assertEqual(outcome.action, mint.ACTION_MINT)
                self.assertIsNone(outcome.proposal)

    def test_ad_hoc_with_an_issue_key_mints_silently(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(branch="feat/PLAT-2100/mint", issue="PLAT-2100"),
            task="lazy mint at first durable write",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_MINT)
        self.assertTrue(outcome.slug.startswith("plat-2100"))

    def test_ad_hoc_with_a_pr_mints_silently(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(pr="1042", repo="acme/platform"),
            task="review the datastore composition",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_MINT)

    def test_branch_alone_is_not_a_derivable_slug(self):
        self.assertFalse(mint.has_derivable_slug(claims.Hints(branch="polds/scratch-work")))
        self.assertTrue(mint.has_derivable_slug(claims.Hints(branch="feat/PLAT-1719/gclb")))

    def test_unknown_dispatch_mode_is_refused(self):
        with self.assertRaises(mint.MintError):
            mint.plan(vault.list_streams(self.vault), DECISION, claims.Hints(), mode="vibes")


class ClaimsAreCheckedFirst(VaultCase):
    """Always join rather than duplicate — except an archived stream, which stays closed."""

    def test_a_live_stream_claiming_the_pr_is_joined(self):
        write_brief(self.vault, "pr905-followup", claim_block={"pr": [905], "repo": ["acme/platform"]})
        before = set(self.slugs())
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(pr="905", repo="acme/platform"),
            task="revisit the composite",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_JOIN)
        self.assertEqual(outcome.slug, "pr905-followup")
        self.assertFalse(outcome.minted)
        self.assertEqual(set(self.slugs()), before)

    def test_archived_pr905_is_not_revived_by_new_work_on_the_same_pr(self):
        archived_before = self.brief_text(REVIEW)
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(pr="905", repo="acme/platform", issue="PLAT-1962"),
            task="PLAT-1962 recheck the datastore gap",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_MINT)
        self.assertNotEqual(outcome.slug, REVIEW)
        # The archived stream is untouched: same bytes, still archived, still claiming the PR.
        self.assertEqual(self.brief_text(REVIEW), archived_before)
        closed = vault.read_stream(self.vault, REVIEW)
        self.assertEqual(closed.state, "archived")
        self.assertIn("905", closed.claim("pr"))
        # And the new stream really exists, claiming the same PR.
        fresh = vault.read_stream(self.vault, outcome.slug)
        self.assertEqual(fresh.state, "active")
        self.assertIn("905", fresh.claim("pr"))

    def test_a_slug_colliding_with_an_archived_stream_gets_a_discriminator(self):
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(stream=REVIEW),
            task="recheck the composite",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.slug, f"{REVIEW}-2")
        self.assertEqual(vault.read_stream(self.vault, REVIEW).state, "archived")

    def test_one_issue_across_many_streams_refuses_and_files(self):
        for n in (1, 2):
            write_brief(self.vault, f"plat-1719-part-{n}", claim_block={"issue": ["PLAT-1719"]})
        before = set(self.slugs())
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(issue="PLAT-1719"),
            task="PLAT-1719 gclb wiring",
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_AMBIGUOUS)
        self.assertFalse(outcome.minted)
        self.assertEqual(set(self.slugs()), before)
        filed = self.unassigned()[0].read_text(encoding="utf-8")
        self.assertIn("plat-1719-part-1", filed)
        self.assertIn("plat-1719-part-2", filed)

    def test_branch_match_alone_never_joins_silently(self):
        write_brief(self.vault, "someone-elses-stream", claim_block={"branch": ["polds/shared"]})
        outcome = mint.durable_write(
            self.vault, DECISION, claims.Hints(branch="polds/shared"), task="review that branch"
        )
        self.assertEqual(outcome.action, mint.ACTION_PROMPT)
        self.assertEqual(
            [b.slug for b in outcome.proposal.hint_candidates], ["someone-elses-stream"]
        )
        self.assertIn("someone-elses-stream", outcome.proposal.prompt_text())


class MintedBriefShape(VaultCase):
    """A minted brief must be a schema-valid brief the moment it exists."""

    def mint_one(self, contribution=DECISION, **kwargs):
        hints = kwargs.pop(
            "hints", claims.Hints(branch="feat/PLAT-2100/mint", repo="polds/message-board")
        )
        return mint.durable_write(
            self.vault,
            contribution,
            hints,
            task=kwargs.pop("task", "PLAT-2100 lazy mint at first durable write"),
            mode=kwargs.pop("mode", "start-task"),
            now=FIXED_NOW,
            **kwargs,
        )

    def test_sections_are_present_in_canonical_order(self):
        outcome = self.mint_one()
        text = self.brief_text(outcome.slug)
        positions = [text.index(f"## {name}") for name in vault.SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_ground_truth_is_a_transclusion_and_the_file_is_left_to_hooks(self):
        outcome = self.mint_one()
        brief = vault.read_stream(self.vault, outcome.slug)
        self.assertEqual(brief.sections["Ground truth"], writer.GROUND_TRUTH_TRANSCLUSION)
        self.assertFalse(
            (vault.streams_dir(self.vault) / outcome.slug / writer.GROUND_TRUTH_FILE).exists()
        )

    def test_frontmatter_registers_the_claims_the_session_carried(self):
        outcome = self.mint_one()
        brief = vault.read_stream(self.vault, outcome.slug)
        self.assertEqual(brief.state, "active")
        self.assertEqual(brief.slug, outcome.slug)
        self.assertEqual(brief.claim("branch"), ["feat/PLAT-2100/mint"])
        self.assertEqual(brief.claim("issue"), ["PLAT-2100"])
        self.assertEqual(brief.claim("repo"), ["polds/message-board"])
        for kind in vault.CLAIM_KINDS:
            self.assertIn(kind, brief.claims)

    def test_goal_falls_back_to_the_task_when_unstated(self):
        outcome = self.mint_one()
        self.assertIn(
            "lazy mint at first durable write", vault.read_stream(self.vault, outcome.slug).sections["Goal"]
        )

    def test_a_stream_with_no_goal_at_all_is_refused(self):
        with self.assertRaises(mint.MintError):
            mint.render_brief("some-slug", goal="   ")

    def test_a_decision_lands_in_decided_with_its_why(self):
        outcome = self.mint_one()
        decided = vault.read_stream(self.vault, outcome.slug).sections["Decided"]
        self.assertIn("Resolve claims before minting.", decided)
        self.assertIn("Why: a second stream for one PR splits the handoff in half.", decided)

    def test_a_blocker_lands_in_open_tagged_with_who(self):
        outcome = self.mint_one(
            mint.Contribution(kind="blocker", summary="needs a prod credential", who="operator")
        )
        brief = vault.read_stream(self.vault, outcome.slug)
        self.assertIn("needs a prod credential", brief.sections["Open"])
        self.assertEqual(len(brief.operator_asks()), 1)

    def test_an_artifact_lands_in_decided_with_its_wikilink(self):
        outcome = self.mint_one(
            mint.Contribution(
                kind="artifact",
                summary="Schema lives in [[brief-schema]]",
                why="every later task is written against it",
            )
        )
        self.assertIn("[[brief-schema]]", vault.read_stream(self.vault, outcome.slug).sections["Decided"])

    def test_minting_never_writes_a_ground_truth_section(self):
        outcome = self.mint_one(
            mint.Contribution(kind="blocker", summary="waiting on review", who="operator")
        )
        brief = vault.read_stream(self.vault, outcome.slug)
        self.assertEqual(brief.sections["Ground truth"], writer.GROUND_TRUTH_TRANSCLUSION)

    def test_the_vault_path_is_never_inferred(self):
        """The library takes a root; only the CLI reads the environment."""
        saved = os.environ.pop(vault.VAULT_ENV, None)
        if saved is not None:
            self.addCleanup(os.environ.__setitem__, vault.VAULT_ENV, saved)
        outcome = self.mint_one()
        self.assertTrue(outcome.minted)


class MintingIsAtomic(VaultCase):
    """Two sessions racing on one slug must not both win."""

    def race(self, create, threads=8, slug="raced-stream"):
        """Run `create` from many threads released together; return each thread's (created, goal)."""
        results = [None] * threads
        barrier = threading.Barrier(threads)

        def run(i):
            barrier.wait()
            try:
                _, created = create(slug, f"goal number {i}")
            except Exception as err:  # recorded, not swallowed: a raise is a failed race too
                results[i] = ("error", repr(err))
                return
            results[i] = (created, f"goal number {i}")

        workers = [threading.Thread(target=run, args=(i,)) for i in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        return results

    def safe_create(self, slug, goal):
        return mint.create_stream(self.vault, slug, goal=goal, now=FIXED_NOW)

    def naive_create(self, slug, goal):
        """The primitive this module deliberately does not use: last writer wins."""
        directory = vault.streams_dir(self.vault) / slug
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / writer.BRIEF_FILE
        path.write_text(mint.render_brief(slug, goal=goal, now=FIXED_NOW), encoding="utf-8")
        return path, True

    def test_exactly_one_racer_creates_the_stream(self):
        results = self.race(self.safe_create)
        winners = [goal for created, goal in results if created is True]
        self.assertEqual(len(winners), 1, results)
        self.assertIn(winners[0], self.brief_text("raced-stream"))
        self.assertEqual(len([1 for created, _ in results if created is False]), len(results) - 1)

    def test_the_race_harness_catches_the_naive_primitive(self):
        """Mutation check: swap in last-writer-wins and the assertion above must fail."""
        results = self.race(self.naive_create, slug="raced-naive")
        winners = [goal for created, goal in results if created is True]
        self.assertGreater(len(winners), 1)

    def test_the_losing_session_joins_instead_of_clobbering(self):
        # `streams=[]` hides the existing stream from resolution, which is exactly what a racing
        # session sees: it listed the vault before the other session created the directory.
        mint.create_stream(self.vault, "raced-stream", goal="the winner's goal", now=FIXED_NOW)
        outcome = mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(stream="raced-stream"),
            task="raced stream",
            streams=[],
            now=FIXED_NOW,
        )
        self.assertEqual(outcome.action, mint.ACTION_JOIN)
        self.assertTrue(outcome.raced)
        self.assertFalse(outcome.minted)
        brief = vault.read_stream(self.vault, "raced-stream")
        self.assertEqual(brief.sections["Goal"], "the winner's goal")
        self.assertIn("Resolve claims before minting.", brief.sections["Decided"])

    def test_a_slug_cannot_escape_the_streams_directory(self):
        before = self.digest()
        for bad in ("../escape", "/etc/passwd", "a/b", ".hidden", "Upper-Case", ""):
            with self.subTest(slug=bad), self.assertRaises(mint.MintError):
                mint.create_stream(self.vault, bad, goal="x", now=FIXED_NOW)
        self.assertEqual(self.digest(), before)


class CommandLine(VaultCase):
    def run_cli(self, argv, env_vault=True):
        if env_vault:
            saved = os.environ.get(vault.VAULT_ENV)
            os.environ[vault.VAULT_ENV] = str(self.vault)
            self.addCleanup(
                lambda: os.environ.__setitem__(vault.VAULT_ENV, saved)
                if saved is not None
                else os.environ.pop(vault.VAULT_ENV, None)
            )
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mint.main(argv)
        return code, out.getvalue()

    def test_bar_subcommand_prints_both_halves(self):
        code, out = self.run_cli(["bar"], env_vault=False)
        self.assertEqual(code, 0)
        for kind in mint.bar_kinds():
            self.assertIn(kind, out)

    def test_dry_run_creates_nothing(self):
        before = self.digest()
        code, out = self.run_cli(
            [
                "write",
                "--kind", "decision",
                "--summary", "Resolve claims before minting",
                "--why", "duplicate streams split the handoff",
                "--task", "PLAT-2100 lazy mint",
                "--dry-run",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn(mint.ACTION_MINT, out)
        self.assertEqual(self.digest(), before)

    def test_write_mints_and_reports_where_it_landed(self):
        code, out = self.run_cli(
            [
                "write",
                "--kind", "decision",
                "--summary", "Resolve claims before minting",
                "--why", "duplicate streams split the handoff",
                "--task", "PLAT-2100 lazy mint",
                "--mode", "start-task",
            ]
        )
        self.assertEqual(code, 0)
        self.assertIn("plat-2100", out)
        self.assertIn("plat-2100-lazy-mint", self.slugs())

    def test_below_bar_write_reports_the_filing(self):
        code, out = self.run_cli(
            ["write", "--kind", "ran-tests", "--summary", "62 tests passed", "--task", "run tests"]
        )
        self.assertEqual(code, 0)
        self.assertIn(writer.UNASSIGNED_DIR, out)
        self.assertEqual(len(self.unassigned()), 1)


class FixturesStayReadOnly(VaultCase):
    """`examples/` is hand-verified input; a test that writes into it has corrupted the corpus."""

    def test_a_full_mint_flow_leaves_examples_byte_identical(self):
        before = tree_digest(FIXTURE_VAULT)
        mint.durable_write(
            self.vault,
            DECISION,
            claims.Hints(pr="905", repo="acme/platform"),
            task="PLAT-1962 recheck the datastore gap",
            now=FIXED_NOW,
        )
        mint.durable_write(self.vault, RAN_TESTS, claims.Hints(), now=FIXED_NOW)
        self.assertEqual(tree_digest(FIXTURE_VAULT), before)


if __name__ == "__main__":
    unittest.main()
