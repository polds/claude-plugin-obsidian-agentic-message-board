"""Task 2 verification."""

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import brief_read, vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"


class TestRender(unittest.TestCase):
    def setUp(self):
        self.review = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        self.design = vault.read_stream(FIXTURE_VAULT, "message-board-design")

    def test_renders_all_sections_in_canonical_order(self):
        out = brief_read.render(self.review)
        positions = [out.index(f"## {name}") for name in vault.SECTIONS]
        self.assertEqual(positions, sorted(positions))

    def test_expands_ground_truth_transclusion(self):
        out = brief_read.render(self.review)
        self.assertNotIn("![[", out)
        self.assertIn("PR 905", out)
        self.assertIn("closed", out)

    def test_archived_state_is_labeled_not_hidden(self):
        out = brief_read.render(self.review)
        self.assertIn("ARCHIVED", out)
        self.assertIn("Revive this branch", out)

    def test_claims_rendered_and_empty_kinds_omitted(self):
        out = brief_read.render(self.review)
        self.assertIn("905", out)
        self.assertIn("PLAT-1962", out)
        self.assertNotIn("branch ", out.split("## Goal")[0])

    def test_claims_expand_to_reachable_urls(self):
        """A cold-start reader has no repo access; an unlinked ID is a dead end for them."""
        out = brief_read.render(self.review)
        self.assertIn("https://github.com/acme/platform/pull/905", out)

    def test_body_citations_become_links(self):
        """The exact gap a cold-start agent reported: `a1b2c3d4e` and `#1025` cited, unreachable."""
        decided = brief_read.render(self.review).split("## Decided")[1]
        self.assertIn("https://github.com/acme/platform/commit/a1b2c3d4e", decided)
        self.assertIn("https://github.com/acme/platform/pull/1025", decided)

    def test_guessed_links_stay_out_of_the_body(self):
        """A wrong link costs more than an unlinked reference — guesses belong in claims only."""
        body = brief_read.render(self.review).split("## Goal")[1]
        self.assertNotIn("linear.app", body)

    def test_operator_asks_surfaced_for_active_stream(self):
        self.assertIn("need the operator", brief_read.render(self.design))

    def test_no_operator_banner_when_none_pending(self):
        self.assertNotIn("need the operator", brief_read.render(self.review))


class TestCli(unittest.TestCase):
    def test_unknown_slug_lists_available(self):
        with self.assertRaises(vault.VaultError) as ctx:
            vault.read_stream(FIXTURE_VAULT, "nope")
        self.assertIn("plat-1962-review-pr905", str(ctx.exception))

    def test_wrong_arg_count_returns_usage_code(self):
        self.assertEqual(brief_read.main([]), 2)


if __name__ == "__main__":
    unittest.main()
