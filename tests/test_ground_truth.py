"""Task 8 verification.

The hook's value rests entirely on independence from agent self-report, so the tests that matter are
the ones asserting what it must NOT touch, and that unobservable facts are recorded as negatives
rather than omitted.
"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import io  # noqa: E402
import os  # noqa: E402
import time  # noqa: E402
import json  # noqa: E402

from plugin.hooks import session_end, session_start  # noqa: E402
from plugin.lib import traces, vault  # noqa: E402

FIXTURES = REPO / "examples"


def unsign(repo: str) -> None:
    """Neutralize the developer's global git config inside a scratch repo.

    Commit signing is configured globally on the machines this is developed on, and a scratch repo
    inherits it: `git commit` then fails outright, or worse, blocks on a hardware key prompt and the
    suite appears to hang. This is the same defect as a test shelling out to the operator's real
    shell — a test that reads ambient configuration is testing the machine, not the code.
    """
    for key, value in (("commit.gpgsign", "false"), ("tag.gpgsign", "false")):
        subprocess.run(["git", "-C", repo, "config", key, value], check=True)


class ScratchVault(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        shutil.copytree(FIXTURES / "streams", self.root / "streams")
        self.addCleanup(self.tmp.cleanup)

    def stream_dir(self, slug: str) -> Path:
        return self.root / "streams" / slug

    def set_claim(self, slug: str, key: str, value: str):
        path = self.stream_dir(slug) / "BRIEF.md"
        path.write_text(path.read_text().replace(f"  {key}: []", f"  {key}: [{value}]", 1))


class TestObserve(unittest.TestCase):
    def test_non_git_directory_records_absence_explicitly(self):
        """A missing line and a negative finding are different claims."""
        with tempfile.TemporaryDirectory() as tmp:
            facts = session_end.observe(tmp)
            self.assertEqual(facts["repo"], session_end.NOT_A_REPO)
            self.assertIn("branch", facts)
            self.assertIn("commit", facts)
            self.assertEqual(facts["branch"], "n/a")

    def test_real_repo_reports_branch_and_commit(self):
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["git", "init", "-q", tmp], check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", tmp, "config", "user.name", "t"], check=True)
            unsign(tmp)
            (Path(tmp) / "f.txt").write_text("x")
            subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
            subprocess.run(["git", "-C", tmp, "commit", "-qm", "initial"], check=True)

            facts = session_end.observe(tmp)
            self.assertNotEqual(facts["repo"], session_end.NOT_A_REPO)
            self.assertNotEqual(facts["commit"], "n/a")
            self.assertEqual(facts["dirty"], "no")

    def test_every_key_is_always_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = set(session_end.observe(tmp))
        self.assertEqual(keys, {"repo", "branch", "commit", "dirty", "cwd"})


class TestWrite(ScratchVault):
    def test_writes_ground_truth_for_the_named_stream(self):
        self.assertIn(
            "hello",
            session_end.write_ground_truth(
                self.root, "message-board-design", {"repo": "hello"}
            ).read_text(),
        )

    def test_never_touches_brief_or_archive(self):
        """The independence of ground truth is the entire value of this hook."""
        slug = "message-board-design"
        watched = [
            self.stream_dir(slug) / "BRIEF.md",
            self.stream_dir(slug) / "decided-archive.md",
        ]
        before = {p: p.read_bytes() for p in watched if p.is_file()}
        session_end.write_ground_truth(self.root, slug, {"repo": "x", "branch": "y"})
        after = {p: p.read_bytes() for p in watched if p.is_file()}
        self.assertEqual(before, after)

    def test_overwrites_rather_than_appends(self):
        slug = "message-board-design"
        session_end.write_ground_truth(self.root, slug, {"repo": "first"})
        text = session_end.write_ground_truth(self.root, slug, {"repo": "second"}).read_text()
        self.assertIn("second", text)
        self.assertNotIn("first", text)

    def test_unknown_stream_raises(self):
        with self.assertRaises(vault.VaultError):
            session_end.write_ground_truth(self.root, "nope", {"repo": "x"})

    def test_output_parses_as_a_frontmatter_document(self):
        slug = "message-board-design"
        path = session_end.write_ground_truth(self.root, slug, {"repo": "x"})
        front = vault.parse_frontmatter(path.read_text().split("---")[1])
        self.assertEqual(front["source"], "hook")
        self.assertTrue(front["updated"])

    def test_transclusion_still_resolves_after_a_write(self):
        """A hook write must not break the brief's ![[ground-truth]] embed."""
        slug = "plat-1962-review-pr905"
        session_end.write_ground_truth(self.root, slug, {"repo": "acme/platform"})
        brief = vault.read_stream(self.root, slug)
        resolved = vault.resolve_transclusions(brief)
        self.assertNotIn("![[", resolved["Ground truth"])
        self.assertIn("acme/platform", resolved["Ground truth"])


