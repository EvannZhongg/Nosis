import tempfile
import unittest
from pathlib import Path

from agent_core import (
    ReadSkillTool,
    Session,
    Skill,
    SkillRegistry,
    ToolExecutionContext,
    Workspace,
    builtin_catalog,
)
from agent_core.prompting import render_system_prompt


def write_skill(
    root: Path,
    directory: str,
    *,
    name: str = "demo-skill",
    description: str = "Does the demo task.",
    metadata: str = "",
    body: str = "# Demo\n\nFollow these instructions.",
) -> Path:
    skill_directory = root / directory
    skill_directory.mkdir(parents=True)
    skill_path = skill_directory / "SKILL.md"
    skill_path.write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{metadata}"
        "---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return skill_path


class SkillRegistryTest(unittest.TestCase):
    def test_discovers_skill_metadata_without_loading_body_into_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(
                root,
                "demo",
                name="Demo Skill",
                description="Use this for demonstrations.",
                body="SECRET DETAILED INSTRUCTIONS",
            )

            registry = SkillRegistry.discover(root)
            prompt = registry.prompt_section()

        self.assertEqual(registry.names, ("Demo Skill",))
        self.assertIn("Demo Skill", prompt)
        self.assertIn("Use this for demonstrations.", prompt)
        self.assertNotIn("SECRET DETAILED INSTRUCTIONS", prompt)

    def test_metadata_can_disable_registration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_skill(
                root,
                "disabled",
                metadata="metadata: {nosis: {register: false}}\n",
            )

            registry = SkillRegistry.discover(root)

        self.assertFalse(registry)
        self.assertEqual(registry.names, ())

    def test_missing_directory_produces_an_empty_registry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = SkillRegistry.discover(Path(directory) / "missing")

        self.assertFalse(registry)

    def test_discovery_keeps_first_duplicate_name_and_warns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = write_skill(
                root, "a-first", name="same", description="First."
            )
            second = write_skill(
                root, "b-second", name="same", description="Second."
            )

            registry = SkillRegistry.discover(root)

        self.assertEqual(registry.names, ("same",))
        self.assertEqual(registry.get("same").directory, first.parent.resolve())
        self.assertNotEqual(registry.get("same").directory, second.parent.resolve())
        self.assertEqual(len(registry.warnings), 1)
        self.assertIn("Skipping duplicate skill 'same'", registry.warnings[0])

    def test_explicit_registry_still_rejects_duplicate_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = (
                Skill("same", "First.", root / "first"),
                Skill("same", "Second.", root / "second"),
            )

            with self.assertRaisesRegex(ValueError, "already registered"):
                SkillRegistry(skills)

    def test_discovery_skips_invalid_skills_and_keeps_valid_ones(self) -> None:
        invalid_contents = {
            "missing-front-matter": "# No front matter\n",
            "invalid-yaml": "---\nname: [broken\n---\n",
            "invalid-name": (
                "---\nname: 123\ndescription: Invalid name.\n---\n"
            ),
            "invalid-metadata": (
                "---\nname: invalid-metadata\n"
                "description: Invalid metadata.\nmetadata: []\n---\n"
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, content in invalid_contents.items():
                skill_directory = root / name
                skill_directory.mkdir()
                (skill_directory / "SKILL.md").write_text(
                    content, encoding="utf-8"
                )
            write_skill(root, "valid", name="valid-skill")

            registry = SkillRegistry.discover(root)

        self.assertEqual(registry.names, ("valid-skill",))
        self.assertEqual(len(registry.warnings), len(invalid_contents))
        for directory_name in invalid_contents:
            self.assertTrue(
                any(directory_name in item for item in registry.warnings),
                directory_name,
            )

    def test_system_prompt_includes_registered_skill_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            write_skill(skills, "demo")

            prompt = render_system_prompt(
                "System for {{workspace}}",
                Workspace(root),
                skills=SkillRegistry.discover(skills),
            )

        self.assertIn("## Available Skills", prompt)
        self.assertIn("demo-skill: Does the demo task.", prompt)


class ReadSkillToolTest(unittest.TestCase):
    def context(self, root: Path, registry: SkillRegistry):
        return ToolExecutionContext(
            workspace=Workspace(root),
            session=Session(),
            skills=registry,
        )

    def test_reads_skill_and_referenced_text_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            skill_path = write_skill(skills, "demo")
            reference = skill_path.parent / "references" / "details.md"
            reference.parent.mkdir()
            reference.write_text("details", encoding="utf-8")
            registry = SkillRegistry.discover(skills)
            tool = ReadSkillTool()
            context = self.context(root, registry)

            default = tool.execute({"name": "demo-skill"}, context)
            detail = tool.execute(
                {"name": "demo-skill", "path": "references/details.md"},
                context,
            )

        self.assertIn("# Demo", default["content"])
        self.assertIn("1| details", detail["content"])

    def test_reads_skill_in_line_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            write_skill(
                skills,
                "demo",
                body="first\nsecond\nthird\nfourth",
            )
            context = self.context(root, SkillRegistry.discover(skills))

            result = ReadSkillTool().execute(
                {
                    "name": "demo-skill",
                    "offset": 7,
                    "limit": 2,
                },
                context,
            )

        self.assertIn("7| second", result["content"])
        self.assertIn("8| third", result["content"])
        self.assertNotIn("fourth", result["content"])
        self.assertIn("Use offset=9 to continue", result["content"])

    def test_rejects_invalid_line_ranges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            write_skill(skills, "demo")
            context = self.context(root, SkillRegistry.discover(skills))

            for arguments in (
                {"name": "demo-skill", "offset": 0},
                {"name": "demo-skill", "limit": True},
            ):
                with self.subTest(arguments=arguments):
                    with self.assertRaisesRegex(
                        ValueError, "positive integer"
                    ):
                        ReadSkillTool().execute(arguments, context)

    def test_rejects_paths_outside_the_skill_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            write_skill(skills, "demo")
            (skills / "outside.txt").write_text("outside", encoding="utf-8")
            context = self.context(root, SkillRegistry.discover(skills))

            with self.assertRaisesRegex(ValueError, "inside the skill"):
                ReadSkillTool().execute(
                    {"name": "demo-skill", "path": "../outside.txt"},
                    context,
                )

    def test_rejects_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            skills = root / "skills"
            skill_path = write_skill(skills, "demo")
            context = self.context(root, SkillRegistry.discover(skills))

            with self.assertRaisesRegex(ValueError, "must be relative"):
                ReadSkillTool().execute(
                    {"name": "demo-skill", "path": str(skill_path)},
                    context,
                )

    def test_catalog_exposes_tool_only_when_a_skill_is_registered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            empty = self.context(root, SkillRegistry())
            self.assertEqual(
                builtin_catalog().select(("read_skill",), empty).definitions,
                (),
            )

            skills = root / "skills"
            write_skill(skills, "demo")
            populated = self.context(root, SkillRegistry.discover(skills))
            definitions = builtin_catalog().select(
                ("read_skill",), populated
            ).definitions

        self.assertEqual([item.name for item in definitions], ["read_skill"])
        self.assertEqual(
            definitions[0].parameters["properties"]["name"]["enum"],
            ["demo-skill"],
        )
        self.assertEqual(
            definitions[0].parameters["properties"]["offset"]["default"],
            1,
        )
        self.assertEqual(
            definitions[0].parameters["properties"]["limit"]["default"],
            2000,
        )


if __name__ == "__main__":
    unittest.main()
