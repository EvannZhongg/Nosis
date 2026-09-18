import json
import re
from pathlib import Path

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...path_utils import path_for_comparison


DEFAULT_SEARCH_LIMIT = 200
MAX_SEARCH_LIMIT = 200
MAX_OUTPUT_CHARS = 16 * 1024
MAX_FILE_SIZE_BYTES = 1024 * 1024
MAX_SCANNED_PATHS = 10_000
DEFAULT_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "target",
        "venv",
        "vendor",
    }
)


class SearchFilesTool(Tool):
    name = "search_files"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Search matching UTF-8 workspace files recursively. Supports "
                "regular expressions, fixed strings, glob filtering, and "
                "paginated results."
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
                        "description": "Text or Python regular expression.",
                    },
                    "glob": {
                        "type": "string",
                        "default": "**/*",
                        "description": (
                            "Glob applied to paths relative to 'path'."
                        ),
                    },
                    "offset": {
                        "type": "integer",
                        "minimum": 0,
                        "default": 0,
                        "description": (
                            "Number of matching lines to skip."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_SEARCH_LIMIT,
                        "default": DEFAULT_SEARCH_LIMIT,
                        "description": (
                            "Maximum number of matches to return."
                        ),
                    },
                    "case_insensitive": {
                        "type": "boolean",
                        "default": False,
                        "description": "Match text without case sensitivity.",
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
        workspace = context.workspace
        directory_path = workspace.resolve_path(options["path"])
        if not directory_path.is_dir():
            raise ValueError("search_files path must be a directory")

        matcher = _create_matcher(
            options["pattern"],
            options["case_insensitive"],
            options["fixed_strings"],
        )
        files, skipped_files, path_limit_reached = _collect_files(
            directory_path,
            options["glob"],
        )

        matches: list[dict[str, JSONValue]] = []
        scanned_files = 0
        matching_lines = 0
        has_more = False

        for file_path in files:
            try:
                if file_path.stat().st_size > MAX_FILE_SIZE_BYTES:
                    skipped_files += 1
                    continue
                content = file_path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                skipped_files += 1
                continue

            scanned_files += 1
            for line_number, line in enumerate(
                content.splitlines(),
                start=1,
            ):
                if not matcher(line):
                    continue
                if matching_lines < options["offset"]:
                    matching_lines += 1
                    continue
                if len(matches) >= options["limit"]:
                    has_more = True
                    break

                match = {
                    "path": path_for_comparison(file_path).relative_to(
                        path_for_comparison(workspace.path)
                    ).as_posix(),
                    "line_number": line_number,
                    "line": line,
                }
                if not _result_fits(
                    [*matches, match],
                    options["offset"],
                ):
                    if matches:
                        has_more = True
                        break
                    match = _truncate_match_to_fit(
                        match,
                        options["offset"],
                    )

                matches.append(match)
                matching_lines += 1
            if has_more:
                break

        if path_limit_reached:
            has_more = True

        return {
            "matches": matches,
            "has_more": has_more,
            "next_offset": (
                options["offset"] + len(matches) if has_more else None
            ),
            "scanned_files": scanned_files,
            "skipped_files": skipped_files,
        }


def _parse_arguments(arguments: dict[str, JSONValue]) -> dict[str, object]:
    allowed = {
        "path",
        "pattern",
        "glob",
        "offset",
        "limit",
        "case_insensitive",
        "fixed_strings",
    }
    if not set(arguments) <= allowed:
        raise ValueError(
            "search_files accepts only 'path', 'pattern', 'glob', "
            "'offset', 'limit', 'case_insensitive', and 'fixed_strings'"
        )

    path = arguments.get("path")
    pattern = arguments.get("pattern")
    glob = arguments.get("glob", "**/*")
    offset = arguments.get("offset", 0)
    limit = arguments.get("limit", DEFAULT_SEARCH_LIMIT)
    case_insensitive = arguments.get("case_insensitive", False)
    fixed_strings = arguments.get("fixed_strings", False)

    if not isinstance(path, str) or not path:
        raise ValueError(
            "search_files requires a non-empty string 'path'"
        )
    if not isinstance(pattern, str) or not pattern:
        raise ValueError(
            "search_files requires a non-empty string 'pattern'"
        )
    if not isinstance(glob, str) or not glob:
        raise ValueError(
            "search_files requires 'glob' to be a non-empty string"
        )
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise ValueError(
            "search_files requires 'offset' to be a non-negative integer"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
        or limit > MAX_SEARCH_LIMIT
    ):
        raise ValueError(
            f"search_files requires 'limit' to be between 1 and "
            f"{MAX_SEARCH_LIMIT}"
        )
    if not isinstance(case_insensitive, bool):
        raise ValueError(
            "search_files requires 'case_insensitive' to be a boolean"
        )
    if not isinstance(fixed_strings, bool):
        raise ValueError(
            "search_files requires 'fixed_strings' to be a boolean"
        )

    return {
        "path": path,
        "pattern": pattern,
        "glob": glob,
        "offset": offset,
        "limit": limit,
        "case_insensitive": case_insensitive,
        "fixed_strings": fixed_strings,
    }


def _create_matcher(
    pattern: object,
    case_insensitive: object,
    fixed_strings: object,
):
    assert isinstance(pattern, str)
    assert isinstance(case_insensitive, bool)
    assert isinstance(fixed_strings, bool)

    if fixed_strings:
        expected = pattern.casefold() if case_insensitive else pattern

        def matches(line: str) -> bool:
            candidate = line.casefold() if case_insensitive else line
            return expected in candidate

        return matches

    flags = re.IGNORECASE if case_insensitive else 0
    expression = re.compile(pattern, flags)
    return expression.search


def _collect_files(
    directory_path: Path,
    glob: object,
) -> tuple[list[Path], int, bool]:
    assert isinstance(glob, str)
    files = []
    skipped_files = 0
    scanned_paths = 0
    directories = [directory_path]

    while directories:
        current_directory = directories.pop()
        try:
            entries = sorted(
                current_directory.iterdir(),
                key=lambda path: path.name,
            )
        except OSError:
            continue

        child_directories = []
        for entry in entries:
            if scanned_paths >= MAX_SCANNED_PATHS:
                return (
                    sorted(files, key=lambda path: path.as_posix()),
                    skipped_files,
                    True,
                )
            scanned_paths += 1

            relative_path = entry.relative_to(directory_path)
            if entry.is_symlink() or _escapes_search_root(
                entry,
                directory_path,
            ):
                if _matches_glob(relative_path, glob):
                    skipped_files += 1
                continue
            if entry.is_dir():
                if entry.name not in DEFAULT_EXCLUDED_DIRECTORIES:
                    child_directories.append(entry)
                continue
            if entry.is_file() and _matches_glob(relative_path, glob):
                files.append(entry)

        directories.extend(reversed(child_directories))

    return (
        sorted(files, key=lambda path: path.as_posix()),
        skipped_files,
        False,
    )


def _escapes_search_root(entry: Path, search_root: Path) -> bool:
    """Report whether *entry* resolves to something outside the search root.

    Directory junctions on Windows are reparse points that are not
    symlinks, so the entry has to be resolved to catch them.
    """
    return not path_for_comparison(entry.resolve()).is_relative_to(
        path_for_comparison(search_root)
    )


def _matches_glob(path: Path, pattern: str) -> bool:
    if path.match(pattern):
        return True
    if pattern.startswith("**/"):
        return path.match(pattern[3:])
    return False


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
            "scanned_files": MAX_SCANNED_PATHS,
            "skipped_files": MAX_SCANNED_PATHS,
        },
        {
            "matches": matches,
            "has_more": False,
            "next_offset": None,
            "scanned_files": MAX_SCANNED_PATHS,
            "skipped_files": MAX_SCANNED_PATHS,
        },
    )
    return all(
        len(
            json.dumps(
                {"ok": True, "output": result},
                ensure_ascii=False,
            )
        )
        <= MAX_OUTPUT_CHARS
        for result in possible_results
    )


def _truncate_match_to_fit(
    match: dict[str, JSONValue],
    offset: object,
) -> dict[str, JSONValue]:
    assert isinstance(offset, int)
    line = match["line"]
    assert isinstance(line, str)

    low = 0
    high = len(line)
    while low < high:
        middle = (low + high + 1) // 2
        candidate = {
            **match,
            "line": line[:middle],
            "line_truncated": True,
        }
        if _result_fits(
            [candidate],
            offset,
        ):
            low = middle
        else:
            high = middle - 1

    truncated = {
        **match,
        "line": line[:low],
        "line_truncated": True,
    }
    if not _result_fits(
        [truncated],
        offset,
    ):
        raise ValueError("search_files match path exceeds output limit")
    return truncated
