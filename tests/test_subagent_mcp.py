import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from agent_core import LLMResponse, ToolCall
from agent_runtime.plane_manager import ExecutionPlaneManager
from tests.agent_fixtures import MockProvider
from tests.bridge_fixtures import BridgeTestCase, open_session_message


class SubagentMcpTest(BridgeTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.provider = MockProvider([])
        self.enterContext(patch.object(
            ExecutionPlaneManager, "_provider_for",
            side_effect=lambda config, *_: self.provider if config is not None else None,
        ))

    def open(self, roles, *, mcp_enabled=True, server_enabled=True):
        bridge, _ = self.make_bridge([], self.root)
        bridge.open_session(open_session_message(self.root, agent_config={
            "max_same_tool_calls": 5, "output_reserve_tokens": 100, "generate_session_title": False,
            "workspace_instruction_files": [], "memory": {"enabled": False},
            "main_agent": {"tools": {"subagent": True}},
            "subagent_roles": {name: {"description": name, **settings} for name, settings in roles.items()},
            "mcp": {"enabled": mcp_enabled, "servers": {"docs": {
                "enabled": server_enabled,
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                "env": {"TEST_MCP_INSTRUCTIONS": "Use docs for research."},
                "tools": {"enabled": ["echo", "identity"], "approval": "never"},
            }}},
        }))
        return bridge

    def test_role_selection_and_actual_child_execution_use_shared_mcp(self):
        self.provider = MockProvider([
            LLMResponse(content=None, tool_calls=(ToolCall("echo", "mcp__docs__echo", {"text": "research"}),)),
            "Researched.",
        ])
        bridge = self.open({
            "white": {"mcp": {"docs": {"echo": True}}},
            "black": {"mcp": {"docs": {"echo": False}}},
            "all": {"mcp": {"docs": True}},
            "none": {},
            "off": {"mcp": {"docs": False}},
        })
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        runtime = plane.agent._tools._context.subagents
        self.assertEqual(runtime.roles.get("white").tools, ("mcp__docs__echo",))
        self.assertEqual(runtime.roles.get("black").tools, ("mcp__docs__identity",))
        self.assertEqual(runtime.roles.get("all").tools, ("mcp__docs__echo", "mcp__docs__identity"))
        self.assertEqual(runtime.roles.get("none").tools, ())
        self.assertEqual(runtime.roles.get("off").tools, ())
        result = plane.agent._tools.execute(ToolCall(
            "delegate", "subagent", {"role": "white", "task": "Research"},
        ))
        self.assertIsNone(result.error)
        request = self.provider.requests[0]
        self.assertEqual([tool.name for tool in request.tools], ["mcp__docs__echo"])
        self.assertIn("Use docs for research.", request.system_prompt)
        self.assertIn("research", str(self.provider.requests[-1].messages))
        self.assertIs(runtime._catalog, plane.catalog)
        self.assertIs(runtime._tool_policy, bridge.host.sessions.permissions)

    def test_rebuild_refreshes_blacklist_but_not_whitelist(self):
        bridge = self.open({
            "white": {"mcp": {"docs": {"echo": True}}},
            "black": {"mcp": {"docs": {"echo": False}}},
        })
        first = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertEqual(first.role_tools["black"], ("mcp__docs__identity",))
        self.assertIs(bridge.host.planes.ensure(bridge.host.sessions), first)
        path = self.root / "agent_config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcp"]["servers"]["docs"]["tools"]["enabled"].append("hidden")
        path.write_text(json.dumps(document), encoding="utf-8")
        second = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(first, second)
        self.assertEqual(second.role_tools["white"], first.role_tools["white"])
        self.assertEqual(second.role_tools["black"], ("mcp__docs__hidden", "mcp__docs__identity"))

    def test_all_tools_respects_global_and_server_enablement(self):
        for mcp_enabled, server_enabled in ((False, True), (True, False)):
            with self.subTest(mcp_enabled=mcp_enabled, server_enabled=server_enabled):
                bridge = self.open({"all": {"mcp": {"docs": True}}},
                                   mcp_enabled=mcp_enabled, server_enabled=server_enabled)
                plane = bridge.host.planes.ensure(bridge.host.sessions)
                self.assertEqual(plane.role_tools["all"], ())
                bridge.close()

    def test_invalid_references_fail_at_assembly_and_close_mcp(self):
        for selection, message in (
            ({"typo": True}, "unknown MCP server reference 'typo'"),
            ({"docs": {"hidden": True}}, "docs.*hidden"),
            ({"docs": {"hidden": False}}, "docs.*hidden"),
        ):
            with self.subTest(selection=selection):
                bridge = self.open({"reader": {"mcp": selection}})
                from agent_core.mcp.manager import McpClientManager

                closed = []
                close = McpClientManager.close

                def record_close(manager):
                    closed.append(manager)
                    close(manager)

                with patch.object(McpClientManager, "close", record_close):
                    with self.assertRaisesRegex(ValueError, f"subagent role 'reader'.*{message}"):
                        bridge.host.planes.ensure(bridge.host.sessions)
                self.assertEqual(len(closed), 1)
                self.assertIsNone(closed[0]._thread)
                self.assertIsNone(bridge.host.planes.current)
                bridge.close()

    def test_disabled_role_is_not_resolved(self):
        bridge = self.open({"off": {"enabled": False, "mcp": {"missing": True}}})
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertEqual(plane.role_tools, {})
