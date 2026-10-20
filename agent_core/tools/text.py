"""Bounded text views shared by file tools and Session artifacts."""

from pathlib import Path
from typing import TextIO

from .base import JSONValue
from .budget import MAX_TOOL_RESULT_CHARS, escaped_length, envelope_chars


DEFAULT_READ_LIMIT = 2000
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
# Even six-character JSON escapes leave room for metadata and line numbers.
MAX_ARTIFACT_LINE_CHARS = MAX_TOOL_RESULT_CHARS // 8
STATUS_RESERVE_CHARS = 256
DISCARD_CHUNK_CHARS = 8192
TRUNCATED_LINE_NOTICE = " …（该行被截断）"


def read_text_file(
    path: Path,
    metadata: dict[str, JSONValue],
    offset: int,
    limit: int,
    *,
    tool_name: str,
) -> dict[str, JSONValue]:
    size = path.stat().st_size
    if size > MAX_FILE_SIZE_BYTES:
        raise ValueError(
            f"{tool_name} refused file because its size is "
            f"{size} bytes, exceeding the maximum of {MAX_FILE_SIZE_BYTES} bytes"
        )
    output: dict[str, JSONValue] = {
        **metadata, "file_size_bytes": size, "content": ""
    }
    budget = MAX_TOOL_RESULT_CHARS - envelope_chars(output) - STATUS_RESERVE_CHARS
    try:
        with path.open("r", encoding="utf-8", errors="strict", newline=None) as file:
            output["content"] = read_text_range(file, offset, limit, budget)
    except UnicodeDecodeError as error:
        raise ValueError(f"{tool_name} requires a UTF-8 text file") from error
    return output


def write_text_artifact(path: Path, source: TextIO) -> int:
    """Write a reading view with every physical line bounded.

    Long lines are split without dropping characters. Existing line breaks
    remain in place; this is a text view, not a machine-readable serialization.
    """
    size = 0
    with path.open("w", encoding="utf-8", newline="\n") as file:
        chunk = source.readline(MAX_ARTIFACT_LINE_CHARS + 1)
        while chunk:
            if len(chunk) > MAX_ARTIFACT_LINE_CHARS and not chunk.endswith("\n"):
                text = chunk[:MAX_ARTIFACT_LINE_CHARS] + "\n"
                chunk = chunk[MAX_ARTIFACT_LINE_CHARS:] + source.readline(
                    MAX_ARTIFACT_LINE_CHARS
                )
            else:
                text = chunk
                chunk = source.readline(MAX_ARTIFACT_LINE_CHARS + 1)
            file.write(text)
            size += len(text)
    return size


def read_text_range(
    file: TextIO,
    offset: int,
    limit: int,
    budget: int,
) -> str:
    """Read a bounded, line-numbered range from an open text file.

    *budget* counts characters as they will appear inside the JSON
    result, not as they appear in the file, so a line dense with quotes
    or newlines consumes what it will actually cost.
    """
    skipped_lines = _skip_lines(file, offset - 1)
    if skipped_lines < offset - 1:
        return f"(End of file — {skipped_lines} lines total)"

    notice_chars = escaped_length(TRUNCATED_LINE_NOTICE)
    rendered_lines: list[str] = []
    body_chars = 0
    line_number = offset
    eof = False
    character_limit_reached = False
    next_offset: int | None = None

    while len(rendered_lines) < limit:
        prefix = f"{line_number}| "
        # The separator is a newline, which costs two characters escaped.
        separator_chars = 2 if rendered_lines else 0
        available_chars = (
            budget
            - body_chars
            - separator_chars
            - len(prefix)
        )
        if available_chars < notice_chars:
            character_limit_reached = True
            next_offset = line_number
            break

        # A line that does not fit here may still fit on a page of its
        # own, so remember where it starts and hand it to the next call
        # whole rather than truncating what the budget could carry.
        line_start = file.tell()
        line, line_truncated, eof_after_line = _read_line_bounded(
            file,
            budget - len(prefix),
        )
        if line is None:
            eof = True
            break

        escaped_chars = escaped_length(line)
        if (line_truncated or escaped_chars > available_chars) and (
            rendered_lines
        ):
            file.seek(line_start)
            next_offset = line_number
            character_limit_reached = True
            break

        if line_truncated or escaped_chars > available_chars:
            # Alone on its page and still too long: truncation is the
            # only way to make progress.
            line = (
                _trim_to_escaped(line, available_chars - notice_chars)
                + TRUNCATED_LINE_NOTICE
            )
            escaped_chars = escaped_length(line)
            line_truncated = True
            character_limit_reached = True

        rendered_line = f"{prefix}{line}"
        rendered_lines.append(rendered_line)
        body_chars += separator_chars + len(prefix) + escaped_chars
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
    return _join_content(rendered_lines, status)


def _trim_to_escaped(line: str, budget: int) -> str:
    """Return the longest prefix of *line* that fits *budget* escaped."""
    used = 0
    for index, character in enumerate(line):
        cost = escaped_length(character)
        if used + cost > budget:
            return line[:index]
        used += cost
    return line


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
