import errno
import os
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
                "with space (context), + (add), or - (remove)."
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
    lines = patch.splitlines()
    if not lines or lines[0] != "*** Begin Patch":
        raise ValueError("apply_patch patch must start with '*** Begin Patch'")
    if lines[-1] != "*** End Patch":
        raise ValueError("apply_patch patch must end with '*** End Patch'")

    changes: list[_FileChange] = []
    seen_paths: set[str] = set()
    index = 1
    while index < len(lines) - 1:
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
    source = original.splitlines()
    trailing_newline = original.endswith(("\n", "\r"))
    result: list[str] = []
    source_index = 0
    index = 0
    while index < len(body):
        header = body[index]
        if not header.startswith("@@"):
            raise ValueError(
                f"apply_patch expected @@ hunk in '{path}', got {header!r}"
            )
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
        if not old_lines:
            raise ValueError(
                f"apply_patch update hunk for '{path}' needs context or removal"
            )
        match_index = _find_hunk(source, old_lines, source_index, path)
        result.extend(source[source_index:match_index])
        result.extend(new_lines)
        source_index = match_index + len(old_lines)
    result.extend(source[source_index:])
    content = "\n".join(result)
    if trailing_newline and result:
        content += "\n"
    return content


def _find_hunk(
    source: list[str],
    old_lines: list[str],
    start: int,
    path: str,
) -> int:
    matches = [
        index
        for index in range(start, len(source) - len(old_lines) + 1)
        if source[index : index + len(old_lines)] == old_lines
    ]
    if not matches:
        raise ValueError(f"apply_patch hunk did not match '{path}'")
    if len(matches) > 1:
        raise ValueError(f"apply_patch hunk is ambiguous in '{path}'")
    return matches[0]