class TestRun(ScratchVault):
    def test_unresolvable_session_writes_a_trace_not_nothing(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(self.root, elsewhere, session="s1", env={})
        self.assertEqual(outcome, "unassigned")
        traces = list((self.root / "unassigned").glob("*.md"))
        self.assertEqual(len(traces), 1)
        self.assertIn("Repo:", traces[0].read_text())

    def test_explicit_stream_env_routes_ground_truth(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root,
                elsewhere,
                env={session_end.session_start.STREAM_ENV: "message-board-design"},
            )
        self.assertEqual(outcome, "message-board-design")
        self.assertIn(
            "source: hook",
            (self.stream_dir("message-board-design") / "ground-truth.md").read_text(),
        )

    def test_archived_stream_is_never_resolved_into(self):
        self.set_claim("message-board-design", "branch", "irrelevant")
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(self.root, elsewhere, env={})
        self.assertNotEqual(outcome, "plat-1962-review-pr905")


class TestHookPayload(unittest.TestCase):
    """Where the session reference actually comes from.

    It arrives as JSON on stdin. Reading `CLAUDE_SESSION_ID` instead fails silently: every trace is
    stamped `unknown`, which reads as a session that had no id rather than a hook that never looked —
    and the session reference is most of what makes a trace promotable.
    """

    def test_session_id_comes_from_stdin_json(self):
        payload = session_start.hook_payload(io.StringIO(json.dumps({"session_id": "abc123"})))
        self.assertEqual(session_start.payload_session(payload), "abc123")

    def test_cwd_comes_from_stdin_json(self):
        payload = session_start.hook_payload(io.StringIO(json.dumps({"cwd": "/tmp/x"})))
        self.assertEqual(payload["cwd"], "/tmp/x")

    def test_missing_id_falls_back_rather_than_raising(self):
        self.assertEqual(session_start.payload_session({}), session_start.UNKNOWN_SESSION)

    def test_malformed_input_is_survivable(self):
        """A hook that raises on junk is worse than one that records less than it could."""
        for text in ("", "not json", "[1, 2]"):
            self.assertEqual(session_start.hook_payload(io.StringIO(text)), {})

    def test_an_interactive_terminal_is_never_read(self):
        """Blocking on a tty would hang the session rather than merely under-record it."""

        class Tty(io.StringIO):
            def isatty(self):
                return True

        self.assertEqual(session_start.hook_payload(Tty("{}")), {})

    def test_an_idle_open_pipe_does_not_block(self):
        """The tty guard is not enough, and this is not hypothetical.

        Any non-tty stdin left open with nothing on it — a parent process, CI, a test runner —
        makes a bare read() wait forever. This exact case wedged a full test run at 0% CPU for
        fifteen minutes, which in a SessionEnd hook is a hung shutdown.
        """
        read_fd, write_fd = os.pipe()
        self.addCleanup(os.close, write_fd)
        with os.fdopen(read_fd, encoding="utf-8") as idle:
            started = time.monotonic()
            self.assertEqual(session_start.hook_payload(idle, timeout=0.2), {})
            self.assertLess(time.monotonic() - started, 5)

    def test_a_pipe_carrying_json_is_still_read(self):
        """Paired with the timeout: the guard must not make the hook deaf to real input."""
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "w", encoding="utf-8") as sink:
            sink.write(json.dumps({"session_id": "piped"}))
        with os.fdopen(read_fd, encoding="utf-8") as source:
            payload = session_start.hook_payload(source, timeout=5)
        self.assertEqual(session_start.payload_session(payload), "piped")


