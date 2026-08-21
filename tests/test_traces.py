"""Task 7 verification.

Every test runs against a tempfile scratch vault built from `examples/`. The fixtures are read-only
input — nothing here writes into them, and nothing here touches a real vault.

Two properties carry the task and get the most attention below:

1. **The queue has one format.** Traces and `writer`-routed writes land in the same directory, so the
   header keys and filename scheme are compared against `writer`'s own output rather than against a
   copy of them written down here. A test that restates the format would pass through a divergence.
2. **Promotion is lossless.** Proven end to end — record a trace carrying every field, promote it,
   parse what landed in the target stream, and compare field by field.
"""

import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import inbox, traces, vault, writer  # noqa: E402

FIXTURE_VAULT = REPO / "examples"
DESIGN = "message-board-design"
REVIEW = "plat-1962-review-pr905"

FIXED_NOW = datetime(2026, 8, 14, 9, 30, 0, tzinfo=timezone.utc)

# One trace carrying every field, reused wherever "all of it must survive" is the point.
FULL_TRACE = {
    "summary": "Read the erpc scrape path and ran the suite; nothing decided.",
    "session": "opus-7",
    "cwd": "/Users/p/work/platform",
    "branch": "feat/PLAT-1719/gclb",
    "proposed_stream": "plat-1719-gclb",
}


def header_keys(path: Path) -> list[str]:
    """The frontmatter keys of a queue file, in file order."""
    front = path.read_text(encoding="utf-8").split("---", 2)[1]
    return [line.split(":", 1)[0] for line in front.strip().splitlines() if line.strip()]


class VaultCase(unittest.TestCase):
    """Base: a disposable copy of the hand-verified fixtures."""

    def setUp(self):
        scratch = Path(tempfile.mkdtemp(prefix="message-board-test-"))
        self.addCleanup(shutil.rmtree, scratch, ignore_errors=True)
        shutil.copytree(FIXTURE_VAULT / "streams", scratch / "streams")
        self.vault = scratch

    def record(self, **kwargs):
        fields = {**FULL_TRACE, **kwargs}
        summary = fields.pop("summary")
        fields.setdefault("now", FIXED_NOW)
        return traces.record(self.vault, summary, **fields)

    def unassigned(self):
        return self.vault / traces.UNASSIGNED_DIR

    def make_git_vault(self):
        """Pruning refuses outside git, so tests that prune have to make the vault a repo first."""
        (self.vault / ".git").mkdir()

    def set_state(self, slug, state):
        path = self.vault / "streams" / slug / "BRIEF.md"
        current = vault.read_stream(self.vault, slug).state
        path.write_text(
            path.read_text(encoding="utf-8").replace(f"state: {current}\n", f"state: {state}\n", 1),
            encoding="utf-8",
        )


class TestSharedQueueFormat(VaultCase):
    """The trace format is `writer`'s format. Divergence here silently splits the triage queue."""

    def routed(self):
        return writer.write_unassigned(
            self.vault, "- 2026-08-14 — A decision. Why: a reason.",
            reason="no explicit stream declared", session="opus-7", cwd="/tmp/w", branch="b",
            now=FIXED_NOW,
        )

    def test_header_keys_match_writer_exactly(self):
        self.assertEqual(header_keys(self.record()), header_keys(self.routed()))

    def test_header_keys_are_the_documented_contract(self):
        self.assertEqual(
            header_keys(self.record()),
            [
                "kind", "created", "session", "summary", "repo", "cwd", "branch", "reason",
                "proposed_stream",
            ],
        )

    def test_kind_is_the_shared_queue_kind(self):
        self.assertIn("kind: unresolved-write", self.record().read_text(encoding="utf-8"))

    def test_reason_distinguishes_a_trace_from_a_routed_write(self):
        """Same file shape, so `reason` is what tells triage why each file is here."""
        self.assertIn(f"reason: {traces.BELOW_BAR_REASON}", self.record().read_text(encoding="utf-8"))
        self.assertIn("reason: no explicit stream declared", self.routed().read_text(encoding="utf-8"))

    def test_created_stamp_parses_with_the_format_traces_reads(self):
        """Pruning needs an age; that only works if this module's parser matches writer's printer."""
        trace = traces.read_trace(self.record())
        self.assertEqual(trace.created_at(), FIXED_NOW)

    def test_both_writers_land_in_one_directory(self):
        self.assertEqual(self.record().parent, self.routed().parent)
        self.assertEqual(self.record().parent, self.unassigned())

    def test_listing_reads_both_writers(self):
        trace = self.record()
        routed = self.routed()
        self.assertEqual({t.path for t in traces.list_traces(self.vault)}, {trace, routed})


