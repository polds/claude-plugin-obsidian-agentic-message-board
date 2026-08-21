"""Task 14 verification — external reference resolution.

Synthetic streams are built in `tempfile`; `examples/` is read-only fixture input, and the cases
that assert against it are the ones that matter most: it is the real brief a cold-start agent
failed to follow up on.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import refs, vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"

BRIEF_TEMPLATE = """---
stream: {slug}
title: {slug}
state: active
owner: tester
updated: 2026-08-13T00:00:00Z
claims:
  repo: [{repo}]
  branch: [{branch}]
  worktree: [{worktree}]
  issue: [{issue}]
  pr: [{pr}]
---

## Goal

Synthetic stream.

## Decided

{decided}

## Open

None.
"""


class StreamBuilder(unittest.TestCase):
    """Writes real stream directories under tempfile so the vault parse path is exercised too."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="refs-test-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.counter = 0

    def brief(self, repo="", branch="", worktree="", issue="", pr="", decided="None."):
        self.counter += 1
        slug = f"s{self.counter}"
        directory = self.root / "streams" / slug
        directory.mkdir(parents=True)
        (directory / "BRIEF.md").write_text(
            BRIEF_TEMPLATE.format(
                slug=slug,
                repo=repo,
                branch=branch,
                worktree=worktree,
                issue=issue,
                pr=pr,
                decided=decided,
            ),
            encoding="utf-8",
        )
        return vault.read_stream(self.root, slug)

    def context(self, env=None, **claims):
        return refs.reference_context(self.brief(**claims), env=env or {})


class TestParseRepo(unittest.TestCase):
    def test_bare_owner_name_assumes_the_default_host_and_says_so(self):
        repo = refs.parse_repo("acme/platform")
        self.assertEqual(repo.url, "https://github.com/acme/platform")
        self.assertFalse(repo.host_certain)

    def test_claim_url_supplies_the_host_rather_than_the_default(self):
        repo = refs.parse_repo("https://git.example.com/team/service")
        self.assertEqual(repo.host, "git.example.com")
        self.assertTrue(repo.host_certain)

    def test_ssh_remote_form_parses_and_drops_the_git_suffix(self):
        repo = refs.parse_repo("git@github.com:acme/platform.git")
        self.assertEqual(repo.url, "https://github.com/acme/platform")

    def test_userinfo_is_not_part_of_the_location(self):
        self.assertEqual(refs.parse_repo("https://token@github.com/o/r").host, "github.com")

    def test_gitlab_subgroup_path_is_kept_whole_and_uses_gitlab_conventions(self):
        repo = refs.parse_repo("https://gitlab.com/group/sub/proj")
        self.assertEqual(repo.path, "group/sub/proj")
        self.assertEqual(repo.family, "gitlab")
        self.assertTrue(repo.pr_url(7).endswith("/group/sub/proj/-/merge_requests/7"))
        self.assertTrue(repo.commit_url("abc1234").endswith("/-/commit/abc1234"))

    def test_bitbucket_conventions_differ_from_github(self):
        repo = refs.parse_repo("https://bitbucket.org/team/repo")
        self.assertEqual(repo.family, "bitbucket")
        self.assertTrue(repo.pr_url(3).endswith("/pull-requests/3"))

    def test_unknown_self_hosted_forge_falls_to_the_github_shape(self):
        repo = refs.parse_repo("https://code.internal/team/repo")
        self.assertEqual(repo.family, "github")
        self.assertTrue(repo.pr_url(3).endswith("/pull/3"))

    def test_browser_pasted_url_is_truncated_back_to_the_repo(self):
        repo = refs.parse_repo("https://github.com/acme/platform/pull/905")
        self.assertEqual(repo.path, "acme/platform")
        self.assertTrue(repo.pr_url(905).endswith("/acme/platform/pull/905"))

    def test_owner_alone_is_not_a_location(self):
        self.assertIsNone(refs.parse_repo("acme"))
        self.assertIsNone(refs.parse_repo("   "))


