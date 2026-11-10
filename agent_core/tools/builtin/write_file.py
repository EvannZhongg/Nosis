from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..files import bind_file
from ...path_utils import relative_to_root


class WriteFileTool(Tool):
    name = "write_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Write an entire UTF-8 workspace file atomically, creating missing "
                "parent directories within the workspace. Existing files require "
                "overwrite=true; use edit_file for targeted changes."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete UTF-8 file contents.",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Replace an existing file when true (default false).",
                    },
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        path = arguments.get("path")
        content = arguments.get("content")
        overwrite = arguments.get("overwrite", False)

        if not isinstance(path, str) or not path:
            raise ValueError("write_file requires a non-empty string 'path'")
        if not isinstance(content, str):
            raise ValueError("write_file requires a string 'content'")
        if not isinstance(overwrite, bool):
            raise ValueError("write_file requires a boolean 'overwrite'")
        if set(arguments) - {"path", "content", "overwrite"}:
            raise ValueError(
                "write_file accepts only 'path', 'content', and 'overwrite'"
            )

        workspace = context.workspace
        relative = relative_to_root(workspace.resolve_path(path), workspace.path)
        data = content.encode("utf-8")
        with bind_file(workspace.path, relative, create_parents=True) as file:
            mode = file.mode()
            if mode is not None and not overwrite:
                raise ValueError(
                    "file already exists; use edit_file, or explicitly set overwrite=true"
                )
            file.write(data, replace=overwrite, mode=mode)
        return {
            "path": relative.as_posix(),
            "created": mode is None,
            "created_parent_dirs": list(file.created_parents),
            "bytes_written": len(data),
        }
