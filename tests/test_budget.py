"""Task 4 verification.

The decisive test in this module is `TestHandCompaction`. The design fixture was compacted by hand
during the design session — 18 entries moved out of `## Decided` into `decided-archive.md` — and that
compaction is a recorded human judgment on real data. The test rebuilds the pre-compaction brief in a
tempdir and asks the tool to reproduce it. Everything else here checks a rule in isolation; that test
checks the tool is worth running.

`examples/` is read-only fixture input and must stay byte-identical, so every test that mutates works
on a `tempfile` copy. Nothing here touches a real vault.
"""

import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import budget, vault, writer  # noqa: E402

FIXTURE_VAULT = REPO / "examples"
DESIGN = "message-board-design"
REVIEW = "plat-1962-review-pr905"

# The docs the design brief links live in the repo, not the vault. Passing the repo as a search root
# is the caller's job by design — this module never guesses where a `[[wikilink]]` resolves.
DOC_ROOTS = [FIXTURE_VAULT, REPO]


def write_brief(path, *, slug="s", state="active", claims=None, sections=None):
    """Build a minimal brief on disk. Synthetic only where a fixture cannot express the case."""
    claims = claims or {}
    lines = ["---", f"stream: {slug}", "title: t", f"state: {state}", "owner: o", "claims:"]
    for kind in vault.CLAIM_KINDS:
        lines.append(f"  {kind}: [{', '.join(claims.get(kind, []))}]")
    lines.append("---")
    lines.append("")
    for name in vault.SECTIONS:
        lines += [f"## {name}", "", (sections or {}).get(name, ""), ""]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