class TestRecord(VaultCase):
    def test_filename_is_timestamp_and_session(self):
        self.assertEqual(self.record().name, "20260814T093000Z-opus-7.md")

    def _record_saying(self, summary: str):
        return self.record(summary=summary)

    def test_concurrent_sessions_never_collide(self):
        """Same session, same second, different content — `O_CREAT|O_EXCL` walks the suffix.

        An `exists()` pre-check would reintroduce the TOCTOU race this avoids: two sessions can both
        see "absent" before either creates the file.
        """
        first, second, third = (self._record_saying(f"Did thing {n}.") for n in range(3))
        self.assertEqual(len({first, second, third}), 3)
        for path in (first, second, third):
            self.assertTrue(path.is_file())
        self.assertEqual(len(list(self.unassigned().glob("*.md"))), 3)

    def test_identical_content_is_written_once(self):
        """The real duplication: one session ends, resumes, and ends again on the same commit.

        Fourteen indistinguishable files for two work items is a queue nobody can read, which is the
        same as not having one.
        """
        first, second, third = (self.record() for _ in range(3))
        self.assertEqual(first, second)
        self.assertEqual(first, third)
        self.assertEqual(len(list(self.unassigned().glob("*.md"))), 1)

    def test_a_changed_field_is_a_new_trace(self):
        """Paired with the collapse above: dedupe must not swallow a trace triage could act on."""
        self.record()
        moved = self.record(branch="feat/PLAT-1719/other")
        self.assertEqual(len(list(self.unassigned().glob("*.md"))), 2)
        self.assertTrue(moved.is_file())

    def test_dedupe_off_restores_the_duplicates(self):
        """Mutation check: the collapse is the dedupe doing work, not the fixture writing one file."""
        for _ in range(3):
            writer.write_unassigned(
                self.vault, "same body", reason="r", session="opus-7", now=FIXED_NOW, dedupe=False
            )
        self.assertEqual(len(list(self.unassigned().glob("*.md"))), 3)

    def test_a_pruned_trace_does_not_suppress_its_recurrence(self):
        """A stale content marker is stale, not authoritative — else pruning silences a live signal."""
        first = self.record()
        first.unlink()
        again = self.record()
        self.assertTrue(again.is_file())

    def test_carries_everything_needed_to_promote_it_later(self):
        text = self.record().read_text(encoding="utf-8")
        self.assertIn("cwd: /Users/p/work/platform", text)
        self.assertIn("branch: feat/PLAT-1719/gclb", text)
        self.assertIn("session: opus-7", text)
        self.assertIn("proposed_stream: plat-1719-gclb", text)
        self.assertIn(FULL_TRACE["summary"], text)

    def test_absence_is_recorded_explicitly_not_omitted(self):
        text = traces.record(self.vault, "Ran the suite.", session="opus-7", now=FIXED_NOW).read_text(
            encoding="utf-8"
        )
        self.assertIn("cwd: n/a", text)
        self.assertIn("branch: n/a", text)
        self.assertIn("proposed_stream: n/a", text)

    def test_summary_collapses_to_one_line(self):
        path = traces.record(self.vault, "Read the\nscrape path", session="s", now=FIXED_NOW)
        trace = traces.read_trace(path)
        self.assertEqual(trace.summary, "Read the scrape path")

    def test_empty_summary_is_refused(self):
        for summary in ("", "   \n  "):
            with self.assertRaises(traces.TraceError):
                traces.record(self.vault, summary, session="s")
        self.assertFalse(self.unassigned().exists())

    def test_session_with_path_characters_cannot_escape_the_queue(self):
        path = traces.record(self.vault, "Nothing decided.", session="../../etc/passwd", now=FIXED_NOW)
        self.assertEqual(path.parent, self.unassigned())
        self.assertNotIn("/", path.name[:-3])

    def test_no_stream_is_touched(self):
        before = {p: p.read_bytes() for p in (self.vault / "streams").rglob("*.md")}
        self.record()
        self.assertEqual({p: p.read_bytes() for p in (self.vault / "streams").rglob("*.md")}, before)


class TestTriageListing(VaultCase):
    """A queue nobody can see is a queue nobody works."""

    def test_empty_queue_lists_cleanly(self):
        self.assertEqual(traces.list_traces(self.vault), [])
        self.assertEqual(traces.pending(self.vault), [])
        self.assertIn("0 traces", traces.render_queue([]))

    def test_oldest_first(self):
        old = self.record(now=FIXED_NOW - timedelta(days=9), session="old")
        mid = self.record(now=FIXED_NOW - timedelta(days=4), session="mid")
        new = self.record(now=FIXED_NOW, session="new")
        self.assertEqual([t.path for t in traces.list_traces(self.vault)], [old, mid, new])

    def test_fields_survive_the_round_trip_into_the_listing(self):
        self.record()
        trace = traces.list_traces(self.vault)[0]
        self.assertEqual(trace.session, "opus-7")
        self.assertEqual(trace.cwd, "/Users/p/work/platform")
        self.assertEqual(trace.branch, "feat/PLAT-1719/gclb")
        self.assertEqual(trace.proposed_stream, "plat-1719-gclb")
        self.assertEqual(trace.summary, FULL_TRACE["summary"])
        self.assertEqual(trace.reason, traces.BELOW_BAR_REASON)

    def test_promoted_traces_leave_the_pending_queue(self):
        name = self.record().name
        self.assertEqual(len(traces.pending(self.vault)), 1)
        traces.promote(self.vault, name, DESIGN, promoted_by="operator", now=FIXED_NOW)
        self.assertEqual(traces.pending(self.vault), [])
        self.assertEqual(len(traces.list_traces(self.vault)), 1)

    def test_render_shows_the_work_item_its_age_and_the_command_that_resolves_it(self):
        self.record(now=FIXED_NOW - timedelta(days=3), repo="acme/platform")
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW)
        self.assertIn("1 work item(s) waiting (1 traces)", text)
        self.assertIn("acme/platform @ feat/PLAT-1719/gclb", text)
        self.assertIn("3d old", text)
        self.assertIn("/Users/p/work/platform", text)
        # The point of the reframe: the listing hands over the next command instead of a filename,
        # and it addresses the item by something that survives the queue renumbering.
        self.assertIn("traces adopt 'feat/PLAT-1719/gclb'", text)
        self.assertIn("traces dismiss 'feat/PLAT-1719/gclb'", text)

    def test_render_proposes_the_slug_start_task_would_have_derived(self):
        self.record(repo="acme/platform")
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW)
        self.assertIn("plat-1719", text)

    def test_render_names_the_stream_already_claiming_the_work(self):
        """Adoption must offer to join, not to mint a second stream for work already owned."""
        self.record(branch="design-branch")
        streams = vault.list_streams(self.vault)
        design = next(b for b in streams if b.slug == DESIGN)
        design.claims["branch"] = ["design-branch"]
        text = traces.render_queue(traces.list_traces(self.vault), streams, now=FIXED_NOW)
        self.assertIn(f"claimed by: {DESIGN}", text)
        self.assertNotIn("no stream claims this", text)

    def test_a_worktree_claim_only_counts_while_it_is_still_live(self):
        """The main clone is the working directory of everything in a repo.

        A stream claiming that path matched every work item there, so the listing invited filing a
        dozen unrelated sessions into one stream. The read path already refuses such a claim unless
        the checkout is still on one of the stream's branches; the listing must refuse it too, or it
        recommends what resolution would never do.
        """
        clone = self.vault / "clone"
        clone.mkdir()
        self.record(branch="other/work", cwd=str(clone))
        streams = vault.list_streams(self.vault)
        stream = next(b for b in streams if b.slug == DESIGN)
        stream.claims["worktree"] = [str(clone)]
        stream.claims["branch"] = ["some/feature"]

        item = traces.work_items(traces.list_traces(self.vault), streams)[0]
        self.assertEqual(item.claimed_by, "")
        self.assertIn("no stream claims this", traces.render_queue(traces.list_traces(self.vault), streams))

    def test_a_worktree_claim_with_no_branch_claim_still_counts(self):
        """Paired with the refusal above: review and design streams have no branch to check."""
        clone = self.vault / "clone"
        clone.mkdir()
        self.record(branch="other/work", cwd=str(clone))
        streams = vault.list_streams(self.vault)
        stream = next(b for b in streams if b.slug == DESIGN)
        stream.claims["worktree"] = [str(clone)]
        stream.claims["branch"] = []

        item = traces.work_items(traces.list_traces(self.vault), streams)[0]
        self.assertEqual(item.claimed_by, DESIGN)

    def test_many_traces_one_branch_render_as_one_work_item(self):
        """The queue's failure mode: fourteen files, two decisions."""
        for n in range(7):
            self.record(summary=f"Session {n} ended.")
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW)
        self.assertIn("1 work item(s) waiting (7 traces)", text)
        self.assertIn("7 trace(s)", text)

    def test_undated_trace_still_lists(self):
        """A malformed entry nobody can see is an entry nobody fixes."""
        path = self.unassigned() / "hand-written.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nkind: unresolved-write\nsession: s\n---\n\nsomething.\n", encoding="utf-8")
        listed = traces.list_traces(self.vault)
        self.assertEqual([t.name for t in listed], ["hand-written.md"])
        self.assertIsNone(listed[0].created_at())
        self.assertIn("undated", traces.render_queue(listed, now=FIXED_NOW))


