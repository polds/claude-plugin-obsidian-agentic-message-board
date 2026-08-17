"""Task 3 verification.

Every test writes into a tempfile copy of `examples/`. Nothing here — and nothing the writer does —
may touch a real vault, so the scratch copy is built fresh per test and torn down after.
"""

import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import vault, writer  # noqa: E402

FIXTURE_VAULT = REPO / "examples"
DESIGN = "message-board-design"
REVIEW = "plat-1962-review-pr905"

FIXED_NOW = datetime(2026, 8, 14, 9, 30, 0, tzinfo=timezone.utc)


class VaultCase(unittest.TestCase):
    """Base: a disposable copy of the hand-verified fixtures."""

    def setUp(self):
        scratch = Path(tempfile.mkdtemp(prefix="message-board-test-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        shutil.copytree(FIXTURE_VAULT / "streams", scratch / "streams")
        self.vault = scratch

    def brief_text(self, slug=DESIGN):
        return (self.vault / "streams" / slug / "BRIEF.md").read_text(encoding="utf-8")

    def stream_file(self, name, slug=DESIGN):
        return self.vault / "streams" / slug / name

    def set_state(self, slug, state):
        """Put a fixture into a given lifecycle state. REVIEW ships archived, which now refuses
        writes, so tests about anything *other* than the lifecycle open it first."""
        path = self.stream_file("BRIEF.md", slug)
        current = vault.read_stream(self.vault, slug).state
        path.write_text(
            path.read_text(encoding="utf-8").replace(f"state: {current}\n", f"state: {state}\n", 1),
            encoding="utf-8",
        )


class TestDecisionFormat(unittest.TestCase):
    def test_canonical_shape(self):
        entry = writer.format_decision(
            "Route unresolved writes to unassigned/", "guessing corrupts two streams", date(2026, 8, 14)
        )
        self.assertEqual(
            entry,
            "- 2026-08-14 — Route unresolved writes to unassigned/. "
            "Why: guessing corrupts two streams.",
        )

    def test_existing_terminal_punctuation_is_not_doubled(self):
        entry = writer.format_decision("Ship it.", "It works.", date(2026, 8, 14))
        self.assertEqual(entry, "- 2026-08-14 — Ship it. Why: It works.")

    def test_missing_rationale_is_rejected(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.format_decision("Close the PR as superseded", "")
        self.assertIn("Why:", str(ctx.exception))

    def test_whitespace_rationale_is_rejected(self):
        with self.assertRaises(writer.WriteError):
            writer.format_decision("Close the PR", "   \n  ")

    def test_placeholder_rationales_are_rejected(self):
        """A "why" of "tbd" passes a format check and still gets the decision re-litigated."""
        for why in ("n/a", "TBD", "none", "obvious.", "see above", "-"):
            with self.assertRaises(writer.WriteError, msg=why):
                writer.format_decision("Close the PR", why)

    def test_empty_decision_is_rejected(self):
        with self.assertRaises(writer.WriteError):
            writer.format_decision("   ", "because")

    def test_inline_why_is_rejected_rather_than_doubled(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.format_decision("Close the PR. Why: superseded", "superseded")
        self.assertIn("separately", str(ctx.exception))

    def test_multiline_input_collapses_to_one_entry(self):
        entry = writer.format_decision("Close\nthe PR", "base is\nstale", date(2026, 8, 14))
        self.assertEqual(entry.count("\n"), 0)
        self.assertIn("Close the PR.", entry)


class TestAppendDecision(VaultCase):
    def test_entry_lands_in_decided(self):
        writer.append_decision(
            self.vault, DESIGN, "Adopt the writer module", "hand edits drift", on=date(2026, 8, 14),
            now=FIXED_NOW,
        )
        brief = vault.read_stream(self.vault, DESIGN)
        self.assertIn(
            "- 2026-08-14 — Adopt the writer module. Why: hand edits drift.",
            brief.sections["Decided"],
        )

    def test_existing_decided_entries_survive(self):
        self.set_state(REVIEW, "review")
        before = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        writer.append_decision(self.vault, REVIEW, "Keep it closed", "nothing changed", now=FIXED_NOW)
        after = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        self.assertIn("Close as superseded rather than review-and-merge.", after)
        self.assertEqual(len(after.splitlines()), len(before.splitlines()) + 1)

    def test_other_sections_are_untouched(self):
        before = vault.read_stream(self.vault, DESIGN).sections
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        after = vault.read_stream(self.vault, DESIGN).sections
        for name in ("Goal", "Open", "Next", "Do not"):
            self.assertEqual(before[name], after[name], name)

    def test_updated_timestamp_is_bumped(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        self.assertEqual(vault.read_stream(self.vault, DESIGN).updated, "2026-08-14T09:30:00Z")

    def test_frontmatter_is_otherwise_preserved(self):
        self.set_state(REVIEW, "review")
        writer.append_decision(self.vault, REVIEW, "Keep it closed", "nothing changed", now=FIXED_NOW)
        brief = vault.read_stream(self.vault, REVIEW)
        self.assertEqual(brief.slug, REVIEW)
        self.assertEqual(brief.state, "review")
        self.assertEqual(brief.owner, "opus-review")
        self.assertEqual(brief.claim("pr"), ["905"])
        self.assertEqual(brief.claim("repo"), ["acme/platform"])

    def test_rationale_is_validated_before_the_file_is_opened(self):
        before = self.brief_text()
        with self.assertRaises(writer.WriteError):
            writer.append_decision(self.vault, DESIGN, "Adopt the writer", "")
        self.assertEqual(before, self.brief_text())

    def test_bullet_is_blank_separated_from_preceding_prose(self):
        """Obsidian folds a bullet placed straight under a paragraph into that paragraph."""
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", on=date(2026, 8, 14),
                               now=FIXED_NOW)
        decided = vault.read_stream(self.vault, DESIGN).sections["Decided"]
        entry = "- 2026-08-14 — Adopt the writer. Why: drift."
        self.assertIn(f"\n\n{entry}", decided)

    def test_consecutive_bullets_are_not_blank_separated(self):
        self.set_state(REVIEW, "review")
        writer.append_decision(self.vault, REVIEW, "First", "reason one", now=FIXED_NOW)
        writer.append_decision(self.vault, REVIEW, "Second", "reason two", now=FIXED_NOW)
        decided = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        self.assertNotIn("\n\n", decided)

    def test_round_trips_readable_by_the_parser(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        brief = vault.read_stream(self.vault, DESIGN)
        for name in vault.SECTIONS:
            self.assertIn(name, brief.sections)


class TestSectionOrder(VaultCase):
    def test_canonical_order_preserved_on_write(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        text = self.brief_text()
        positions = [text.index(f"## {name}") for name in vault.SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_scrambled_input_is_restored_to_canonical_order(self):
        path = self.stream_file("BRIEF.md")
        front, body = writer._split_frontmatter(path.read_text(encoding="utf-8"))
        preamble, sections = writer.split_body(body)
        path.write_text("---" + front + "---\n\n" + writer.render_body(preamble, sections[::-1]),
                        encoding="utf-8")

        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        text = self.brief_text()
        self.assertEqual(
            [text.index(f"## {n}") for n in vault.SECTIONS],
            sorted(text.index(f"## {n}") for n in vault.SECTIONS),
        )

    def test_unknown_sections_are_carried_not_dropped(self):
        ordered = writer.canonical_order([("Notes", "keep me"), ("Goal", "g")])
        self.assertEqual([name for name, _ in ordered], vault.SECTIONS + ["Notes"])
        self.assertEqual(dict(ordered)["Notes"], "keep me")

    def test_section_update_replaces_content_in_place(self):
        writer.update_section(self.vault, DESIGN, "Next", "1. Ship Task 3.", now=FIXED_NOW)
        brief = vault.read_stream(self.vault, DESIGN)
        self.assertEqual(brief.sections["Next"], "1. Ship Task 3.")
        self.assertIn("Design a Claude Code plugin", brief.sections["Goal"])

    def test_append_entry_adds_a_bullet(self):
        writer.append_entry(self.vault, DESIGN, "Do not", "Add a sidecar database", now=FIXED_NOW)
        self.assertIn("- Add a sidecar database", vault.read_stream(self.vault, DESIGN).sections["Do not"])

    def test_unknown_section_name_is_rejected(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.update_section(self.vault, DESIGN, "Status", "whatever")
        self.assertIn("Goal", str(ctx.exception))

    def test_decided_cannot_be_replaced_wholesale(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.update_section(self.vault, DESIGN, "Decided", "- nothing")
        self.assertIn("append-only", str(ctx.exception))

    def test_decided_rejects_bare_bullet_appends(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.append_entry(self.vault, DESIGN, "Decided", "we chose X")
        self.assertIn("Why:", str(ctx.exception))


class TestGroundTruthIsHookOwned(VaultCase):
    def test_ground_truth_file_is_byte_identical_after_a_write(self):
        path = self.stream_file("ground-truth.md")
        before = path.read_bytes()
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        writer.update_section(self.vault, DESIGN, "Open", "- nothing `who: agent`", now=FIXED_NOW)
        self.assertEqual(before, path.read_bytes())

    def test_section_update_is_refused(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.update_section(self.vault, DESIGN, "Ground truth", "- **Branch:** main")
        self.assertIn("hook-written", str(ctx.exception))

    def test_entry_append_is_refused(self):
        with self.assertRaises(writer.WriteError):
            writer.append_entry(self.vault, DESIGN, "Ground truth", "tests passed")

    def test_transclusion_pointer_survives_verbatim(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        self.assertEqual(
            vault.read_stream(self.vault, DESIGN).sections["Ground truth"], "![[ground-truth]]"
        )

    def test_missing_section_is_restored_as_a_pointer_only(self):
        ordered = dict(writer.canonical_order([("Goal", "g")]))
        self.assertEqual(ordered["Ground truth"], "![[ground-truth]]")

    def test_hook_owned_file_is_not_addressable(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer._stream_file(self.vault, DESIGN, "ground-truth.md")
        self.assertIn("hooks", str(ctx.exception))

    def test_inbox_is_not_addressable(self):
        with self.assertRaises(writer.WriteError):
            writer._stream_file(self.vault, DESIGN, "inbox")


class TestUnassignedRouting(VaultCase):
    def record(self, slug, **kwargs):
        return writer.record_decision(
            self.vault, slug, "Cut the personas layer", "artifacts become chat logs",
            on=date(2026, 8, 14), now=FIXED_NOW, **kwargs
        )

    def test_no_slug_routes_to_unassigned(self):
        outcome = self.record(None)
        self.assertTrue(outcome.unassigned)
        self.assertIsNone(outcome.stream)
        self.assertEqual(outcome.path.parent, self.vault / "unassigned")
        self.assertIn("no explicit stream", outcome.reason)

    def test_blank_slug_is_treated_as_no_slug(self):
        for slug in ("", "   "):
            self.assertTrue(self.record(slug).unassigned)

    def test_unknown_slug_routes_and_names_available_streams(self):
        outcome = self.record("typo-stream")
        self.assertTrue(outcome.unassigned)
        self.assertIn("typo-stream", outcome.reason)
        self.assertIn(DESIGN, outcome.reason)

    def test_no_brief_is_modified_when_routing(self):
        before = {slug: self.brief_text(slug) for slug in (DESIGN, REVIEW)}
        self.record(None)
        for slug, text in before.items():
            self.assertEqual(text, self.brief_text(slug))

    def test_traversal_slug_is_refused_not_followed(self):
        outcome = self.record("../../etc")
        self.assertTrue(outcome.unassigned)
        self.assertIn("not a valid stream slug", outcome.reason)
        self.assertEqual(outcome.path.parent, self.vault / "unassigned")

    def test_trace_carries_the_decision_and_enough_to_promote_it(self):
        outcome = self.record(None, session="opus-7", cwd="/tmp/work", branch="feat/PLAT-1719/gclb")
        text = outcome.path.read_text(encoding="utf-8")
        self.assertIn("- 2026-08-14 — Cut the personas layer. Why: artifacts become chat logs.", text)
        self.assertIn("session: opus-7", text)
        self.assertIn("cwd: /tmp/work", text)
        self.assertIn("branch: feat/PLAT-1719/gclb", text)

    def test_unknown_slug_is_recorded_as_a_promotion_hint(self):
        outcome = self.record("typo-stream")
        self.assertIn("proposed_stream: typo-stream", outcome.path.read_text(encoding="utf-8"))

    def test_absence_is_recorded_explicitly_not_omitted(self):
        text = self.record(None).path.read_text(encoding="utf-8")
        self.assertIn("branch: n/a", text)
        self.assertIn("cwd: n/a", text)

    def test_filename_is_timestamp_and_session(self):
        outcome = self.record(None, session="opus-7")
        self.assertEqual(outcome.path.name, "20260814T093000Z-opus-7.md")

    def test_same_second_writes_never_collide(self):
        first = writer.record_decision(
            self.vault, None, "Cut the personas layer", "artifacts become chat logs",
            on=date(2026, 8, 14), now=FIXED_NOW, session="opus-7",
        ).path
        second = writer.record_decision(
            self.vault, None, "Cut the moderator", "a status note cannot go astray",
            on=date(2026, 8, 14), now=FIXED_NOW, session="opus-7",
        ).path
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_file() and second.is_file())

    def test_the_same_decision_routed_twice_is_filed_once(self):
        """Routing is not exempt from dedupe: a re-run should not double the triage queue."""
        first = self.record(None, session="opus-7").path
        second = self.record(None, session="opus-7").path
        self.assertEqual(first, second)
        self.assertEqual(len(list((self.vault / "unassigned").glob("*.md"))), 1)

    def test_rationale_is_rejected_before_routing(self):
        """A decision with no reason is malformed wherever it lands; the queue is not a dumping ground."""
        with self.assertRaises(writer.WriteError):
            writer.record_decision(self.vault, None, "Cut the personas layer", "")
        self.assertFalse((self.vault / "unassigned").exists())

    def test_valid_slug_writes_the_brief_and_no_trace(self):
        outcome = self.record(DESIGN)
        self.assertFalse(outcome.unassigned)
        self.assertEqual(outcome.stream, DESIGN)
        self.assertFalse((self.vault / "unassigned").exists())
        self.assertIn("Cut the personas layer", vault.read_stream(self.vault, DESIGN).sections["Decided"])


class TestArchivedStreamsRefuseWrites(VaultCase):
    """REVIEW ships `state: archived`.

    Reopening is its own act. A write that lands without flipping state leaves the dashboard
    reporting a closed stream whose content is actively changing — the operator surface lying is the
    one failure this project cannot absorb.
    """

    def test_decision_is_refused_and_names_the_reopen_path(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags")
        self.assertIn("archived", str(ctx.exception))
        self.assertIn("reopen=True", str(ctx.exception))

    def test_section_update_is_refused(self):
        with self.assertRaises(writer.WriteError):
            writer.update_section(self.vault, REVIEW, "Next", "1. Something")

    def test_entry_append_is_refused(self):
        with self.assertRaises(writer.WriteError):
            writer.append_entry(self.vault, REVIEW, "Do not", "Something")

    def test_archive_is_refused(self):
        with self.assertRaises(writer.WriteError):
            writer.archive_decision(self.vault, REVIEW, "File no review findings", "[[spec]]")

    def test_refused_archive_leaves_no_half_move(self):
        """The archive file is appended before the brief is rewritten, so the refusal must come first."""
        before = self.stream_file("decided-archive.md", REVIEW)
        self.assertFalse(before.exists())
        with self.assertRaises(writer.WriteError):
            writer.archive_decision(self.vault, REVIEW, "File no review findings", "[[spec]]")
        self.assertFalse(before.exists())

    def test_record_decision_raises_rather_than_routing_to_unassigned(self):
        """Routing answers uncertainty about *which* stream; here the objection is the lifecycle."""
        with self.assertRaises(writer.WriteError):
            writer.record_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags")
        self.assertFalse((self.vault / "unassigned").exists())

    def test_refused_write_leaves_the_brief_byte_identical(self):
        before = self.brief_text(REVIEW)
        with self.assertRaises(writer.WriteError):
            writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags")
        self.assertEqual(before, self.brief_text(REVIEW))

    def test_non_archived_states_write_without_reopen(self):
        for state in ("active", "blocked", "review", "idle"):
            self.set_state(REVIEW, state)
            writer.append_decision(self.vault, REVIEW, f"Note under {state}", "it matters",
                                   now=FIXED_NOW)
            self.assertEqual(vault.read_stream(self.vault, REVIEW).state, state)


class TestReopen(VaultCase):
    def test_reopen_flips_state_to_active(self):
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        self.assertEqual(vault.read_stream(self.vault, REVIEW).state, "active")

    def test_reopened_stream_is_resolvable_again(self):
        """Coherence: what the dashboard lists as open must be what the writer may write."""
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        brief = vault.read_stream(self.vault, REVIEW)
        self.assertFalse(brief.archived)
        self.assertTrue(brief.resolvable)

    def test_reopen_is_recorded_with_its_own_rationale(self):
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        decided = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        self.assertIn("Reopened this stream; it was archived.", decided)
        self.assertIn("Why:", decided.split("Reopened this stream")[1])

    def test_reopen_precedes_the_content_that_triggered_it(self):
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        decided = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        self.assertLess(decided.index("Reopened this stream"), decided.index("Reopen for one gap"))

    def test_reopen_preserves_prior_decisions_and_section_order(self):
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        text = self.brief_text(REVIEW)
        self.assertIn("Close as superseded rather than review-and-merge.", text)
        self.assertEqual(
            [text.index(f"## {n}") for n in vault.SECTIONS],
            sorted(text.index(f"## {n}") for n in vault.SECTIONS),
        )

    def test_reopen_does_not_touch_ground_truth(self):
        path = self.stream_file("ground-truth.md", REVIEW)
        before = path.read_bytes()
        writer.append_decision(self.vault, REVIEW, "Reopen for one gap", "Datastore lacks flags",
                               now=FIXED_NOW, reopen=True)
        self.assertEqual(before, path.read_bytes())

    def test_reopen_is_recorded_once_across_later_writes(self):
        writer.append_decision(self.vault, REVIEW, "First", "reason one", now=FIXED_NOW, reopen=True)
        writer.append_decision(self.vault, REVIEW, "Second", "reason two", now=FIXED_NOW)
        decided = vault.read_stream(self.vault, REVIEW).sections["Decided"]
        self.assertEqual(decided.count("Reopened this stream"), 1)

    def test_reopen_on_an_active_stream_is_a_no_op(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW,
                               reopen=True)
        brief = vault.read_stream(self.vault, DESIGN)
        self.assertEqual(brief.state, "active")
        self.assertNotIn("Reopened this stream", brief.sections["Decided"])

    def test_reopen_permits_section_updates_and_archiving(self):
        writer.update_section(self.vault, REVIEW, "Next", "1. Check the flags gap.", now=FIXED_NOW,
                              reopen=True)
        self.assertEqual(vault.read_stream(self.vault, REVIEW).state, "active")
        writer.archive_decision(self.vault, REVIEW, "File no review findings", "[[spec]]",
                                now=FIXED_NOW)
        self.assertNotIn(
            "File no review findings", vault.read_stream(self.vault, REVIEW).sections["Decided"]
        )


class TestArchive(VaultCase):
    def setUp(self):
        super().setUp()
        # Archiving a decision is a write; the closed fixture has to be reopened first.
        self.set_state(REVIEW, "active")

    def test_entry_moves_with_a_pointer(self):
        archive = writer.archive_decision(
            self.vault, REVIEW, "File no review findings", "[[agent-handoff-board]] Findings",
            now=FIXED_NOW,
        )
        text = archive.read_text(encoding="utf-8")
        self.assertIn("File no review findings", text)
        self.assertIn("→ [[agent-handoff-board]] Findings", text)
        self.assertNotIn(
            "File no review findings", vault.read_stream(self.vault, REVIEW).sections["Decided"]
        )

    def test_existing_archive_entries_survive(self):
        before = self.stream_file("decided-archive.md").read_text(encoding="utf-8")
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "hand edits drift", now=FIXED_NOW)
        writer.archive_decision(self.vault, DESIGN, "Adopt the writer", "[[CLAUDE]]", now=FIXED_NOW)
        after = self.stream_file("decided-archive.md").read_text(encoding="utf-8")
        self.assertIn("Handoff document first, event ledger second", after)
        self.assertGreater(len(after), len(before))

    def test_prose_placeholder_in_decided_is_not_mistaken_for_an_entry(self):
        """The design fixture's ## Decided holds italic prose, not bullets. Only bullets archive."""
        with self.assertRaises(writer.WriteError):
            writer.archive_decision(self.vault, DESIGN, "Empty", "[[CLAUDE]]", now=FIXED_NOW)

    def test_archive_is_created_when_absent(self):
        self.stream_file("decided-archive.md", REVIEW).unlink(missing_ok=True)
        archive = writer.archive_decision(self.vault, REVIEW, "File no review findings", "[[spec]]",
                                          now=FIXED_NOW)
        self.assertTrue(archive.is_file())
        self.assertIn(f"stream: {REVIEW}", archive.read_text(encoding="utf-8"))

    def test_pointer_is_mandatory(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.archive_decision(self.vault, REVIEW, "File no review findings", "  ")
        self.assertIn("pointer", str(ctx.exception))

    def test_ambiguous_match_refuses(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.archive_decision(self.vault, REVIEW, "2026-08-13", "[[spec]]")
        self.assertIn("Narrow the match", str(ctx.exception))

    def test_missing_match_refuses(self):
        with self.assertRaises(writer.WriteError):
            writer.archive_decision(self.vault, REVIEW, "no such decision", "[[spec]]")


class TestBriefIntegrity(VaultCase):
    def test_missing_stream_reports_the_path(self):
        with self.assertRaises(writer.WriteError) as ctx:
            writer.append_decision(self.vault, "nope", "d", "w")
        self.assertIn("BRIEF.md", str(ctx.exception))

    def test_brief_without_frontmatter_is_refused_not_rebuilt(self):
        self.stream_file("BRIEF.md").write_text("## Goal\n\nno frontmatter\n", encoding="utf-8")
        with self.assertRaises(writer.WriteError) as ctx:
            writer.append_decision(self.vault, DESIGN, "d", "because it matters")
        self.assertIn("frontmatter", str(ctx.exception))

    def test_repeated_writes_do_not_accumulate_blank_lines(self):
        for i in range(3):
            writer.append_decision(self.vault, DESIGN, f"Decision {i}", "it matters", now=FIXED_NOW)
        self.assertNotIn("\n\n\n", self.brief_text())

    def test_no_temp_files_left_behind(self):
        writer.append_decision(self.vault, DESIGN, "Adopt the writer", "drift", now=FIXED_NOW)
        leftovers = [p.name for p in self.stream_file("BRIEF.md").parent.iterdir()
                     if p.name.startswith(".brief-write-")]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()