class ScratchCase(unittest.TestCase):
    """Base: a disposable copy of the hand-verified fixtures."""

    def setUp(self):
        scratch = Path(tempfile.mkdtemp(prefix="message-board-budget-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        shutil.copytree(FIXTURE_VAULT / "streams", scratch / "streams")
        self.vault = scratch

    def stream_dir(self, slug=DESIGN):
        return self.vault / "streams" / slug

    def audit(self, slug=DESIGN, **kwargs):
        kwargs.setdefault("roots", [self.vault] + DOC_ROOTS)
        return budget.audit(vault.read_stream(self.vault, slug), **kwargs)

    def synthetic(self, slug="synthetic", **kwargs):
        write_brief(self.vault / "streams" / slug / "BRIEF.md", slug=slug, **kwargs)
        return self.audit(slug)


def reconstruct_pre_compaction(stream: Path):
    """Undo the hand compaction: move every archived entry back into `## Decided`.

    Returns [(brief line, {artifacts the human pointed at})]. The ` → [[pointer]]` tail is stripped
    from the line before it goes back, because the pointer was *added* by the archiving step. Leaving
    it in would hand the tool the answer and turn the acceptance test into a tautology — the tool has
    to rediscover where each decision is encoded, and only then can its pointer be compared with the
    human's.
    """
    archive = stream / "decided-archive.md"
    restored = []
    for line in archive.read_text(encoding="utf-8").splitlines():
        if not line.startswith("- "):
            continue
        entry, _, pointer = line.partition(" → ")
        restored.append((entry.strip(), set(re.findall(r"\[\[([^\]|#]+)", pointer))))

    brief_path = stream / "BRIEF.md"
    text = brief_path.read_text(encoding="utf-8")
    decided = "\n".join(entry for entry, _ in restored)
    text, count = re.subn(
        r"(## Decided\n\n).*?(\n\n## Open)", lambda m: m.group(1) + decided + m.group(2), text, flags=re.S
    )
    assert count == 1, "fixture layout changed; reconstruction no longer applies"
    brief_path.write_text(text, encoding="utf-8")
    # Removed so the tool cannot find a decision "encoded" in the very file it was archived into.
    archive.unlink()
    return restored


class TestWordCount(unittest.TestCase):
    def test_counts_words_not_markdown_punctuation(self):
        """Bullets, quote markers and table pipes are structure, not prose, and would inflate a
        list-heavy section against a budget the operator reasons about in words."""
        self.assertEqual(budget.count_words("- one two three"), 3)
        self.assertEqual(budget.count_words("- a b\n\n> c | d"), 4)
        self.assertEqual(budget.count_words("1. a b"), 3)  # a list number is a token an agent reads

    def test_empty_section_is_zero(self):
        self.assertEqual(budget.count_words("   \n\n  "), 0)


class TestSectionReporting(ScratchCase):
    """Acceptance criterion 1: word count per section."""

    def test_every_canonical_section_is_reported(self):
        report = self.audit()
        for name in vault.SECTIONS:
            self.assertIn(name, report.section_words)

    def test_counts_are_per_section_not_just_a_total(self):
        report = self.audit()
        self.assertGreater(report.section_words["Do not"], 0)
        self.assertGreater(report.section_words["Open"], 0)
        self.assertEqual(report.total_words, sum(
            words for name, words in report.section_words.items()
            if name not in budget.EXCLUDED_FROM_BUDGET
        ))

    def test_transcluded_ground_truth_is_reported_but_not_charged(self):
        """The owner cannot compact a hook-written file, so it must not count against their budget —
        but an agent still pays to read it, so it must not vanish from the report either."""
        report = self.audit()
        self.assertGreater(report.transcluded_words, 0)
        self.assertEqual(report.section_words["Ground truth"], 1)  # just `![[ground-truth]]`
        self.assertNotIn("transcluded", str(report.total_words))
        self.assertIn("transcluded", budget.render(report))

    def test_hand_compacted_fixture_is_within_budget(self):
        report = self.audit()
        self.assertFalse(report.over_budget)
        self.assertEqual(report.overage, 0)

    def test_over_budget_is_flagged_with_the_overage(self):
        report = self.synthetic(sections={"Next": "word " * 600})
        self.assertTrue(report.over_budget)
        self.assertEqual(report.overage, report.total_words - budget.WORD_BUDGET)
        self.assertTrue(any(w.startswith("OVER BUDGET") for w in report.warnings))

    def test_budget_is_overridable(self):
        self.assertTrue(self.audit(budget=50).over_budget)


class TestBothHalvesRequired(ScratchCase):
    """The compaction rule: an entry archives only once the decision *and* its rationale are encoded.

    Each case links the same artifact and varies only which halves it contains, so the encoding rule
    is the single variable.
    """

    def artifact(self, body):
        path = self.vault / "artifacts" / "adr-7.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        return path

    def stream_with(self, entry):
        return self.synthetic(
            slug="rule",
            sections={"Goal": "Records live in [[adr-7]].", "Decided": entry},
        )

    ENTRY = (
        "- 2026-08-13 — Resolve the vault path from an environment variable and fail loudly when "
        "unset. Why: a silent default writes the operator's notes somewhere they never chose."
    )

    def test_decision_and_rationale_encoded_is_a_candidate(self):
        self.artifact(
            "# ADR 7\n\n## Vault resolution\n\nThe vault path resolves from an environment variable "
            "and fails loudly when it is unset. A silent default would write the operator's notes "
            "somewhere they never chose, so no default is invented.\n"
        )
        report = self.stream_with(self.ENTRY)
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.candidates[0].pointer, '[[adr-7]] "Vault resolution"')

    def test_decision_without_its_rationale_is_not_a_candidate(self):
        """The failure this rule exists to prevent: the artifact says what, never why, so archiving
        would leave a decision nobody can defend."""
        self.artifact(
            "# ADR 7\n\n## Vault resolution\n\nThe vault path resolves from an environment variable "
            "and fails loudly when it is unset. Callers pass the resolved path down. The resolver "
            "lives in vault.py and is covered by its own tests.\n"
        )
        report = self.stream_with(self.ENTRY)
        self.assertEqual(report.candidates, [])
        self.assertIn("rationale", report.retained[0].retained)

    def test_rationale_without_the_decision_is_not_a_candidate(self):
        self.artifact(
            "# ADR 7\n\n## Defaults\n\nA silent default writes the operator's notes somewhere they "
            "never chose, which is why nothing here defaults.\n"
        )
        report = self.stream_with(self.ENTRY)
        self.assertEqual(report.candidates, [])
        self.assertIn("not found", report.retained[0].retained)

    def test_rationale_in_a_different_section_does_not_count(self):
        """A reason two sections away from the decision is not where the next agent will look."""
        self.artifact(
            "# ADR 7\n\n## Vault resolution\n\nThe vault path resolves from an environment variable "
            "and fails loudly when it is unset. Callers pass the resolved path down through every "
            "entry point in the plugin.\n\n## Defaults\n\nA silent default writes the operator's "
            "notes somewhere they never chose, so nothing defaults anywhere in this codebase.\n"
        )
        self.assertEqual(self.stream_with(self.ENTRY).candidates, [])

    def test_unlinked_artifact_is_never_searched(self):
        """Only artifacts the brief links are in play — the walk starts at the brief's own graph."""
        self.artifact(
            "# ADR 7\n\n## Vault resolution\n\nThe vault path resolves from an environment variable "
            "and fails loudly when it is unset, because a silent default would write the operator's "
            "notes somewhere they never chose.\n"
        )
        report = self.synthetic(slug="unlinked", sections={"Goal": "No links.", "Decided": self.ENTRY})
        self.assertEqual(report.candidates, [])
        self.assertIn("links no artifact", report.retained[0].retained)

    def test_missing_artifact_is_reported_not_silently_ignored(self):
        report = self.synthetic(
            slug="dangling", sections={"Goal": "See [[nowhere-at-all]].", "Decided": self.ENTRY}
        )
        self.assertEqual(report.unresolved_links, ["nowhere-at-all"])
        self.assertTrue(any(w.startswith("UNRESOLVED LINKS") for w in report.warnings))

    def test_stream_own_archive_cannot_encode_its_own_decisions(self):
        """`decided-archive.md` holds the text of archived decisions verbatim. If it counted as an
        encoding, every entry would prove itself and the rule would collapse."""
        stream = self.vault / "streams" / "selfref"
        write_brief(
            stream / "BRIEF.md",
            slug="selfref",
            sections={"Goal": "See [[decided-archive]].", "Decided": self.ENTRY},
        )
        (stream / "decided-archive.md").write_text(
            "# archive\n\n## Old\n\n" + self.ENTRY * 3 + "\n", encoding="utf-8"
        )
        self.assertEqual(self.audit("selfref").candidates, [])


class TestNeverEvictsByAgeOrCount(ScratchCase):
    """Acceptance criterion 3. Age and count are the wrong signals: they keep last week's trivia and
    drop the foundational decision from month one."""

    def test_forty_ancient_unencoded_entries_yield_no_candidates(self):
        entries = "\n".join(
            f"- 2019-01-{day:02d} — Ancient decision number {day} about subsystem {day}. "
            f"Why: reason number {day} that appears in no artifact anywhere."
            for day in range(1, 41)
        )
        report = self.synthetic(
            slug="ancient", sections={"Goal": "Everything is linked in [[CLAUDE]].", "Decided": entries}
        )
        self.assertTrue(report.over_budget)
        self.assertEqual(report.candidates, [])
        self.assertEqual(len(report.retained), 40)

    def test_report_never_mentions_age_or_count_as_grounds(self):
        """Paired with the presence assertion below so it cannot pass on an empty renderer."""
        entries = "\n".join(
            f"- 2019-01-{day:02d} — Ancient decision {day}. Why: reason {day}." for day in range(1, 41)
        )
        text = budget.render(
            self.synthetic(slug="ancient2", sections={"Decided": entries})
        )
        self.assertIn("Never evicted by age or count", text)
        self.assertNotIn("oldest", text.lower())
        self.assertNotIn("too many", text.lower())


class TestNeverArchives(ScratchCase):
    """Acceptance criterion 2: candidates only. This module proposes; `writer.archive_decision` moves."""

    def test_auditing_leaves_the_stream_byte_identical(self):
        stream = self.stream_dir()
        before = {p.name: p.read_bytes() for p in stream.iterdir() if p.is_file()}
        reconstruct_pre_compaction(stream)
        after_reconstruction = {p.name: p.read_bytes() for p in stream.iterdir() if p.is_file()}

        self.audit()
        self.audit()  # twice: a first run must not prime a second into acting

        self.assertEqual(
            {p.name: p.read_bytes() for p in stream.iterdir() if p.is_file()}, after_reconstruction
        )
        self.assertNotEqual(before, after_reconstruction)  # the reconstruction really did change it
        self.assertFalse((stream / "decided-archive.md").exists())

    def test_render_says_the_candidates_are_proposals(self):
        reconstruct_pre_compaction(self.stream_dir())
        text = budget.render(self.audit())
        self.assertIn("nothing has been archived", text)
        self.assertIn("writer.archive_decision", text)


class TestAsymmetry(ScratchCase):
    """docs/spec/brief-schema.md, "Expected asymmetry"."""

    def test_claims_classify_the_stream(self):
        self.assertEqual(budget.classify(vault.read_stream(self.vault, REVIEW)), budget.KIND_CODE)
        self.assertEqual(budget.classify(vault.read_stream(self.vault, DESIGN)), budget.KIND_DOCUMENT)

    def test_code_stream_retains_its_decisions(self):
        """The review fixture's rationale cites commits and file paths, not documents. Nothing
        encodes it, so nothing may be archived — this is the retention half of the asymmetry."""
        report = self.audit(REVIEW)
        self.assertEqual(report.kind, budget.KIND_CODE)
        self.assertEqual(len(report.entries), 3)
        self.assertEqual(report.candidates, [])

    def test_document_stream_drains(self):
        """The other half, on the same corpus: the design fixture's decisions are all encoded."""
        reconstruct_pre_compaction(self.stream_dir())
        report = self.audit()
        self.assertEqual(report.kind, budget.KIND_DOCUMENT)
        self.assertGreater(len(report.candidates), len(report.retained))

    def test_empty_decided_on_a_code_stream_warns_about_lost_rationale(self):
        report = self.synthetic(
            slug="code-empty", claims={"pr": ["905"], "repo": ["org/repo"]}, sections={"Decided": ""}
        )
        self.assertEqual(report.kind, budget.KIND_CODE)
        loss = [w for w in report.warnings if w.startswith("RATIONALE LOSS")]
        self.assertEqual(len(loss), 1)
        self.assertIn("not a tidy stream", loss[0])

    def test_empty_decided_on_a_document_stream_does_not_warn(self):
        """Presence assertion paired with the absence above: the warning must be about the *kind* of
        stream, not about emptiness, or it is noise the operator learns to ignore."""
        report = self.synthetic(slug="doc-empty", sections={"Decided": ""})
        self.assertEqual(report.kind, budget.KIND_DOCUMENT)
        self.assertEqual([w for w in report.warnings if w.startswith("RATIONALE LOSS")], [])

    def test_fully_draining_a_code_stream_warns_rather_than_congratulates(self):
        path = self.vault / "artifacts" / "adr-9.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "# ADR 9\n\n## Retry policy\n\nRequests retry three times with exponential backoff, "
            "because a fixed interval synchronises every client onto the same recovery spike and "
            "takes the service down a second time.\n",
            encoding="utf-8",
        )
        report = self.synthetic(
            slug="code-drained",
            claims={"pr": ["12"]},
            sections={
                "Goal": "Ship retries, recorded in [[adr-9]].",
                "Decided": (
                    "- 2026-08-13 — Requests retry three times with exponential backoff. Why: a "
                    "fixed interval synchronises every client onto the same recovery spike and "
                    "takes the service down a second time."
                ),
            },
        )
        self.assertEqual(len(report.candidates), 1)
        loss = [w for w in report.warnings if w.startswith("RATIONALE LOSS")]
        self.assertEqual(len(loss), 1)
        self.assertIn("rarely why", loss[0])

    def test_document_stream_with_nothing_encoded_reads_as_unwritten_output(self):
        report = self.synthetic(
            slug="doc-pending",
            sections={
                "Goal": "See [[CLAUDE]].",
                "Decided": "- 2026-08-13 — Pick lilac for the banner. Why: contrast against ochre.",
            },
        )
        self.assertTrue(any(w.startswith("NOT YET ENCODED") for w in report.warnings))
        self.assertEqual([w for w in report.warnings if w.startswith("RATIONALE LOSS")], [])