class TestPromotionIsLossless(VaultCase):
    """Write a trace, promote it, assert every field survives into the target stream."""

    def promote_full(self, slug=DESIGN, **kwargs):
        path = self.record(**kwargs)
        return path, traces.promote(self.vault, path.name, slug, promoted_by="operator", now=FIXED_NOW)

    def promoted_fields(self, promotion):
        entries = [e for e in inbox.list_entries(self.vault, promotion.stream)
                   if e.path == promotion.entry]
        self.assertEqual(len(entries), 1)
        return traces.parse_promotion(entries[0].body)

    def test_every_field_survives_into_the_stream(self):
        path, promotion = self.promote_full()
        before = traces.read_trace(path)
        landed = self.promoted_fields(promotion)
        for key in ("summary", "session", "cwd", "branch", "created", "reason", "proposed_stream"):
            self.assertEqual(landed[key], getattr(before, key), key)
        self.assertEqual(landed["trace"], path.name)

    def test_absent_fields_survive_as_explicit_absence(self):
        path = traces.record(self.vault, "Ran the suite.", session="opus-7", now=FIXED_NOW)
        promotion = traces.promote(self.vault, path.name, DESIGN, now=FIXED_NOW)
        landed = self.promoted_fields(promotion)
        self.assertEqual(landed["cwd"], "n/a")
        self.assertEqual(landed["branch"], "n/a")
        self.assertEqual(landed["summary"], "Ran the suite.")

    def test_promoted_entry_is_attributed_to_the_originating_session(self):
        _, promotion = self.promote_full()
        entry = [e for e in inbox.list_entries(self.vault, DESIGN) if e.path == promotion.entry][0]
        self.assertEqual(entry.author, "opus-7")
        self.assertEqual(entry.kind, traces.PROMOTION_KIND)
        self.assertEqual(entry.slug, DESIGN)

    def test_trace_file_is_byte_identical_after_promotion(self):
        """Promotion marks, never rewrites — the queue file stays as the session wrote it."""
        path = self.record()
        before = path.read_bytes()
        traces.promote(self.vault, path.name, DESIGN, now=FIXED_NOW)
        self.assertEqual(path.read_bytes(), before)

    def test_brief_and_ground_truth_are_untouched(self):
        """Promotion is a non-owner write; it goes to the inbox and nowhere else."""
        stream = self.vault / "streams" / DESIGN
        before = {p: p.read_bytes() for p in (stream / "BRIEF.md", stream / "ground-truth.md")}
        _, promotion = self.promote_full()
        self.assertEqual({p: p.read_bytes() for p in before}, before)
        self.assertEqual(promotion.entry.parent, inbox.inbox_dir(self.vault, DESIGN))

    def test_promotion_is_recorded_with_its_target(self):
        path, promotion = self.promote_full()
        trace = traces.read_trace(path, traces._markers(self.unassigned()))
        self.assertTrue(trace.promoted)
        self.assertEqual(trace.promoted_stream, DESIGN)
        self.assertEqual(trace.promoted_by, "operator")
        self.assertEqual(trace.promoted_entry, promotion.entry.name)
        self.assertEqual(trace.promoted_at, "2026-08-14T09:30:00Z")

    def test_second_promotion_is_refused(self):
        path, _ = self.promote_full()
        with self.assertRaises(traces.TraceError) as ctx:
            traces.promote(self.vault, path.name, REVIEW, now=FIXED_NOW)
        self.assertIn("already promoted", str(ctx.exception))
        self.assertEqual(inbox.list_entries(self.vault, REVIEW), [])

    def test_trailing_md_is_optional(self):
        path = self.record()
        promotion = traces.promote(self.vault, path.stem, DESIGN, now=FIXED_NOW)
        self.assertEqual(promotion.trace.name, path.name)

    def test_unknown_trace_names_what_is_waiting(self):
        self.record(session="opus-7")
        with self.assertRaises(traces.TraceError) as ctx:
            traces.promote(self.vault, "nope.md", DESIGN)
        self.assertIn("20260814T093000Z-opus-7.md", str(ctx.exception))

    def test_trace_name_cannot_escape_the_queue(self):
        with self.assertRaises(traces.TraceError):
            traces.promote(self.vault, f"../streams/{DESIGN}/BRIEF", DESIGN)

    def test_unknown_stream_lists_available_streams(self):
        path = self.record()
        with self.assertRaises(vault.VaultError) as ctx:
            traces.promote(self.vault, path.name, "typo-stream")
        self.assertIn(DESIGN, str(ctx.exception))
        self.assertEqual(traces.pending(self.vault)[0].path, path)

    def test_archived_stream_is_refused(self):
        """REVIEW ships archived: a closed stream whose content keeps changing makes the board lie."""
        path = self.record()
        with self.assertRaises(traces.TraceError) as ctx:
            traces.promote(self.vault, path.name, REVIEW, now=FIXED_NOW)
        self.assertIn("archived", str(ctx.exception))
        self.assertEqual(inbox.list_entries(self.vault, REVIEW), [])

    def test_reopened_stream_accepts_the_promotion(self):
        self.set_state(REVIEW, "active")
        path = self.record()
        promotion = traces.promote(self.vault, path.name, REVIEW, now=FIXED_NOW)
        self.assertEqual(promotion.stream, REVIEW)

    def test_promotion_body_round_trips_through_its_own_parser(self):
        trace = traces.read_trace(self.record())
        parsed = traces.parse_promotion(traces.promotion_body(trace))
        self.assertEqual(parsed["summary"], trace.summary)
        self.assertEqual(parsed["cwd"], trace.cwd)

    def test_values_containing_colons_survive(self):
        """Labels are split on the first colon only; a summary may legitimately contain more."""
        path = traces.record(
            self.vault, "Checked https://example.com/x: nothing decided", session="s",
            cwd="/tmp/a:b", now=FIXED_NOW,
        )
        promotion = traces.promote(self.vault, path.name, DESIGN, now=FIXED_NOW)
        landed = self.promoted_fields(promotion)
        self.assertEqual(landed["summary"], "Checked https://example.com/x: nothing decided")
        self.assertEqual(landed["cwd"], "/tmp/a:b")


