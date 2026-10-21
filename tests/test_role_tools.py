import unittest
from pathlib import Path
from types import SimpleNamespace

from agent_core import Session, ToolExecutionContext, Workspace, builtin_catalog
from agent_core.mcp.config import McpConfig, McpServerConfig
from agent_core.mcp.tool import McpTool
from agent_runtime.role_tools import resolve_role_tools


class RoleToolsTest(unittest.TestCase):
    def resolve(self, selection, *, names=("write", "search", "read.page"), tools=("list_directory", "read_file")):
        remote_tools = tuple(
            McpTool(server.identifier, name, name, {"type": "object"})
            for server in self.servers if server.enabled for name in names
        )
        origins = {tool.name: (tool.server_name, tool.remote_name) for tool in remote_tools}
        mcp = SimpleNamespace(config=McpConfig(True, self.servers), tool_identity=origins.get)
        catalog = builtin_catalog().extend(remote_tools)
        selected = resolve_role_tools(tools, selection, catalog, mcp)
        context = ToolExecutionContext(Workspace(Path.cwd()), Session(), mcp=mcp)
        return tuple(tool.name for tool in catalog.select(selected, context).definitions)

    def setUp(self):
        self.servers = (
            McpServerConfig("docs", "stdio", command="unused"),
            McpServerConfig("docs", "stdio", command="unused", namespace="review.kit"),
            McpServerConfig("disabled", "stdio", command="unused", enabled=False),
        )

    def test_server_flags_and_omission(self):
        self.assertEqual(self.resolve({}), ("read_file", "list_directory"))
        self.assertEqual(self.resolve({"docs": False, "disabled": True}), ("read_file", "list_directory"))
        self.assertEqual(self.resolve({"docs": True}), (
            "read_file", "list_directory", "mcp__docs__read_page", "mcp__docs__search", "mcp__docs__write",
        ))

    def test_raw_names_and_server_identity_are_not_parsed_from_qualified_names(self):
        self.assertEqual(self.resolve({"review.kit:docs": {"read.page": True}}), (
            "read_file", "list_directory", "mcp__review_kit_docs__read_page",
        ))
        with self.assertRaisesRegex(ValueError, "read_page"):
            self.resolve({"review.kit:docs": {"read_page": True}})
        with self.assertRaisesRegex(ValueError, "unknown MCP server reference"):
            self.resolve({"review_kit_docs": True})

    def test_whitelist_and_blacklist_handle_new_tools_differently(self):
        for selection, before, after in (
            ({"docs": {"search": True}}, ("mcp__docs__search",), ("mcp__docs__search",)),
            ({"docs": {"write": False}}, ("mcp__docs__search",), ("mcp__docs__new", "mcp__docs__search")),
            ({"docs": True}, ("mcp__docs__search", "mcp__docs__write"),
             ("mcp__docs__new", "mcp__docs__search", "mcp__docs__write")),
        ):
            with self.subTest(selection=selection):
                self.assertEqual(self.resolve(selection, names=("search", "write"), tools=()), before)
                self.assertEqual(self.resolve(selection, names=("search", "write", "new"), tools=()), after)

    def test_explicit_references_must_be_available_even_in_blacklist(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                with self.assertRaisesRegex(ValueError, "docs.*missing"):
                    self.resolve({"docs": {"missing": enabled}})
        with self.assertRaisesRegex(ValueError, "after Tool Catalog assembly.*missing"):
            self.resolve({}, tools=("missing",))

    def test_schema_order_ignores_declaration_and_discovery_order(self):
        forward = self.resolve({"review.kit:docs": True, "docs": True})
        reverse = self.resolve({"docs": True, "review.kit:docs": True},
                               names=("read.page", "search", "write"), tools=("read_file", "list_directory"))
        self.assertEqual(forward, reverse)


if __name__ == "__main__":
    unittest.main()
