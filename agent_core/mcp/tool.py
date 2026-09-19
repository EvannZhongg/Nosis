import re
from typing import TYPE_CHECKING, Any

from ..tools import JSONValue, Tool, ToolDefinition
from ..tools.context import ToolExecutionContext

if TYPE_CHECKING:
    from .manager import McpClientManager


_INVALID_TOOL_NAME = re.compile(r"[^A-Za-z0-9_-]")


def qualified_tool_name(server_name: str, tool_name: str) -> str:
    server_name = _INVALID_TOOL_NAME.sub("_", server_name)
    normalized = _INVALID_TOOL_NAME.sub("_", tool_name)
    if not server_name or not normalized:
        raise ValueError(
            f"MCP server '{server_name}' returned an empty tool name"
        )
    return f"mcp__{server_name}__{normalized}"


class McpTool(Tool):
    """A remote MCP tool, named and described by its server.

    The manager it calls is a Runtime singleton reached through the
    context, so the instance itself stays stateless apart from the
    identity discovered at startup.
    """

    def __init__(
        self,
        server_name: str,
        remote_name: str,
        description: str | None,
        input_schema: dict[str, Any],
    ) -> None:
        self.server_name = server_name
        self.remote_name = remote_name
        self.name = qualified_tool_name(server_name, remote_name)
        self._definition = ToolDefinition(
            name=self.name,
            description=description or f"MCP tool {remote_name}",
            parameters=input_schema,
        )

    def available(self, context: ToolExecutionContext) -> bool:
        return context.mcp is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return self._definition

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        manager: "McpClientManager | None" = context.mcp
        if manager is None:
            raise ValueError("this runtime has no MCP client manager")
        return manager.call_tool(
            self.server_name,
            self.remote_name,
            arguments,
        )