class TestObserveSession(unittest.TestCase):
    """What the session was about, read off its own transcript.

    This exists because a trace used to carry five negatives and a directory path — enough to group
    it, nothing to judge it by. An operator who cannot tell what a queue entry is will not triage it,
    which makes strict-bar minting lossy no matter how good the queue mechanics are.
    """

    def transcript(self, *records) -> str:
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return str(path)

    def said(self, text, **extra):
        return {"type": "user", "message": {"content": text}, **extra}

    def test_the_generated_title_is_the_summary(self):
        found = session_end.observe_session(
            self.transcript(self.said("hello"), {"type": "ai-title", "aiTitle": "Fix the deploy"})
        )
        self.assertEqual(session_end.summarize(found), "Fix the deploy")

    def test_a_later_title_supersedes_an_earlier_one(self):
        """The title is refined as a session goes on; the last one describes what it became."""
        found = session_end.observe_session(
            self.transcript(
                {"type": "ai-title", "aiTitle": "Look at a file"},
                {"type": "ai-title", "aiTitle": "Rewrite the scheduler"},
            )
        )
        self.assertEqual(found["title"], "Rewrite the scheduler")

    def test_the_opening_prompt_is_the_fallback(self):
        """Short sessions never get a title, and those are exactly the ones that leave traces."""
        found = session_end.observe_session(self.transcript(self.said("why is CI red?")))
        self.assertEqual(session_end.summarize(found), "why is CI red?")

    def test_slash_command_envelopes_are_not_the_prompt(self):
        """Otherwise every session that opened with a slash command reads as '/init'."""
        found = session_end.observe_session(
            self.transcript(
                self.said("<command-name>/init</command-name>"),
                self.said("<system-reminder>context</system-reminder>"),
                self.said("actually rename the module"),
            )
        )
        self.assertEqual(found["asked"], "actually rename the module")

    def test_subagent_turns_are_not_the_operator(self):
        found = session_end.observe_session(
            self.transcript(self.said("sub-agent chatter", isSidechain=True), self.said("the real ask"))
        )
        self.assertEqual(found["asked"], "the real ask")

    def test_structured_content_blocks_are_read(self):
        found = session_end.observe_session(
            self.transcript({"type": "user", "message": {"content": [{"type": "text", "text": "ping"}]}})
        )
        self.assertEqual(found["asked"], "ping")

    def test_long_prompts_are_truncated_to_one_line(self):
        found = session_end.observe_session(self.transcript(self.said("word\n" * 500)))
        self.assertLessEqual(len(found["asked"]), session_end.SUMMARY_CHARS)
        self.assertNotIn("\n", found["asked"])

    def test_absence_is_a_value_not_a_gap(self):
        """Every key returns even with nothing to read — a silently missing summary is untriageable."""
        for transcript in (None, "", "/nope/missing.jsonl"):
            found = session_end.observe_session(transcript)
            self.assertEqual(set(found), {"title", "asked", "turns"})
            self.assertEqual(session_end.summarize(found), session_end.NO_SUMMARY)

    def test_a_corrupt_transcript_does_not_fail_the_session(self):
        """A hook that raises on shutdown is worse than one that records less than it could."""
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text("not json\n{\n" + json.dumps({"type": "ai-title", "aiTitle": "ok"}))
        self.assertEqual(session_end.observe_session(str(path))["title"], "ok")