class TestExpandClaims(StreamBuilder):
    def test_fixture_repo_pr_and_issue_all_resolve(self):
        brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        by_kind = {r.kind: r for r in refs.expand_claims(brief, env={})}
        self.assertEqual(by_kind["repo"].url, "https://github.com/acme/platform")
        self.assertEqual(by_kind["pr"].url, "https://github.com/acme/platform/pull/905")
        self.assertEqual(
            by_kind["issue"].url, "https://linear.app/acme/issue/PLAT-1962"
        )

    def test_issue_url_derived_from_repo_owner_is_marked_a_guess(self):
        brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        issue = [r for r in refs.expand_claims(brief, env={}) if r.kind == "issue"][0]
        self.assertFalse(issue.certain)
        self.assertIn("guessed from repo owner", issue.basis)

    def test_configured_issue_base_beats_the_guess_and_is_certain(self):
        brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        env = {refs.ISSUE_BASE_ENV: "https://linear.app/acme"}
        issue = [r for r in refs.expand_claims(brief, env=env) if r.kind == "issue"][0]
        self.assertEqual(issue.url, "https://linear.app/acme/issue/PLAT-1962")
        self.assertTrue(issue.certain)

    def test_a_linear_base_that_already_ends_in_issue_is_not_doubled(self):
        """The operator pastes the prefix the browser shows, which includes `/issue`.

        Doubling it produces `.../issue/issue/KEY` — dead, but plausible enough in a rendered brief
        that nobody clicks it to find out.
        """
        brief = self.brief(issue="ACME-12")
        for base in ("https://linear.app/acme/issue", "https://linear.app/acme/issue/"):
            env = {refs.ISSUE_BASE_ENV: base}
            issue = [r for r in refs.expand_claims(brief, env=env) if r.kind == "issue"][0]
            self.assertEqual(issue.url, "https://linear.app/acme/issue/ACME-12", base)

    def test_jira_style_issue_base_appends_the_key_directly(self):
        brief = self.brief(issue="ACME-12")
        env = {refs.ISSUE_BASE_ENV: "https://acme.atlassian.net/browse"}
        issue = [r for r in refs.expand_claims(brief, env=env) if r.kind == "issue"][0]
        self.assertEqual(issue.url, "https://acme.atlassian.net/browse/ACME-12")

    def test_explicit_template_is_used_verbatim(self):
        brief = self.brief(issue="ACME-12")
        env = {refs.ISSUE_BASE_ENV: "https://tracker.example.com/t/{key}/view"}
        issue = [r for r in refs.expand_claims(brief, env=env) if r.kind == "issue"][0]
        self.assertEqual(issue.url, "https://tracker.example.com/t/ACME-12/view")

    def test_issue_claim_that_is_already_a_url_is_used_as_written(self):
        brief = self.brief(issue="https://linear.app/acme/issue/ACME-12")
        issue = [r for r in refs.expand_claims(brief, env={}) if r.kind == "issue"][0]
        self.assertEqual(issue.url, "https://linear.app/acme/issue/ACME-12")
        self.assertTrue(issue.certain)

    def test_branch_claim_resolves_against_the_single_repo_claim(self):
        brief = self.brief(repo="o/r", branch="feat/PLAT-1719/sql")
        branch = [r for r in refs.expand_claims(brief, env={}) if r.kind == "branch"][0]
        self.assertEqual(branch.url, "https://github.com/o/r/tree/feat/PLAT-1719/sql")

    def test_claims_degrade_gracefully_when_the_repo_is_missing(self):
        brief = self.brief(branch="topic", pr="905")
        by_kind = {r.kind: r for r in refs.expand_claims(brief, env={})}
        self.assertEqual(by_kind["pr"].url, "")
        self.assertIn("no `repo` claim", by_kind["pr"].basis)
        # Paired with a presence assertion: the claim is still reported, just without a URL.
        self.assertEqual(by_kind["pr"].text, "905")
        self.assertEqual(by_kind["branch"].text, "topic")

    def test_two_repo_claims_make_a_bare_pr_number_ambiguous_not_wrong(self):
        brief = self.brief(repo="o/one, o/two", pr="905")
        pr = [r for r in refs.expand_claims(brief, env={}) if r.kind == "pr"][0]
        self.assertEqual(pr.url, "")
        self.assertIn("2 `repo` claims", pr.basis)

    def test_pr_claim_written_as_a_url_is_kept(self):
        brief = self.brief(repo="o/r", pr="https://github.com/o/r/pull/905")
        pr = [r for r in refs.expand_claims(brief, env={}) if r.kind == "pr"][0]
        self.assertEqual(pr.url, "https://github.com/o/r/pull/905")

    def test_worktree_is_reported_without_a_url(self):
        brief = self.brief(worktree="/tmp/wt")
        worktree = [r for r in refs.expand_claims(brief, env={}) if r.kind == "worktree"][0]
        self.assertEqual(worktree.url, "")
        self.assertEqual(worktree.text, "/tmp/wt")

    def test_forge_base_env_replaces_the_default_host(self):
        brief = self.brief(repo="team/service", pr="7")
        env = {refs.FORGE_BASE_ENV: "https://ghe.example.com"}
        by_kind = {r.kind: r for r in refs.expand_claims(brief, env=env)}
        self.assertEqual(by_kind["repo"].url, "https://ghe.example.com/team/service")
        self.assertEqual(by_kind["pr"].url, "https://ghe.example.com/team/service/pull/7")

    def test_claim_lines_distinguish_derived_from_guessed(self):
        brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        lines = refs.claim_lines(refs.expand_claims(brief, env={}))
        joined = "\n".join(lines)
        self.assertIn("-> https://github.com/acme/platform/pull/905", joined)
        self.assertIn("~> https://linear.app/acme/issue/PLAT-1962", joined)

    def test_claim_lines_on_no_claims_is_empty(self):
        self.assertEqual(refs.claim_lines([]), [])


