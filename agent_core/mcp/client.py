"""The MCP dependency consumed by tools, independent of its resource owner."""

from typing import Protocol

from ..execution.process import CancellationSignal
from ..tools.base import JSONValue


class McpClient(Protocol):
    def call_tool(
        self, server: str, tool: str, arguments: dict[str, JSONValue], *,
        cancellation: CancellationSignal | None = None,
    ) -> JSONValue: ...
