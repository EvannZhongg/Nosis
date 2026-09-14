import os
import sys
import unittest
from pathlib import Path

from agent_core import Session, ToolCall, ToolExecutionContext, Workspace
from agent_core.mcp.config import load_mcp_config
from agent_core.mcp.tool import McpTool, qualified_tool_name
from agent_core.mcp.manager import McpClientManager
from agent_core.tools.policy import McpApprovalPolicy


class McpConfigTest(unittest.TestCase):
    def test_loads_stdio_and_expands_environment(self) -> None:
        os.environ["MCP_TOKEN_TEST"] = "secret"
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {
                    "demo": {
                        "transport": "stdio",
                        "command": "python",
                        "args": ["server.py"],
                        "env": {"TOKEN": "${MCP_TOKEN_TEST}"},
                        "tools": {
                            "enabled": ["echo"],
                            "approval": {"always": ["echo"]},
                        },
                    }
                },
            }
        )
        server = config.servers[0]
        self.assertEqual(server.env["TOKEN"], "secret")
        self.assertEqual(server.tools.enabled, frozenset({"echo"}))
        self.assertEqual(
            server.tools.require_approval,
            frozenset({"echo"}),
        )

    def test_defaults_to_all_tools_with_approval(self) -> None:
        config = load_mcp_config(
            {
                "servers": {
                    "demo": {
                        "command": "python",
                        "tools": {
                            "enabled": ["read"],
                        },
                    }
                }
            }
        )

        self.assertEqual(
            config.servers[0].tools.enabled,
            frozenset({"read"}),
        )
        self.assertIsNone(config.servers[0].tools.require_approval)

    def test_allows_disabling_approval_for_all_tools(self) -> None:
        config = load_mcp_config(
            {
                "servers": {
                    "demo": {
                        "command": "python",
                        "tools": {"approval": "never"},
                    }
                }
            }
        )

        self.assertEqual(
            config.servers[0].tools.require_approval,
            frozenset(),
        )

    def test_rejects_approval_tools_outside_enabled_tools(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "contains tools not listed",
        ):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "command": "python",
                            "tools": {
                                "enabled": ["read"],
                                "approval": {"always": ["write"]},
                            },
                        }
                    }
                }
            )

    def test_infers_stdio_from_command(self) -> None:
        config = load_mcp_config(
            {
                "servers": {
                    "demo": {
                        "command": "python",
                        "args": ["server.py"],
                    }
                }
            }
        )

        self.assertEqual(config.servers[0].transport, "stdio")

    def test_infers_streamable_http_from_url(self) -> None:
        config = load_mcp_config(
            {
                "servers": {
                    "demo": {
                        "url": "https://example.test/mcp",
                    }
                }
            }
        )

        self.assertEqual(
            config.servers[0].transport,
            "streamable_http",
        )

    def test_rejects_ambiguous_inferred_transport(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "both 'command' and 'url'",
        ):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "command": "python",
                            "url": "https://example.test/mcp",
                        }
                    }
                }
            )

    def test_rejects_missing_transport_identity(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "must specify 'transport', 'command', or 'url'",
        ):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "args": ["server.py"],
                        }
                    }
                }
            )

    def test_rejects_explicit_null_transport(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "must be 'stdio' or 'streamable_http'",
        ):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "transport": None,
                            "command": "python",
                        }
                    }
                }
            )

    def test_rejects_transport_specific_fields(self) -> None:
        with self.assertRaises(ValueError):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "transport": "stdio",
                            "command": "python",
                            "url": "https://example.test/mcp",
                        }
                    }
                }
            )

    def test_rejects_unknown_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown"):
            load_mcp_config(
                {
                    "servers": {
                        "demo": {
                            "transport": "streamable_http",
                            "url": "https://example.test/mcp",
                            "unexpected": True,
                        }
                    }
                }
            )


class McpToolTest(unittest.TestCase):
    def test_qualifies_remote_name_and_delegates_through_context(self) -> None:
        calls = []

        class Manager:
            def call_tool(self, server, tool, arguments):
                calls.append((server, tool, arguments))
                return {"ok": True}

        adapted = McpTool("demo", "echo.text", "Echo", {"type": "object"})
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            mcp=Manager(),
        )

        self.assertEqual(adapted.name, "mcp__demo__echo_text")
        self.assertEqual(adapted.definition(context).name, "mcp__demo__echo_text")
        self.assertEqual(adapted.execute({"text": "hi"}, context), {"ok": True})
        self.assertEqual(calls, [("demo", "echo.text", {"text": "hi"})])
        self.assertEqual(qualified_tool_name("demo", "echo.text"), "mcp__demo__echo_text")

    def test_is_unavailable_without_a_manager(self) -> None:
        adapted = McpTool("demo", "echo", "Echo", {"type": "object"})

        self.assertFalse(
            adapted.available(
                ToolExecutionContext(
                    workspace=Workspace(Path.cwd()), session=Session()
                )
            )
        )

    def test_approval_policy_only_prompts_required_tools(self) -> None:
        prompts = []
        policy = McpApprovalPolicy(
            lambda call: prompts.append(call.name) or False,
            lambda name: name == "mcp__demo__echo",
        )
        with self.assertRaises(PermissionError):
            policy.authorize(ToolCall("1", "mcp__demo__echo", {}))
        policy.authorize(ToolCall("2", "mcp__other__echo", {}))
        self.assertEqual(prompts, ["mcp__demo__echo"])


class McpClientManagerTest(unittest.TestCase):
    def test_discovers_calls_and_closes_fake_stdio_server(self) -> None:
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {
                    "fake": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                        "tools": {
                            "enabled": ["echo"],
                            "approval": "never",
                        },
                    }
                },
            }
        )
        statuses = []
        manager = McpClientManager(config, Path.cwd(), statuses.append)
        try:
            tools = manager.start()
            self.assertEqual(
                [tool.name for tool in tools],
                ["mcp__fake__echo"],
            )
            self.assertEqual(
                manager.tool_identity("mcp__fake__echo"),
                ("fake", "echo"),
            )
            self.assertFalse(manager.requires_approval("mcp__fake__echo"))
            result = tools[0].execute(
                {"text": "hello"},
                ToolExecutionContext(
                    workspace=Workspace(Path.cwd()),
                    session=Session(),
                    mcp=manager,
                ),
            )
            self.assertFalse(result["is_error"])
            self.assertEqual(result["structured_content"], {"echo": "hello"})
        finally:
            manager.close()
        self.assertEqual(statuses[0].status, "connecting")
        self.assertEqual(statuses[-1].status, "closed")


if __name__ == "__main__":
    unittest.main()
