import os
from collections.abc import Iterator
from pathlib import Path

from ...path_utils import path_for_comparison
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..globs import matches_path_glob


DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 200


class ListDirectoryTool(Tool):
    name = "list_directory"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Discover workspace entries with glob filtering, optional "
                "recursive traversal, and pagination. Discovery streams in "
                "filesystem order and keeps only one result page in memory."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory relative to the workspace root.",
                    },
                    "glob": {
                        "type": "string",
                        "default": "*",
                        "description": (
                            "Full-path glob relative to 'path'. '*' matches "
                            "one path segment; use '**' for recursive segments."
                        ),
                    },
                    "recursive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Descend into subdirectories when true.",
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Number of matching entries to skip.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_LIMIT,
                        "default": DEFAULT_LIST_LIMIT,
                        "description": "Maximum number of entries to return.",
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
        path, glob, recursive, offset, limit = _parse_arguments(arguments)
        workspace = context.workspace
        directory_path = workspace.resolve_path(path)
        if not directory_path.is_dir():
            raise ValueError("list_directory path must be a directory")

        entries: list[dict[str, JSONValue]] = []
        matched_entries = 0
        has_more = False
        for entry in _iter_entries(directory_path, recursive):
            relative_path = entry.relative_to(directory_path)
            if not matches_path_glob(relative_path, glob):
                continue
            if matched_entries < offset:
                matched_entries += 1
                continue
            if len(entries) >= limit:
                has_more = True
                break
            entries.append(
                {
                    "name": relative_path.as_posix(),
                    "type": _entry_type(entry),
                }
            )
            matched_entries += 1

        display_path = path_for_comparison(directory_path).relative_to(
            path_for_comparison(workspace.path)
        ).as_posix()
        return {
            "path": display_path,
            "entries": entries,
            "has_more": has_more,
            "next_offset": offset + len(entries) if has_more else None,
        }


def _parse_arguments(
    arguments: dict[str, JSONValue],
) -> tuple[str, str, bool, int, int]:
    allowed = {"path", "glob", "recursive", "offset", "limit"}
    if not set(arguments) <= allowed:
        raise ValueError(
            "list_directory accepts only 'path', 'glob', 'recursive', "
            "'offset', and 'limit'"
        )
    path = arguments.get("path")
    glob = arguments.get("glob", "*")
    recursive = arguments.get("recursive", False)
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", DEFAULT_LIST_LIMIT)
    if not isinstance(path, str) or not path:
        raise ValueError("list_directory requires a non-empty string 'path'")
    if not isinstance(glob, str) or not glob:
        raise ValueError("list_directory requires 'glob' to be a non-empty string")
    if not isinstance(recursive, bool):
        raise ValueError("list_directory requires 'recursive' to be a boolean")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(
            "list_directory requires 'offset' to be a non-negative integer"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_LIST_LIMIT
    ):
        raise ValueError(
            f"list_directory requires 'limit' to be between 1 and "
            f"{MAX_LIST_LIMIT}"
        )
    return path, glob, recursive, offset, limit


def _iter_entries(directory_path: Path, recursive: bool) -> Iterator[Path]:
    directories = [directory_path]
    while directories:
        current_directory = directories.pop()
        try:
            with os.scandir(current_directory) as entries:
                for item in entries:
                    entry = Path(item.path)
                    yield entry
                    if (
                        recursive
                        and item.is_dir(follow_symlinks=False)
                        and not _escapes_directory(entry, directory_path)
                    ):
                        directories.append(entry)
        except OSError:
            continue


def _escapes_directory(entry: Path, root: Path) -> bool:
    """Reject Windows junctions that resolve outside the requested tree."""
    return not path_for_comparison(entry.resolve()).is_relative_to(
        path_for_comparison(root)
    )


def _entry_type(entry: Path) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir():
        return "directory"
    if entry.is_file():
        return "file"
    return "other"
