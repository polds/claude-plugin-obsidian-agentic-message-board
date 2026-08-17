"""Task 1 verification.

Fixtures are the real hand-verified streams in examples/, not invented data — so a parser that
passes here has parsed content a human already checked for accuracy.
"""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"


class TestResolve(unittest.TestCase):
    def test_missing_vault_env_names_the_variable(self):
        with self.assertRaises(vault.VaultError) as ctx:
            vault.resolve_vault({})
        self.assertIn(vault.VAULT_ENV, str(ctx.exception))

    def test_blank_vault_env_is_treated_as_unset(self):
        with self.assertRaises(vault.VaultError):
            vault.resolve_vault({vault.VAULT_ENV: "   "})

    def test_nonexistent_path_reports_the_path_tried(self):
        with self.assertRaises(vault.VaultError) as ctx:
            vault.resolve_vault({vault.VAULT_ENV: "/nope/not/here"})
        self.assertIn("/nope/not/here", str(ctx.exception))

    def test_never_silently_defaults(self):
        """A default would write a user's notes somewhere they did not choose."""
        for env in ({}, {vault.VAULT_ENV: ""}):
            with self.assertRaises(vault.VaultError):
                vault.resolve_vault(env)

    def test_resolves_existing_directory(self):
        self.assertEqual(
            vault.resolve_vault({vault.VAULT_ENV: str(FIXTURE_VAULT)}), FIXTURE_VAULT
        )


class TestFrontmatter(unittest.TestCase):
    def test_parses_scalars_lists_and_one_nested_level(self):
        data = vault.parse_frontmatter(
            "stream: plat-1962-review-pr905\n"
            "state: archived\n"
            "claims:\n"
            "  repo: [acme/platform]\n"
            "  branch: []\n"
            "  pr: [905]\n"
        )
        self.assertEqual(data["stream"], "plat-1962-review-pr905")
        self.assertEqual(data["claims"]["repo"], ["acme/platform"])
        self.assertEqual(data["claims"]["branch"], [])
        self.assertEqual(data["claims"]["pr"], ["905"])

    def test_absent_frontmatter_yields_empty(self):
        self.assertEqual(vault.parse_frontmatter(""), {})


class TestSections(unittest.TestCase):
    def test_splits_on_headings_and_preserves_content(self):
        sections = vault.parse_sections("## Goal\nline one\n\n## Next\n1. do it\n")
        self.assertEqual(sections["Goal"], "line one")
        self.assertEqual(sections["Next"], "1. do it")

    def test_ignores_hash_inside_body(self):
        sections = vault.parse_sections("## Goal\nsee #905 for detail\n")
        self.assertEqual(sections["Goal"], "see #905 for detail")


class TestFixtureStreams(unittest.TestCase):
    def setUp(self):
        self.vault = FIXTURE_VAULT

    def test_lists_both_fixture_streams_sorted(self):
        slugs = [b.slug for b in vault.list_streams(self.vault)]
        self.assertEqual(slugs, ["message-board-design", "plat-1962-review-pr905"])

    def test_every_brief_has_all_canonical_sections(self):
        for brief in vault.list_streams(self.vault):
            for name in vault.SECTIONS:
                self.assertIn(name, brief.sections, f"{brief.slug} missing '{name}'")

    def test_parses_claims_from_review_stream(self):
        brief = vault.read_stream(self.vault, "plat-1962-review-pr905")
        self.assertEqual(brief.claim("repo"), ["acme/platform"])
        self.assertEqual(brief.claim("pr"), ["905"])
        self.assertEqual(brief.claim("issue"), ["PLAT-1962"])

    def test_review_stream_has_no_branch_or_worktree_claim(self):
        """Measured reality: the PLAT-1962 review branch was never created."""
        brief = vault.read_stream(self.vault, "plat-1962-review-pr905")
        self.assertEqual(brief.claim("branch"), [])
        self.assertEqual(brief.claim("worktree"), [])

    def test_archived_stream_is_readable_but_not_resolvable(self):
        brief = vault.read_stream(self.vault, "plat-1962-review-pr905")
        self.assertTrue(brief.archived)
        self.assertFalse(brief.resolvable)
        self.assertIn("Do not", brief.sections)
        self.assertIn("Revive this branch", brief.sections["Do not"])

    def test_active_stream_is_resolvable(self):
        self.assertTrue(vault.read_stream(self.vault, "message-board-design").resolvable)

    def test_unknown_slug_lists_available_streams(self):
        with self.assertRaises(vault.VaultError) as ctx:
            vault.read_stream(self.vault, "does-not-exist")
        self.assertIn("message-board-design", str(ctx.exception))

    def test_operator_asks_derived_from_open_section(self):
        brief = vault.read_stream(self.vault, "message-board-design")
        asks = brief.operator_asks()
        self.assertTrue(asks)
        self.assertTrue(all("who: operator" in a for a in asks))

    def test_transclusion_resolves_ground_truth(self):
        brief = vault.read_stream(self.vault, "plat-1962-review-pr905")
        self.assertIn("![[ground-truth]]", brief.sections["Ground truth"])
        resolved = vault.resolve_transclusions(brief)
        self.assertNotIn("![[", resolved["Ground truth"])
        self.assertIn("PR 905", resolved["Ground truth"])

    def test_missing_transclusion_target_is_left_intact(self):
        brief = vault.read_stream(self.vault, "message-board-design")
        brief.sections["Goal"] = "![[no-such-file]]"
        self.assertEqual(vault.resolve_transclusions(brief)["Goal"], "![[no-such-file]]")


if __name__ == "__main__":
    unittest.main()
