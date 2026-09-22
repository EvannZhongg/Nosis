import os
from collections.abc import Iterator
from heapq import nsmallest
from pathlib import Path

from ...path_utils import path_for_comparison
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext


DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 200


class ListDirectoryTool(Tool):
    name = "list_directory"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Discover workspace entries with glob filtering, optional "
                "recursive traversal, and stable path-ordered pagination. "
                "Discovery uses bounded memory instead of loading a whole "
                "large directory into a result list."
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
                        "description": "Glob applied to paths relative to 'path'.",
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

        candidates = nsmallest(
            offset + limit + 1,
            (
                entry
                for entry in _iter_entries(directory_path, recursive)
                if _matches_glob(entry.relative_to(directory_path), glob)
            ),
            key=lambda entry: entry.relative_to(directory_path).as_posix(),
        )
        has_more = len(candidates) > offset + limit
        entries: list[dict[str, JSONValue]] = [
            {
                "name": entry.relative_to(directory_path).as_posix(),
                "type": _entry_type(entry),
            }
            for entry in candidates[offset : offset + limit]
        ]

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
                    if recursive and item.is_dir(follow_symlinks=False):
                        directories.append(entry)
        except OSError:
            continue


def _matches_glob(path: Path, pattern: str) -> bool:
    if path.match(pattern):
        return True
    if pattern.startswith("**/"):
        return path.match(pattern[3:])
    return False


def _entry_type(entry: Path) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir():
        return "directory"
    if entry.is_file():
        return "file"
    return "other"
