import errno
import os
import tempfile

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...path_utils import path_for_comparison


class WriteFileTool(Tool):
    name = "write_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Write an entire UTF-8 workspace file atomically. Existing files "
                "require overwrite=true; use edit_file for targeted changes."
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
        file_path = workspace.resolve_path(path)
        if file_path.exists() and not overwrite:
            raise ValueError(
                "file already exists; use edit_file, or explicitly set overwrite=true"
            )
        if not file_path.parent.exists():
            raise FileNotFoundError(
                errno.ENOENT,
                os.strerror(errno.ENOENT),
                str(file_path),
            )

        data = content.encode("utf-8")
        temporary_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=file_path.parent,
                prefix=f".{file_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary_path = temporary.name
                temporary.write(data)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, file_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass

        return {
            "path": path_for_comparison(file_path).relative_to(
                path_for_comparison(workspace.path)
            ).as_posix(),
            "bytes_written": len(data),
            "overwritten": overwrite,
        }