class TestPrune(VaultCase):
    """Promoted traces drain; unpromoted ones are held and counted.

    Sweeping an unpromoted trace by age deletes the evidence that triage stopped happening — the
    design's top residual risk, made invisible. So the default sweeps only what is already re-homed,
    and every prune reports the split whether or not it removed anything.
    """

    def aged(self, days, session="old"):
        return self.record(now=FIXED_NOW - timedelta(days=days), session=session)

    def aged_promoted(self, days, session="promoted", slug=DESIGN):
        path = self.aged(days, session)
        traces.promote(self.vault, path.name, slug, promoted_by="operator", now=FIXED_NOW)
        return path

    def test_refuses_outside_a_git_repo(self):
        """The whole non-lossy argument is git history. Without a repo this is deletion."""
        path = self.aged_promoted(90)
        with self.assertRaises(traces.TraceError) as ctx:
            traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertIn("git", str(ctx.exception))
        self.assertTrue(path.is_file())

    def test_refuses_outside_git_even_for_a_dry_run(self):
        self.aged_promoted(90)
        with self.assertRaises(traces.TraceError):
            traces.prune(self.vault, older_than_days=30, now=FIXED_NOW)

    def test_dry_run_is_the_default_and_deletes_nothing(self):
        self.make_git_vault()
        path = self.aged_promoted(90)
        result = traces.prune(self.vault, older_than_days=30, now=FIXED_NOW)
        self.assertFalse(result.applied)
        self.assertEqual(result.paths, [path])
        self.assertTrue(path.is_file())

    def test_promoted_traces_sweep_once_past_the_window(self):
        """Their content already lives in a stream inbox, so the queue copy is redundant."""
        self.make_git_vault()
        old = self.aged_promoted(90, "promoted-old")
        fresh = self.aged_promoted(2, "promoted-fresh")
        result = traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertEqual(result.paths, [old])
        self.assertEqual([t.name for t in result.swept_triaged], [old.name])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_file())

    def test_untriaged_traces_never_sweep_by_default(self):
        """However old. Age-sweeping the backlog deletes the proof that triage stopped."""
        self.make_git_vault()
        ancient = self.aged(4000, "never-triaged")
        result = traces.prune(self.vault, older_than_days=1, now=FIXED_NOW, apply=True)
        self.assertEqual(result.swept, [])
        self.assertTrue(ancient.is_file())
        self.assertEqual([t.name for t in result.held_back], [ancient.name])

    def test_mixed_queue_drains_only_the_triaged_half(self):
        self.make_git_vault()
        promoted = self.aged_promoted(90, "promoted-old")
        stale = self.aged(120, "stale-untriaged")
        fresh = self.aged(3, "fresh-untriaged")
        result = traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertEqual(result.paths, [promoted])
        self.assertFalse(promoted.exists())
        self.assertTrue(stale.is_file() and fresh.is_file())
        self.assertEqual([t.name for t in result.backlog], [stale.name, fresh.name])
        self.assertEqual([t.name for t in result.held_back], [stale.name])
        self.assertEqual(result.oldest_untriaged.name, stale.name)

    def test_include_untriaged_is_the_explicit_opt_in(self):
        self.make_git_vault()
        promoted = self.aged_promoted(90, "promoted-old")
        stale = self.aged(120, "stale-untriaged")
        fresh = self.aged(3, "fresh-untriaged")
        result = traces.prune(
            self.vault, older_than_days=30, now=FIXED_NOW, apply=True, include_untriaged=True
        )
        self.assertEqual(set(result.paths), {promoted, stale})
        self.assertEqual([t.name for t in result.swept_untriaged], [stale.name])
        self.assertEqual([t.name for t in result.swept_triaged], [promoted.name])
        self.assertEqual(result.held_back, [])
        self.assertTrue(fresh.is_file())

    def test_window_is_configurable(self):
        self.make_git_vault()
        path = self.aged_promoted(10)
        self.assertEqual(traces.prune(self.vault, older_than_days=30, now=FIXED_NOW).swept, [])
        self.assertEqual(traces.prune(self.vault, older_than_days=7, now=FIXED_NOW).paths, [path])

    def test_default_window_exists_but_is_only_a_default(self):
        self.make_git_vault()
        self.aged_promoted(traces.DEFAULT_RETENTION_DAYS + 1)
        self.assertEqual(len(traces.prune(self.vault, now=FIXED_NOW).swept), 1)

    def test_undatable_traces_are_kept(self):
        """Age that cannot be established cannot be established as expired."""
        self.make_git_vault()
        path = self.unassigned() / "hand-written.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nkind: unresolved-write\n---\n\nsomething.\n", encoding="utf-8")
        result = traces.prune(
            self.vault, older_than_days=0, now=FIXED_NOW, apply=True, include_untriaged=True
        )
        self.assertEqual(result.swept, [])
        self.assertTrue(path.is_file())

    def test_promotion_marker_is_swept_with_its_trace(self):
        self.make_git_vault()
        path = self.aged(90)
        promotion = traces.promote(self.vault, path.name, DESIGN, now=FIXED_NOW)
        traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertFalse(path.exists())
        self.assertFalse(promotion.marker.exists())
        # The promoted content is in the stream, which is what made sweeping safe.
        self.assertEqual(len(inbox.list_entries(self.vault, DESIGN)), 1)

    def test_negative_window_is_refused(self):
        self.make_git_vault()
        with self.assertRaises(traces.TraceError):
            traces.prune(self.vault, older_than_days=-1, now=FIXED_NOW)

    def test_nothing_else_prunes(self):
        """Never automatic: recording, listing, and promoting must never remove a trace."""
        self.make_git_vault()
        old = self.aged(400)
        fresh = self.record(session="fresh")
        traces.list_traces(self.vault)
        traces.pending(self.vault)
        traces.promote(self.vault, fresh.name, DESIGN, now=FIXED_NOW)
        self.assertTrue(old.is_file())
        self.assertTrue(fresh.is_file())

    def test_streams_are_never_touched_by_a_prune(self):
        self.make_git_vault()
        self.aged_promoted(90)
        before = {p: p.read_bytes() for p in (self.vault / "streams").rglob("*") if p.is_file()}
        traces.prune(
            self.vault, older_than_days=30, now=FIXED_NOW, apply=True, include_untriaged=True
        )
        after = {p: p.read_bytes() for p in (self.vault / "streams").rglob("*") if p.is_file()}
        self.assertEqual(after, before)

    def test_empty_queue_prunes_to_nothing(self):
        self.make_git_vault()
        result = traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertEqual((result.swept, result.kept), ([], []))


