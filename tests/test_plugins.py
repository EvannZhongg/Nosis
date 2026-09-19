import json
import tempfile
import unittest
from pathlib import Path

from agent_core import SkillLoader
from interfaces.bridge.plugins import PluginLoader, PluginManager


def write_manifest(root: Path, name: str, **fields: object) -> Path:
    plugin = root / name
    plugin.mkdir(parents=True)
    manifest = plugin / "plugin.json"
    manifest.write_text(
        json.dumps({"name": name, **fields}),
        encoding="utf-8",
    )
    return manifest


def write_skill(root: Path, name: str = "shared") -> None:
    skill = root / "skills" / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: Skill from {root.name}.\n"
        "---\n",
        encoding="utf-8",
    )


class PluginLoaderTest(unittest.TestCase):
    def test_loads_declarative_metadata_and_component_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = write_manifest(
                root,
                "example",
                version="1.2.0",
                description="Example package",
                dependencies=["base"],
                capabilities=["skills", "mcp"],
                components={
                    "skills": ["skills"],
                    "mcp": ["mcp/server.json"],
                    "tools": ["tools/tool.py"],
                    "agents": ["agents/researcher.json"],
                    "hooks": ["hooks/on_start.py"],
                },
            )

            plugin = PluginLoader().load(manifest)

        self.assertEqual(plugin.name, "example")
        self.assertEqual(plugin.version, "1.2.0")
        self.assertEqual(plugin.dependencies, ("base",))
        self.assertEqual(plugin.capabilities, ("skills", "mcp"))
        self.assertEqual(plugin.components.skills, (plugin.root / "skills",))
        self.assertEqual(plugin.components.mcp, (plugin.root / "mcp/server.json",))
        self.assertEqual(plugin.components.tools, (plugin.root / "tools/tool.py",))
        self.assertEqual(
            plugin.components.agents,
            (plugin.root / "agents/researcher.json",),
        )
        self.assertEqual(
            plugin.components.hooks,
            (plugin.root / "hooks/on_start.py",),
        )

    def test_rejects_component_references_outside_the_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manifest = write_manifest(
                Path(directory),
                "example",
                components={"skills": ["../shared-skills"]},
            )

            with self.assertRaisesRegex(ValueError, "stay inside"):
                PluginLoader().load(manifest)


class PluginManagerTest(unittest.TestCase):
    def test_warns_when_a_declared_skill_source_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_manifest(
                root,
                "missing-skills",
                components={"skills": ["skills"]},
            )

            manager = PluginManager.discover(root)
            skills = SkillLoader().load(
                manager.skill_sources(),
                warnings=manager.warnings,
            )

        self.assertEqual(skills.names, ())
        self.assertEqual(len(skills.warnings), 1)
        self.assertIn("Skipping Skill component", skills.warnings[0])
        self.assertIn("directory does not exist", skills.warnings[0])

    def test_warns_and_skips_missing_or_invalid_mcp_components(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            missing = write_manifest(
                root,
                "missing-mcp",
                components={"mcp": ["missing.json"]},
            )
            invalid = write_manifest(
                root,
                "invalid-mcp",
                components={"mcp": [".mcp.json"]},
            )
            (invalid.parent / ".mcp.json").write_text(
                "not json",
                encoding="utf-8",
            )

            servers, warnings = PluginManager.discover(root).load_mcp_servers()

        self.assertEqual(servers, ())
        self.assertEqual(len(warnings), 2)
        self.assertTrue(
            any(
                "Skipping MCP component for plugin 'missing-mcp'" in item
                for item in warnings
            )
        )
        self.assertTrue(
            any(
                "Skipping MCP component for plugin 'invalid-mcp'" in item
                for item in warnings
            )
        )

    def test_loads_enabled_mcp_components_with_plugin_namespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enabled = write_manifest(
                root,
                "enabled",
                components={"mcp": [".mcp.json"]},
            )
            (enabled.parent / ".mcp.json").write_text(
                json.dumps(
                    {
                        "demo": {
                            "type": "stdio",
                            "command": "python",
                        }
                    }
                ),
                encoding="utf-8",
            )
            disabled = write_manifest(
                root,
                "disabled",
                enabled=False,
                components={"mcp": [".mcp.json"]},
            )
            (disabled.parent / ".mcp.json").write_text(
                "not json",
                encoding="utf-8",
            )

            servers, warnings = PluginManager.discover(root).load_mcp_servers()

        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].identifier, "enabled:demo")
        self.assertEqual(
            servers[0].cwd,
            str(enabled.parent.resolve()),
        )
        self.assertEqual(warnings, ())

    def test_discovers_plugins_and_registers_only_enabled_skill_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            enabled_manifest = write_manifest(
                root,
                "enabled",
                components={"skills": ["skills"]},
            )
            write_skill(enabled_manifest.parent)
            disabled_manifest = write_manifest(
                root,
                "disabled",
                enabled=False,
                components={"skills": ["skills"]},
            )
            write_skill(disabled_manifest.parent)

            manager = PluginManager.discover(root)
            skills = SkillLoader().load(manager.skill_sources())

        self.assertEqual(
            tuple(plugin.name for plugin in manager.plugins),
            ("disabled", "enabled"),
        )
        self.assertEqual(
            tuple(plugin.name for plugin in manager.enabled_plugins),
            ("enabled",),
        )
        self.assertEqual(skills.names, ("enabled:shared",))
        self.assertEqual(skills.get("enabled:shared").namespace, "enabled")

    def test_skips_invalid_manifests_with_a_discovery_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = write_manifest(root, "valid")
            invalid = root / "invalid"
            invalid.mkdir()
            (invalid / "plugin.json").write_text("not json", encoding="utf-8")

            manager = PluginManager.discover(root)

        self.assertEqual(manager.plugins[0].root, valid.parent.resolve())
        self.assertEqual(len(manager.warnings), 1)
        self.assertIn("Skipping plugin", manager.warnings[0])


if __name__ == "__main__":
    unittest.main()
