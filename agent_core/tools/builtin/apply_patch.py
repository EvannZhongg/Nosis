import errno
import os
import re
from dataclasses import dataclass
from pathlib import Path

from ...path_utils import path_for_comparison
from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from .write_file import write_bytes_atomic


@dataclass(frozen=True)
class _FileChange:
    operation: str
    path: str
    content: str | None = None


@dataclass(frozen=True)
class _TextLine:
    text: str
    ending: str


@dataclass(frozen=True)
class _HunkHeader:
    old_start: int
    old_count: int
    new_start: int
    new_count: int


_HUNK_HEADER = re.compile(
    r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?"
)


class ApplyPatchTool(Tool):
    name = "apply_patch"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Apply one patch to workspace files. The patch must use "
                "'*** Begin Patch' and '*** End Patch', with one or more "
                "'*** Add File:', '*** Update File:', or '*** Delete File:' "
                "sections. Update sections contain @@ hunks whose lines start "
                "with space (context), + (add), or - (remove). Standard "
                "unified-diff hunk ranges are validated when present."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "patch": {
                        "type": "string",
                        "description": "Complete patch text to apply.",
                    }
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        if set(arguments) != {"patch"}:
            raise ValueError("apply_patch accepts only the 'patch' argument")
        patch = arguments.get("patch")
        if not isinstance(patch, str) or not patch:
            raise ValueError("apply_patch requires a non-empty string 'patch'")

        changes = _parse_patch(patch, context)
        for change in changes:
            file_path = context.workspace.resolve_path(change.path)
            if change.operation == "deleted":
                file_path.unlink()
            else:
                assert change.content is not None
                write_bytes_atomic(file_path, change.content.encode("utf-8"))

        return {
            "files": [
                {"path": change.path, "operation": change.operation}
                for change in changes
            ]
        }


def _parse_patch(
    patch: str,
    context: ToolExecutionContext,
) -> list[_FileChange]:
    lines = _split_patch_lines(patch)
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("apply_patch patch must start with '*** Begin Patch'")
    if lines[-1] != "*** End Patch":
        raise ValueError("apply_patch patch must end with '*** End Patch'")
    while len(lines) > 2 and not lines[-2]:
        del lines[-2]

    changes: list[_FileChange] = []
    seen_paths: set[str] = set()
    index = 1
    while index < len(lines) - 1:
        if not lines[index]:
            index += 1
            continue
        header = lines[index]
        index += 1
        operation, raw_path = _parse_file_header(header)
        file_path = context.workspace.resolve_path(raw_path)
        display_path = path_for_comparison(file_path).relative_to(
            path_for_comparison(context.workspace.path)
        ).as_posix()
        if display_path in seen_paths:
            raise ValueError(
                f"apply_patch contains multiple operations for '{display_path}'"
            )
        seen_paths.add(display_path)

        body_start = index
        while index < len(lines) - 1 and not lines[index].startswith("*** "):
            index += 1
        body = lines[body_start:index]
        while body and not body[-1]:
            body.pop()
        if operation == "added":
            if file_path.exists():
                raise ValueError(
                    f"apply_patch cannot add existing file '{display_path}'"
                )
            if not file_path.parent.is_dir():
                raise FileNotFoundError(
                    errno.ENOENT,
                    os.strerror(errno.ENOENT),
                    str(file_path.parent),
                )
            if any(not line.startswith("+") for line in body):
                raise ValueError(
                    f"apply_patch add lines for '{display_path}' must start with '+'"
                )
            content = "\n".join(line[1:] for line in body)
            if body:
                content += "\n"
            changes.append(_FileChange(operation, display_path, content))
            continue

        if not file_path.is_file():
            raise ValueError(
                f"apply_patch requires existing file '{display_path}'"
            )
        try:
            original = file_path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError(
                f"apply_patch requires UTF-8 file '{display_path}'"
            ) from error

        if operation == "deleted":
            if body:
                raise ValueError(
                    f"apply_patch delete for '{display_path}' must have no body"
                )
            changes.append(_FileChange(operation, display_path))
            continue

        if not body or not body[0].startswith("@@"):
            raise ValueError(
                f"apply_patch update for '{display_path}' requires an @@ hunk"
            )
        content = _apply_hunks(original, body, display_path)
        changes.append(_FileChange(operation, display_path, content))

    if not changes:
        raise ValueError("apply_patch requires at least one file operation")
    return changes


def _parse_file_header(header: str) -> tuple[str, str]:
    prefixes = {
        "*** Add File: ": "added",
        "*** Update File: ": "updated",
        "*** Delete File: ": "deleted",
    }
    for prefix, operation in prefixes.items():
        if header.startswith(prefix):
            path = header[len(prefix) :]
            if not path:
                break
            return operation, path
    raise ValueError(f"apply_patch invalid file header: {header!r}")


