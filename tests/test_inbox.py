"""Task 10 verification.

Every test builds a throwaway vault under tempfile. The fixtures in examples/ are read-only test
corpus and the operator's real vault is off limits entirely, so nothing here resolves
MESSAGE_BOARD_VAULT or writes outside the temporary directory it created.
"""

import hashlib
import io
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import inbox, vault  # noqa: E402

SLUG = "demo-stream"

# Frozen so every concurrent appender computes the identical filename prefix. Collision is then
# guaranteed by construction rather than left to timing luck.
FROZEN = datetime(2026, 8, 13, 16, 42, tzinfo=timezone.utc)

BRIEF = """---
stream: demo-stream
title: Demo stream
state: active
owner: opus-main
updated: 2026-08-13T17:05:00Z
claims:
  repo: []
  branch: []
  worktree: []
  issue: []
  pr: []
---

## Goal

Exercise the inbox.

## Ground truth

![[ground-truth]]

## Decided

- 2026-08-13 — Keep one writer per file. Why: appends stay atomic.

## Open

## Next

## Do not
"""

GROUND_TRUTH = """---
updated: 2026-08-13T17:05:00Z
---

branch: main
tests: pass
"""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class InboxTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.vault = Path(self._tmp.name)
        self.stream = self.vault / "streams" / SLUG
        self.stream.mkdir(parents=True)
        (self.stream / "BRIEF.md").write_text(BRIEF, encoding="utf-8")
        (self.stream / "ground-truth.md").write_text(GROUND_TRUTH, encoding="utf-8")

    def append(self, **kwargs):
        kwargs.setdefault("author", "reviewer-7")
        kwargs.setdefault("body", "PR 905 has an empty reviews API response.")
        return inbox.append(self.vault, SLUG, **kwargs)


class TestAppendIsolation(InboxTestCase):
    """Acceptance 1: a new file, and nothing else on disk changes."""

    def test_creates_a_new_file_in_the_stream_inbox(self):
        path = self.append()
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent, self.stream / "inbox")
        self.assertEqual(path.suffix, ".md")

    def test_owner_written_files_are_byte_identical_afterward(self):
        before = {name: digest(self.stream / name) for name in ("BRIEF.md", "ground-truth.md")}
        self.append()
        self.append(author="sonnet-sibling", body="Second contributor.")
        for name, checksum in before.items():
            self.assertEqual(digest(self.stream / name), checksum, f"{name} was modified")

    def test_an_existing_entry_is_never_touched_by_a_later_append(self):
        first = self.append()
        before = digest(first)
        self.append(author="someone-else", body="Different contribution.")
        self.assertEqual(digest(first), before)

    def test_inbox_directory_is_created_on_demand(self):
        self.assertFalse((self.stream / "inbox").exists())
        self.append()
        self.assertTrue((self.stream / "inbox").is_dir())


class TestAttribution(InboxTestCase):
    def test_frontmatter_carries_author_stream_kind_and_timestamp(self):
        path = self.append(kind="review", now=FROZEN)
        front = vault.parse_frontmatter(path.read_text(encoding="utf-8").split("---")[1])
        self.assertEqual(front["author"], "reviewer-7")
        self.assertEqual(front["stream"], SLUG)
        self.assertEqual(front["kind"], "review")
        self.assertEqual(front["created"], "2026-08-13T16:42:00Z")

    def test_body_is_preserved_verbatim(self):
        body = "Line one.\n\n- bullet\n- bullet two\n\nSee [[brief-schema]]."
        path = self.append(body=body)
        self.assertIn(body, path.read_text(encoding="utf-8"))

    def test_naive_timestamp_is_read_as_utc_not_local(self):
        path = self.append(now=datetime(2026, 8, 13, 16, 42))
        self.assertIn("created: 2026-08-13T16:42:00Z", path.read_text(encoding="utf-8"))

    def test_unattributed_append_is_refused(self):
        with self.assertRaises(inbox.InboxError):
            self.append(author="   ")
        self.assertFalse((self.stream / "inbox").exists())

    def test_empty_body_is_refused(self):
        with self.assertRaises(inbox.InboxError):
            self.append(body="\n  \n")


