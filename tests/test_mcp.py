import asyncio
import base64
import tempfile
import os
import sys
import socket
import subprocess
import time
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core import AgentCancelled, ImagePart, Session, ToolCall, ToolExecutionContext, ToolOutput, TurnControl, Workspace
from agent_core.mcp.config import (
    load_mcp_config,
    load_mcp_server_map,
    merge_mcp_servers,
    namespace_mcp_servers,
)
from agent_core.mcp.tool import McpTool, qualified_tool_name
from agent_core.mcp.manager import McpClientManager, McpServerInstructions
from agent_core.tools.policy import McpApprovalPolicy
from tests.image_fixtures import png_bytes


class McpConfigTest(unittest.TestCase):
    def test_rejects_removed_resource_scope(self) -> None:
        for scope in ("global", "workspace", "session"):
            with self.subTest(scope=scope), self.assertRaisesRegex(ValueError, "unknown.*scope"):
                load_mcp_config({"servers": {"demo": {"command": "python", "scope": scope}}})

    def test_loads_standard_server_map_transport_names(self) -> None:
        servers = load_mcp_server_map(
            {
                "local": {"type": "stdio", "command": "python"},
                "remote": {
                    "type": "http",
                    "url": "https://example.test/mcp",
                },
            }
        )

        self.assertEqual(
            [server.transport for server in servers],
            ["stdio", "streamable_http"],
        )

    def test_namespaces_plugin_servers_and_binds_their_working_directory(
        self,
    ) -> None:
        root = Path.cwd() / "plugin-root"
        servers = namespace_mcp_servers(
            load_mcp_server_map(
                {"demo": {"type": "stdio", "command": "python"}}
            ),
            "example",
            base_directory=root,
        )

        server = servers[0]
        self.assertEqual(server.identifier, "example:demo")
        self.assertEqual(server.cwd, str(root.resolve()))

    def test_merges_namespaced_server_collections(self) -> None:
        standalone = load_mcp_config(
            {
                "enabled": True,
                "servers": {"demo": {"command": "python"}},
            }
        )
        plugin = namespace_mcp_servers(standalone.servers, "example")

        merged = merge_mcp_servers((standalone.servers, plugin))

        self.assertEqual(
            [server.identifier for server in merged],
            ["demo", "example:demo"],
        )

    def test_merge_rejects_duplicate_server_identity(self) -> None:
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {"demo": {"command": "python"}},
            }
        )

        with self.assertRaisesRegex(ValueError, "already registered"):
            merge_mcp_servers((config.servers, config.servers))

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
    def test_normalizes_namespaced_server_in_tool_name(self) -> None:
        self.assertEqual(
            qualified_tool_name("example:demo", "echo.text"),
            "mcp__example_demo__echo_text",
        )

    def test_qualifies_remote_name_and_delegates_through_context(self) -> None:
        calls = []
        control = TurnControl()

        class Manager:
            def call_tool(self, server, tool, arguments, *, cancellation=None):
                calls.append((server, tool, arguments, cancellation))
                return {"ok": True}

        adapted = McpTool("demo", "echo.text", "Echo", {"type": "object"})
        context = ToolExecutionContext(
            workspace=Workspace(Path.cwd()),
            session=Session(),
            mcp=Manager(),
            cancellation=control,
        )

        self.assertEqual(adapted.name, "mcp__demo__echo_text")
        self.assertEqual(adapted.definition(context).name, "mcp__demo__echo_text")
        self.assertEqual(adapted.execute({"text": "hi"}, context), {"ok": True})
        self.assertEqual(calls, [("demo", "echo.text", {"text": "hi"}, control)])
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

    def test_converts_standard_image_content_to_tool_media(self) -> None:
        class Manager:
            def call_tool(self, server, tool, arguments, *, cancellation=None):
                return {
                    "content": [
                        {
                            "type": "image",
                            "data": base64.b64encode(png_bytes(12, 8)).decode(),
                            "mime_type": "image/png",
                        }
                    ],
                    "is_error": False,
                }

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            context = ToolExecutionContext(
                workspace=workspace, session=Session(), mcp=Manager()
            )
            output = McpTool(
                "demo", "screenshot", "Capture", {"type": "object"}
            ).execute({}, context)

            self.assertIsInstance(output, ToolOutput)
            assert isinstance(output, ToolOutput)
            self.assertEqual(len(output.attachments), 1)
            self.assertIsInstance(output.attachments[0], ImagePart)
            self.assertTrue(
                (workspace.path / output.attachments[0].path).is_file()
            )
            self.assertNotIn("data", output.output["content"][0])

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
    def test_instructions_are_scoped_to_selected_tools_and_frozen_at_startup(self) -> None:
        config = load_mcp_config({"enabled": True, "servers": {
            name: {"command": "unused", "enabled": name != "disabled"}
            for name in ("first", "second", "blank", "missing", "filtered", "failed", "disabled")
        }})
        manager = McpClientManager(config, Path.cwd())
        self.addCleanup(manager.close)
        sessions = {}

        async def start_server(stack, server):
            if server.name == "failed":
                raise RuntimeError("discovery failed")
            session = SimpleNamespace(instructions={
                "blank": " \n\t", "missing": None,
            }.get(server.name, f"  Use {server.name}.\n\nKeep the full guide.  "))
            sessions[server.name] = session
            return session, tuple(
                McpTool(server.identifier, name, name, {"type": "object"})
                for name in (() if server.name == "filtered" else ("echo", "other"))
            )

        with patch.object(manager, "_start_server", start_server):
            manager.start()
            manager.wait_ready()

        expected = (
            McpServerInstructions("first", "Use first.\n\nKeep the full guide."),
            McpServerInstructions("second", "Use second.\n\nKeep the full guide."),
        )
        selected = (
            "mcp__second__echo", "mcp__first__echo", "mcp__first__echo",
            "read_file", "mcp__blank__echo", "mcp__missing__echo",
            "mcp__filtered__echo", "mcp__failed__echo", "mcp__disabled__echo",
        )
        self.assertEqual(manager.instructions_for(selected), expected)
        self.assertEqual(manager.instructions_for(reversed(selected)), expected)
        self.assertEqual(manager.instructions_for((
            "mcp__first__other", "mcp__first__echo", "mcp__second__other",
        )), expected)
        self.assertEqual(manager.instructions_for(("mcp__second__echo",)), expected[1:])
        self.assertEqual(manager.instructions_for(("read_file",)), ())
        self.assertEqual(manager.instructions_for(()), ())
        self.assertNotIn("disabled", sessions)
        sessions["first"].instructions = "Changed after startup."
        self.assertEqual(manager.instructions_for(selected), expected)

    def test_streamable_http_connections_close_independently(self) -> None:
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name("fake_mcp_server.py")), str(port)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            env={**os.environ, "TEST_MCP_INSTRUCTIONS": "Use echo over HTTP."},
        )
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.terminate)
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                self.assertIsNone(process.poll())
                self.assertLess(time.monotonic(), deadline, "HTTP MCP server did not start")
                time.sleep(0.02)
        config = load_mcp_config({"enabled": True, "servers": {
            "fake": {"url": f"http://127.0.0.1:{port}/mcp", "tools": {"enabled": ["echo"]}},
        }})
        managers = [McpClientManager(config, Path.cwd()) for _ in range(2)]
        for manager in managers:
            self.addCleanup(manager.close)
            manager.start()
            self.assertEqual(tuple(tool.name for tool in manager.wait_ready()), ("mcp__fake__echo",))
            self.assertEqual(manager.instructions_for(manager.tool_names), (
                McpServerInstructions("fake", "Use echo over HTTP."),
            ))
            self.assertEqual(manager.call_tool("fake", "echo", {"text": "http"})["structured_content"], {"echo": "http"})
        thread = managers[0]._thread
        managers[0].close()
        self.assertFalse(thread.is_alive())
        self.assertEqual(managers[1].call_tool("fake", "echo", {"text": "live"})["structured_content"], {"echo": "live"})

    def test_managers_own_independent_stdio_servers(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = load_mcp_config({"enabled": True, "servers": {
            "fake": {
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
            },
        }})
        managers = [McpClientManager(config, root) for _ in range(2)]
        for manager in managers:
            self.addCleanup(manager.close)
            manager.start()
        for manager in managers:
            manager.wait_ready()
        identities = [manager.call_tool("fake", "identity", {})["structured_content"] for manager in managers]
        self.assertNotEqual(identities[0]["pid"], identities[1]["pid"])
        self.assertEqual([Path(item["cwd"]) for item in identities], [root, root])
        thread = managers[0]._thread
        managers[0].close()
        self.assertFalse(thread.is_alive())
        with self.assertRaisesRegex(ConnectionError, "closed"):
            managers[0].call_tool("fake", "echo", {"text": "closed"})
        self.assertEqual(managers[1].call_tool("fake", "echo", {"text": "live"})["structured_content"], {"echo": "live"})

    def test_close_cancels_active_remote_call_and_stops_manager(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        config = load_mcp_config({"enabled": True, "servers": {
            "fake": {
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
            },
        }})
        manager = McpClientManager(config, root)
        self.addCleanup(manager.close)
        manager.start()
        manager.wait_ready()
        started, release = root / "started", root / "release"
        errors = []

        def call():
            try:
                manager.call_tool("fake", "wait", {"started": str(started), "release": str(release)})
            except BaseException as error:
                errors.append(error)

        caller = threading.Thread(target=call, daemon=True)
        caller.start()
        thread = manager._thread
        try:
            deadline = time.monotonic() + 5
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started.exists())
            manager.close()
            caller.join(3)
            self.assertFalse(caller.is_alive())
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], AgentCancelled)
            self.assertTrue(Path(str(started) + ".stopped").exists())
        finally:
            release.touch()
            caller.join(3)

    def test_background_start_allows_independent_work_before_discovery_finishes(self) -> None:
        config = load_mcp_config({"enabled": True, "servers": {
            "fake": {"transport": "stdio", "command": sys.executable},
        }})
        manager = McpClientManager(config, Path.cwd())
        entered = threading.Event()
        release = threading.Event()

        async def start_server(stack, server):
            entered.set()
            await asyncio.to_thread(release.wait)
            return SimpleNamespace(instructions=None), ()

        with patch.object(manager, "_start_server", start_server):
            try:
                manager.start()
                self.assertTrue(entered.wait(2))
                self.assertFalse(manager._ready.is_set())
                release.set()
                self.assertEqual(manager.wait_ready(), ())
                self.assertTrue(manager._ready.is_set())
            finally:
                release.set()
                manager.close()

    def test_cancel_stops_only_the_active_request_and_keeps_the_manager_usable(self) -> None:
        entered = threading.Event()
        stopped = threading.Event()
        calls = []

        class RemoteSession:
            instructions = None

            async def call_tool(self, name, arguments):
                calls.append(name)
                if name == "slow":
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        stopped.set()
                return SimpleNamespace(is_error=False, model_dump=lambda **kw: {"ok": True})

        async def start_server(stack, config):
            return RemoteSession(), ()

        config = load_mcp_config({"enabled": True, "servers": {
            "fake": {"transport": "stdio", "command": sys.executable, "call_timeout_seconds": 2},
        }})
        manager = McpClientManager(config, Path.cwd())
        control = TurnControl()
        errors = []

        def call():
            try:
                manager.call_tool("fake", "slow", {}, cancellation=control)
            except BaseException as error:
                errors.append(error)

        with patch.object(manager, "_start_server", start_server):
            manager.start()
            manager.wait_ready()
        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        try:
            self.assertTrue(entered.wait(3))
            control.cancel()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(stopped.is_set())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], AgentCancelled)
            with self.assertRaises(AgentCancelled):
                manager.call_tool("fake", "never-start", {}, cancellation=control)
            self.assertEqual(manager.call_tool("fake", "echo", {}), {"ok": True})
            self.assertEqual(calls, ["slow", "echo"])
        finally:
            control.cancel()
            thread.join(3)
            manager.close()

    def test_discovers_calls_and_closes_fake_stdio_server(self) -> None:
        config = load_mcp_config(
            {
                "enabled": True,
                "servers": {
                    "fake": {
                        "transport": "stdio",
                        "command": sys.executable,
                        "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                        "env": {"TEST_MCP_INSTRUCTIONS": "Use echo to repeat text."},
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
            manager.start()
            tools = manager.wait_ready()
            self.assertEqual(
                [tool.name for tool in tools],
                ["mcp__fake__echo"],
            )
            self.assertEqual(
                manager.tool_identity("mcp__fake__echo"),
                ("fake", "echo"),
            )
            self.assertFalse(manager.requires_approval("mcp__fake__echo"))
            self.assertEqual(manager.instructions_for(manager.tool_names), (
                McpServerInstructions("fake", "Use echo to repeat text."),
            ))
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
        """Allow startup overhead beyond the slowest server timeout."""
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
                manager.wait_ready()
            finally:
                manager.close()

        self.assertEqual(len(waited), 1)
        self.assertGreater(waited[0], max(budgets))
        self.assertGreaterEqual(waited[0] - max(budgets), 5)

    def test_starts_servers_concurrently(self) -> None:
        config = load_mcp_config({"enabled": True, "servers": {
            name: {"command": "unused"} for name in ("first", "second")
        }})
        manager = McpClientManager(config, Path.cwd())
        entered = set()
        both_started = threading.Event()
        release = threading.Event()

        async def start_server(stack, server):
            entered.add(server.identifier)
            if len(entered) == 2:
                both_started.set()
            await asyncio.to_thread(release.wait)
            return SimpleNamespace(instructions=None), ()

        with patch.object(manager, "_start_server", start_server):
            manager.start()
            try:
                self.assertTrue(both_started.wait(5))
                self.assertFalse(manager._ready.is_set())
                release.set()
                self.assertEqual(manager.wait_ready(), ())
            finally:
                release.set()
                manager.close()

    def test_startup_timeout_isolates_slow_server_and_closes_connections(self) -> None:
        config = load_mcp_config({"enabled": True, "servers": {
            name: {
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("fake_mcp_server.py"))],
                "env": {"TEST_MCP_DELAY_SECONDS": "3" if name == "slow" else "0"},
                "startup_timeout_seconds": 1 if name == "slow" else 15,
                "tools": {"enabled": ["echo"]},
            } for name in ("slow", "healthy")
        }})
        statuses = []
        manager = McpClientManager(config, Path.cwd(), statuses.append)
        manager.start()
        thread = manager._thread
        try:
            self.assertEqual(tuple(tool.name for tool in manager.wait_ready()), ("mcp__healthy__echo",))
            self.assertEqual(manager.call_tool("healthy", "echo", {"text": "ready"})["structured_content"], {"echo": "ready"})
            failure = next(status for status in statuses if status.server == "slow" and status.status == "unavailable")
            self.assertEqual(failure.error, "TimeoutError")
        finally:
            manager.close()
        self.assertFalse(thread.is_alive())


if __name__ == "__main__":
    unittest.main()
