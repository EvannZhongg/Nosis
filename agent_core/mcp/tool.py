import base64
import binascii
import re
import uuid
from typing import TYPE_CHECKING, Any

from ..media import UnsupportedImageError, image_extension, probe_image
from ..content import ImagePart
from ..path_utils import path_for_comparison
from ..tools import JSONValue, Tool, ToolDefinition, ToolOutput
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
    ) -> JSONValue | ToolOutput:
        manager: "McpClientManager | None" = context.mcp
        if manager is None:
            raise ValueError("this runtime has no MCP client manager")
        result = manager.call_tool(
            self.server_name,
            self.remote_name,
            arguments,
            cancellation=context.cancellation,
        )
        return _adapt_result_media(result, context)


def _adapt_result_media(
    result: JSONValue,
    context: ToolExecutionContext,
) -> JSONValue | ToolOutput:
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return result
    content: list[JSONValue] = []
    attachments = []
    for item in result["content"]:
        if not isinstance(item, dict) or item.get("type") != "image":
            content.append(item)
            continue
        data = item.get("data")
        mime_type = item.get("mime_type")
        if not isinstance(data, str) or not isinstance(mime_type, str):
            content.append(item)
            continue
        try:
            payload = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("MCP tool returned invalid base64 image data") from error
        try:
            suffix = image_extension(mime_type)
        except KeyError as error:
            raise ValueError(
                f"MCP tool returned unsupported image type: {mime_type}"
            ) from error
        directory = context.workspace.resolve_path(".nosis/attachments")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"mcp-{uuid.uuid4().hex}{suffix}"
        path.write_bytes(payload)
        try:
            info = probe_image(path)
        except (UnsupportedImageError, OSError):
            path.unlink(missing_ok=True)
            raise
        stored = path_for_comparison(path).relative_to(
            path_for_comparison(context.workspace.path)
        ).as_posix()
        attachments.append(
            ImagePart(
                path=stored,
                mime_type=info.mime_type,
                filename=path.name,
                size_bytes=info.size_bytes,
            )
        )
        content.append(
            {
                "type": "text",
                "text": f"[Image returned as attachment: {stored}]",
            }
        )
    if not attachments:
        return result
    output = dict(result)
    output["content"] = content
    return ToolOutput(output=output, attachments=tuple(attachments))
