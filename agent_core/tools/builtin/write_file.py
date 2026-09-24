import errno
import os
import tempfile
from pathlib import Path

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...path_utils import path_for_comparison


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
        file_path = workspace.resolve_path(path)
        existed = file_path.exists()
        if existed and not overwrite:
            raise ValueError(
                "file already exists; use edit_file, or explicitly set overwrite=true"
            )

        missing_parent_dirs = _missing_parent_directories(
            file_path,
            workspace.path,
        )
        for directory in missing_parent_dirs:
            directory.mkdir()

        data = content.encode("utf-8")
        write_bytes_atomic(file_path, data)

        workspace_path = path_for_comparison(workspace.path)
        return {
            "path": path_for_comparison(file_path).relative_to(
                workspace_path
            ).as_posix(),
            "created": not existed,
            "created_parent_dirs": [
                path_for_comparison(directory)
                .relative_to(workspace_path)
                .as_posix()
                for directory in missing_parent_dirs
            ],
            "bytes_written": len(data),
        }


def _missing_parent_directories(path: Path, workspace_path: Path) -> list[Path]:
    """Validate the complete parent chain before returning directories to create."""
    workspace_comparison = path_for_comparison(workspace_path)
    missing: list[Path] = []
    current = path.parent

    while current != workspace_path:
        try:
            path_for_comparison(current).relative_to(workspace_comparison)
        except ValueError as error:
            raise ValueError(
                "workspace path must stay within the workspace"
            ) from error

        if current.exists():
            if not current.is_dir():
                raise NotADirectoryError(
                    errno.ENOTDIR,
                    os.strerror(errno.ENOTDIR),
                    str(current),
                )
            break
        missing.append(current)
        current = current.parent

    missing.reverse()
    return missing


def write_bytes_atomic(path: Path, data: bytes) -> None:
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = temporary.name
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass
