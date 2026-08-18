"""The plugin's packaging surface, kept honest by tests.

Skills are prose and nothing executes them; manifests are JSON and nothing parses them until a user
installs the plugin. Both fail silently on a green suite unless something asserts their shape, so
this module is the suite's stake in the install story: manifests parse, versions agree, hook sources
compile, and every skill carries the frontmatter that decides whether it fires at all.
"""

import json
import py_compile
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_MANIFEST = REPO_ROOT / ".claude-plugin" / "plugin.json"
HOOKS_CONFIG = REPO_ROOT / ".claude-plugin" / "hooks.json"
SKILLS_DIR = REPO_ROOT / "plugin" / "skills"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _frontmatter(skill_md: Path) -> dict:
    """Parse the YAML frontmatter block without a YAML dependency.

    Only flat `key: value` lines are read, which is all SKILL.md frontmatter uses.
    """
    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    end = text.index("\n---", 4)
    fields = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()
    return fields


class TestPluginManifest(unittest.TestCase):
    def test_manifest_parses_with_required_fields(self):
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        for field in ("name", "version", "description"):
            self.assertIn(field, manifest)
            self.assertTrue(manifest[field], f"{field} must be non-empty")

    def test_version_is_semver(self):
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        self.assertRegex(manifest["version"], SEMVER)

    def test_changelog_has_a_section_for_the_manifest_version(self):
        # The release workflow publishes the changelog section matching the tagged
        # version, so a manifest version with no section is a release that will fail.
        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        changelog = CHANGELOG.read_text(encoding="utf-8")
        self.assertIn(f"## [{manifest['version']}]", changelog)


class TestHooksConfig(unittest.TestCase):
    def test_hooks_config_parses_and_registers_both_session_hooks(self):
        config = json.loads(HOOKS_CONFIG.read_text(encoding="utf-8"))
        self.assertIn("SessionStart", config["hooks"])
        self.assertIn("SessionEnd", config["hooks"])

    def test_hook_sources_compile(self):
        hook_sources = sorted((REPO_ROOT / "plugin" / "hooks").glob("*.py"))
        self.assertTrue(hook_sources, "no hook sources found")
        for source in hook_sources:
            py_compile.compile(str(source), doraise=True)


class TestSkillFrontmatter(unittest.TestCase):
    def test_every_skill_has_frontmatter_with_name_and_description(self):
        skill_docs = sorted(SKILLS_DIR.glob("*/SKILL.md"))
        self.assertTrue(skill_docs, "no skills found")
        for doc in skill_docs:
            fields = _frontmatter(doc)
            self.assertEqual(
                fields.get("name"), doc.parent.name,
                f"{doc}: frontmatter name must match the skill directory",
            )
            self.assertTrue(fields.get("description"), f"{doc}: description is the retrieval key")

    def test_every_description_states_what_it_is_not_for(self):
        # CLAUDE.md: the description is a retrieval key, and stating what a skill is
        # NOT for is what keeps the wrong skill from firing.
        for doc in sorted(SKILLS_DIR.glob("*/SKILL.md")):
            description = _frontmatter(doc).get("description", "")
            self.assertIn("NOT", description, f"{doc}: description never says what it is not for")


class TestChangelogExtraction(unittest.TestCase):
    def test_release_notes_extraction_round_trips(self):
        import importlib.util

        script = REPO_ROOT / ".github" / "scripts" / "extract_changelog.py"
        spec = importlib.util.spec_from_file_location("extract_changelog", script)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        manifest = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8"))
        notes = module.extract(CHANGELOG.read_text(encoding="utf-8"), manifest["version"])
        self.assertTrue(notes.strip(), "release notes for the current version are empty")

        with self.assertRaises(SystemExit):
            module.extract(CHANGELOG.read_text(encoding="utf-8"), "999.999.999")


if __name__ == "__main__":
    unittest.main()
