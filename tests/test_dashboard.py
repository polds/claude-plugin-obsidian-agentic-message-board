"""Task 11 verification.

The fixture vault in examples/ is read, never written — these tests build their own vaults in a
temp directory whenever they need to exercise a shape the fixtures do not contain.
"""

import re
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin import dashboard  # noqa: E402
from plugin.lib import vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"
NOW = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
WIDTH = 100

# Vocabulary a verification signal must never wear. If any of these can attach to an ambiguous
# case, the operator learns to discount all of them.
VERDICT_WORDS = ["error", "fail", "invalid", "violation", "mismatch", "wrong", "broken", "bogus"]


def write_stream(root, slug, frontmatter, body, ground_truth=None, inbox=()):
    """Build a stream directory in a scratch vault."""
    stream = Path(root) / "streams" / slug
    stream.mkdir(parents=True)
    (stream / "BRIEF.md").write_text(f"---\n{frontmatter}---\n{body}", encoding="utf-8")
    if ground_truth is not None:
        (stream / "ground-truth.md").write_text(
            f"---\nsource: hook\n---\n\n{ground_truth}", encoding="utf-8"
        )
    if inbox:
        (stream / "inbox").mkdir()
        for name in inbox:
            (stream / "inbox" / name).write_text("note\n", encoding="utf-8")
    return stream


BASIC_BODY = "## Goal\n\ng\n\n## Ground truth\n\n![[ground-truth]]\n\n## Decided\n\n## Open\n\n## Next\n\n## Do not\n"


