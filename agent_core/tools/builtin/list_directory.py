import os
import stat
import time
from collections.abc import Iterator
from dataclasses import dataclass
from heapq import nsmallest
from pathlib import Path

from ...execution.process import CancellationSignal
from ...path_utils import path_for_comparison
from ...turn_control import AgentCancelled
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..globs import matches_path_glob


DEFAULT_LIST_LIMIT = 200
MAX_LIST_LIMIT = 200
DEFAULT_LIST_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class _ListState:
    deadline: float
    timeout_seconds: int
    cancellation: CancellationSignal | None

    def check(self) -> None:
        if self.cancellation is not None and self.cancellation.cancelled:
            raise AgentCancelled
        if time.monotonic() >= self.deadline:
            raise TimeoutError(
                f"list_directory timed out after {self.timeout_seconds} seconds"
            )


class ListDirectoryTool(Tool):
    name = "list_directory"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Discover workspace entries with glob filtering, optional "
                "recursive traversal, and stable lexicographic cursor "
                "pagination. Each call scans the requested tree but keeps "
                "only one result page in memory. Symlinks and directory "
                "junctions are listed but never traversed. Listings time out "
                f"after {DEFAULT_LIST_TIMEOUT_SECONDS} seconds by default."
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
                    "cursor": {
                        "type": "string",
                        "description": (
                            "Return entries whose relative path sorts after "
                            "this value. Use next_cursor from the previous page."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_LIST_LIMIT,
                        "default": DEFAULT_LIST_LIMIT,
                        "description": "Maximum number of entries to return.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "default": DEFAULT_LIST_TIMEOUT_SECONDS,
                        "description": (
                            "Listing time budget in seconds. Exceeding the "
                            "budget returns a timeout error."
                        ),
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
        path, glob, recursive, cursor, limit, timeout_seconds = _parse_arguments(arguments)
        state = _ListState(
            deadline=time.monotonic() + timeout_seconds,
            timeout_seconds=timeout_seconds,
            cancellation=context.cancellation,
        )
        state.check()
        workspace = context.workspace
        directory_path = workspace.resolve_path(path)
        if not directory_path.is_dir():
            raise ValueError("list_directory path must be a directory")

        candidates = nsmallest(
            limit + 1,
            _matching_entries(directory_path, glob, recursive, cursor, state),
            key=lambda candidate: candidate[0],
        )
        has_more = len(candidates) > limit
        entries: list[dict[str, JSONValue]] = []
        for name, entry in candidates[:limit]:
            state.check()
            entries.append({"name": name, "type": _entry_type(entry)})

        display_path = path_for_comparison(directory_path).relative_to(
            path_for_comparison(workspace.path)
        ).as_posix()
        state.check()
        return {
            "path": display_path,
            "entries": entries,
            "has_more": has_more,
            "next_cursor": entries[-1]["name"] if has_more else None,
        }


def _parse_arguments(
    arguments: dict[str, JSONValue],
) -> tuple[str, str, bool, str | None, int, int]:
    allowed = {"path", "glob", "recursive", "cursor", "limit", "timeout_seconds"}
    if not set(arguments) <= allowed:
        raise ValueError(
            "list_directory accepts only 'path', 'glob', 'recursive', "
            "'cursor', 'limit', and 'timeout_seconds'"
        )
    path = arguments.get("path")
    glob = arguments.get("glob", "*")
    recursive = arguments.get("recursive", False)
    cursor = arguments.get("cursor")
    limit = arguments.get("limit", DEFAULT_LIST_LIMIT)
    timeout_seconds = arguments.get("timeout_seconds", DEFAULT_LIST_TIMEOUT_SECONDS)
    if not isinstance(path, str) or not path:
        raise ValueError("list_directory requires a non-empty string 'path'")
    if not isinstance(glob, str) or not glob:
        raise ValueError("list_directory requires 'glob' to be a non-empty string")
    if not isinstance(recursive, bool):
        raise ValueError("list_directory requires 'recursive' to be a boolean")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("list_directory requires 'cursor' to be a non-empty string")
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
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or timeout_seconds < 1
    ):
        raise ValueError("list_directory requires 'timeout_seconds' to be a positive integer")
    return path, glob, recursive, cursor, limit, timeout_seconds


def _iter_entries(
    directory_path: Path, recursive: bool, state: _ListState,
) -> Iterator[Path]:
    directories = [directory_path]
    while directories:
        state.check()
        current_directory = directories.pop()
        try:
            with os.scandir(current_directory) as entries:
                for item in entries:
                    state.check()
                    entry = Path(item.path)
                    yield entry
                    if (
                        recursive
                        and item.is_dir(follow_symlinks=False)
                        # Windows junctions are directories even with
                        # follow_symlinks=False; never expand their targets.
                        and not (
                            os.name == "nt"
                            and item.stat(follow_symlinks=False).st_reparse_tag
                            == stat.IO_REPARSE_TAG_MOUNT_POINT
                        )
                    ):
                        directories.append(entry)
        except TimeoutError:
            raise
        except OSError:
            continue


def _matching_entries(
    directory_path: Path,
    glob: str,
    recursive: bool,
    cursor: str | None,
    state: _ListState,
) -> Iterator[tuple[str, Path]]:
    for entry in _iter_entries(directory_path, recursive, state):
        relative_path = entry.relative_to(directory_path)
        name = relative_path.as_posix()
        if matches_path_glob(relative_path, glob) and (
            cursor is None or name > cursor
        ):
            yield name, entry


def _entry_type(entry: Path) -> str:
    if entry.is_symlink():
        return "symlink"
    if entry.is_dir():
        return "directory"
    if entry.is_file():
        return "file"
    return "other"