class TestLinkText(StreamBuilder):
    def test_the_references_the_cold_start_agent_could_not_follow_now_resolve(self):
        brief = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")
        out = refs.link_text(brief.sections["Decided"], refs.reference_context(brief, env={}))
        self.assertIn("[a1b2c3d4e](https://github.com/acme/platform/commit/a1b2c3d4e)", out)
        self.assertIn("[#1025](https://github.com/acme/platform/pull/1025)", out)

    def test_without_a_repo_claim_the_same_text_is_returned_untouched(self):
        ctx = self.context()
        text = "merged as a1b2c3d4e / #1025"
        self.assertEqual(refs.link_text(text, ctx), text)
        self.assertIn("a1b2c3d4e", refs.link_text(text, ctx))

    def test_qualified_pr_reference_needs_no_claim_at_all(self):
        ctx = self.context()
        out = refs.link_text("see acme/platform#905 for context", ctx)
        self.assertIn("[acme/platform#905](https://github.com/acme/platform/pull/905)", out)

    def test_qualified_reference_prefers_the_host_the_claim_named(self):
        ctx = self.context(repo="https://git.example.com/team/service")
        out = refs.link_text("team/service#12 landed", ctx)
        self.assertIn("https://git.example.com/team/service/pull/12", out)

    def test_gitlab_claim_expands_body_refs_with_gitlab_conventions(self):
        ctx = self.context(repo="https://gitlab.com/group/proj")
        out = refs.link_text("fixed in #42 and 1a2b3c4", ctx)
        self.assertIn("https://gitlab.com/group/proj/-/merge_requests/42", out)
        self.assertIn("https://gitlab.com/group/proj/-/commit/1a2b3c4", out)

    def test_issue_key_expands_only_when_the_stream_claims_that_project(self):
        ctx = self.context(issue="PLAT-1962", env={refs.ISSUE_BASE_ENV: "https://linear.app/acme"})
        out = refs.link_text("PLAT-1719 spans nine branches; encoded as UTF-8", ctx)
        self.assertIn("[PLAT-1719](https://linear.app/acme/issue/PLAT-1719)", out)
        self.assertIn("UTF-8", out)
        self.assertNotIn("issue/UTF-8", out)

    def test_a_guessed_tracker_workspace_is_never_written_into_the_body(self):
        # The mutation check for rule 2: the same key, the same claims, the only difference being
        # whether the workspace was configured or inferred. Inference must leave the text alone.
        guessed = self.context(repo="acme/platform", issue="PLAT-1962")
        confirmed = self.context(
            repo="acme/platform",
            issue="PLAT-1962",
            env={refs.ISSUE_BASE_ENV: "https://linear.app/acme"},
        )
        self.assertEqual(refs.link_text("see PLAT-1719", guessed), "see PLAT-1719")
        self.assertIn("linear.app/acme/issue/PLAT-1719", refs.link_text("see PLAT-1719", confirmed))

    def test_two_repo_claims_leave_bare_references_alone(self):
        ctx = self.context(repo="o/one, o/two")
        text = "merged as a1b2c3d4e / #1025"
        self.assertEqual(refs.link_text(text, ctx), text)

    def test_code_spans_and_fences_are_never_rewritten(self):
        ctx = self.context(repo="o/r")
        text = "run `git show a1b2c3d4e` first\n\n```\nsee #1025\n```\n"
        self.assertEqual(refs.link_text(text, ctx), text)

    def test_existing_links_are_not_double_wrapped(self):
        ctx = self.context(repo="o/r")
        text = "see [#1025](https://github.com/o/r/pull/1025) and [[commit-notes]]"
        self.assertEqual(refs.link_text(text, ctx), text)

    def test_a_reference_already_inside_a_url_is_left_alone(self):
        ctx = self.context(repo="o/r")
        text = "https://github.com/o/r/commit/a1b2c3d4e"
        self.assertEqual(refs.link_text(text, ctx), text)

    def test_numbers_and_hex_words_are_not_mistaken_for_commits(self):
        ctx = self.context(repo="o/r")
        text = "1234567 rows, 20260813 records, deadbeef sentinel"
        self.assertEqual(refs.link_text(text, ctx), text)
        # Paired presence assertion: the detector is live in this very context.
        self.assertIn("/commit/1a2b3c4", refs.link_text("1a2b3c4", ctx))

    def test_heading_markers_are_not_pr_references(self):
        ctx = self.context(repo="o/r")
        self.assertEqual(refs.link_text("## Decided", ctx), "## Decided")

    def test_multiple_references_on_one_line_all_survive_rewriting(self):
        ctx = self.context(repo="o/r")
        out = refs.link_text("#1 then a1b2c3d4e then #2", ctx)
        self.assertEqual(out.count("](https://github.com/o/r/"), 3)