class TestRationaleClause(ScratchCase):
    def test_entries_without_a_why_clause_are_reported(self):
        report = self.synthetic(
            slug="nowhy", sections={"Decided": "- 2026-08-13 — Something was settled here somehow."}
        )
        self.assertTrue(any(w.startswith("NO RATIONALE") for w in report.warnings))

    def test_prose_in_the_decided_section_is_not_parsed_as_an_entry(self):
        report = self.audit()  # the fixture's ## Decided is italic commentary, no bullets
        self.assertEqual(report.entries, [])
        self.assertEqual([w for w in report.warnings if w.startswith("NO RATIONALE")], [])


class TestArchiveHandoff(ScratchCase):
    """The proposals have to be actionable by the module that does the moving."""

    def test_match_strings_are_unique_within_the_section(self):
        reconstruct_pre_compaction(self.stream_dir())
        report = self.audit()
        matches = [entry.match for entry in report.entries]
        self.assertEqual(len(matches), len(set(matches)))
        for entry in report.entries:
            hits = [other for other in report.entries if entry.match in other.line]
            self.assertEqual(len(hits), 1, entry.match)

    def test_candidates_round_trip_through_writer_archive_decision(self):
        """End-to-end: audit proposes, the writer archives, the brief comes back under budget."""
        reconstruct_pre_compaction(self.stream_dir())
        before = self.audit()
        self.assertTrue(before.over_budget)

        for entry in before.candidates:
            writer.archive_decision(self.vault, DESIGN, entry.match, entry.pointer)

        after = self.audit()
        self.assertEqual(len(after.entries), len(before.retained))
        self.assertFalse(after.over_budget)
        archived = (self.stream_dir() / "decided-archive.md").read_text(encoding="utf-8")
        self.assertEqual(archived.count("\n- "), len(before.candidates))


