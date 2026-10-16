import os
import re
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from ...execution.process import CancellationSignal
from ...path_utils import path_for_comparison
from ...turn_control import AgentCancelled
from ..base import JSONValue, Tool, ToolDefinition
from ..budget import output_fits
from ..context import ToolExecutionContext
from ..globs import matches_path_glob


DEFAULT_SEARCH_LIMIT = 200
MAX_SEARCH_LIMIT = 200
MAX_CONTEXT_LINES = 20
MAX_FILE_SIZE_BYTES = 1024 * 1024
DEFAULT_SEARCH_TIMEOUT_SECONDS = 60
DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".npm",
        ".pnpm-store",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".yarn",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "pip-cache",
        "target",
        "uv-cache",
        "venv",
        "vendor",
    }
)


@dataclass
class _SearchState:
    deadline: float
    timeout_seconds: int
    cancellation: CancellationSignal | None
    skipped_files: int = 0

    def check(self) -> None:
        if self.cancellation is not None and self.cancellation.cancelled:
            raise AgentCancelled
        if time.monotonic() >= self.deadline:
            raise TimeoutError(
                f"search_files timed out after {self.timeout_seconds} seconds"
            )


class SearchFilesTool(Tool):
    name = "search_files"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Search workspace files recursively. In content mode, "
                "pattern matches lines in UTF-8 files and optional context "
                "lines are returned around each hit. In files mode, pattern "
                "matches file paths relative to the requested directory. "
                "Glob filtering, regular expressions, fixed strings, and "
                "paginated matches are supported. Symlinks, directory "
                "junctions, and common dependency/cache/build directories "
                "are skipped. Searches time out after "
                f"{DEFAULT_SEARCH_TIMEOUT_SECONDS} seconds by default."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Directory path relative to the workspace root."
                        ),
                    },
                    "pattern": {
                        "type": "string",
                        "description": (
                            "Text or Python regular expression matched against "
                            "lines in content mode or relative paths in files "
                            "mode."
                        ),
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["content", "files"],
                        "default": "content",
                        "description": "Search file contents or file paths.",
                    },
                    "glob": {
                        "type": "string",
                        "default": "**/*",
                        "description": (
                            "Full-path glob relative to 'path'. '*' matches "
                            "one path segment; use '**' for recursive segments."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": "Number of matches to skip.",
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "default": DEFAULT_SEARCH_LIMIT,
                        "description": "Maximum number of matches to return.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "minimum": 1,
                        "default": DEFAULT_SEARCH_TIMEOUT_SECONDS,
                        "description": (
                            "Search time budget in seconds. Exceeding the "
                            "budget returns a timeout error."
                        ),
                    },
                    "context_before": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_CONTEXT_LINES,
                        "default": 0,
                        "description": (
                            "Content mode only: lines to return before each hit."
                        ),
                    },
                    "context_after": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": MAX_CONTEXT_LINES,
                        "default": 0,
                        "description": (
                            "Content mode only: lines to return after each hit."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Match without case sensitivity.",
                    },
                    "fixed_strings": {
                        "type": "boolean",
                        "default": False,
                        "description": (
                            "Treat pattern as literal text instead of a "
                            "regular expression."
                        ),
                    },
                },
                "required": ["path", "pattern"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        options = _parse_arguments(arguments)
        timeout_seconds = options["timeout_seconds"]
        assert isinstance(timeout_seconds, int)
        state = _SearchState(
            deadline=time.monotonic() + timeout_seconds,
            timeout_seconds=timeout_seconds,
            cancellation=context.cancellation,
        )
        state.check()
        workspace = context.workspace
        directory_path = workspace.resolve_path(options["path"])
        if not directory_path.is_dir():
            raise ValueError("search_files path must be a directory")

        matcher = _create_matcher(
            options["pattern"],
            options["case_insensitive"],
            options["fixed_strings"],
        )
        files = _iter_files(directory_path, options["glob"], state)
        workspace_root = path_for_comparison(workspace.path)
        matches: list[dict[str, JSONValue]] = []
        scanned_files = 0
        seen_matches = 0
        has_more = False

        for file_path in files:
            state.check()
            relative_search_path = file_path.relative_to(
                directory_path
            ).as_posix()
            workspace_path = path_for_comparison(file_path).relative_to(
                workspace_root
            ).as_posix()
            if options["mode"] == "files":
                scanned_files += 1
                candidates = (
                    iter(({"path": workspace_path},))
                    if matcher(relative_search_path)
                    else iter(())
                )
            else:
                try:
                    if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                        state.skipped_files += 1
                        continue
                    lines = file_path.read_bytes().decode("utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    state.skipped_files += 1
                    continue
                scanned_files += 1
                candidates = _content_matches(
                    workspace_path,
                    lines,
                    matcher,
                    options["context_before"],
                    options["context_after"],
                    state,
                )

            for match in candidates:
                state.check()
                if seen_matches < options["offset"]:
                    seen_matches += 1
                    continue
                if len(matches) >= options["limit"]:
                    has_more = True
                    break
                if not _result_fits([*matches, match], options["offset"]):
                    if matches:
                        has_more = True
                        break
                    match = _truncate_match_to_fit(match, options["offset"])
                matches.append(match)
                seen_matches += 1
            if has_more:
                break

        state.check()
        return {
            "matches": matches,
            "has_more": has_more,
            "next_offset": (
                options["offset"] + len(matches) if has_more else None
            ),
            "scanned_files": scanned_files,
            "skipped_files": state.skipped_files,
        }


def _parse_arguments(arguments: dict[str, JSONValue]) -> dict[str, object]:
    allowed = {
        "path",
        "pattern",
        "mode",
        "glob",
        "offset",
        "limit",
        "timeout_seconds",
        "context_before",
        "context_after",
        "case_insensitive",
        "fixed_strings",
    }
    if not set(arguments) <= allowed:
        raise ValueError(
            "search_files accepts only 'path', 'pattern', 'mode', 'glob', "
            "'offset', 'limit', 'timeout_seconds', 'context_before', "
            "'context_after', 'case_insensitive', and 'fixed_strings'"
        )

    options: dict[str, object] = {
        "path": arguments.get("path"),
        "pattern": arguments.get("pattern"),
        "mode": arguments.get("mode", "content"),
        "glob": arguments.get("glob", "**/*"),
        "offset": arguments.get("offset", 0),
        "limit": arguments.get("limit", DEFAULT_SEARCH_LIMIT),
        "timeout_seconds": arguments.get(
            "timeout_seconds", DEFAULT_SEARCH_TIMEOUT_SECONDS
        ),
        "context_before": arguments.get("context_before", 0),
        "context_after": arguments.get("context_after", 0),
        "case_insensitive": arguments.get("case_insensitive", False),
        "fixed_strings": arguments.get("fixed_strings", False),
    }
    if not isinstance(options["path"], str) or not options["path"]:
        raise ValueError("search_files requires a non-empty string 'path'")
    if not isinstance(options["pattern"], str) or not options["pattern"]:
        raise ValueError("search_files requires a non-empty string 'pattern'")
    if options["mode"] not in ("content", "files"):
        raise ValueError("search_files requires 'mode' to be 'content' or 'files'")
    if not isinstance(options["glob"], str) or not options["glob"]:
        raise ValueError("search_files requires 'glob' to be a non-empty string")
    _validate_bounded_integer(options["offset"], "offset", minimum=0)
    _validate_bounded_integer(
        options["timeout_seconds"], "timeout_seconds", minimum=1
    )
    _validate_bounded_integer(
        options["limit"], "limit", minimum=1, maximum=MAX_SEARCH_LIMIT
    )
    _validate_bounded_integer(
        options["context_before"],
        "context_before",
        minimum=0,
        maximum=MAX_CONTEXT_LINES,
    )
    _validate_bounded_integer(
        options["context_after"],
        "context_after",
        minimum=0,
        maximum=MAX_CONTEXT_LINES,
    )
    if options["mode"] == "files" and (
        options["context_before"] or options["context_after"]
    ):
        raise ValueError(
            "search_files context is available only in content mode"
        )
    if not isinstance(options["case_insensitive"], bool):
        raise ValueError(
            "search_files requires 'case_insensitive' to be a boolean"
        )
    if not isinstance(options["fixed_strings"], bool):
        raise ValueError("search_files requires 'fixed_strings' to be a boolean")
    return options


def _validate_bounded_integer(
    value: object,
    name: str,
    *,
    minimum: int,
    maximum: int | None = None,
) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(
            f"search_files requires '{name}' to be an integer >= {minimum}"
        )
    if maximum is not None and value > maximum:
        raise ValueError(
            f"search_files requires '{name}' to be between {minimum} and "
            f"{maximum}"
        )


def _create_matcher(
    pattern: object,
    case_insensitive: object,
    fixed_strings: object,
) -> Callable[[str], object]:
    assert isinstance(pattern, str)
    assert isinstance(case_insensitive, bool)
    assert isinstance(fixed_strings, bool)
    if fixed_strings:
        expected = pattern.casefold() if case_insensitive else pattern

        def matches(value: str) -> bool:
            candidate = value.casefold() if case_insensitive else value
            return expected in candidate

        return matches
    flags = re.IGNORECASE if case_insensitive else 0
    return re.compile(pattern, flags).search


def _iter_files(
    directory_path: Path,
    glob: object,
    state: _SearchState,
) -> Iterator[Path]:
    assert isinstance(glob, str)
    yield from _iter_directory_files(directory_path, directory_path, glob, state)


def _iter_directory_files(
    current_directory: Path,
    search_root: Path,
    glob: str,
    state: _SearchState,
) -> Iterator[Path]:
    state.check()
    try:
        with os.scandir(current_directory) as iterator:
            entries = []
            for entry in iterator:
                state.check()
                entries.append(entry)
        entries.sort(key=lambda entry: entry.name)
    except TimeoutError:
        raise
    except OSError:
        return
    for entry in entries:
        state.check()
        entry_path = Path(entry.path)
        relative_path = entry_path.relative_to(search_root)
        try:
            # Windows scandir caches the reparse tag; junctions are not
            # symlinks and must be rejected before following a directory.
            linked = entry.is_symlink() or (
                os.name == "nt"
                and entry.stat(follow_symlinks=False).st_reparse_tag
                == stat.IO_REPARSE_TAG_MOUNT_POINT
            )
            is_directory = not linked and entry.is_dir(follow_symlinks=False)
            is_file = not linked and entry.is_file(follow_symlinks=False)
        except OSError:
            if matches_path_glob(relative_path, glob):
                state.skipped_files += 1
            continue
        if linked:
            if matches_path_glob(relative_path, glob):
                state.skipped_files += 1
        elif is_directory:
            if entry.name not in DEFAULT_EXCLUDED_DIRECTORIES:
                yield from _iter_directory_files(
                    entry_path, search_root, glob, state
                )
        elif is_file and matches_path_glob(relative_path, glob):
            yield entry_path


def _content_matches(
    path: str,
    lines: list[str],
    matcher: Callable[[str], object],
    context_before: object,
    context_after: object,
    state: _SearchState,
) -> Iterator[dict[str, JSONValue]]:
    assert isinstance(context_before, int)
    assert isinstance(context_after, int)
    for index, line in enumerate(lines):
        state.check()
        if not matcher(line):
            continue
        match: dict[str, JSONValue] = {
            "path": path,
            "line_number": index + 1,
            "line": line,
        }
        if context_before:
            start = max(0, index - context_before)
            match["context_before"] = [
                {"line_number": line_index + 1, "line": lines[line_index]}
                for line_index in range(start, index)
            ]
        if context_after:
            stop = min(len(lines), index + context_after + 1)
            match["context_after"] = [
                {"line_number": line_index + 1, "line": lines[line_index]}
                for line_index in range(index + 1, stop)
            ]
        yield match


def _result_fits(
    matches: list[dict[str, JSONValue]],
    offset: object,
) -> bool:
    assert isinstance(offset, int)
    possible_results = (
        {
            "matches": matches,
            "has_more": True,
            "next_offset": offset + len(matches),
            "scanned_files": 999_999_999,
            "skipped_files": 999_999_999,
        },
        {
            "matches": matches,
            "has_more": False,
            "next_offset": None,
            "scanned_files": 999_999_999,
            "skipped_files": 999_999_999,
        },
    )
    return all(output_fits(result) for result in possible_results)


def _truncate_match_to_fit(
    match: dict[str, JSONValue],
    offset: object,
) -> dict[str, JSONValue]:
    assert isinstance(offset, int)
    strings = [match.get("line")]
    for key in ("context_before", "context_after"):
        context_lines = match.get(key, [])
        assert isinstance(context_lines, list)
        strings.extend(
            item.get("line")
            for item in context_lines
            if isinstance(item, dict)
        )
    lengths = [len(value) for value in strings if isinstance(value, str)]
    low = 0
    high = max(lengths, default=0)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = _truncate_match_lines(match, middle)
        if _result_fits([candidate], offset):
            low = middle
        else:
            high = middle - 1
    truncated = _truncate_match_lines(match, low)
    if not _result_fits([truncated], offset):
        raise ValueError("search_files match path exceeds output limit")
    return truncated


def _truncate_match_lines(
    match: dict[str, JSONValue], limit: int
) -> dict[str, JSONValue]:
    result = dict(match)
    line = result.get("line")
    if isinstance(line, str) and len(line) > limit:
        result["line"] = line[:limit]
        result["line_truncated"] = True
    for key in ("context_before", "context_after"):
        context_lines = result.get(key)
        if not isinstance(context_lines, list):
            continue
        truncated_lines: list[JSONValue] = []
        for item in context_lines:
            assert isinstance(item, dict)
            context_line = item.get("line")
            truncated_item = dict(item)
            if isinstance(context_line, str) and len(context_line) > limit:
                truncated_item["line"] = context_line[:limit]
                truncated_item["line_truncated"] = True
            truncated_lines.append(truncated_item)
        result[key] = truncated_lines
    return result