class TestPruneReportsTheSplit(VaultCase):
    """The counts are the operator's only signal that triage has stalled, so they always print."""

    def report(self, **kwargs):
        self.make_git_vault()
        return traces.render_prune(
            traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, **kwargs)
        )

    def build_mixed_queue(self):
        self.record(now=FIXED_NOW - timedelta(days=90), session="promoted-old")
        traces.promote(self.vault, traces.pending(self.vault)[0].name, DESIGN, now=FIXED_NOW)
        self.record(now=FIXED_NOW - timedelta(days=120), session="stale-untriaged")
        self.record(now=FIXED_NOW - timedelta(days=3), session="fresh-untriaged")

    def test_split_is_reported_on_a_dry_run(self):
        self.build_mixed_queue()
        text = self.report()
        self.assertIn("would remove 1 trace(s) older than 30d: 1 triaged, 0 untriaged.", text)
        self.assertIn("2 untriaged (1 past the window, held back)", text)
        self.assertIn("Nothing was deleted. Re-run with --apply.", text)

    def test_split_is_reported_when_applied(self):
        self.build_mixed_queue()
        text = self.report(apply=True)
        self.assertIn("removed 1 trace(s) older than 30d: 1 triaged, 0 untriaged.", text)
        self.assertNotIn("Re-run with --apply", text)

    def test_oldest_untriaged_age_is_reported(self):
        self.build_mixed_queue()
        text = self.report(apply=True)
        self.assertIn("20260416T093000Z-stale-untriaged.md (120d old)", text)
        self.assertIn("Triage has stalled", text)

    def test_backlog_is_reported_even_when_the_prune_is_a_no_op(self):
        """A no-op prune is exactly when a growing backlog most needs to be visible."""
        self.record(now=FIXED_NOW - timedelta(days=200), session="never-triaged")
        text = self.report(apply=True)
        self.assertIn("removed 0 trace(s)", text)
        self.assertIn("1 untriaged (1 past the window, held back)", text)
        self.assertIn("(200d old)", text)
        self.assertIn("Triage has stalled", text)

    def test_worked_queue_says_so(self):
        self.record(now=FIXED_NOW - timedelta(days=90), session="promoted-old")
        traces.promote(self.vault, traces.pending(self.vault)[0].name, DESIGN, now=FIXED_NOW)
        text = self.report(apply=True)
        self.assertIn("No untriaged traces waiting. The queue is worked.", text)
        self.assertNotIn("stalled", text)

    def test_explicit_discard_is_reported_as_untriaged(self):
        self.record(now=FIXED_NOW - timedelta(days=200), session="never-triaged")
        text = self.report(apply=True, include_untriaged=True)
        self.assertIn("removed 1 trace(s) older than 30d: 0 triaged, 1 untriaged.", text)
        self.assertIn("No untriaged traces waiting.", text)

    def test_empty_queue_reports_a_worked_queue(self):
        self.assertIn("No untriaged traces waiting.", self.report(apply=True))


