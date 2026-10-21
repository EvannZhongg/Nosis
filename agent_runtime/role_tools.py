"""Resolve role declarations against one fully assembled Tool Catalog."""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from agent_core.tools import ROLE_TOOL_NAMES, ToolCatalog

if TYPE_CHECKING:
    from agent_core.mcp.manager import McpClientManager


def resolve_role_tools(
    tools: Iterable[str],
    mcp_selection: dict[str, bool | dict[str, bool]],
    catalog: ToolCatalog,
    mcp: "McpClientManager",
) -> tuple[str, ...]:
    selected = set(tools)
    available = set(catalog.names)
    missing = selected - available
    if missing:
        raise ValueError(f"tool(s) unavailable after Tool Catalog assembly: {', '.join(sorted(missing))}")

    servers: dict[str, dict[str, str]] = {
        server.identifier: {} for server in mcp.config.servers
    }
    for name in catalog.names:
        identity = mcp.tool_identity(name)
        if identity is not None:
            server, remote_name = identity
            servers[server][remote_name] = name

    for server, selection in mcp_selection.items():
        if server not in servers:
            raise ValueError(f"unknown MCP server reference '{server}' after Tool Catalog assembly")
        server_tools = servers[server]
        if isinstance(selection, bool):
            if selection:
                selected.update(server_tools.values())
            continue
        missing = set(selection) - server_tools.keys()
        if missing:
            raise ValueError(
                f"MCP tool reference(s) unavailable after Tool Catalog assembly "
                f"for server '{server}': {', '.join(sorted(missing))}"
            )
        whitelist = next(iter(selection.values()))
        selected.update(
            qualified for remote, qualified in server_tools.items()
            if (remote in selection) == whitelist
        )

    # Keep the builtin schema prefix stable; discovery and JSON key order must
    # not change the MCP schema order sent to the provider.
    return (
        *(name for name in ROLE_TOOL_NAMES if name in selected),
        *sorted(selected - set(ROLE_TOOL_NAMES)),
    )
