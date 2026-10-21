"""Exercise Plugin composition through the existing Bridge and execution plane."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import LLMResponse, McpConfig, McpServerConfig, ToolCall
from agent_core.tools import ROLE_TOOL_NAMES
from agent_runtime.plane_manager import ExecutionPlaneManager
from agent_core.mcp.manager import McpClientManager
from agent_core.mcp.tool import McpTool
from tests.test_agent import MockProvider
from tests.test_bridge import BridgeTestCase, emitted, open_session_message
from tests.test_plugins import write_agent, write_manifest, write_skill


TOOL = "mcp__review-kit_analyzer__echo"


def calls(*items: ToolCall) -> LLMResponse:
    return LLMResponse(content=None, tool_calls=items)


class McpRegistrationIsolationTest(unittest.TestCase):
    def test_collision_discards_partial_server_registration(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        statuses = []
        manager = McpClientManager(McpConfig(enabled=True, servers=tuple(
            McpServerConfig(name=name, transport="stdio", command="python")
            for name in ("bad", "healthy")
        )), root, statuses.append)
        self.addCleanup(manager.close)

        async def start_server(stack, server):
            names = ("unique", "dup-name", "dup.name", "dup_name") if server.name == "bad" else ("echo",)
            return SimpleNamespace(instructions=f"Use {server.identifier}."), tuple(
                McpTool(server.identifier, name, name, {"type": "object"})
                for name in names
            )

        with patch.object(manager, "_start_server", start_server):
            manager.start()
            manager.wait_ready()
        self.assertEqual(manager.tool_names, ("mcp__healthy__echo",))
        self.assertIsNone(manager.tool_identity("mcp__bad__unique"))
        self.assertEqual(
            [entry.server for entry in manager.instructions_for(("mcp__bad__unique", *manager.tool_names))],
            ["healthy"],
        )
        self.assertEqual([(status.server, status.status) for status in statuses], [
            ("bad", "connecting"), ("healthy", "connecting"),
            ("bad", "unavailable"), ("healthy", "ready"),
        ])
        self.assertIn("duplicate qualified tool name", statuses[2].error)


class PluginInteropTest(BridgeTestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        manifest = write_manifest(self.root / "plugins", "review-kit", components={
            "skills": ["skills"], "agents": ["agents/reviewer.md"], "mcp": [".mcp.json"],
        })
        self.plugin = manifest.parent
        write_skill(self.plugin, "code-review")
        self.agent_path = write_agent(self.plugin, fields=f"tools: [read_file, {TOOL}]\n")
        self.server_config = {
            "command": sys.executable,
            "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
            "env": {"TEST_MCP_INSTRUCTIONS": "Use analyzer echo for code review."},
            "tools": {"enabled": ["echo"], "approval": "never"},
        }
        self.write_mcp()
        self.main = MockProvider([])
        self.child = MockProvider([])
        self.enterContext(patch.object(
            ExecutionPlaneManager, "_provider_for",
            side_effect=lambda config, *_: None if config is None else (
                self.child if config.model == "child" else self.main
            ),
        ))

    def write_mcp(self, **other_servers: dict) -> None:
        (self.plugin / ".mcp.json").write_text(
            json.dumps({"analyzer": self.server_config, **other_servers}), encoding="utf-8",
        )

    def open(self, lines: list[str] | None = None, *, mcp_enabled: bool = True):
        bridge, stdout = self.make_bridge(lines or [], self.root)
        bridge.open_session(open_session_message(self.root, agent_config={
            "max_same_tool_calls": 5, "output_reserve_tokens": 100,
            "workspace_instruction_files": [],
            "memory": {"enabled": False},
            "main_agent": {"tools": {"subagent": True}},
            "mcp": {"enabled": mcp_enabled},
        }, provider_config={
            "main_agent": {"provider": "main"}, "subagent": {"provider": "child"},
            "subagent_roles": {"review-kit:reviewer": {"provider": "child"}},
            "providers": {
                "main": {"model": "main", "max_context_tokens": 1000},
                "child": {"model": "child", "max_context_tokens": 1000},
            },
        }))
        return bridge, stdout

    def test_skill_to_reviewer_to_mcp_uses_shared_catalog_and_policy(self) -> None:
        self.main = MockProvider([
            calls(ToolCall("skill", "read_skill", {"name": "review-kit:code-review"})),
            calls(ToolCall("delegate", "subagent", {"role": "review-kit:reviewer", "task": "Review sample.py"})),
            "Review delivered.",
        ])
        self.child = MockProvider([
            calls(ToolCall("read", "read_file", {"path": "sample.py"})),
            calls(ToolCall("analyze", TOOL, {"text": "source review"})),
            "Reviewer findings.",
        ])
        (self.root / "sample.py").write_text("print('hello')\n", encoding="utf-8")
        bridge, stdout = self.open()
        bridge.run_turn({"turn_id": "review", "text": "Use the code review skill."})
        messages = emitted(stdout)
        completed = [message for message in messages if message["type"] == "turn_completed"]
        self.assertEqual(len(completed), 1, messages)
        self.assertEqual(len(self.child.requests), 3)
        self.assertEqual({tool.name for tool in self.child.requests[0].tools}, {"read_file", "read_skill", TOOL})
        self.assertIn("review-kit:code-review", self.child.requests[0].system_prompt)
        self.assertIn("Review the requested code.", self.child.requests[0].system_prompt)
        for request in (self.main.requests[0], self.child.requests[0]):
            self.assertIn("### review-kit:analyzer", request.system_prompt)
            self.assertNotIn("Tools:", request.system_prompt)
            self.assertNotIn(TOOL, request.system_prompt)
            self.assertEqual(request.system_prompt.count("Use analyzer echo for code review."), 1)
        for requests in (self.main.requests, self.child.requests):
            self.assertTrue(all(request.system_prompt == requests[0].system_prompt for request in requests))
        self.assertTrue(any("source review" in str(message.content) for message in self.child.requests[-1].messages))
        plane = bridge.host.planes.current
        runtime = plane.agent._tools._context.subagents
        self.assertIs(runtime._catalog, plane.catalog)
        self.assertIs(runtime._tool_policy, bridge.host.sessions.permissions)
        self.assertTrue(any("subagents" in path.parts for path in self.root.rglob("*.jsonl")))

    def test_mcp_instructions_follow_each_agents_actual_tool_selection(self) -> None:
        other = {**self.server_config, "env": {"TEST_MCP_INSTRUCTIONS": "Other server guide."}}
        self.write_mcp(other=other)
        self.main = MockProvider(["done"])
        self.child = MockProvider(["reviewed", "reviewed without MCP"])
        bridge, stdout = self.open()
        bridge.run_turn({"turn_id": "instructions", "text": "Review the workspace."})
        self.assertTrue(any(message["type"] == "turn_completed" for message in emitted(stdout)))
        plane = bridge.host.planes.current
        result = plane.agent._tools.execute(ToolCall(
            "review", "subagent", {"role": "review-kit:reviewer", "task": "Review"},
        ))
        self.assertIsNone(result.error)
        self.assertIn("Other server guide.", self.main.requests[0].system_prompt)
        self.assertIn("Use analyzer echo for code review.", self.child.requests[0].system_prompt)
        self.assertNotIn("Other server guide.", self.child.requests[0].system_prompt)

        write_agent(self.plugin, fields="tools: [read_file]\n")
        bridge.host.planes.close()
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        result = plane.agent._tools.execute(ToolCall(
            "review-local", "subagent", {"role": "review-kit:reviewer", "task": "Review locally"},
        ))
        self.assertIsNone(result.error)
        self.assertNotIn("## MCP Server Instructions", self.child.requests[-1].system_prompt)

    def test_reviewer_cannot_call_tools_outside_its_selection(self) -> None:
        self.child = MockProvider([
            calls(ToolCall("hidden", "mcp__review-kit_analyzer__hidden", {})),
            calls(ToolCall("echo", TOOL, {"text": "still works"})),
            "done",
        ])
        bridge, _ = self.open()
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        result = plane.agent._tools.execute(ToolCall("review", "subagent", {"role": "review-kit:reviewer", "task": "Review"}))
        self.assertIsNone(result.error)
        self.assertIn("tool_not_found", str(self.child.requests[1].messages))
        self.assertIn("still works", str(self.child.requests[2].messages))

    def test_child_mcp_approval_denial_and_approval_use_bridge(self) -> None:
        self.server_config["tools"]["approval"] = "always"
        self.write_mcp()
        for approved in (False, True):
            with self.subTest(approved=approved):
                self.child = MockProvider([calls(ToolCall("analyze", TOOL, {"text": "review"})), "done"])
                bridge, stdout = self.open([json.dumps({
                    "type": "approval_response", "request_id": "None:1", "approved": approved,
                })])
                plane = bridge.host.planes.ensure(bridge.host.sessions)
                result = plane.agent._tools.execute(ToolCall("review", "subagent", {"role": "review-kit:reviewer", "task": "Review"}))
                self.assertIsNone(result.error)
                approvals = [message for message in emitted(stdout) if message["type"] == "approval_request"]
                self.assertEqual(len(approvals), 1)
                self.assertEqual(approvals[0]["kind"], "mcp")
                self.assertEqual(approvals[0]["server"], "review-kit:analyzer")
                self.assertEqual(approvals[0]["tool_name"], "echo")
                result_messages = str(self.child.requests[-1].messages)
                self.assertEqual("PermissionError" in result_messages, not approved)
                bridge.close()

    def test_missing_dynamic_tools_skip_only_affected_role(self) -> None:
        manifest = self.plugin / "plugin.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["components"]["agents"].extend(["agents/legacy.md", "agents/empty.md"])
        manifest.write_text(json.dumps(document), encoding="utf-8")
        write_agent(self.plugin, "legacy")
        write_agent(self.plugin, "empty", fields="tools: []\n")
        for reason in ("global_disabled", "server_disabled", "filtered", "startup_failure", "typo"):
            with self.subTest(reason=reason):
                config = json.loads(json.dumps(self.server_config))
                if reason == "server_disabled":
                    config["enabled"] = False
                elif reason == "filtered":
                    config["tools"]["enabled"] = ["hidden"]
                elif reason == "startup_failure":
                    config["command"] = str(self.root / "missing-python")
                (self.plugin / ".mcp.json").write_text(json.dumps({"analyzer": config}), encoding="utf-8")
                write_agent(self.plugin, fields=f"tools: [{TOOL + '_typo' if reason == 'typo' else TOOL}]\n")
                bridge, _ = self.open(mcp_enabled=reason != "global_disabled")
                plane = bridge.host.planes.ensure(bridge.host.sessions)
                runtime = plane.agent._tools._context.subagents
                self.assertEqual([role.name for role in runtime.roles], ["review-kit:legacy", "review-kit:empty"])
                self.assertEqual(runtime.roles.get("review-kit:legacy").tools, ROLE_TOOL_NAMES)
                self.assertEqual(runtime.roles.get("review-kit:empty").tools, ())
                self.assertIn("review-kit:code-review", plane.skills.names)
                self.assertEqual(len(plane.runtime_warnings), 1)
                self.assertIn(str(self.agent_path.resolve()), plane.runtime_warnings[0])
                self.assertIn("after Tool Catalog assembly", plane.runtime_warnings[0])
                bridge.close()

    def test_mcp_name_collision_does_not_break_other_components(self) -> None:
        self.write_mcp(**{"analyzer:bad": self.server_config})
        # A malformed server map is isolated at its declared component boundary.
        bridge, _ = self.open()
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIn("review-kit:code-review", plane.skills.names)
        self.assertTrue(any("MCP server names" in warning for warning in plane.runtime_warnings))
        bridge.close()
        self.write_mcp()
        other = write_manifest(self.root / "plugins", "review.kit", components={"mcp": [".mcp.json"]}).parent
        # Both plugin names normalize to the same model-facing tool prefix.
        (other / ".mcp.json").write_text(json.dumps({"analyzer": self.server_config}), encoding="utf-8")
        (self.plugin / "plugin.json").write_text(json.dumps({"name": "review_kit", "components": {"mcp": [".mcp.json"]}}), encoding="utf-8")
        bridge, stdout = self.open()
        provider_path = self.root / "provider_config.json"
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
        provider.pop("subagent_roles")
        provider_path.write_text(json.dumps(provider), encoding="utf-8")
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIn("mcp__review_kit_analyzer__echo", plane.catalog.names)
        failures = [message for message in emitted(stdout) if message["type"] == "mcp_server_status" and message["status"] == "unavailable"]
        self.assertTrue(any("duplicate qualified tool name" in message["error"] for message in failures), failures)

    def test_reassembly_refreshes_declarations_and_disable_removes_components(self) -> None:
        self.server_config["tools"]["enabled"].append("identity")
        self.write_mcp()
        bridge, _ = self.open()
        first = bridge.host.planes.ensure(bridge.host.sessions)
        identity_tool = "mcp__review-kit_analyzer__identity"
        identity = first.agent._tools.execute(ToolCall("identity", identity_tool, {}))
        first_pid = identity.output["structured_content"]["pid"]
        self.agent_path.write_text(self.agent_path.read_text(encoding="utf-8").replace("Review the requested code.", "Updated reviewer instructions."), encoding="utf-8")
        skill = self.plugin / "skills" / "code-review" / "SKILL.md"
        skill.write_text(skill.read_text(encoding="utf-8").replace("Skill from review-kit.", "Updated skill description."), encoding="utf-8")
        self.assertIs(bridge.host.planes.ensure(bridge.host.sessions), first)
        bridge.host.planes.close()
        second = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(first, second)
        self.assertEqual(second.plugin_agents[0].instructions, "Updated reviewer instructions.")
        self.assertEqual(second.skills.get("review-kit:code-review").description, "Updated skill description.")
        result = second.agent._tools.execute(ToolCall("echo", TOOL, {"text": "after rebuild"}))
        self.assertIsNone(result.error)
        identity = second.agent._tools.execute(ToolCall("identity", identity_tool, {}))
        self.assertNotEqual(identity.output["structured_content"]["pid"], first_pid)
        self.server_config["tools"]["enabled"] = ["identity"]
        self.write_mcp()
        write_agent(self.plugin, fields=f"tools: [{identity_tool}]\n")
        third = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(third, second)
        self.assertEqual(third.plugin_agents[0].tools, (identity_tool,))
        self.assertNotIn(TOOL, third.catalog.names)
        manifest = self.plugin / "plugin.json"
        document = json.loads(manifest.read_text(encoding="utf-8"))
        document["enabled"] = False
        manifest.write_text(json.dumps(document), encoding="utf-8")
        # Remove explicit routing to a role that is no longer declared.
        provider_path = self.root / "provider_config.json"
        provider = json.loads(provider_path.read_text(encoding="utf-8"))
        provider.pop("subagent_roles")
        provider_path.write_text(json.dumps(provider), encoding="utf-8")
        disabled = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(disabled, third)
        self.assertEqual(disabled.plugin_agents, ())
        self.assertEqual(disabled.skills.names, ())
        self.assertEqual(disabled.mcp.tool_names, ())
        self.assertNotIn(identity_tool, disabled.catalog.names)

    def test_runtime_hosts_own_independent_mcp_connections(self) -> None:
        self.server_config["tools"]["enabled"].append("identity")
        self.write_mcp()
        first, _ = self.open()
        second, _ = self.open()
        planes = [bridge.host.planes.ensure(bridge.host.sessions) for bridge in (first, second)]
        identities = [plane.mcp.call_tool("review-kit:analyzer", "identity", {})["structured_content"] for plane in planes]
        self.assertNotEqual(identities[0]["pid"], identities[1]["pid"])
        first.close()
        self.assertEqual(
            planes[1].mcp.call_tool("review-kit:analyzer", "echo", {"text": "still connected"})["structured_content"],
            {"echo": "still connected"},
        )
        self.assertFalse((self.root / "mcp").exists())
