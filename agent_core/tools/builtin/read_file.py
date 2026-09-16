from pathlib import Path
from typing import TextIO

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ..paths import resolve_readable_path


DEFAULT_READ_LIMIT = 2000
MAX_READ_CHARS = 64 * 1024
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
STATUS_RESERVE_CHARS = 256
DISCARD_CHUNK_CHARS = 8192
TRUNCATED_LINE_NOTICE = " …（该行被截断）"


class ReadFileTool(Tool):
    name = "read_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Read a range of lines from a UTF-8 workspace file with "
                "line numbers. Refuses files larger than 50 MiB and limits "
                "returned content to 64K characters; overlong lines are "
                "truncated with an explicit notice."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path relative to the workspace root.",
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
        file_size_bytes = file_path.stat().st_size
        if file_size_bytes > MAX_FILE_SIZE_BYTES:
            raise ValueError(
                "read_file refused file because its size is "
                f"{file_size_bytes} bytes, exceeding the maximum of "
                f"{MAX_FILE_SIZE_BYTES} bytes"
            )

        try:
            with file_path.open(
                "r",
                encoding="utf-8",
                errors="strict",
                newline=None,
            ) as file:
                content = read_text_range(file, offset, limit)
        except UnicodeDecodeError as error:
            raise ValueError(
                "read_file requires a UTF-8 text file"
            ) from error

        return {
            "path": display_path,
            "file_size_bytes": file_size_bytes,
            "content": content,
        }


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


def read_text_range(file: TextIO, offset: int, limit: int) -> str:
    """Read a bounded, line-numbered range from an open text file."""
    skipped_lines = _skip_lines(file, offset - 1)
    if skipped_lines < offset - 1:
        return f"(End of file — {skipped_lines} lines total)"

    rendered_lines: list[str] = []
    body_chars = 0
    line_number = offset
    eof = False
    character_limit_reached = False
    next_offset: int | None = None

    while len(rendered_lines) < limit:
        prefix = f"{line_number}| "
        separator_chars = 1 if rendered_lines else 0
        available_chars = (
            MAX_READ_CHARS
            - STATUS_RESERVE_CHARS
            - body_chars
            - separator_chars
            - len(prefix)
        )
        if available_chars < len(TRUNCATED_LINE_NOTICE):
            character_limit_reached = True
            next_offset = line_number
            break

        line, line_truncated, eof_after_line = _read_line_bounded(
            file,
            available_chars,
        )
        if line is None:
            eof = True
            break

        if line_truncated:
            line = (
                line[
                    : available_chars - len(TRUNCATED_LINE_NOTICE)
                ]
                + TRUNCATED_LINE_NOTICE
            )
            character_limit_reached = True

        rendered_line = f"{prefix}{line}"
        rendered_lines.append(rendered_line)
        body_chars += separator_chars + len(rendered_line)
        line_number += 1

        if eof_after_line:
            eof = True
            break
        if line_truncated:
            next_offset = line_number
            break

    if not eof and next_offset is None:
        lookahead, _, _ = _read_line_bounded(file, 0)
        if lookahead is None:
            eof = True
        else:
            next_offset = line_number

    end_line = line_number - 1
    status = _status(
        offset=offset,
        end_line=end_line,
        eof=eof,
        next_offset=next_offset,
        character_limit_reached=character_limit_reached,
    )
    content = _join_content(rendered_lines, status)
    if len(content) > MAX_READ_CHARS:
        raise RuntimeError("text range exceeded its output character limit")
    return content


def _skip_lines(file: TextIO, count: int) -> int:
    skipped = 0
    while skipped < count:
        line, _, _ = _read_line_bounded(file, 0)
        if line is None:
            break
        skipped += 1
    return skipped


def _read_line_bounded(
    file: TextIO,
    max_chars: int,
) -> tuple[str | None, bool, bool]:
    chunk = file.readline(max_chars + 1)
    if chunk == "":
        return None, False, True
    if chunk.endswith("\n"):
        return chunk[:-1], False, False
    if len(chunk) <= max_chars:
        return chunk, False, True

    line = chunk[:max_chars]
    while True:
        remainder = file.readline(DISCARD_CHUNK_CHARS)
        if remainder == "":
            return line, True, True
        if remainder.endswith("\n"):
            return line, True, False


def _status(
    offset: int,
    end_line: int,
    eof: bool,
    next_offset: int | None,
    character_limit_reached: bool,
) -> str:
    if eof:
        status = f"End of file — {end_line} lines total"
        if character_limit_reached:
            status += ". character limit reached"
        return f"({status})"

    if end_line < offset:
        status = "No complete lines shown"
    elif offset == end_line:
        status = f"Showing line {end_line}"
    else:
        status = f"Showing lines {offset}-{end_line}"
    if character_limit_reached:
        status += ". character limit reached"
    if next_offset is not None:
        status += f". Use offset={next_offset} to continue"
    return f"({status}.)"


def _join_content(rendered_lines: list[str], status: str) -> str:
    if not rendered_lines:
        return status
    content = "\n".join(rendered_lines)
    return f"{content}\n\n{status}"