class TestAdopt(VaultCase):
    """The whole triage workflow in one call.

    The property under test is that nothing the traces already carry has to be retyped: repo,
    branch, and every working directory become claims by being read off the group.
    """

    def adopt(self, selector="1", **kwargs):
        return traces.adopt(self.vault, selector, now=FIXED_NOW, **kwargs)

    def test_mints_a_stream_claiming_what_the_traces_observed(self):
        self.record(repo="acme/platform")
        adoption = self.adopt(slug="plat-1719-gclb")
        brief = vault.read_stream(self.vault, "plat-1719-gclb")
        self.assertTrue(adoption.minted)
        self.assertEqual(brief.claim("repo"), ["acme/platform"])
        self.assertEqual(brief.claim("branch"), ["feat/PLAT-1719/gclb"])
        self.assertEqual(brief.claim("worktree"), ["/Users/p/work/platform"])
        self.assertIn("PLAT-1719", brief.claim("issue"))

    def test_promotes_every_trace_in_the_group_at_once(self):
        for n in range(7):
            self.record(summary=f"Session {n} ended.")
        adoption = self.adopt(slug="plat-1719-gclb")
        self.assertEqual(len(adoption.promotions), 7)
        self.assertEqual(traces.pending(self.vault), [])
        entries = list((vault.streams_dir(self.vault) / "plat-1719-gclb" / "inbox").glob("*.md"))
        self.assertEqual(len(entries), 7)

    def test_claims_every_worktree_the_branch_was_seen_in(self):
        """One branch, a main clone and a tool-managed worktree. Claiming one would not resolve the other."""
        self.record(summary="From the clone.", cwd="/Users/p/work/platform")
        self.record(summary="From the sandbox.", cwd="/Users/p/.sculptor/abc/code")
        self.adopt(slug="plat-1719-gclb")
        brief = vault.read_stream(self.vault, "plat-1719-gclb")
        self.assertEqual(
            brief.claim("worktree"), ["/Users/p/work/platform", "/Users/p/.sculptor/abc/code"]
        )

    def test_derives_the_slug_when_none_is_given(self):
        self.record()
        self.assertIn("plat-1719", self.adopt().stream)

    def test_never_claims_main(self):
        """A `main` claim resolves every stream in the repo and therefore none of them."""
        self.record(branch="main")
        brief = vault.read_stream(self.vault, self.adopt(slug="some-work").stream)
        self.assertEqual(brief.claim("branch"), [])

    def test_joins_a_stream_that_already_claims_the_branch_instead_of_minting(self):
        self.record(branch="design-branch")
        writer.append_entry(self.vault, DESIGN, "Next", "placeholder", now=FIXED_NOW)
        path = vault.streams_dir(self.vault) / DESIGN / "BRIEF.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  branch: []", "  branch: [design-branch]"
            ),
            encoding="utf-8",
        )
        adoption = self.adopt()
        self.assertFalse(adoption.minted)
        self.assertEqual(adoption.stream, DESIGN)

    def test_placeholder_goal_is_announced_as_an_operator_ask(self):
        """A minted stream with an invented goal is worse than one that says its goal is missing."""
        self.record()
        brief = vault.read_stream(self.vault, self.adopt(slug="plat-1719-gclb").stream)
        self.assertIn("TODO", brief.sections["Goal"])
        self.assertEqual(len(brief.operator_asks()), 1)

    def test_a_supplied_goal_files_no_ask(self):
        self.record()
        brief = vault.read_stream(
            self.vault, self.adopt(slug="plat-1719-gclb", goal="Promote image tags.").stream
        )
        self.assertEqual(brief.sections["Goal"], "Promote image tags.")
        self.assertEqual(brief.operator_asks(), [])

    def test_a_supplied_goal_is_not_reported_as_a_placeholder(self):
        """The output is the only thing the operator sees, so it must not contradict the brief.

        Asserting on the brief alone let `render_adoption` tell every mint its goal was a
        placeholder — including mints that had just written one — which reads as `--goal` being
        silently ignored and gets the goal pointlessly rewritten by hand.
        """
        self.record()
        adoption = self.adopt(slug="plat-1719-gclb", goal="Promote image tags.")
        self.assertTrue(adoption.goal_supplied)
        self.assertNotIn("placeholder", traces.render_adoption(adoption))

    def test_a_missing_goal_is_reported_as_a_placeholder(self):
        self.record()
        adoption = self.adopt(slug="plat-1719-gclb")
        self.assertFalse(adoption.goal_supplied)
        self.assertIn("placeholder", traces.render_adoption(adoption))

    def test_joining_an_existing_stream_never_claims_its_goal_is_a_placeholder(self):
        """Joining writes no goal at all, so the hint would point at a brief it did not touch."""
        self.record(branch="design-branch")
        writer.append_entry(self.vault, DESIGN, "Next", "placeholder", now=FIXED_NOW)
        path = vault.streams_dir(self.vault) / DESIGN / "BRIEF.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "  branch: []", "  branch: [design-branch]"
            ),
            encoding="utf-8",
        )
        adoption = self.adopt()
        self.assertFalse(adoption.minted)
        self.assertNotIn("placeholder", traces.render_adoption(adoption))

    def test_an_ambiguous_selector_refuses_rather_than_picking(self):
        """Adopting the wrong group files unrelated work into one stream — the failure it drains."""
        self.record(branch="feat/one")
        self.record(branch="feat/two")
        with self.assertRaises(traces.TraceError) as caught:
            traces.adopt(self.vault, "feat", now=FIXED_NOW)
        self.assertIn("matches 2 work items", str(caught.exception))

    def test_an_out_of_range_index_names_the_queue_size(self):
        self.record()
        with self.assertRaises(traces.TraceError) as caught:
            traces.adopt(self.vault, "9", now=FIXED_NOW)
        self.assertIn("queue has 1", str(caught.exception))


