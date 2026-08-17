"""Task 5 verification.

Two sources of test data, no invented third: the hand-verified fixtures in `examples/`, and scratch
vaults built in `tempfile`. Branch names, worktree counts and the PLAT-1719 fan-out are the ones
measured against `acme/platform` on 2026-08-13 — the environment the resolver has to survive,
not a friendlier one.
"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import claims, vault  # noqa: E402

FIXTURE_VAULT = REPO / "examples"

# Real branch names from the measured repo. Every convention here is one the parser must survive.
REAL_BRANCHES = {
    "PLAT-2039": "PLAT-2039",
    "plat-1921": "PLAT-1921",
    "polds/plat-1870-integration-tests-speed": "PLAT-1870",
    "feat/PLAT-1719/gclb": "PLAT-1719",
    "peterolds/plat-1962-review-platform905-featcrossplane-cloudsqlinstance": "PLAT-1962",
    "chore/PLAT-2039/force-create-service-agents": "PLAT-2039",
}


def write_brief(vault_root, slug, state="active", claims_block=None, title=""):
    """Materialize one stream in a scratch vault. Shape matches examples/streams/*/BRIEF.md."""
    stream_dir = Path(vault_root) / "streams" / slug
    stream_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "---",
        f"stream: {slug}",
        f"title: {title or slug}",
        f"state: {state}",
        "owner: test-agent",
        "updated: 2026-08-13T00:00:00Z",
        "claims:",
    ]
    block = claims_block or {}
    for kind in vault.CLAIM_KINDS:
        values = block.get(kind, [])
        lines.append(f"  {kind}: [{', '.join(str(v) for v in values)}]")
    lines += ["---", "", "## Goal", "", "test stream", "", "## Do not", "", "- nothing", ""]
    (stream_dir / "BRIEF.md").write_text("\n".join(lines), encoding="utf-8")
    return stream_dir


class ScratchVault(unittest.TestCase):
    """Base class: every test gets its own vault. Nothing here ever touches a real one."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mb-claims-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.vault = self.root / "vault"
        self.vault.mkdir()

    def add(self, slug, **kwargs):
        return write_brief(self.vault, slug, **kwargs)

    def streams(self):
        return vault.list_streams(self.vault)


class TestIssueKeyParsing(unittest.TestCase):
    def test_every_real_branch_convention(self):
        for branch, expected in REAL_BRANCHES.items():
            self.assertEqual(claims.parse_issue_keys(branch), [expected], branch)

    def test_case_insensitive_and_normalized_upward(self):
        for spelling in ("plat-1921", "PLAT-1921", "Plat-1921", "pLaT-1921"):
            self.assertEqual(claims.parse_issue_keys(spelling), ["PLAT-1921"])

    def test_multiple_keys_are_all_returned_in_order(self):
        keys = claims.parse_issue_keys("polds/plat-1962-review-of-PLAT-1719")
        self.assertEqual(keys, ["PLAT-1962", "PLAT-1719"])

    def test_repeated_key_is_deduped(self):
        self.assertEqual(claims.parse_issue_keys("PLAT-2039 and plat-2039"), ["PLAT-2039"])

    def test_no_key_yields_empty(self):
        for text in ("", None, "main", "polds/rename-the-thing"):
            self.assertEqual(claims.parse_issue_keys(text), [])

    def test_version_fragment_is_not_an_issue_key(self):
        """`v1-2-3` would otherwise parse as project V1, issue 2."""
        self.assertEqual(claims.parse_issue_keys("release/v1-2-3"), [])

    def test_trailing_glue_is_not_a_key(self):
        self.assertEqual(claims.parse_issue_keys("plat-1962b"), [])
        self.assertEqual(claims.parse_issue_keys("platform905"), [])

    def test_a_longer_leading_token_reads_as_a_longer_project_key(self):
        """`XPLAT-1719` is a legal key shape; nothing distinguishes it from a typo, so it parses."""
        self.assertEqual(claims.parse_issue_keys("xPLAT-1719"), ["XPLAT-1719"])


class TestNormalizeClaims(unittest.TestCase):
    def test_main_is_never_a_branch_claim(self):
        for spelling in ("main", "refs/heads/main", "master", "HEAD"):
            self.assertIsNone(claims.normalize_claim("branch", spelling), spelling)

    def test_ref_prefix_is_stripped_from_real_branches(self):
        self.assertEqual(
            claims.normalize_claim("branch", "refs/heads/feat/PLAT-1719/gclb"),
            "feat/PLAT-1719/gclb",
        )

    def test_repo_accepts_the_forms_git_and_gh_hand_back(self):
        for spelling in (
            "acme/platform",
            "acme/platform",
            "https://github.com/acme/platform",
            "git@github.com:acme/platform.git",
        ):
            self.assertEqual(claims.normalize_claim("repo", spelling), "acme/platform", spelling)

    def test_pr_accepts_number_hash_and_url(self):
        for spelling in ("905", "#905", "https://github.com/acme/platform/pull/905"):
            self.assertEqual(claims.normalize_claim("pr", spelling), "905", spelling)
        self.assertIsNone(claims.normalize_claim("pr", "not-a-number"))

    def test_issue_normalizes_case(self):
        self.assertEqual(claims.normalize_claim("issue", "plat-1962"), "PLAT-1962")
        self.assertIsNone(claims.normalize_claim("issue", "nope"))

    def test_worktree_resolves_symlinks_and_trailing_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            plain = claims.normalize_claim("worktree", tmp)
            trailing = claims.normalize_claim("worktree", tmp + "/")
            self.assertEqual(plain, trailing)
            self.assertEqual(plain, str(Path(tmp).resolve()))

    def test_unknown_kind_is_a_typo_guard(self):
        with self.assertRaises(claims.ClaimError):
            claims.normalize_claim("ticket", "PLAT-1")


class TestFindStreams(ScratchVault):
    def setUp(self):
        super().setUp()
        self.add("plat-1962-build", claims_block={"issue": ["PLAT-1962"], "pr": ["905"],
                                                  "repo": ["acme/platform"]})
        self.add("plat-1870-tests", claims_block={"issue": ["plat-1870"],
                                                  "branch": ["polds/plat-1870-integration-tests-speed"]})
        self.add("old-review", state="archived", claims_block={"pr": ["905"], "issue": ["PLAT-1962"]})

    def test_finds_by_pr(self):
        found = claims.find_streams(self.streams(), "pr", "#905")
        self.assertEqual([b.slug for b in found], ["plat-1962-build"])

    def test_archived_excluded_by_default_but_reachable_on_request(self):
        self.assertNotIn("old-review", [b.slug for b in claims.find_streams(self.streams(), "pr", 905)])
        both = claims.find_streams(self.streams(), "pr", 905, include_archived=True)
        self.assertEqual(sorted(b.slug for b in both), ["old-review", "plat-1962-build"])

    def test_issue_match_is_case_insensitive_both_ways(self):
        found = claims.find_streams(self.streams(), "issue", "PLAT-1870")
        self.assertEqual([b.slug for b in found], ["plat-1870-tests"])

    def test_repo_matches_on_bare_name_or_full_path(self):
        for spelling in ("platform", "acme/platform"):
            found = claims.find_streams(self.streams(), "repo", spelling)
            self.assertEqual([b.slug for b in found], ["plat-1962-build"], spelling)

    def test_pr_lookup_narrows_by_repo_but_keeps_repo_less_streams(self):
        self.add("other-repo-905", claims_block={"pr": ["905"], "repo": ["acme/acme-web"]})
        self.add("bare-905", claims_block={"pr": ["905"]})
        found = claims.find_streams_claiming_pr(self.streams(), 905, repo="platform")
        self.assertEqual(sorted(b.slug for b in found), ["bare-905", "plat-1962-build"])

    def test_pr_narrowing_never_narrows_to_nothing(self):
        """A repo hint that matches no claimant is a reason to show the ambiguity, not to hide it."""
        self.add("web-905", claims_block={"pr": ["905"], "repo": ["acme/acme-web"]})
        self.add("docs-905", claims_block={"pr": ["905"], "repo": ["acme/acme-docs"]})
        found = claims.find_streams_claiming_pr(self.streams(), 905, repo="unrelated/repo")
        self.assertEqual(sorted(b.slug for b in found), ["docs-905", "plat-1962-build", "web-905"])

    def test_fixture_vault_pr_905_is_archived_and_not_resolvable(self):
        streams = vault.list_streams(FIXTURE_VAULT)
        self.assertEqual(claims.find_streams(streams, "pr", 905), [])
        archived = claims.find_streams(streams, "pr", 905, include_archived=True)
        self.assertEqual([b.slug for b in archived], ["plat-1962-review-pr905"])


class TestWorktreeValidation(ScratchVault):
    def test_vanished_path_is_not_live(self):
        brief = vault.parse_brief(self.add("gone", claims_block={"worktree": ["/nope/gone"]}) / "BRIEF.md")
        self.assertFalse(claims.worktree_claim_is_live(brief, "/nope/gone", branch_at=lambda p: "x"))

    def test_existing_path_with_no_branch_claim_is_live(self):
        """Review and design streams have no branch at all; existence is the only check available."""
        wt = self.root / "wt-a"
        wt.mkdir()
        brief = vault.parse_brief(self.add("design", claims_block={"worktree": [str(wt)]}) / "BRIEF.md")
        self.assertTrue(claims.worktree_claim_is_live(brief, wt, branch_at=lambda p: None))

    def test_path_reused_by_a_different_branch_is_not_live(self):
        wt = self.root / "wt-b"
        wt.mkdir()
        brief = vault.parse_brief(
            self.add("build", claims_block={"worktree": [str(wt)], "branch": ["polds/plat-1870"]})
            / "BRIEF.md"
        )
        self.assertFalse(claims.worktree_claim_is_live(brief, wt, branch_at=lambda p: "polds/other"))
        self.assertTrue(claims.worktree_claim_is_live(brief, wt, branch_at=lambda p: "polds/plat-1870"))

    def test_detached_head_fails_validation(self):
        wt = self.root / "wt-c"
        wt.mkdir()
        brief = vault.parse_brief(
            self.add("detached", claims_block={"worktree": [str(wt)], "branch": ["polds/plat-1870"]})
            / "BRIEF.md"
        )
        self.assertFalse(claims.worktree_claim_is_live(brief, wt, branch_at=lambda p: "HEAD"))

    def test_git_branch_at_returns_none_outside_a_repo(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(claims.git_branch_at(tmp))


class TestResolveForRead(ScratchVault):
    def setUp(self):
        super().setUp()
        self.wt = self.root / "wt" / "plat-1870"
        self.wt.mkdir(parents=True)
        self.add(
            "plat-1870-tests",
            claims_block={
                "issue": ["PLAT-1870"],
                "branch": ["polds/plat-1870-integration-tests-speed"],
                "worktree": [str(self.wt)],
            },
        )
        self.add("plat-1921-scrape", claims_block={"issue": ["plat-1921"]})
        self.add("closed-work", state="archived", claims_block={"issue": ["PLAT-1921"],
                                                                "branch": ["polds/plat-1921"]})

    def on_branch(self, name):
        return lambda path: name

    def test_explicit_declaration_wins_over_a_conflicting_branch_hint(self):
        res = claims.resolve_for_read(
            self.streams(),
            claims.Hints(stream="plat-1921-scrape", branch="polds/plat-1870-integration-tests-speed"),
        )
        self.assertEqual(res.stream.slug, "plat-1921-scrape")
        self.assertEqual(res.via, "explicit")
        self.assertFalse(res.is_guess)

    def test_unknown_explicit_slug_stops_rather_than_falling_through_to_hints(self):
        res = claims.resolve_for_read(
            self.streams(),
            claims.Hints(stream="typo-slug", branch="polds/plat-1870-integration-tests-speed"),
        )
        self.assertFalse(res.resolved)
        self.assertIn("typo-slug", res.reason)

    def test_archived_stream_is_never_resolved_into(self):
        res = claims.resolve_for_read(self.streams(), claims.Hints(stream="closed-work"))
        self.assertFalse(res.resolved)
        self.assertIn("archived", res.reason)

    def test_validated_worktree_rung(self):
        res = claims.resolve_for_read(
            self.streams(),
            claims.Hints(worktree=str(self.wt)),
            branch_at=self.on_branch("polds/plat-1870-integration-tests-speed"),
        )
        self.assertEqual(res.via, "worktree")
        self.assertEqual(res.stream.slug, "plat-1870-tests")
        self.assertTrue(res.is_guess)

    def test_stale_worktree_falls_through_to_the_branch_rung(self):
        """The path was reused by another tool — a claim that no longer describes reality."""
        res = claims.resolve_for_read(
            self.streams(),
            claims.Hints(worktree=str(self.wt), branch="polds/plat-1870-integration-tests-speed"),
            branch_at=self.on_branch("polds/something-else"),
        )
        self.assertEqual(res.via, "branch")
        self.assertEqual(res.stream.slug, "plat-1870-tests")

    def test_issue_key_rung_parses_the_branch_name(self):
        res = claims.resolve_for_read(self.streams(), claims.Hints(branch="feat/PLAT-1921/gclb"))
        self.assertEqual(res.via, "issue")
        self.assertEqual(res.stream.slug, "plat-1921-scrape")

    def test_issue_rung_skips_archived_claimants(self):
        res = claims.resolve_for_read(self.streams(), claims.Hints(issue="PLAT-1921"))
        self.assertEqual(res.stream.slug, "plat-1921-scrape")

    def test_main_never_resolves_even_when_a_stream_claims_it(self):
        self.add("bad-claim", claims_block={"branch": ["main"]})
        res = claims.resolve_for_read(self.streams(), claims.Hints(branch="main"))
        self.assertEqual(res.via, "none")

    def test_no_hint_matches_yields_nothing_rather_than_a_guess(self):
        res = claims.resolve_for_read(self.streams(), claims.Hints(branch="polds/unrelated"))
        self.assertEqual(res.via, "none")
        self.assertFalse(res.resolved)
        self.assertEqual(res.candidates, [])

    def test_one_issue_spanning_eight_branches_returns_all_and_picks_none(self):
        """PLAT-1719 really does span eight live branches. Ambiguity is the normal case."""
        for n in range(8):
            self.add(f"plat-1719-part-{n}", claims_block={"issue": ["PLAT-1719"]})
        res = claims.resolve_for_read(self.streams(), claims.Hints(branch="feat/PLAT-1719/gclb"))
        self.assertTrue(res.ambiguous)
        self.assertIsNone(res.stream)
        self.assertEqual(len(res.candidates), 8)
        self.assertIn("declare one explicitly", res.reason)

    def test_ambiguity_at_a_rung_stops_the_ladder(self):
        """A weaker rung must not silently rescue a genuine multi-match."""
        self.add("dup-a", claims_block={"branch": ["polds/plat-1921-scrape"], "issue": ["PLAT-1921"]})
        self.add("dup-b", claims_block={"branch": ["polds/plat-1921-scrape"]})
        res = claims.resolve_for_read(self.streams(), claims.Hints(branch="polds/plat-1921-scrape"))
        self.assertEqual(res.via, "branch")
        self.assertTrue(res.ambiguous)
        self.assertEqual(sorted(b.slug for b in res.candidates), ["dup-a", "dup-b"])

    def test_read_resolution_is_never_write_safe_unless_explicit(self):
        hinted = claims.resolve_for_read(
            self.streams(), claims.Hints(branch="polds/plat-1870-integration-tests-speed")
        )
        self.assertTrue(hinted.resolved)
        self.assertFalse(hinted.write_safe)
        explicit = claims.resolve_for_read(self.streams(), claims.Hints(stream="plat-1870-tests"))
        self.assertTrue(explicit.write_safe)


class TestResolveForWrite(ScratchVault):
    def setUp(self):
        super().setUp()
        self.add("plat-1870-tests", claims_block={"branch": ["polds/plat-1870-integration-tests-speed"]})
        self.add("closed-work", state="archived")

    def test_explicit_active_stream_resolves(self):
        brief = claims.resolve_for_write(self.streams(), claims.Hints(stream="plat-1870-tests"))
        self.assertEqual(brief.slug, "plat-1870-tests")

    def test_unambiguous_branch_hint_is_still_refused(self):
        """The strongest possible hint. Still not intent — the same branch serves three jobs."""
        with self.assertRaises(claims.ClaimError) as ctx:
            claims.resolve_for_write(
                self.streams(), claims.Hints(branch="polds/plat-1870-integration-tests-speed")
            )
        self.assertIn("unassigned/", str(ctx.exception))

    def test_unknown_slug_lists_what_exists(self):
        with self.assertRaises(claims.ClaimError) as ctx:
            claims.resolve_for_write(self.streams(), claims.Hints(stream="nope"))
        self.assertIn("plat-1870-tests", str(ctx.exception))

    def test_archived_stream_is_not_writable(self):
        with self.assertRaises(claims.ClaimError) as ctx:
            claims.resolve_for_write(self.streams(), claims.Hints(stream="closed-work"))
        self.assertIn("archived", str(ctx.exception))


class TestSlugDerivation(unittest.TestCase):
    def test_matches_the_start_task_example(self):
        self.assertEqual(
            claims.derive_slug("Fix the eRPC scrape", issue="PLAT-192"),
            "plat-192-fix-erpc-scrape",
        )

    def test_ticket_in_the_task_text_leads_and_is_not_repeated(self):
        self.assertEqual(
            claims.derive_slug("PLAT-192: fix the eRPC scrape"),
            "plat-192-fix-erpc-scrape",
        )

    def test_lowercase_kebab_with_punctuation_stripped(self):
        self.assertEqual(claims.derive_slug("Speed up Integration Tests!"), "speed-up-integration-tests")

    def test_lowercases_a_ticket_given_in_upper_case_and_caps_word_count(self):
        slug = claims.derive_slug(
            "Review the CloudSQLInstance composite before the review window closes", issue="plat-1962"
        )
        self.assertTrue(slug.startswith("plat-1962-"))
        self.assertLessEqual(len(slug.split("-")), 6)  # ticket contributes two segments

    def test_branch_name_alone_derives_the_ticket_slug(self):
        self.assertEqual(claims.derive_slug(issue="polds/plat-1870-integration"), "plat-1870")

    def test_empty_input_refuses_rather_than_inventing(self):
        with self.assertRaises(claims.ClaimError):
            claims.derive_slug("   ")

    def test_unique_slug_appends_a_discriminator(self):
        self.assertEqual(claims.ensure_unique_slug("plat-192-fix", ["other"]), "plat-192-fix")
        self.assertEqual(claims.ensure_unique_slug("plat-192-fix", ["plat-192-fix"]), "plat-192-fix-2")
        self.assertEqual(
            claims.ensure_unique_slug("plat-192-fix", ["plat-192-fix", "plat-192-fix-2"]),
            "plat-192-fix-3",
        )


class TestJoinOrMint(ScratchVault):
    def setUp(self):
        super().setUp()
        self.add(
            "plat-1962-build",
            claims_block={"issue": ["PLAT-1962"], "pr": ["905"], "repo": ["acme/platform"]},
        )

    def test_existing_pr_claim_is_joined_never_duplicated(self):
        decision = claims.join_or_mint(self.streams(), claims.Hints(pr="#905"), task="review the PR")
        self.assertEqual(decision.action, "join")
        self.assertEqual(decision.stream.slug, "plat-1962-build")

    def test_existing_issue_claim_is_joined_never_duplicated(self):
        decision = claims.join_or_mint(
            self.streams(), claims.Hints(branch="polds/plat-1962-fixups"), task="fix it"
        )
        self.assertEqual(decision.action, "join")
        self.assertEqual(decision.stream.slug, "plat-1962-build")

    def test_explicit_slug_joins_when_it_exists(self):
        decision = claims.join_or_mint(self.streams(), claims.Hints(stream="plat-1962-build"))
        self.assertEqual(decision.action, "join")

    def test_issue_spanning_many_streams_refuses_to_pick_or_mint(self):
        for n in range(8):
            self.add(f"plat-1719-part-{n}", claims_block={"issue": ["PLAT-1719"]})
        decision = claims.join_or_mint(
            self.streams(), claims.Hints(branch="feat/PLAT-1719/gclb"), task="add the gclb"
        )
        self.assertEqual(decision.action, "ambiguous")
        self.assertIsNone(decision.stream)
        self.assertEqual(len(decision.candidates), 8)
        self.assertEqual(decision.slug, "")

    def test_two_streams_on_one_pr_is_ambiguous_not_a_join(self):
        self.add("second-look", claims_block={"pr": ["905"], "repo": ["acme/platform"]})
        decision = claims.join_or_mint(self.streams(), claims.Hints(pr=905, repo="platform"))
        self.assertEqual(decision.action, "ambiguous")
        self.assertEqual(len(decision.candidates), 2)

    def test_no_durable_claim_mints_with_a_derived_slug(self):
        decision = claims.join_or_mint(
            self.streams(), claims.Hints(branch="polds/plat-2039-service-agents"),
            task="Force create service agents",
        )
        self.assertEqual(decision.action, "mint")
        self.assertEqual(decision.slug, "plat-2039-force-create-service-agents")

    def test_mint_slug_collision_gets_a_discriminator(self):
        self.add("plat-2039-force-create-service-agents")
        decision = claims.join_or_mint(
            self.streams(), claims.Hints(issue="PLAT-2039"), task="Force create service agents"
        )
        self.assertEqual(decision.action, "mint")
        self.assertEqual(decision.slug, "plat-2039-force-create-service-agents-2")

    def test_branch_and_worktree_matches_are_surfaced_but_never_auto_joined(self):
        """A reviewer on the author's branch must not be merged into the author's stream."""
        wt = self.root / "wt-shared"
        wt.mkdir()
        self.add("author-stream", claims_block={"branch": ["polds/refactor-the-thing"],
                                                "worktree": [str(wt)]})
        decision = claims.join_or_mint(
            self.streams(),
            claims.Hints(branch="polds/refactor-the-thing", worktree=str(wt)),
            task="Review the refactor",
        )
        self.assertEqual(decision.action, "mint")
        self.assertEqual([b.slug for b in decision.hint_candidates], ["author-stream"])
        self.assertEqual(decision.slug, "review-refactor")

    def test_archived_claimant_does_not_swallow_new_work_on_the_same_pr(self):
        """The fixture case: a closed review of PR 905 whose conclusions are final."""
        streams = vault.list_streams(FIXTURE_VAULT)
        decision = claims.join_or_mint(
            streams, claims.Hints(pr=905, repo="acme/platform"), task="Reopen the CloudSQL work"
        )
        self.assertEqual(decision.action, "mint")
        self.assertNotEqual(decision.slug, "plat-1962-review-pr905")

    def test_explicit_slug_with_no_existing_stream_mints_that_exact_slug(self):
        decision = claims.join_or_mint(self.streams(), claims.Hints(stream="plat-2039-agents"))
        self.assertEqual(decision.action, "mint")
        self.assertEqual(decision.slug, "plat-2039-agents")


class TestClaimsFromHints(unittest.TestCase):
    def test_registers_every_kind_and_drops_non_claimable_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            built = claims.claims_from_hints(
                claims.Hints(
                    repo="git@github.com:acme/platform.git",
                    branch="main",
                    worktree=tmp,
                    pr="#905",
                    issue="plat-1962",
                )
            )
        self.assertEqual(sorted(built), sorted(vault.CLAIM_KINDS))
        self.assertEqual(built["branch"], [])  # main is never a claim
        self.assertEqual(built["repo"], ["acme/platform"])
        self.assertEqual(built["pr"], ["905"])
        self.assertEqual(built["issue"], ["PLAT-1962"])
        self.assertEqual(built["worktree"], [str(Path(tmp).resolve())])

    def test_issue_falls_back_to_the_branch_name(self):
        built = claims.claims_from_hints(claims.Hints(branch="feat/PLAT-1719/gclb"))
        self.assertEqual(built["issue"], ["PLAT-1719"])
        self.assertEqual(built["branch"], ["feat/PLAT-1719/gclb"])


if __name__ == "__main__":
    unittest.main()
