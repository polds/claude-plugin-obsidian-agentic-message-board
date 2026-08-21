"""Statusline surface verification.

The statusline is the surface the operator does not choose to look at, so the properties under test
are mostly about what it must never do: vanish when the board is clear, disagree with the dashboard
about what is most urgent, or describe an ambiguous signal as a fault.

Builds its own vaults in tempfile. `examples/` is read-only fixture input.
"""

import io
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin import dashboard, statusline  # noqa: E402
from plugin.lib import vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"

# The same list the dashboard tests forbid. A status line is the surface most likely to train a
# reflex, so an ambiguous signal wearing any of these words is worse here than anywhere else.
VERDICT_WORDS = ["error", "fail", "invalid", "violation", "mismatch", "wrong", "broken", "bogus"]

BODY = (
    "## Goal\n\ng\n\n## Ground truth\n\n![[ground-truth]]\n\n"
    "## Decided\n\n## Open\n\n{open}\n\n## Next\n\n## Do not\n"
)


def write_stream(root, slug, *, updated, state="active", open_items=(), inbox=(), ground_truth=None):
    stream = Path(root) / "streams" / slug
    stream.mkdir(parents=True)
    frontmatter = (
        f"stream: {slug}\nstate: {state}\nowner: tester\nupdated: {updated}\n"
        "claims:\n  repo: []\n  branch: []\n  worktree: []\n  issue: []\n  pr: []\n"
    )
    body = BODY.format(open="\n".join(f"- {item}" for item in open_items))
    (stream / "BRIEF.md").write_text(f"---\n{frontmatter}---\n\n# {slug}\n\n{body}", encoding="utf-8")
    if ground_truth is not None:
        (stream / "ground-truth.md").write_text(
            f"---\nsource: hook\n---\n\n{ground_truth}", encoding="utf-8"
        )
    if inbox:
        (stream / "inbox").mkdir()
        for name in inbox:
            (stream / "inbox" / name).write_text("note\n", encoding="utf-8")
    return stream


class VaultCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "streams").mkdir()


