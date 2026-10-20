from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..paths import resolve_readable_path
from ..text import DEFAULT_READ_LIMIT, read_text_file


class ReadFileTool(Tool):
    name = "read_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Read a range of lines from a UTF-8 file with line numbers. "
                "Paths are relative to the workspace root, except "
                ".nosis/sessions/... paths, which address Runtime Session "
                "artifacts. Refuses files larger than 50 MiB. One call "
                "returns as much as fits in a tool result; when more "
                "remains the status line reports the 'offset' to continue "
                "from. Overlong lines are truncated with an explicit "
                "notice."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Path relative to the workspace root, or a "
                            ".nosis/sessions/... Session artifact path."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 1,
                        "default": 1,
                        "description": "First line to read, starting from 1.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "default": DEFAULT_READ_LIMIT,
                        "description": "Maximum number of lines to return.",
                    },
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        path, offset, limit = _parse_arguments(arguments)
        file_path, display_path = resolve_readable_path(path, context)
        return read_text_file(
            file_path, {"path": display_path}, offset, limit, tool_name=self.name
        )


def _parse_arguments(
    arguments: dict[str, JSONValue],
) -> tuple[str, int, int]:
    path = arguments.get("path")
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", DEFAULT_READ_LIMIT)

    if not isinstance(path, str) or not path:
        raise ValueError("read_file requires a non-empty string 'path'")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 1
    ):
        raise ValueError(
            "read_file requires 'offset' to be a positive integer"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError(
            "read_file requires 'limit' to be a positive integer"
        )
    if not set(arguments) <= {"path", "offset", "limit"}:
        raise ValueError(
            "read_file accepts only 'path', 'offset', and 'limit'"
        )
    return path, offset, limit
