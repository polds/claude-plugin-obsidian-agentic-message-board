"""Doctor verification.

Written after a real failure: the board was fully built, fully tested, and doing nothing, because
hooks run in non-interactive shells that never source ~/.zshrc. Nothing errored. These tests pin the
distinction between "quiet because empty" and "quiet because broken".
"""

import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from plugin.lib import doctor, vault  # noqa: E402

FIXTURES = REPO / "examples"


class Project(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.project = Path(self.tmp.name) / "repo"
        (self.project / ".claude").mkdir(parents=True)
        self.vault = Path(self.tmp.name) / "vault"
        shutil.copytree(FIXTURES / "streams", self.vault / "streams")
        self.addCleanup(self.tmp.cleanup)

    def write_settings(self, cfg: dict):
        (self.project / ".claude" / "settings.json").write_text(json.dumps(cfg))

    def wired(self) -> dict:
        return {
            "env": {vault.VAULT_ENV: str(self.vault)},
            "hooks": {
                ev: [{"hooks": [{"command": f'python3 "$X/plugin/hooks/{f}.py"'}]}]
                for ev, f in (("SessionStart", "session_start"), ("SessionEnd", "session_end"))
            },
        }

    def add_skills(self, *names):
        for name in names:
            d = self.project / ".claude" / "skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text("---\nname: x\n---\n")


# A non-interactive shell that has never heard of the vault. Supplied explicitly because the real
# probe inherits this process's environment, and the operator running the suite very likely *has*
# exported the vault path — which would quietly turn the two failure cases below into passes and
# leave the check that matters most untested on exactly the machines it was written for.
BLIND_SHELL = lambda: ""  # noqa: E731


class TestEnvReachesHooks(Project):
    def test_env_only_in_the_users_terminal_is_reported_as_broken(self):
        """The exact real failure: exported in ~/.zshrc, invisible to every hook."""
        self.write_settings({"hooks": {}})
        check = doctor.check_env_reaches_hooks(
            self.project, {vault.VAULT_ENV: "/some/vault"}, BLIND_SHELL
        )
        self.assertEqual(check.status, doctor.BAD)
        self.assertIn("non-interactive", check.detail)
        self.assertIn("settings.json", check.fix)

    def test_env_in_settings_is_healthy(self):
        self.write_settings(self.wired())
        self.assertEqual(
            doctor.check_env_reaches_hooks(self.project, {}, BLIND_SHELL).status, doctor.OK
        )

    def test_unset_everywhere_is_reported(self):
        self.write_settings({"hooks": {}})
        self.assertEqual(
            doctor.check_env_reaches_hooks(self.project, {}, BLIND_SHELL).status, doctor.BAD
        )

    def test_a_shell_that_can_see_the_path_is_healthy_without_settings(self):
        """Paired with the two failures above: the check must be able to pass, not just fail."""
        self.write_settings({"hooks": {}})
        check = doctor.check_env_reaches_hooks(self.project, {}, lambda: "/some/vault")
        self.assertEqual(check.status, doctor.OK)
        self.assertIn("non-interactive", check.detail)


class TestEffectiveEnv(Project):
    def test_settings_env_is_what_hooks_will_see(self):
        self.write_settings(self.wired())
        merged = doctor.effective_env(self.project, {})
        self.assertEqual(merged[vault.VAULT_ENV], str(self.vault))


class TestChecks(Project):
    def test_missing_skills_is_a_failure_not_a_warning(self):
        """Without discoverable skills nothing can ever write a judgment to the board."""
        self.write_settings(self.wired())
        check = doctor.check_skills(self.project)
        self.assertEqual(check.status, doctor.BAD)
        self.assertIn("brief-write", check.detail)

    def test_present_skills_pass(self):
        self.add_skills("brief-read", "brief-write", "inbox-append")
        self.assertEqual(doctor.check_skills(self.project).status, doctor.OK)

    def test_missing_hooks_reported_by_name(self):
        self.write_settings({"hooks": {}})
        check = doctor.check_hooks(self.project)
        self.assertEqual(check.status, doctor.BAD)
        self.assertIn("SessionEnd", check.detail)

    def test_non_git_vault_warns_but_does_not_fail(self):
        check = doctor.check_vault({vault.VAULT_ENV: str(self.vault)})
        self.assertEqual(check.status, doctor.WARN)
        self.assertIn("NOT a git repo", check.detail)

    def test_git_vault_is_ok_and_counts_streams(self):
        (self.vault / ".git").mkdir()
        check = doctor.check_vault({vault.VAULT_ENV: str(self.vault)})
        self.assertEqual(check.status, doctor.OK)
        self.assertIn("2 stream(s)", check.detail)


class TestVerdict(Project):
    def test_fully_wired_says_quiet_means_empty(self):
        (self.vault / ".git").mkdir()
        self.write_settings(self.wired())
        self.add_skills("brief-read", "brief-write", "inbox-append")
        out = doctor.render(doctor.run(self.project, {}, BLIND_SHELL))
        self.assertIn("Board is wired", out)
        self.assertNotIn("FAIL", out)

    def test_broken_says_silence_is_misconfiguration(self):
        self.write_settings({"hooks": {}})
        out = doctor.render(doctor.run(self.project, {}, BLIND_SHELL))
        self.assertIn("NOT working", out)
        self.assertIn("misconfiguration", out)

    def test_exit_code_is_nonzero_when_broken(self):
        self.write_settings({"hooks": {}})
        with contextlib.redirect_stdout(io.StringIO()):
            code = doctor.main([str(self.project)])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