class TestFixtureVaultIsReadOnly(unittest.TestCase):
    def test_render_does_not_touch_the_vault(self):
        before = {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*") if p.is_file()}
        dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        after = {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class TestOperatorFirst(unittest.TestCase):
    """Criterion 1: operator-tagged `## Open` entries sort first, derived from the body."""

    def setUp(self):
        self.out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)

    def test_operator_block_precedes_every_stream_block(self):
        asks_at = self.out.index("NEEDS OPERATOR")
        self.assertLess(asks_at, self.out.index("ACTIVE —"))
        self.assertLess(asks_at, self.out.index("ARCHIVED —"))

    def test_operator_ask_text_appears_before_any_stream_detail(self):
        self.assertLess(self.out.index("Dashboard surface"), self.out.index("ACTIVE —"))

    def test_asks_come_from_the_body_not_frontmatter(self):
        """No stored needs_operator exists to read; deleting the tag must remove the ask."""
        brief = vault.read_stream(FIXTURE_VAULT, "message-board-design")
        self.assertTrue(dashboard.build_view(brief).asks)
        brief.sections["Open"] = brief.sections["Open"].replace("who: operator", "who: agent")
        self.assertEqual(dashboard.build_view(brief).asks, [])

    def test_streams_with_asks_sort_above_streams_without(self):
        with tempfile.TemporaryDirectory() as tmp:
            quiet = "## Open\n\n- nothing pressing `who: agent`\n"
            loud = "## Open\n\n- decide the thing `who: operator`\n"
            write_stream(tmp, "aaa-quiet", "stream: aaa-quiet\nstate: active\n", quiet)
            write_stream(tmp, "zzz-loud", "stream: zzz-loud\nstate: active\n", loud)
            out = dashboard.render(Path(tmp), now=NOW, width=WIDTH)
            active = out.split("ACTIVE —")[1]
            self.assertLess(active.index("zzz-loud"), active.index("aaa-quiet"))

    def test_who_operator_tag_is_stripped_from_display(self):
        self.assertNotIn("who: operator", self.out)

    def test_counts_operator_and_agent_open_items_separately(self):
        view = dashboard.build_view(vault.read_stream(FIXTURE_VAULT, "message-board-design"))
        self.assertEqual(len(view.asks), 2)
        self.assertEqual(view.agent_open, 1)

    def test_no_asks_says_so_rather_than_omitting_the_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(tmp, "calm", "stream: calm\nstate: active\n", BASIC_BODY)
            out = dashboard.render(Path(tmp), now=NOW, width=WIDTH)
            self.assertIn("NEEDS OPERATOR — 0", out)
            self.assertIn("no stream is waiting on you", out)


class TestCore1962NotAnError(unittest.TestCase):
    """Criterion 2, the measured case.

    The stream concluded a review; ground truth records `Formal reviews submitted: 0`. A closing
    comment was posted instead, which the workflow permits. This is an ambiguous signal. If the
    dashboard renders it red, the operator learns to ignore red and the system loses its one signal.
    """

    def setUp(self):
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        self.block = out[out.index("plat-1962-review-pr905"):]
        self.brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")

    def test_the_disagreement_is_detected_at_all(self):
        """A test that only checks for absence of 'error' would pass on a dashboard that renders
        nothing. The signal must be present *and* framed as ambiguous."""
        view = dashboard.build_view(self.brief)
        self.assertEqual(len(view.signals), 1)
        self.assertIn("Formal reviews submitted: 0", view.signals[0].hooks_side)

    def test_renders_as_needs_adjudication(self):
        self.assertIn("needs adjudication", self.block)

    def test_never_renders_as_a_failure_or_error(self):
        lowered = self.block.lower()
        for word in VERDICT_WORDS:
            self.assertNotIn(word, lowered, f"PLAT-1962 block reads as a verdict: '{word}'")

    def test_shows_both_sides_so_the_operator_can_judge(self):
        self.assertIn("Formal reviews submitted: 0", self.block)
        self.assertIn("stream concluded a review of pr 905", self.block)

    def test_carries_the_benign_reading(self):
        self.assertIn("closing comment", self.block)
        self.assertIn("permitted", self.block)

    def test_cites_the_evidence_for_the_benign_reading(self):
        """The 1 issue comment is what makes this benign; the operator must see it, not infer it."""
        self.assertIn("1 by `polds`", self.block)

    def test_no_verdict_vocabulary_anywhere_in_the_render(self):
        lowered = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH).lower()
        for word in VERDICT_WORDS:
            self.assertNotIn(word, lowered)

    def test_an_in_flight_review_with_zero_reviews_is_not_flagged(self):
        """Zero submitted reviews on an open review stream is progress, not a disagreement."""
        self.brief.state = "review"
        self.assertEqual(dashboard.build_view(self.brief).signals, [])


