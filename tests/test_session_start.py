"""Task 9 verification.

The hook's whole risk profile is asymmetric: a wrong guess is tolerable only because it is labeled
and self-evident, and silence is preferable to a confident wrong match. Tests are written around
those two properties rather than around happy-path output.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.hooks import session_start  # noqa: E402
from plugin.lib import claims, vault  # noqa: E402

FIXTURES = REPO / "examples"


class ScratchVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(FIXTURES / "streams", self.root / "streams")
        self.addCleanup(self.tmp.cleanup)

    def brief_path(self, slug: str) -> Path:
        return self.root / "streams" / slug / "BRIEF.md"

    def set_claim(self, slug: str, line: str, value: str):
        path = self.brief_path(slug)
        text = path.read_text()
        path.write_text(text.replace(f"  {line}: []", f"  {line}: [{value}]", 1))


class TestInjection(ScratchVault):
    def test_explicit_declaration_is_not_labeled_a_guess(self):
        out = session_start.build_injection(
            self.root, claims.Hints(stream="message-board-design")
        )
        self.assertIn("declared explicitly", out)
        self.assertNotIn("GUESS", out)

    def test_inferred_match_is_labeled_a_guess(self):
        self.set_claim("message-board-design", "branch", "feature/x")
        out = session_start.build_injection(self.root, claims.Hints(branch="feature/x"))
        self.assertIn("GUESS", out)
        self.assertIn("message-board-design", out)

    def test_injection_includes_the_rendered_brief(self):
        out = session_start.build_injection(
            self.root, claims.Hints(stream="message-board-design")
        )
        for section in vault.SECTIONS:
            self.assertIn(f"## {section}", out)

    def test_no_match_injects_nothing(self):
        """Silence is a real outcome. A near-miss guess would be worse than starting cold."""
        self.assertEqual(
            session_start.build_injection(self.root, claims.Hints(branch="unclaimed-branch")), ""
        )

    def test_ambiguity_names_candidates_and_withholds_the_brief(self):
        """One issue legitimately spans many active streams — PLAT-1719 spans eight branches.

        Ambiguity is the normal case in a busy repo, so it must withhold rather than pick.
        """
        second = self.root / "streams" / "second-active"
        shutil.copytree(self.root / "streams" / "message-board-design", second)
        (second / "BRIEF.md").write_text(
            (second / "BRIEF.md").read_text().replace(
                "stream: message-board-design", "stream: second-active", 1
            )
        )
        for slug in ("message-board-design", "second-active"):
            self.set_claim(slug, "issue", "PLAT-4242")

        out = session_start.build_injection(self.root, claims.Hints(issue="PLAT-4242"))
        self.assertIn("No brief injected", out)
        self.assertIn("second-active", out)
        self.assertNotIn("## Decided", out)

    def test_archived_streams_are_never_injected(self):
        """Archived streams stay readable on demand, but resolving into one revives dead work."""
        out = session_start.build_injection(self.root, claims.Hints(pr="905"))
        self.assertNotIn("plat-1962-review-pr905", out.replace("[message-board]", ""))


class TestNeverWrites(ScratchVault):
    def test_injection_leaves_the_vault_byte_identical(self):
        before = {
            p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()
        }
        for hints in (
            claims.Hints(stream="message-board-design"),
            claims.Hints(branch="nope"),
            claims.Hints(pr="905"),
        ):
            session_start.build_injection(self.root, hints)
        after = {p: p.read_bytes() for p in sorted(self.root.rglob("*")) if p.is_file()}
        self.assertEqual(before, after)


class TestHints(unittest.TestCase):
    def test_main_is_never_used_as_a_branch_claim(self):
        hints = session_start.gather_hints(str(REPO), env={})
        self.assertNotEqual(hints.branch, "main")

    def test_explicit_env_populates_the_stream_hint(self):
        hints = session_start.gather_hints(
            str(REPO), env={session_start.STREAM_ENV: "message-board-design"}
        )
        self.assertEqual(hints.stream, "message-board-design")

    def test_non_git_directory_yields_no_branch_or_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            hints = session_start.gather_hints(tmp, env={})
            self.assertIsNone(hints.branch)
            self.assertIsNone(hints.repo)
            self.assertEqual(hints.worktree, tmp)


class TestUnconfiguredVault(unittest.TestCase):
    def test_missing_vault_does_not_break_the_session(self):
        """The plugin is additive. A session that never heard of the board still has to start."""
        import os

        saved = os.environ.pop(vault.VAULT_ENV, None)
        try:
            self.assertEqual(session_start.main([str(REPO)]), 0)
        finally:
            if saved is not None:
                os.environ[vault.VAULT_ENV] = saved


if __name__ == "__main__":
    unittest.main()
