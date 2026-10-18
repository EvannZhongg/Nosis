"""Characterize ordering from capability discovery to model-visible schemas."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_core import (
    AgentConfig,
    DirectorySkillSource,
    Session,
    SkillLoader,
    ToolExecutionContext,
    Workspace,
    builtin_catalog,
)
from agent_core.config import SkillConfig
from agent_core.llm import LLMRequest
from agent_core.mcp.config import load_mcp_config
from agent_core.mcp.manager import McpClientManager
from agent_core.mcp.tool import McpTool
from agent_core.providers.base import _request_tools
from agent_core.tools.config import TOOL_NAMES, load_tool_config
from agent_runtime.config import configured_skill_sources
from agent_runtime.plugins import PluginManager
from tests.test_plugins import write_manifest
from tests.test_skills import write_skill


class RegistrationOrderTest(unittest.TestCase):
    def test_tool_config_uses_canonical_order_regardless_of_key_order(self):
        for names in (TOOL_NAMES, tuple(reversed(TOOL_NAMES))):
            with self.subTest(names=names):
                config = load_tool_config(dict.fromkeys(names, True))
                self.assertEqual(config.enabled, TOOL_NAMES)

    def test_tool_selection_and_provider_preserve_requested_order(self):
        context = ToolExecutionContext(Workspace(Path.cwd()), Session())
        catalog = builtin_catalog()
        requested = ("list_directory", "read_file", "search_files")
        self.assertNotEqual(catalog.names[:3], requested)
        for names in (requested, tuple(reversed(requested))):
            with self.subTest(names=names):
                tools = catalog.select(
                    (*names, "unknown", "read_skill", names[0]), context,
                )
                request = LLMRequest("system", (), tools.definitions)
                self.assertEqual(
                    tuple(
                        tool["function"]["name"]
                        for tool in _request_tools(request)
                    ),
                    names,
                )

    def test_skill_sources_preserve_config_order_and_first_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_root, workspace = root / "config", root / "workspace"
            first = write_skill(config_root / "z-root", "z-dir", name="shared")
            write_skill(config_root / "a-root", "a-dir", name="global-second")
            write_skill(workspace / "skills", "a-dir", name="shared")
            write_skill(workspace / "skills", "z-dir", name="workspace-last")
            config = AgentConfig(
                max_same_tool_calls=3,
                output_reserve_tokens=1000,
                tools=load_tool_config({}),
                workspace_instruction_files=(),
                skills=SkillConfig(
                    global_paths=("z-root", "a-root", "z-root"),
                    workspace_paths=("skills",),
                ),
            )
            sources = configured_skill_sources(config_root, config, workspace)
            self.assertEqual(
                tuple(source.directory for source in sources),
                (
                    config_root / "z-root",
                    config_root / "a-root",
                    workspace / "skills",
                ),
            )
            registry = SkillLoader().load(sources)
            self.assertEqual(
                registry.names, ("shared", "global-second", "workspace-last"),
            )
            self.assertEqual(registry.get("shared").directory, first.parent.resolve())
            self.assertEqual(len(registry.warnings), 1)

    def test_filesystem_enumeration_does_not_change_skill_prompt_or_enum(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            standalone = root / "skills"
            write_skill(standalone, "z-dir", name="alpha")
            write_skill(standalone, "a-dir", name="zeta")
            plugins = root / "plugins"
            for folder, name in (
                ("z-folder", "alpha-plugin"), ("a-folder", "zeta-plugin"),
            ):
                manifest = write_manifest(
                    plugins, folder,
                    components={"skills": ["z-skills", "a-skills"]},
                )
                # Discovery is ordered by directory, not by the manifest's name.
                data = json.loads(manifest.read_text(encoding="utf-8"))
                data["name"] = name
                manifest.write_text(json.dumps(data), encoding="utf-8")
                write_skill(manifest.parent / "z-skills", "z-dir", name="alpha")
                write_skill(manifest.parent / "z-skills", "a-dir", name="zeta")
                write_skill(manifest.parent / "a-skills", "a-dir", name="last")

            expected = (
                "zeta", "alpha",
                "zeta-plugin:zeta", "zeta-plugin:alpha", "zeta-plugin:last",
                "alpha-plugin:zeta", "alpha-plugin:alpha", "alpha-plugin:last",
            )
            snapshots = []
            iterdir = Path.iterdir
            for reverse in (False, True):
                def enumerate_children(path):
                    return iter(sorted(
                        iterdir(path), key=lambda child: child.name,
                        reverse=reverse,
                    ))

                with patch.object(Path, "iterdir", enumerate_children):
                    manager = PluginManager.discover(plugins)
                    registry = SkillLoader().load((
                        DirectorySkillSource(standalone),
                        *manager.skill_sources(),
                    ))
                self.assertEqual(registry.names, expected)
                context = ToolExecutionContext(
                    Workspace(root), Session(), skills=registry,
                )
                definition, = builtin_catalog().select(
                    ("read_skill",), context,
                ).definitions
                self.assertEqual(
                    definition.parameters["properties"]["name"]["enum"],
                    list(expected),
                )
                entries = tuple(
                    line.split(": ", 1)[0][2:]
                    for line in registry.prompt_section().splitlines()
                    if line.startswith("- ")
                )
                self.assertEqual(entries, expected)
                snapshots.append((registry.prompt_section(), definition))
            self.assertEqual(*snapshots)

    def test_mcp_connection_completion_does_not_change_server_order(self):
        for server_names in (("z-server", "a-server"), ("a-server", "z-server")):
            with self.subTest(server_names=server_names):
                config = load_mcp_config({"enabled": True, "servers": {
                    name: {"command": "unused"} for name in server_names
                }})
                manager = McpClientManager(config, Path.cwd())
                released = asyncio.Event()
                completed = []

                async def start_server(stack, server):
                    if server.identifier == server_names[0]:
                        await released.wait()
                    completed.append(server.identifier)
                    released.set()
                    return object(), tuple(
                        McpTool(server.identifier, name, "demo", {"type": "object"})
                        for name in ("z-tool", "a-tool")
                    )

                with patch.object(manager, "_start_server", start_server):
                    try:
                        manager.start()
                        tools = manager.wait_ready()
                        self.assertEqual(completed, list(reversed(server_names)))
                        expected = tuple(
                            f"mcp__{server}__{tool}"
                            for server in server_names for tool in ("z-tool", "a-tool")
                        )
                        self.assertEqual(manager.tool_names, expected)
                        context = ToolExecutionContext(
                            Workspace(Path.cwd()), Session(), mcp=manager,
                        )
                        definitions = builtin_catalog().extend(tools).select(
                            manager.tool_names, context,
                        ).definitions
                        self.assertEqual(
                            tuple(tool.name for tool in definitions), expected,
                        )
                    finally:
                        manager.close()

    def test_mcp_remote_page_order_does_not_change_model_visible_tools(self):
        config = load_mcp_config({"enabled": True, "servers": {
            "demo": {"command": "unused", "tools": {"enabled": ["alpha", "zeta"]}},
        }})
        manager = McpClientManager(config, Path.cwd())
        snapshots = []
        for pages in (
            (("zeta", "hidden"), ("alpha",)),
            (("alpha",), ("hidden", "zeta")),
            (("zeta", "alpha", "hidden"),),
        ):
            session = SimpleNamespace(list_tools=AsyncMock(side_effect=[
                SimpleNamespace(tools=[SimpleNamespace(
                    name=name, description="demo", input_schema={"type": "object"},
                ) for name in names], next_cursor=(
                    str(index + 1) if index < len(pages) - 1 else None
                ))
                for index, names in enumerate(pages)
            ]))
            tools = asyncio.run(manager._discover_tools(session, config.servers[0]))
            context = ToolExecutionContext(
                Workspace(Path.cwd()), Session(), mcp=manager,
            )
            definitions = builtin_catalog().extend(tools).select(
                (tool.name for tool in tools), context,
            ).definitions
            self.assertEqual(
                tuple(tool.name for tool in definitions),
                ("mcp__demo__alpha", "mcp__demo__zeta"),
            )
            snapshots.append(_request_tools(LLMRequest("system", (), definitions)))
        for snapshot in snapshots[1:]:
            self.assertEqual(snapshot, snapshots[0])


if __name__ == "__main__":
    unittest.main()