class TestHandCompaction(ScratchCase):
    """The acceptance test.

    `examples/streams/message-board-design/` was compacted by hand during the design session: 18
    `## Decided` entries were judged already-encoded and moved to `decided-archive.md`. That is a
    human judgment on real data, recorded with the pointer each decision was archived against. The
    tool is run against the reconstructed pre-compaction brief and has to arrive at the same answer.
    """

    def setUp(self):
        super().setUp()
        self.expected = reconstruct_pre_compaction(self.stream_dir())
        self.report = self.audit()
        self.by_line = {entry.line: entry for entry in self.report.entries}

    def test_reconstruction_restored_every_archived_entry(self):
        self.assertEqual(len(self.expected), 18)
        self.assertEqual(len(self.report.entries), 18)

    def test_pre_compaction_brief_is_flagged_over_budget(self):
        self.assertTrue(self.report.over_budget)
        self.assertGreater(self.report.total_words, 500)
        self.assertTrue(any(w.startswith("OVER BUDGET") for w in self.report.warnings))

    def test_reproduces_substantially_the_same_archive_candidates(self):
        found = [line for line, _ in self.expected if self.by_line[line].candidate]
        self.assertGreaterEqual(
            len(found),
            15,
            f"only {len(found)}/18 of the hand-archived entries were identified as candidates",
        )

    def test_proposed_pointers_agree_with_the_ones_the_human_recorded(self):
        """Non-circular: the pointer was stripped during reconstruction, so the tool rediscovered
        each artifact from the brief's own links and the artifacts' prose."""
        agreed = [
            line
            for line, artifacts in self.expected
            if artifacts & {e.artifact for e in self.by_line[line].encodings}
        ]
        self.assertGreaterEqual(
            len(agreed), 14, f"only {len(agreed)}/18 proposed pointers matched the human's"
        )

    def test_archiving_the_candidates_reaches_the_hand_compacted_size(self):
        """The compaction the human performed took the brief from over budget to comfortably under."""
        reclaimed = self.report.total_words - self.report.reclaimable_words
        self.assertLess(reclaimed, budget.WORD_BUDGET)