class TestFixtureVaultIsReadOnly(VaultCase):
    def test_render_does_not_touch_the_vault(self):
        before = {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*") if p.is_file()}
        statusline.render(FIXTURE_VAULT, width=200)
        after = {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_the_fixture_vault_renders_something(self):
        """Paired with the absence assertion above: an inert renderer would pass it trivially."""
        self.assertIn("board", statusline.render(FIXTURE_VAULT, width=200))


class TestNeedsYouAlwaysRenders(VaultCase):
    """Property 1: every other segment is dropped when empty, so this one may never be."""

    def test_a_clear_board_says_nothing_needs_you(self):
        write_stream(self.root, "quiet", updated="2026-08-14T00:00:00Z")
        line = statusline.render(self.root, width=200)
        self.assertIn("nothing needs you", line)

    def test_a_board_with_asks_states_the_count(self):
        write_stream(
            self.root,
            "loud",
            updated="2026-08-14T00:00:00Z",
            open_items=["decide the thing `who: operator`", "also this `who: operator`"],
        )
        self.assertIn("2 need you", statusline.render(self.root, width=200))

    def test_an_empty_vault_still_renders_the_segment(self):
        line = statusline.render(self.root, width=200)
        self.assertIn("nothing needs you", line)

    def test_the_segment_survives_the_narrowest_width(self):
        write_stream(
            self.root, "loud", updated="2026-08-14T00:00:00Z",
            open_items=["decide `who: operator`"], inbox=["a.md"],
        )
        line = statusline.render(self.root, width=1)
        self.assertIn("1 need you", line)
        self.assertNotIn("inbox", line)


class TestOrderingMatchesTheDashboard(VaultCase):
    """Property 2: `top:` names the stream the dashboard would list first."""

    def setUp(self):
        super().setUp()
        write_stream(
            self.root, "older", updated="2026-08-10T00:00:00Z",
            open_items=["older ask `who: operator`"],
        )
        write_stream(
            self.root, "newer", updated="2026-08-19T00:00:00Z",
            open_items=["newer ask `who: operator`"],
        )
        write_stream(self.root, "zzz-no-asks", updated="2026-08-20T00:00:00Z")

    def test_top_is_the_most_recently_touched_stream_with_an_ask(self):
        self.assertEqual(statusline.summarize(self.root).top, "newer")

    def test_top_is_not_merely_the_most_recent_stream(self):
        self.assertNotEqual(statusline.summarize(self.root).top, "zzz-no-asks")

    def test_top_agrees_with_the_dashboards_own_ordering(self):
        views = [dashboard.build_view(b) for b in vault.list_streams(self.root)]
        active = sorted((v for v in views if not v.brief.archived), key=dashboard.sort_key)
        expected = next(v.brief.slug for v in active if v.asks)
        self.assertEqual(statusline.summarize(self.root).top, expected)

    def test_no_top_segment_when_nothing_needs_the_operator(self):
        quiet = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (quiet / "streams").mkdir()
        write_stream(quiet, "calm", updated="2026-08-14T00:00:00Z")
        self.assertIsNone(statusline.summarize(quiet).top)
        self.assertNotIn("top:", statusline.render(quiet, width=200))


class TestArchivedStreams(VaultCase):
    def setUp(self):
        super().setUp()
        write_stream(
            self.root, "closed", state="archived", updated="2026-08-20T00:00:00Z",
            open_items=["lingering ask `who: operator`"],
        )
        write_stream(
            self.root, "live", updated="2026-08-10T00:00:00Z",
            open_items=["live ask `who: operator`"],
        )

    def test_archived_asks_are_counted_so_the_number_matches_the_dashboard(self):
        self.assertEqual(len(statusline.summarize(self.root).asks), 2)

    def test_top_never_points_at_an_archived_stream(self):
        """Archived is readable, never resolved into — sending the operator there is a dead end."""
        self.assertEqual(statusline.summarize(self.root).top, "live")


class TestSecondarySegments(VaultCase):
    def test_inbox_counted_when_present_and_omitted_when_empty(self):
        write_stream(self.root, "a", updated="2026-08-14T00:00:00Z", inbox=["x.md", "y.md"])
        self.assertIn("2 inbox", statusline.render(self.root, width=200))

        empty = Path(tempfile.mkdtemp(dir=self.tmp.name))
        (empty / "streams").mkdir()
        write_stream(empty, "a", updated="2026-08-14T00:00:00Z")
        self.assertNotIn("inbox", statusline.render(empty, width=200))

    def test_untriaged_traces_counted_when_present_and_omitted_when_empty(self):
        self.assertNotIn("untriaged", statusline.render(self.root, width=200))
        traces = self.root / "unassigned"
        traces.mkdir()
        (traces / "2026-08-20T1200-abc.md").write_text("trace\n", encoding="utf-8")
        self.assertIn("1 untriaged", statusline.render(self.root, width=200))

    def test_adjudications_counted_when_present_and_omitted_when_empty(self):
        self.assertNotIn("adjudicate", statusline.render(self.root, width=200))
        stream = write_stream(
            self.root, "claimful", updated="2026-08-14T00:00:00Z",
            ground_truth="- **Branch:** does not exist\n",
        )
        text = (stream / "BRIEF.md").read_text(encoding="utf-8")
        (stream / "BRIEF.md").write_text(
            text.replace("  branch: []", "  branch: [gone-branch]"), encoding="utf-8"
        )
        self.assertIn("1 to adjudicate", statusline.render(self.root, width=200))


class TestAmbiguousSignalsAreNotVerdicts(VaultCase):
    def setUp(self):
        super().setUp()
        stream = write_stream(
            self.root, "claimful", updated="2026-08-14T00:00:00Z",
            open_items=["decide `who: operator`"],
            ground_truth="- **Branch:** does not exist\n",
        )
        text = (stream / "BRIEF.md").read_text(encoding="utf-8")
        (stream / "BRIEF.md").write_text(
            text.replace("  branch: []", "  branch: [gone-branch]"), encoding="utf-8"
        )
        self.line = statusline.render(self.root, width=200)

    def test_the_signal_is_present_at_all(self):
        self.assertIn("to adjudicate", self.line)

    def test_no_verdict_vocabulary(self):
        lowered = self.line.lower()
        for word in VERDICT_WORDS:
            self.assertNotIn(word, lowered, f"{word!r} in {self.line!r}")

    def test_no_ansi_escapes_or_box_drawing(self):
        self.assertNotIn("\x1b", self.line)
        self.assertTrue(self.line.isprintable(), repr(self.line))


class TestWidth(VaultCase):
    def setUp(self):
        super().setUp()
        write_stream(
            self.root, "a-stream-with-a-fairly-long-slug", updated="2026-08-14T00:00:00Z",
            open_items=["decide `who: operator`"], inbox=["x.md"],
        )
        traces = self.root / "unassigned"
        traces.mkdir()
        (traces / "t.md").write_text("t\n", encoding="utf-8")

    def test_never_exceeds_the_requested_width_beyond_the_unconditional_segment(self):
        floor = len(statusline.format_line(statusline.summarize(self.root), width=1))
        for width in range(1, 120):
            self.assertLessEqual(len(statusline.render(self.root, width=width)), max(width, floor))

    def test_the_floor_is_only_the_needs_you_segment(self):
        """Paired with the bound above: the floor must not be the whole line."""
        summary = statusline.summarize(self.root)
        floor = statusline.format_line(summary, width=1)
        self.assertEqual(floor, "board  1 need you")
        self.assertLess(len(floor), len(statusline.format_line(summary, width=200)))

    def test_segments_are_dropped_whole_rather_than_truncated(self):
        summary = statusline.summarize(self.root)
        full = statusline.format_line(summary, width=200)
        for width in range(5, len(full) + 1):
            line = statusline.format_line(summary, width=width)
            self.assertTrue(full.startswith(line), f"{line!r} is not a segment prefix of {full!r}")

    def test_a_wide_terminal_keeps_every_segment(self):
        line = statusline.render(self.root, width=200)
        for part in ("need you", "top:", "inbox", "untriaged"):
            self.assertIn(part, line)

    def test_it_is_a_single_line(self):
        self.assertNotIn("\n", statusline.render(self.root, width=200))


class TestEntryPoint(VaultCase):
    """`main` runs on every prompt: it must consume stdin, never hang, and never exit nonzero."""

    def _run(self, env, stdin='{"session_id":"s"}'):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(sys, "stdin", io.StringIO(stdin)), \
                mock.patch.object(sys, "stdout", out):
            code = statusline.main([])
        return code, out.getvalue()

    def test_renders_the_board_for_a_configured_vault(self):
        write_stream(self.root, "a", updated="2026-08-14T00:00:00Z")
        code, out = self._run({vault.VAULT_ENV: str(self.root)})
        self.assertEqual(code, 0)
        self.assertIn("board", out)

    def test_an_unset_vault_points_at_the_diagnostic_instead_of_going_silent(self):
        code, out = self._run({})
        self.assertEqual(code, 0)
        self.assertIn("doctor", out)
        self.assertNotIn("need you", out)

    def test_a_missing_vault_directory_points_at_the_diagnostic(self):
        code, out = self._run({vault.VAULT_ENV: str(self.root / "nope")})
        self.assertEqual(code, 0)
        self.assertIn("doctor", out)

    def test_empty_stdin_is_tolerated(self):
        write_stream(self.root, "a", updated="2026-08-14T00:00:00Z")
        code, out = self._run({vault.VAULT_ENV: str(self.root)}, stdin="")
        self.assertEqual(code, 0)
        self.assertIn("board", out)

    def test_arguments_are_rejected(self):
        with mock.patch.object(sys, "stderr", io.StringIO()):
            self.assertEqual(statusline.main(["--watch"]), 2)


class TestInvocation(VaultCase):
    """Claude Code runs a status line by absolute script path from the session's own directory.

    Nothing in the unit tests above exercises that: they import the module, which hides a broken
    `python3 .../plugin/statusline.py`. This runs the command a reader would paste into settings.json.
    """

    def _spawn(self, argv, cwd):
        import subprocess

        return subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=30,
            # Injected, never inherited: an ambient MESSAGE_BOARD_VAULT would point these at the
            # operator's real vault.
            env={"PATH": os.environ.get("PATH", ""), vault.VAULT_ENV: str(self.root)},
            stdin=subprocess.DEVNULL,
        )

    def test_runs_by_absolute_script_path_from_an_unrelated_directory(self):
        write_stream(self.root, "a", updated="2026-08-14T00:00:00Z")
        done = self._spawn([sys.executable, str(REPO / "plugin" / "statusline.py")], self.root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("board", done.stdout)

    def test_runs_as_a_module_from_the_repo_root(self):
        write_stream(self.root, "a", updated="2026-08-14T00:00:00Z")
        done = self._spawn([sys.executable, "-m", "plugin.statusline"], REPO)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("board", done.stdout)


class TestSpeed(VaultCase):
    def test_renders_the_fixture_vault_fast_enough_for_every_prompt(self):
        import time

        start = time.monotonic()
        statusline.render(FIXTURE_VAULT, width=200)
        self.assertLess(time.monotonic() - start, 0.5)


if __name__ == "__main__":
    unittest.main()


class TestSessionSegment(VaultCase):
    """`this:` answers "is this session tracked?" — the question the operator was guessing at.

    Each state names its exit behavior, and the states must mirror `session_end.run`: the segment
    is a promise about what the hook will write, so a divergence here is a lie on screen.
    """

    def setUp(self):
        super().setUp()
        write_stream(self.root, "tracked-stream", updated="2026-08-14T02:00:00Z")

    def test_an_explicitly_declared_stream_is_named(self):
        with mock.patch.dict(os.environ, {"MESSAGE_BOARD_STREAM": "tracked-stream"}):
            seg = statusline.session_segment(self.root, self.tmp.name)
        self.assertEqual(seg, "this: tracked-stream")

    def test_an_unresolved_session_promises_a_trace(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MESSAGE_BOARD_STREAM", None)
            os.environ.pop("MESSAGE_BOARD_IGNORE", None)
            seg = statusline.session_segment(self.root, self.tmp.name)
        self.assertEqual(seg, "this: untracked (trace on exit)")

    def test_an_ignored_directory_promises_silence(self):
        env = {"MESSAGE_BOARD_IGNORE": self.tmp.name}
        with mock.patch.dict(os.environ, env, clear=False):
            os.environ.pop("MESSAGE_BOARD_STREAM", None)
            seg = statusline.session_segment(self.root, self.tmp.name)
        self.assertEqual(seg, "this: ignored (nothing written on exit)")

    def test_the_segment_is_unconditional_at_any_width(self):
        """Both promises survive the narrowest terminal: tracked-or-not may never be dropped."""
        summary = statusline.summarize(self.root)
        line = statusline.format_line(summary, width=1, this="this: tracked-stream")
        self.assertIn("this: tracked-stream", line)
        self.assertIn("nothing needs you", line)

    def test_without_a_segment_the_line_is_unchanged(self):
        """Presence pairing for the absence above: `this` is additive, not a rewrite."""
        summary = statusline.summarize(self.root)
        self.assertEqual(
            statusline.format_line(summary, width=200),
            statusline.format_line(summary, width=200, this=None),
        )
        self.assertNotIn("this:", statusline.format_line(summary, width=200))

    def test_payload_cwd_prefers_the_explicit_field(self):
        self.assertEqual(statusline.payload_cwd({"cwd": "/a"}), "/a")
        self.assertEqual(statusline.payload_cwd({"workspace": {"current_dir": "/b"}}), "/b")
        self.assertTrue(statusline.payload_cwd({}))  # falls back to a real directory, never ""