class TestShow(VaultCase):
    """Where the detail goes that `list` compresses away."""

    def test_every_trace_in_the_item_is_shown_with_its_subject(self):
        self.record(summary="Fix the deploy", session="s1")
        self.record(summary="Chase a flaky test", session="s2")
        text = traces.render_item(
            traces.select_work_item(self.vault, "feat/PLAT-1719/gclb"), now=FIXED_NOW
        )
        self.assertIn("Fix the deploy", text)
        self.assertIn("Chase a flaky test", text)
        self.assertIn("s1", text)
        self.assertIn("s2", text)

    def test_it_names_the_file_so_the_operator_can_go_further(self):
        name = self.record().name
        text = traces.render_item(traces.select_work_item(self.vault, "1"), now=FIXED_NOW)
        self.assertIn(name, text)

    def test_it_ends_with_the_two_commands_that_resolve_the_item(self):
        self.record()
        text = traces.render_item(traces.select_work_item(self.vault, "1"), now=FIXED_NOW, cli="traces")
        self.assertIn("traces adopt 'feat/PLAT-1719/gclb'", text)
        self.assertIn("traces dismiss 'feat/PLAT-1719/gclb'", text)

    def test_a_dismissed_trace_shows_the_reason_it_was_dismissed(self):
        self.record()
        traces.dismiss(self.vault, "1", why="ran tests only", now=FIXED_NOW)
        item = traces.work_items(traces.list_traces(self.vault))[0]
        self.assertIn("dismissed: ran tests only", traces.render_item(item, now=FIXED_NOW))

    def test_an_unsummarized_trace_explains_why_rather_than_showing_a_sentinel(self):
        """The operator needs to know the subject was never captured, not read a sentinel as prose."""
        writer.write_unassigned(
            self.vault, "- **Repo:** n/a", reason="no hint matched", session="s1",
            branch="feat/PLAT-1719/gclb", now=FIXED_NOW,
        )
        text = traces.render_item(traces.select_work_item(self.vault, "1"), now=FIXED_NOW)
        self.assertIn("never recorded", text)
        self.assertNotIn(f"summary: {writer.NO_SUMMARY}", text)


class TestDefaultBranchIsNotAWorkItem(VaultCase):
    """`main` names the repository, not the work.

    The design already refuses to *claim* a default branch — it would resolve everything and
    therefore nothing. Grouping on one has the same defect: thirteen unrelated sessions were being
    offered for adoption into a single stream named `main`.
    """

    def busy_main(self):
        self.record(branch="main", session="s1", summary="Investigate 503s in CI")
        self.record(branch="main", session="s2", summary="Create Linear issues for the migration")
        self.record(branch="main", session="s3", summary="Run the PR burndown skill")

    def test_each_session_on_a_default_branch_is_its_own_item(self):
        self.busy_main()
        items = traces.work_items(traces.list_traces(self.vault))
        self.assertEqual(len(items), 3)
        self.assertEqual({i.session for i in items}, {"s1", "s2", "s3"})

    def test_a_feature_branch_still_groups_as_one(self):
        """Mutation check: the split is the default-branch rule, not per-session grouping returning."""
        for n in range(3):
            self.record(branch="feat/real-work", session=f"s{n}", summary=f"Step {n}")
        items = traces.work_items(traces.list_traces(self.vault))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].count, 3)

    def test_the_listing_clusters_them_under_one_heading(self):
        self.busy_main()
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW, cli="traces")
        self.assertIn("3 session(s), nothing shared", text)
        self.assertIn("Investigate 503s in CI", text)
        self.assertIn("dismiss all: traces dismiss", text)

    def test_the_cluster_selector_dismisses_every_session_at_once(self):
        """Most of what lands on a default branch is noise; clearing it must cost one command."""
        self.busy_main()
        dismissed = traces.dismiss(
            self.vault, "n/a@main", why="routine work, no stream needed", now=FIXED_NOW
        )
        self.assertEqual(len(dismissed), 3)
        self.assertEqual(traces.pending(self.vault), [])

    def test_adopting_the_cluster_is_refused_with_what_to_do_instead(self):
        self.busy_main()
        with self.assertRaises(traces.TraceError) as caught:
            traces.adopt(self.vault, "n/a@main", now=FIXED_NOW)
        message = str(caught.exception)
        self.assertIn("not one piece of work", message)
        self.assertIn("dismiss", message)

    def test_one_session_can_still_be_adopted_out_of_the_cluster(self):
        self.busy_main()
        adoption = traces.adopt(self.vault, "s2", slug="settler-gke-migration", now=FIXED_NOW)
        self.assertEqual(len(adoption.promotions), 1)
        self.assertEqual(len(traces.pending(self.vault)), 2)

    def test_an_adopted_session_claims_no_branch_and_no_worktree(self):
        """The clone is shared by everything in the repo.

        A worktree claim with no branch claim beside it is live whenever the directory exists, so
        claiming it here would match every work item in the repository — `main`-as-a-claim by
        another route, and the exact bug this grouping change was made to fix.
        """
        self.busy_main()
        adoption = traces.adopt(self.vault, "s1", slug="ci-503s", now=FIXED_NOW)
        brief = vault.read_stream(self.vault, adoption.stream)
        self.assertEqual(brief.claim("branch"), [])
        self.assertEqual(brief.claim("worktree"), [])

    def test_the_session_reference_is_the_selector_not_a_listing_position(self):
        self.busy_main()
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW, cli="traces")
        self.assertIn("traces adopt 's1'", text)
        self.assertNotIn("traces adopt 3 <slug>", text)