class TestTraceCarriesItsSubject(ScratchVault):
    def test_the_trace_header_carries_the_summary(self):
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text(json.dumps({"type": "ai-title", "aiTitle": "Promote image tags"}))
        with tempfile.TemporaryDirectory() as elsewhere:
            session_end.run(self.root, elsewhere, session="s1", env={}, transcript=str(path))
        trace = traces.list_traces(self.root)[0]
        self.assertEqual(trace.summary, "Promote image tags")
        self.assertIn("Promote image tags", traces.render_queue([trace]))

    def test_a_trace_with_no_transcript_says_so_rather_than_showing_a_sentinel(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            session_end.run(self.root, elsewhere, session="s1", env={})
        item = traces.work_items(traces.list_traces(self.root))[0]
        self.assertEqual(item.summaries, [])
        self.assertEqual(item.unsummarized, 1)
        listing = traces.render_queue(traces.list_traces(self.root))
        self.assertIn("no subject recorded", listing)
        self.assertIn("nothing here says what the work was", listing)


class TestIgnoredDirectories(ScratchVault):
    """Some directories host a session most days and never host work worth adopting.

    Without a way to silence them the queue refills faster than it can be worked — ten traces from a
    home directory in two days — and a backlog number that counts inevitable noise is a number the
    operator learns to ignore, which is how triage stops happening.
    """

    def env(self, *paths):
        return {session_end.IGNORE_ENV: os.pathsep.join(str(p) for p in paths)}

    def test_an_ignored_directory_writes_nothing(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(self.root, elsewhere, session="s1", env=self.env(elsewhere))
        self.assertEqual(outcome, "ignored")
        self.assertEqual(traces.list_traces(self.root), [])

    def test_a_directory_inside_an_ignored_one_is_ignored_too(self):
        with tempfile.TemporaryDirectory() as parent:
            child = Path(parent) / "deep" / "nested"
            child.mkdir(parents=True)
            outcome = session_end.run(self.root, str(child), session="s1", env=self.env(parent))
        self.assertEqual(outcome, "ignored")

    def test_an_unlisted_directory_still_traces(self):
        """Paired with the suppressions above: the rule must not silence everything."""
        with tempfile.TemporaryDirectory() as elsewhere, tempfile.TemporaryDirectory() as other:
            outcome = session_end.run(self.root, elsewhere, session="s1", env=self.env(other))
        self.assertEqual(outcome, "unassigned")
        self.assertEqual(len(traces.list_traces(self.root)), 1)

    def test_no_rule_configured_changes_nothing(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            self.assertEqual(session_end.run(self.root, elsewhere, session="s1", env={}), "unassigned")

    def test_a_claimed_directory_still_records_ground_truth(self):
        """An ignore rule silences noise; it must never override an explicit declaration."""
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root,
                elsewhere,
                session="s1",
                env={
                    **self.env(elsewhere),
                    session_end.session_start.STREAM_ENV: "message-board-design",
                },
            )
        self.assertEqual(outcome, "message-board-design")

    def test_a_rule_never_suppresses_a_git_checkout(self):
        """`~` is the obvious rule to write and the parent of every repository.

        Without this guard the one-line config that silences home-directory noise also silences the
        entire board, and it does so invisibly — the queue simply stops filling.
        """
        with tempfile.TemporaryDirectory() as parent:
            repo = Path(parent) / "code"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            outcome = session_end.run(self.root, str(repo), session="s1", env=self.env(parent))
        self.assertEqual(outcome, "unassigned")
        self.assertEqual(len(traces.list_traces(self.root)), 1)

    def test_a_relative_or_symlinked_route_to_the_same_place_matches(self):
        """A rule that silently fails to match is worse than no rule: the operator believes it works."""
        with tempfile.TemporaryDirectory() as real:
            link = Path(tempfile.mkdtemp()) / "link"
            self.addCleanup(shutil.rmtree, link.parent, ignore_errors=True)
            link.symlink_to(real)
            self.assertTrue(session_end.is_ignored(str(link), self.env(real)))


class TestTraceGrouping(ScratchVault):
    """A checkout no stream claims — the case that fills `unassigned/` in practice."""

    def setUp(self):
        super().setUp()
        self.work = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        run = lambda *a: subprocess.run(a, check=True, capture_output=True)  # noqa: E731
        run("git", "init", "-q", "-b", "feat/promote-tags", str(self.work))
        run("git", "-C", str(self.work), "config", "user.email", "t@t")
        run("git", "-C", str(self.work), "config", "user.name", "t")
        unsign(str(self.work))
        run("git", "-C", str(self.work), "remote", "add", "origin",
            "git@github.com:acme/platform.git")
        (self.work / "f.txt").write_text("x")
        run("git", "-C", str(self.work), "add", "-A")
        run("git", "-C", str(self.work), "commit", "-qm", "initial")

    def end(self, session="s1"):
        return session_end.run(self.root, str(self.work), session=session, env={})

    def test_the_trace_carries_repo_in_its_header(self):
        """Triage groups by (repo, branch); recovering repo from rendered prose would be layout-bound."""
        self.assertEqual(self.end(), "unassigned")
        queued = traces.list_traces(self.root)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].repo, "acme/platform")
        self.assertEqual(queued[0].branch, "feat/promote-tags")

    def test_a_session_ending_repeatedly_leaves_one_trace(self):
        """The observed failure: fourteen files for two work items, all byte-identical."""
        for _ in range(5):
            self.end()
        self.assertEqual(len(traces.list_traces(self.root)), 1)

    def test_a_different_session_on_the_same_commit_is_its_own_trace(self):
        """Paired with the collapse: dedupe must not merge two sessions into one record."""
        self.end("s1")
        self.end("s2")
        self.assertEqual(len(traces.list_traces(self.root)), 2)

    def test_repeats_render_as_a_single_work_item(self):
        for n in range(4):
            self.end(f"s{n}")
        text = traces.render_queue(traces.list_traces(self.root), vault.list_streams(self.root))
        self.assertIn("1 work item(s) waiting (4 traces)", text)
        self.assertIn("acme/platform @ feat/promote-tags", text)