class TestLintDecided(StreamBuilder):
    def setUp(self):
        super().setUp()
        self.fixture = vault.read_stream(FIXTURE_VAULT, "plat-1962-review-pr905")

    def test_fixture_flags_exactly_the_entries_that_cite_unlinked_evidence(self):
        findings = refs.lint_decided(self.fixture, env={})
        self.assertEqual([f.index for f in findings], [1, 3])

    def test_the_cold_start_complaints_are_named_in_the_output(self):
        report = "\n".join(line for f in refs.lint_decided(self.fixture, env={}) for line in f.lines())
        for cited in ("a1b2c3d4e", "#1025", "PLAT-1719"):
            self.assertIn(cited, report)

    def test_findings_carry_the_url_the_author_should_paste_in(self):
        detail = "\n".join(refs.lint_decided(self.fixture, env={})[0].details)
        self.assertIn("https://github.com/acme/platform/commit/a1b2c3d4e", detail)
        self.assertIn("https://github.com/acme/platform/pull/1025", detail)

    def test_backticked_file_paths_count_as_citations_and_report_as_unreachable(self):
        detail = "\n".join(refs.lint_decided(self.fixture, env={})[1].details)
        self.assertIn("datastore.xrd.yaml", detail)
        self.assertIn("unreachable", detail)

    def test_an_entry_with_no_evidence_is_not_flagged(self):
        brief = self.brief(decided="- 2026-08-13 — Ship it. Why: it works.")
        self.assertEqual(refs.lint_decided(brief, env={}), [])

    def test_a_linked_citation_is_not_flagged(self):
        brief = self.brief(
            repo="o/r",
            decided="- 2026-08-13 — Superseded by "
            "[a1b2c3d4e](https://github.com/o/r/commit/a1b2c3d4e). Why: shipped elsewhere.",
        )
        self.assertEqual(refs.lint_decided(brief, env={}), [])

    def test_a_wikilinked_artifact_is_not_flagged(self):
        brief = self.brief(decided="- 2026-08-13 — Adopted [[brief-schema]]. Why: settled.")
        self.assertEqual(refs.lint_decided(brief, env={}), [])

    def test_an_unlinked_citation_beside_a_linked_one_is_still_reported(self):
        brief = self.brief(
            repo="o/r",
            decided="- 2026-08-13 — Adopted [[brief-schema]]; superseded by a1b2c3d4e. Why: x.",
        )
        findings = refs.lint_decided(brief, env={})
        self.assertEqual(len(findings), 1)
        self.assertIn("a1b2c3d4e", findings[0].message)

    def test_a_wrapped_entry_is_linted_as_one_entry(self):
        brief = self.brief(
            repo="o/r",
            decided="- 2026-08-13 — Closed as superseded,\n  merged as a1b2c3d4e / #1025. Why: x.",
        )
        findings = refs.lint_decided(brief, env={})
        self.assertEqual(len(findings), 1)
        self.assertEqual(len(findings[0].citations), 2)

    def test_lint_is_advisory_and_never_raises_on_a_brief_with_nothing_to_go_on(self):
        brief = self.brief(decided="- 2026-08-13 — Superseded by a1b2c3d4e. Why: x.")
        findings = refs.lint_decided(brief, env={})
        self.assertEqual(len(findings), 1)
        self.assertIn("unreachable", "\n".join(findings[0].details))
        self.assertTrue(str(findings[0]).startswith("advisory:"))

    def test_design_stream_with_no_code_claims_produces_no_advisories(self):
        design = vault.read_stream(FIXTURE_VAULT, "message-board-design")
        self.assertEqual(refs.lint_decided(design, env={}), [])
        # Paired presence assertion: that brief really does have a ## Decided section to lint.
        self.assertIn("Decided", design.sections)


class TestFixtureIsReadOnly(unittest.TestCase):
    def test_resolution_never_writes_to_the_fixture_vault(self):
        before = {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*.md")}
        for slug in ("plat-1962-review-pr905", "message-board-design"):
            brief = vault.read_stream(FIXTURE_VAULT, slug)
            ctx = refs.reference_context(brief, env={})
            refs.expand_claims(brief, ctx)
            refs.lint_decided(brief, ctx)
            for body in brief.sections.values():
                refs.link_text(body, ctx)
        self.assertTrue(before)
        self.assertEqual(before, {p: p.stat().st_mtime_ns for p in FIXTURE_VAULT.rglob("*.md")})


if __name__ == "__main__":
    unittest.main()