class TestSelectorsSurviveTheQueueChanging(VaultCase):
    """Indices renumber; the printed selector must not.

    This is the one failure in the reframe that would be silent: an operator reads a listing, adopts
    item 1, then dismisses "item 3" — which is now a different work item, and the command succeeds.
    """

    def setUp(self):
        super().setUp()
        for branch in ("feat/one", "feat/two", "feat/three"):
            self.record(branch=branch, summary=f"Work on {branch}.")

    def test_the_listing_prints_selectors_not_bare_indices(self):
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW)
        self.assertIn("adopt 'feat/one'", text)
        self.assertIn("dismiss 'feat/three'", text)

    def test_printed_commands_match_how_the_tool_was_invoked(self):
        """A listing that prints a command the reader cannot run is the SKILL.md doc-bug class."""
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW, cli="traces")
        self.assertIn("traces adopt 'feat/one'", text)
        self.assertNotIn("python3 -m", text)

        as_module = traces.render_queue(
            traces.list_traces(self.vault), now=FIXED_NOW, cli=traces.MODULE_PROG
        )
        self.assertIn("python3 -m plugin.lib.traces adopt 'feat/one'", as_module)

    def test_a_selector_still_names_the_same_item_after_an_earlier_one_leaves(self):
        traces.adopt(self.vault, "feat/one", slug="work-one", now=FIXED_NOW)
        item = traces.select_work_item(self.vault, "feat/three")
        self.assertEqual(item.branch, "feat/three")

    def test_an_index_does_not(self):
        """Mutation check: the hazard the selectors exist to avoid is real, not hypothetical."""
        before = traces.select_work_item(self.vault, "3").branch
        traces.adopt(self.vault, "feat/one", slug="work-one", now=FIXED_NOW)
        with self.assertRaises(traces.TraceError):
            traces.select_work_item(self.vault, "3")
        self.assertEqual(before, "feat/three")

    def test_an_exact_branch_beats_a_substring_of_another(self):
        """`feat/one` is a substring of nothing here, but an exact match must never read ambiguous."""
        self.record(branch="feat/one-more", summary="Adjacent work.")
        self.assertEqual(traces.select_work_item(self.vault, "feat/one").branch, "feat/one")


class TestSessionsOutsideAnyCheckout(VaultCase):
    """No repo and no branch. Real: sessions started in a home directory."""

    def test_different_directories_are_different_work_items(self):
        self.record(repo=None, branch=None, cwd="/Users/p", summary="One.")
        self.record(repo=None, branch=None, cwd="/Users/p/notes", summary="Two.")
        items = traces.work_items(traces.list_traces(self.vault))
        self.assertEqual(len(items), 2)

    def test_the_directory_is_the_selector(self):
        self.record(repo=None, branch=None, cwd="/Users/p", summary="One.")
        text = traces.render_queue(traces.list_traces(self.vault), now=FIXED_NOW)
        self.assertIn("dismiss '/Users/p'", text)
        self.assertEqual(traces.select_work_item(self.vault, "/Users/p").where, "/Users/p")

    def test_the_directory_is_claimed_as_a_worktree_never_as_a_branch(self):
        self.record(repo=None, branch=None, cwd="/Users/p/notes", summary="One.")
        adoption = traces.adopt(self.vault, "/Users/p/notes", slug="notes-work", now=FIXED_NOW)
        brief = vault.read_stream(self.vault, adoption.stream)
        self.assertEqual(brief.claim("branch"), [])
        self.assertEqual(brief.claim("worktree"), ["/Users/p/notes"])


class TestDismiss(VaultCase):
    """The verb the queue was missing: an exit that is neither promotion nor age."""

    def test_dismissal_leaves_the_pending_queue_but_not_the_vault(self):
        self.record()
        dismissed = traces.dismiss(self.vault, "1", why="noise", now=FIXED_NOW)
        self.assertEqual(len(dismissed), 1)
        self.assertEqual(traces.pending(self.vault), [])
        self.assertEqual(len(traces.list_traces(self.vault)), 1)
        self.assertTrue(dismissed[0].path.is_file())

    def test_the_reason_is_recorded_and_readable(self):
        self.record()
        traces.dismiss(self.vault, "1", why="ran tests only", dismissed_by="peter", now=FIXED_NOW)
        trace = traces.list_traces(self.vault)[0]
        self.assertTrue(trace.dismissed)
        self.assertEqual(trace.dismissed_why, "ran tests only")
        self.assertEqual(trace.dismissed_by, "peter")

    def test_a_reason_is_required(self):
        """Without one, 'dismissed' and 'silently dropped' are the same record."""
        self.record()
        for why in ("", "   "):
            with self.assertRaises(traces.TraceError):
                traces.dismiss(self.vault, "1", why=why, now=FIXED_NOW)
        self.assertEqual(len(traces.pending(self.vault)), 1)

    def test_dismissed_traces_sweep_like_promoted_ones(self):
        self.make_git_vault()
        self.record(now=FIXED_NOW - timedelta(days=200))
        traces.dismiss(self.vault, "1", why="noise", now=FIXED_NOW)
        result = traces.prune(self.vault, older_than_days=30, now=FIXED_NOW, apply=True)
        self.assertEqual(len(result.swept_triaged), 1)
        self.assertEqual(result.backlog, [])

    def test_dismissal_does_not_count_as_a_stalled_backlog(self):
        """A backlog number that counts deliberate dismissals is a number the operator learns to ignore."""
        self.make_git_vault()
        self.record(now=FIXED_NOW - timedelta(days=200))
        traces.dismiss(self.vault, "1", why="noise", now=FIXED_NOW)
        text = traces.render_prune(
            traces.prune(self.vault, older_than_days=999, now=FIXED_NOW)
        )
        self.assertIn("No untriaged traces waiting. The queue is worked.", text)
        self.assertNotIn("Triage has stalled", text)

    def test_promotion_supersedes_an_earlier_dismissal(self):
        """Reversing a call must not leave the trace reading as both discarded and filed."""
        name = self.record().name
        traces.dismiss(self.vault, "1", why="noise", now=FIXED_NOW)
        traces.promote(self.vault, name, DESIGN, now=FIXED_NOW)
        trace = traces.list_traces(self.vault)[0]
        self.assertTrue(trace.promoted)
        self.assertFalse(trace.dismissed)


class TestFixturesAreReadOnly(unittest.TestCase):
    def test_examples_are_untouched(self):
        """The fixtures are input. A test that writes into them poisons every later run."""
        self.assertFalse((FIXTURE_VAULT / traces.UNASSIGNED_DIR).exists())
        for slug in (DESIGN, REVIEW):
            self.assertFalse((FIXTURE_VAULT / "streams" / slug / "inbox").exists())


if __name__ == "__main__":
    unittest.main()