if __name__ == "__main__":
    unittest.main()


class TestResolvedSessionLeavesARecord(ScratchVault):
    """A tracked session must leave the same one-line summary a trace would carry.

    Before this, the sessions doing the most useful work — the tracked ones — left only a git
    snapshot, and the operator could not tell "nothing happened" from "nothing was written".
    """

    def transcript(self, *records) -> str:
        path = Path(tempfile.mkdtemp()) / "t.jsonl"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        return str(path)

    def run_resolved(self, transcript=None) -> str:
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root,
                elsewhere,
                env={session_end.session_start.STREAM_ENV: "message-board-design"},
                transcript=transcript,
            )
        self.assertEqual(outcome, "message-board-design")
        return (self.stream_dir("message-board-design") / "ground-truth.md").read_text()

    def test_the_session_title_lands_in_ground_truth(self):
        text = self.run_resolved(
            self.transcript(
                {"type": "ai-title", "aiTitle": "Fixing the flaky scrape"},
                {"type": "user", "message": {"content": "please fix the scrape"}},
            )
        )
        self.assertIn("- **Session:** Fixing the flaky scrape", text)
        self.assertIn("- **Turns:** 1", text)

    def test_a_missing_transcript_records_absence_not_a_gap(self):
        """Paired presence/absence: the key still renders, carrying the explicit sentinel."""
        text = self.run_resolved(transcript=None)
        self.assertIn("- **Session:** no summary recorded", text)
        self.assertIn("- **Turns:** 0", text)


class TestDisabledSessions(ScratchVault):
    """MESSAGE_BOARD_DISABLE is the dispatcher's word that a session is automation, not work.

    One triage pipeline left ~180 traces in a week, most from inside real checkouts where an
    ignore rule deliberately cannot reach. The flag writes nothing at all — and the check runs
    before resolution, because ground truth from a fleet worker would overwrite the record of the
    operator session the board exists to describe.
    """

    def test_a_disabled_session_writes_no_trace(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root, elsewhere, session="s1", env={session_end.DISABLE_ENV: "1"}
            )
        self.assertEqual(outcome, "disabled")
        self.assertEqual(traces.list_traces(self.root), [])

    def test_disable_wins_even_when_a_stream_resolves(self):
        """The check precedes resolution: a resolvable automation session still writes nothing."""
        target = self.stream_dir("message-board-design") / session_end.GROUND_TRUTH_FILE
        before = target.read_text() if target.is_file() else None
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root,
                elsewhere,
                session="s1",
                env={
                    session_end.DISABLE_ENV: "1",
                    session_end.session_start.STREAM_ENV: "message-board-design",
                },
            )
        self.assertEqual(outcome, "disabled")
        after = target.read_text() if target.is_file() else None
        self.assertEqual(before, after)

    def test_the_same_session_without_the_flag_still_writes(self):
        """Presence pair for the absences above: the flag is the difference, not the setup."""
        with tempfile.TemporaryDirectory() as elsewhere:
            outcome = session_end.run(
                self.root,
                elsewhere,
                session="s1",
                env={session_end.session_start.STREAM_ENV: "message-board-design"},
            )
        self.assertEqual(outcome, "message-board-design")
        target = self.stream_dir("message-board-design") / session_end.GROUND_TRUTH_FILE
        self.assertTrue(target.is_file())

    def test_only_a_real_value_disarms(self):
        """`=true` from a well-meaning dispatcher must not silently fail to disable."""
        for value in ("1", "true", "TRUE", "yes", "on", "anything"):
            self.assertTrue(session_end.is_disabled({session_end.DISABLE_ENV: value}), value)
        for value in ("", "0", "false", "no", "off", " "):
            self.assertFalse(session_end.is_disabled({session_end.DISABLE_ENV: value}), repr(value))
        self.assertFalse(session_end.is_disabled({}))
