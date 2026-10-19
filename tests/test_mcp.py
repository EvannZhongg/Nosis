import base64
import tempfile
import os
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core import ImagePart, Session, ToolCall, ToolExecutionContext, ToolOutput, TurnControl, Workspace
from agent_core.mcp.config import (
    load_mcp_config,
    load_mcp_server_map,
    merge_mcp_servers,
    namespace_mcp_servers,
)
from agent_core.mcp.tool import McpTool, qualified_tool_name
from agent_core.tools.policy import McpApprovalPolicy
from tests.image_fixtures import png_bytes


class McpConfigTest(unittest.TestCase):
    def test_validates_resource_scope(self):
        for scope in ("global", "workspace", "session"):
            config = load_mcp_config({"servers": {"demo": {"command": "python", "scope": scope}}})
            self.assertEqual(config.servers[0].scope, scope)
        for scope in (None, [], {}, "unknown"):
            with self.assertRaisesRegex(ValueError, "scope"):
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
                {"demo": {"type": "stdio", "command": "python", "scope": "global"}}
            ),
            "example",
            base_directory=root,
        )

        server = servers[0]
        self.assertEqual(server.identifier, "example:demo")
        self.assertEqual(server.cwd, str(root.resolve()))
        self.assertEqual(server.scope, "global")

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


if __name__ == "__main__":
    unittest.main()
