import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()), session=Session()
        )
        policy = McpApprovalPolicy(
            lambda call: prompts.append(call.name) or False,
            lambda name: name == "mcp__demo__echo",
        )
        with self.assertRaises(PermissionError):
            policy.authorize(ToolCall("1", "mcp__demo__echo", {}), context)
        policy.authorize(ToolCall("2", "mcp__other__echo", {}), context)
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

    def test_overall_wait_covers_the_slowest_server_budget(self) -> None:
        """The overall wait must leave one server budget room to act.

        Each server budget turns a slow server into an "unavailable"
        status. If the overall wait ended first — the manager thread can
        start late while the main thread imports the provider stack — the
        whole agent would fail instead, which is what this guards.
        """
        budgets = (3, 7, 2)
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {
                    f"server{index}": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                        "startup_timeout_seconds": budget,
                    }
                    for index, budget in enumerate(budgets)
                },
            }
        )

        waited: list[float] = []
        manager = McpClientManager(config, Path.cwd())
        original_wait = manager._ready.wait

        def record_wait(timeout: float | None = None) -> bool:
            waited.append(timeout if timeout is not None else -1)
            return original_wait(timeout)

        with patch.object(manager._ready, "wait", record_wait):
            try:
                manager.start()
            finally:
                manager.close()

        self.assertEqual(len(waited), 1)
        self.assertGreater(waited[0], max(budgets))
        self.assertGreaterEqual(waited[0] - max(budgets), 5)

    def test_starts_servers_concurrently(self) -> None:
        """Startup costs the slowest server, not the sum of the servers.

        The stdio helpers charge themselves a startup delay, so starting
        them one after another would need both delays before the first
        turn could run. The delay is far above the interpreter and
        handshake cost the two servers pay at the same time, so the sum
        stays separable from the slowest server.

        The delay is also far above the fixed cost of a start (interpreter,
        mcp import and handshake: roughly a second, and noisy). With a delay
        near that fixed cost the budget sat on top of the concurrent startup
        time and the test failed on a slow machine: 2.9s of a 3.0s budget.
        A start that serialized the servers still costs the fixed work plus
        both delays, well past this budget.
        """
        delay = 3.0
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {
                    "first": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                        "env": {"TEST_MCP_DELAY_SECONDS": str(delay)},
                    },
                    "second": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                        "env": {"TEST_MCP_DELAY_SECONDS": str(delay)},
                    },
                },
            }
        )

        manager = McpClientManager(config, Path.cwd())
        started = time.monotonic()
        try:
            tools = manager.start()
        finally:
            manager.close()

        self.assertEqual(
            sorted(tool.name for tool in tools),
            ["mcp__first__echo", "mcp__first__hidden",
             "mcp__second__echo", "mcp__second__hidden"],
        )
        self.assertLess(time.monotonic() - started, 2 * delay)


if __name__ == "__main__":
    unittest.main()