class TestArtifactResolution(ScratchCase):
    def test_no_roots_resolves_nothing(self):
        """Never hardcode a path: with nowhere to search, the honest answer is None."""
        self.assertIsNone(budget.resolve_artifact("CLAUDE", []))

    def test_resolves_nested_files_by_bare_name(self):
        found = budget.resolve_artifact("brief-schema", [REPO])
        self.assertEqual(found, REPO / "docs" / "spec" / "brief-schema.md")

    def test_hidden_directories_are_skipped(self):
        root = self.vault / "hidden-root"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "decoy.md").write_text("x", encoding="utf-8")
        self.assertIsNone(budget.resolve_artifact("decoy", [root]))

    def test_wikilink_aliases_and_headings_reduce_to_the_file(self):
        self.assertEqual(
            budget.wikilinks("see [[a|the A doc]] and [[b#Section]] and [[a]]"), ["a", "b"]
        )


class TestEntryParsing(unittest.TestCase):
    def test_splits_decision_from_rationale(self):
        decision, why = budget.split_decision("Ship it. Why: it works.")
        self.assertEqual(decision, "Ship it.")
        self.assertEqual(why, "it works.")

    def test_archive_pointer_tail_is_not_part_of_the_decision(self):
        """Otherwise an entry could match an artifact on the artifact's own name."""
        decision, why = budget.split_decision("Ship it. → [[adr-1]] \"Shipping\"")
        self.assertEqual(decision, "Ship it.")
        self.assertEqual(why, "")

    def test_coverage_of_an_empty_needle_is_unknown_not_perfect(self):
        self.assertEqual(budget.coverage(set(), {"a", "b"}), 0.0)


if __name__ == "__main__":
    unittest.main()