class TestRefusals(InboxTestCase):
    """Acceptance 2: subagents report to their parent; only the parent writes."""

    def test_subagent_append_is_refused_and_writes_nothing(self):
        with self.assertRaises(inbox.InboxError) as ctx:
            self.append(is_subagent=True)
        self.assertIn("parent", str(ctx.exception))
        self.assertFalse((self.stream / "inbox").exists())

    def test_unknown_stream_lists_what_exists_instead_of_guessing(self):
        with self.assertRaises(vault.VaultError) as ctx:
            inbox.append(self.vault, "not-a-stream", author="a", body="b")
        self.assertIn(SLUG, str(ctx.exception))

    def test_refused_append_leaves_no_partial_file(self):
        for kwargs in ({"is_subagent": True}, {"author": ""}, {"body": ""}):
            with self.assertRaises(inbox.InboxError):
                self.append(**kwargs)
        self.assertEqual(inbox.list_entries(self.vault, SLUG), [])


class TestConcurrentAppends(InboxTestCase):
    """The contention proof: same stream, same frozen minute, no locking, nothing lost."""

    def test_threads_appending_in_the_same_second_all_survive(self):
        writers = 16
        barrier = threading.Barrier(writers)
        paths: list[Path] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def contribute(index: int) -> None:
            try:
                barrier.wait()  # release every thread into os.open at once
                path = inbox.append(
                    self.vault, SLUG, author="reviewer-7", body=f"finding {index}", now=FROZEN
                )
            except BaseException as err:  # noqa: BLE001 - surfaced by the assertion below
                with lock:
                    errors.append(err)
                return
            with lock:
                paths.append(path)

        threads = [threading.Thread(target=contribute, args=(i,)) for i in range(writers)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [])
        self.assertEqual(len(set(paths)), writers, "two appenders claimed the same filename")
        entries = inbox.list_entries(self.vault, SLUG)
        self.assertEqual(len(entries), writers)
        bodies = {entry.body for entry in entries}
        self.assertEqual(bodies, {f"finding {i}" for i in range(writers)})

    def test_separate_processes_appending_in_the_same_second_all_survive(self):
        """Threads share an interpreter; separate processes share only the filesystem.

        O_EXCL is a kernel guarantee, so this is the case that actually proves no lock is needed.
        """
        writers = 8
        code = (
            "import sys;"
            "sys.path.insert(0, sys.argv[1]);"
            "from datetime import datetime, timezone;"
            "from pathlib import Path;"
            "from plugin.lib import inbox;"
            "inbox.append(Path(sys.argv[2]), sys.argv[3], author='reviewer-7',"
            " body='process ' + sys.argv[4],"
            " now=datetime(2026, 8, 13, 16, 42, tzinfo=timezone.utc))"
        )
        processes = [
            subprocess.Popen(
                [sys.executable, "-c", code, str(REPO), str(self.vault), SLUG, str(index)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for index in range(writers)
        ]
        for process in processes:
            _, stderr = process.communicate(timeout=60)
            self.assertEqual(process.returncode, 0, stderr.decode())

        entries = inbox.list_entries(self.vault, SLUG)
        self.assertEqual(len(entries), writers)
        self.assertEqual(
            {entry.body for entry in entries}, {f"process {i}" for i in range(writers)}
        )
        self.assertEqual(len({entry.path for entry in entries}), writers)

    def test_same_minute_same_author_gets_distinct_filenames(self):
        first = self.append(now=FROZEN)
        second = self.append(now=FROZEN)
        self.assertNotEqual(first, second)
        self.assertTrue(first.name.startswith("2026-08-13T1642-reviewer-7-"))
        self.assertTrue(second.name.startswith("2026-08-13T1642-reviewer-7-"))


class TestFolding(InboxTestCase):
    """Acceptance 3: the owner sees what is unfolded, and folding keeps attribution."""

    def setUp(self):
        super().setUp()
        self.first = self.append(author="reviewer-7", body="first", now=FROZEN)
        self.second = self.append(author="sonnet-sibling", body="second", now=FROZEN)

    def test_lists_unfolded_entries_for_the_owner(self):
        self.assertEqual(
            [e.name for e in inbox.list_unfolded(self.vault, SLUG)],
            [self.first.name, self.second.name],
        )
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main")
        self.assertEqual(
            [e.name for e in inbox.list_unfolded(self.vault, SLUG)], [self.second.name]
        )

    def test_folding_does_not_delete_the_entry_or_alter_its_bytes(self):
        before = digest(self.first)
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main")
        self.assertTrue(self.first.is_file())
        self.assertEqual(digest(self.first), before)

    def test_folded_entry_keeps_its_author_and_gains_fold_attribution(self):
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main", now=FROZEN)
        folded = [e for e in inbox.list_entries(self.vault, SLUG) if e.folded]
        self.assertEqual(len(folded), 1)
        self.assertEqual(folded[0].author, "reviewer-7")
        self.assertEqual(folded[0].folded_by, "opus-main")
        self.assertEqual(folded[0].folded_at, "2026-08-13T16:42:00Z")

    def test_double_fold_is_refused_and_names_the_first_folder(self):
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main")
        with self.assertRaises(inbox.InboxError) as ctx:
            inbox.fold(self.vault, SLUG, self.first.name, folded_by="someone-else")
        self.assertIn("opus-main", str(ctx.exception))

    def test_folding_an_unknown_entry_lists_the_unfolded_ones(self):
        with self.assertRaises(inbox.InboxError) as ctx:
            inbox.fold(self.vault, SLUG, "nope.md", folded_by="opus-main")
        self.assertIn(self.first.name, str(ctx.exception))

    def test_fold_without_an_owner_is_refused(self):
        with self.assertRaises(inbox.InboxError):
            inbox.fold(self.vault, SLUG, self.first.name, folded_by=" ")

    def test_fold_markers_are_not_mistaken_for_entries(self):
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main")
        self.assertEqual(len(inbox.list_entries(self.vault, SLUG)), 2)

    def test_folding_never_touches_the_brief(self):
        before = digest(self.stream / "BRIEF.md")
        inbox.fold(self.vault, SLUG, self.first.name, folded_by="opus-main")
        self.assertEqual(digest(self.stream / "BRIEF.md"), before)


class TestListing(InboxTestCase):
    def test_stream_without_an_inbox_lists_nothing_rather_than_failing(self):
        self.assertEqual(inbox.list_entries(self.vault, SLUG), [])
        self.assertEqual(inbox.list_unfolded(self.vault, SLUG), [])

    def test_entries_are_ordered_by_creation_time(self):
        later = self.append(body="later", now=datetime(2026, 8, 14, 9, 0, tzinfo=timezone.utc))
        earlier = self.append(body="earlier", now=FROZEN)
        self.assertEqual(
            [e.name for e in inbox.list_entries(self.vault, SLUG)], [earlier.name, later.name]
        )

    def test_summary_reports_the_first_content_line(self):
        self.append(body="\n\nheadline finding\ndetail\n")
        self.assertEqual(inbox.list_unfolded(self.vault, SLUG)[0].summary(), "headline finding")


class TestCli(InboxTestCase):
    def setUp(self):
        super().setUp()
        # Point the CLI at the throwaway vault for the duration of each test. The operator's real
        # vault may well be configured in this shell, and a test must never reach it.
        patch = mock.patch.dict(os.environ, {vault.VAULT_ENV: str(self.vault)})
        patch.start()
        self.addCleanup(patch.stop)

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = inbox.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_no_arguments_returns_usage_code(self):
        code, output = self.run_cli([])
        self.assertEqual(code, 2)
        self.assertIn("usage", output)

    def test_unknown_command_returns_usage_code(self):
        self.assertEqual(self.run_cli(["frobnicate"])[0], 2)

    def test_missing_vault_env_is_reported_not_defaulted(self):
        with mock.patch.dict(os.environ, {vault.VAULT_ENV: ""}):
            code, output = self.run_cli(["list", SLUG])
        self.assertEqual(code, 1)
        self.assertIn(vault.VAULT_ENV, output)

    def test_append_through_the_cli_writes_one_entry(self):
        code, _ = self.run_cli(["append", SLUG, "--author", "reviewer-7", "--body", "cli finding"])
        self.assertEqual(code, 0)
        self.assertEqual([e.body for e in inbox.list_unfolded(self.vault, SLUG)], ["cli finding"])

    def test_cli_refuses_a_subagent_append(self):
        code, output = self.run_cli(
            ["append", SLUG, "--author", "sub", "--body", "x", "--subagent"]
        )
        self.assertEqual(code, 1)
        self.assertIn("parent", output)
        self.assertEqual(inbox.list_entries(self.vault, SLUG), [])

    def test_cli_fold_then_list_shows_the_remaining_work(self):
        self.run_cli(["append", SLUG, "--author", "reviewer-7", "--body", "one"])
        entry = inbox.list_unfolded(self.vault, SLUG)[0]
        self.assertEqual(self.run_cli(["fold", SLUG, entry.name, "--owner", "opus-main"])[0], 0)
        self.assertEqual(inbox.list_unfolded(self.vault, SLUG), [])
        self.assertEqual(len(inbox.list_entries(self.vault, SLUG)), 1)

        code, output = self.run_cli(["list", SLUG])
        self.assertEqual(code, 0)
        self.assertIn("0 unfolded entries", output)
        self.assertIn("folded by opus-main", self.run_cli(["list", SLUG, "--all"])[1])


if __name__ == "__main__":
    unittest.main()