def _apply_hunks(original: str, body: list[str], path: str) -> str:
    source = _split_text_lines(original)
    original_has_trailing_newline = bool(source and source[-1].ending)
    newline = _preferred_newline(source)
    result: list[_TextLine] = []
    source_index = 0
    index = 0
    while index < len(body):
        header = _parse_hunk_header(body[index], path)
        index += 1
        hunk: list[str] = []
        while index < len(body) and not body[index].startswith("@@"):
            line = body[index]
            if not line or line[0] not in " +-":
                raise ValueError(
                    f"apply_patch invalid hunk line in '{path}': {line!r}"
                )
            hunk.append(line)
            index += 1
        old_lines = [line[1:] for line in hunk if line[0] != "+"]
        new_lines = [line[1:] for line in hunk if line[0] != "-"]
        if not old_lines and header is None:
            raise ValueError(
                f"apply_patch update hunk for '{path}' needs context or removal"
            )
        if header is not None:
            if len(old_lines) != header.old_count:
                raise ValueError(
                    f"apply_patch old line count does not match hunk header "
                    f"in '{path}'"
                )
            if len(new_lines) != header.new_count:
                raise ValueError(
                    f"apply_patch new line count does not match hunk header "
                    f"in '{path}'"
                )
            match_index = (
                header.old_start - 1
                if header.old_count
                else header.old_start
            )
            lines_before_hunk = len(result) + match_index - source_index
            expected_new_start = (
                lines_before_hunk + 1
                if header.new_count
                else lines_before_hunk
            )
            if header.new_start != expected_new_start:
                raise ValueError(
                    f"apply_patch new start does not match hunk position "
                    f"in '{path}'"
                )
            if match_index < source_index or not _lines_match(
                source, old_lines, match_index
            ):
                raise ValueError(
                    f"apply_patch hunk range did not match '{path}'"
                )
        else:
            match_index = _find_hunk(source, old_lines, source_index, path)
        result.extend(source[source_index:match_index])
        matched_index = match_index
        for line in hunk:
            if line[0] == " ":
                result.append(source[matched_index])
                matched_index += 1
            elif line[0] == "-":
                matched_index += 1
            else:
                result.append(_TextLine(line[1:], newline))
        source_index = match_index + len(old_lines)
    result.extend(source[source_index:])
    if result and not original_has_trailing_newline:
        result[-1] = _TextLine(result[-1].text, "")
    return "".join(line.text + line.ending for line in result)


def _split_patch_lines(patch: str) -> list[str]:
    lines = patch.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def _split_text_lines(content: str) -> list[_TextLine]:
    lines: list[_TextLine] = []
    start = 0
    for match in re.finditer(r"\r\n|\r|\n", content):
        lines.append(
            _TextLine(content[start : match.start()], match.group())
        )
        start = match.end()
    if start < len(content):
        lines.append(_TextLine(content[start:], ""))
    return lines


def _preferred_newline(lines: list[_TextLine]) -> str:
    counts = {"\r\n": 0, "\n": 0, "\r": 0}
    first: str | None = None
    for line in lines:
        if not line.ending:
            continue
        counts[line.ending] += 1
        if first is None:
            first = line.ending
    if first is None:
        return "\n"
    return max(counts, key=lambda ending: (counts[ending], ending == first))


def _parse_hunk_header(header: str, path: str) -> _HunkHeader | None:
    if header == "@@":
        return None
    match = _HUNK_HEADER.fullmatch(header)
    if match is None:
        raise ValueError(
            f"apply_patch invalid hunk header in '{path}': {header!r}"
        )
    old_start = int(match.group(1))
    old_count = int(match.group(2) or 1)
    new_start = int(match.group(3))
    new_count = int(match.group(4) or 1)
    if old_start < (0 if old_count == 0 else 1) or new_start < (
        0 if new_count == 0 else 1
    ):
        raise ValueError(
            f"apply_patch hunk starts must be positive in '{path}'"
        )
    return _HunkHeader(old_start, old_count, new_start, new_count)


def _find_hunk(
    source: list[_TextLine],
    old_lines: list[str],
    start: int,
    path: str,
) -> int:
    matches = [
        index
        for index in range(start, len(source) - len(old_lines) + 1)
        if _lines_match(source, old_lines, index)
    ]
    if not matches:
        raise ValueError(f"apply_patch hunk did not match '{path}'")
    if len(matches) > 1:
        raise ValueError(f"apply_patch hunk is ambiguous in '{path}'")
    return matches[0]


def _lines_match(
    source: list[_TextLine], old_lines: list[str], start: int
) -> bool:
    return [
        line.text for line in source[start : start + len(old_lines)]
    ] == old_lines