class TestOtherAdjudications(unittest.TestCase):
    def render_one(self, tmp, frontmatter, body, ground_truth):
        write_stream(tmp, "probe", frontmatter, body, ground_truth)
        return dashboard.build_view(vault.read_stream(Path(tmp), "probe"))

    def test_branch_claim_hooks_could_not_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = self.render_one(
                tmp,
                "stream: probe\nstate: active\nclaims:\n  branch: [feat/x]\n",
                BASIC_BODY,
                "- **Local branch `feat/x`:** does not exist — never created\n",
            )
            self.assertEqual(len(view.signals), 1)
            self.assertIn("branch claim not witnessed", view.signals[0].headline)

    def test_worktree_claim_hooks_could_not_see(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = self.render_one(
                tmp,
                "stream: probe\nstate: active\nclaims:\n  worktree: [/tmp/wt]\n",
                BASIC_BODY,
                "- **Worktree:** none registered for this stream\n",
            )
            self.assertEqual(len(view.signals), 1)
            self.assertIn("worktree", view.signals[0].headline)

    def test_claim_the_hooks_confirm_is_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = self.render_one(
                tmp,
                "stream: probe\nstate: active\nclaims:\n  branch: [feat/x]\n",
                BASIC_BODY,
                "- **Branch:** `feat/x`, 3 commits ahead of main\n",
            )
            self.assertEqual(view.signals, [])

    def test_tests_pass_claim_against_tests_not_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            view = self.render_one(
                tmp,
                "stream: probe\nstate: active\n",
                BASIC_BODY.replace("## Decided\n", "## Decided\n\n- 2026-08-13 — ship it. Why: tests pass.\n"),
                "- **Tests:** not run — no code changed in this stream\n",
            )
            self.assertEqual(len(view.signals), 1)
            self.assertIn("test claim", view.signals[0].headline)

    def test_every_signal_carries_a_benign_reading(self):
        """Structural guard: a signal without its innocent explanation is a verdict."""
        with tempfile.TemporaryDirectory() as tmp:
            view = self.render_one(
                tmp,
                "stream: probe\nstate: active\nclaims:\n  branch: [feat/x]\n  worktree: [/tmp/wt]\n",
                BASIC_BODY,
                "- **Branch:** does not exist\n- **Worktree:** none registered\n",
            )
            self.assertEqual(len(view.signals), 2)
            for signal in view.signals:
                self.assertTrue(signal.benign.strip())
                self.assertTrue(signal.hooks_side.strip())
                self.assertTrue(signal.brief_side.strip())

    def test_missing_ground_truth_file_produces_no_signals(self):
        """An unwritten ground truth is silence, not evidence against the brief."""
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(tmp, "bare", "stream: bare\nstate: active\nclaims:\n  branch: [b]\n", BASIC_BODY)
            self.assertEqual(dashboard.build_view(vault.read_stream(Path(tmp), "bare")).signals, [])


class TestGroundTruthParsing(unittest.TestCase):
    def test_parses_hook_fact_lines(self):
        gt = dashboard.GroundTruth(
            "- **PR 905:** `closed`, not merged\n- **Formal reviews submitted:** 0\nprose line\n"
        )
        self.assertEqual(len(gt.facts), 2)
        self.assertEqual(gt.fact(r"formal review")[1], "0")
        self.assertIsNone(gt.fact(r"tests"))

    def test_fact_lookup_is_case_insensitive(self):
        gt = dashboard.GroundTruth("- **Tests:** not run\n")
        self.assertEqual(gt.fact(r"\btests?\b")[0], "Tests")


class TestInboxAndArchive(unittest.TestCase):
    """Criterion 3: unfolded inbox counts, archived listed separately."""

    def test_inbox_counted_and_each_entry_named(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(
                tmp,
                "busy",
                "stream: busy\nstate: active\n",
                BASIC_BODY,
                inbox=["2026-08-13T1642-reviewer-7.md", "2026-08-13T1701-opus-2.md", ".DS_Store"],
            )
            out = dashboard.render(Path(tmp), now=NOW, width=WIDTH)
            self.assertIn("inbox 2", out)
            self.assertIn("2026-08-13T1642-reviewer-7.md", out)
            self.assertIn("2026-08-13T1701-opus-2.md", out)
            self.assertNotIn(".DS_Store", out)

    def test_inbox_total_in_summary_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(tmp, "a", "stream: a\nstate: active\n", BASIC_BODY, inbox=["1.md"])
            write_stream(tmp, "b", "stream: b\nstate: active\n", BASIC_BODY, inbox=["1.md", "2.md"])
            self.assertIn("3 inbox", dashboard.render(Path(tmp), now=NOW, width=WIDTH))

    def test_missing_inbox_directory_counts_zero(self):
        brief = vault.read_stream(FIXTURE_VAULT, "message-board-design")
        self.assertEqual(dashboard.inbox_entries(brief), [])

    def test_archived_streams_listed_in_their_own_section(self):
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        active = out[out.index("ACTIVE —"):out.index("ARCHIVED —")]
        archived = out[out.index("ARCHIVED —"):]
        self.assertIn("message-board-design", active)
        self.assertNotIn("plat-1962-review-pr905", active)
        self.assertIn("plat-1962-review-pr905", archived)

    def test_archived_streams_are_shown_not_hidden(self):
        """A closed stream's `## Do not` is often its most valuable content."""
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        self.assertIn("ARCHIVED — 1", out)
        self.assertRegex(out, r"(?s)plat-1962-review-pr905.*?do-not 2")

    def test_unassigned_traces_surface_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(tmp, "a", "stream: a\nstate: active\n", BASIC_BODY)
            (Path(tmp) / "unassigned").mkdir()
            (Path(tmp) / "unassigned" / "2026-08-13T1200-sess.md").write_text("x", encoding="utf-8")
            self.assertIn("UNASSIGNED TRACES — 1", dashboard.render(Path(tmp), now=NOW, width=WIDTH))


class TestRenderShape(unittest.TestCase):
    def test_renders_the_whole_fixture_vault_in_under_a_second(self):
        start = time.perf_counter()
        dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_every_stream_appears_exactly_once_as_a_heading(self):
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        for slug in ("message-board-design", "plat-1962-review-pr905"):
            self.assertEqual(len(re.findall(rf"^  {slug}  \[", out, re.M)), 1)

    def test_lines_respect_the_requested_width(self):
        for width in (64, 80, 100):
            for line in dashboard.render(FIXTURE_VAULT, now=NOW, width=width).splitlines():
                if line.startswith("vault  "):
                    continue  # a path wrapped across lines is a path the operator cannot copy
                self.assertLessEqual(len(line), width + 20, f"runaway line at width {width}: {line}")

    def test_no_ansi_escapes_or_box_drawing(self):
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        self.assertNotIn("\x1b", out)
        self.assertFalse(re.search(r"[─-╿▀-▟]", out))

    def test_shows_state_owner_claims_and_recency(self):
        out = dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH)
        self.assertIn("[archived] opus-review", out)
        self.assertIn("pr=905", out)
        self.assertIn("issue=PLAT-1962", out)
        self.assertIn("ago", out)

    def test_relative_age_uses_the_coarsest_useful_unit(self):
        stamp = datetime(2026, 8, 14, 3, 0, tzinfo=timezone.utc)
        self.assertEqual(dashboard._ago(stamp, NOW), "just now")
        self.assertEqual(dashboard._ago(datetime(2026, 8, 14, 2, 0, tzinfo=timezone.utc), NOW), "1h ago")
        self.assertEqual(dashboard._ago(datetime(2026, 8, 11, 3, 0, tzinfo=timezone.utc), NOW), "3d ago")
        self.assertEqual(dashboard._ago(None, NOW), "age unknown")

    def test_unparseable_timestamp_does_not_crash_the_render(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_stream(tmp, "odd", "stream: odd\nstate: active\nupdated: yesterday\n", BASIC_BODY)
            self.assertIn("age unknown", dashboard.render(Path(tmp), now=NOW, width=WIDTH))

    def test_empty_vault_renders_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = dashboard.render(Path(tmp), now=NOW, width=WIDTH)
            self.assertIn("0 streams", out)
            self.assertIn("(none)", out)

    def test_points_at_the_full_brief_reader(self):
        """The dashboard is the index; drilling in must not require opening a session."""
        self.assertIn("plugin.lib.brief_read", dashboard.render(FIXTURE_VAULT, now=NOW, width=WIDTH))


class TestCli(unittest.TestCase):
    def test_unset_vault_exits_with_an_actionable_message(self):
        import io
        import contextlib
        import os

        saved = os.environ.pop(vault.VAULT_ENV, None)
        try:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = dashboard.main([])
            self.assertEqual(code, 1)
            self.assertIn(vault.VAULT_ENV, err.getvalue())
        finally:
            if saved is not None:
                os.environ[vault.VAULT_ENV] = saved

    def test_arguments_are_rejected(self):
        import io
        import contextlib

        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(dashboard.main(["--json"]), 2)


if __name__ == "__main__":
    unittest.main()
