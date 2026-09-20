"""Read instructions and resources from a registered skill."""

from pathlib import Path

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...path_utils import path_for_comparison
from .read_file import (
    DEFAULT_READ_LIMIT,
    MAX_FILE_SIZE_BYTES,
    content_budget,
    read_text_range,
)


class ReadSkillTool(Tool):
    name = "read_skill"
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return context.skills is not None and bool(context.skills)

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        names = list(context.skills.names) if context.skills else []
        return ToolDefinition(
            name=self.name,
            description=(
                "Read a range of lines from a registered skill's instructions "
                "or a referenced UTF-8 text file. Start with SKILL.md and "
                "load only the ranges and referenced files you need."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "enum": names,
                        "description": "Exact skill name from the catalog.",
                    },
                    "path": {
                        "type": "string",
                        "default": "SKILL.md",
                        "description": (
                            "File path relative to the skill directory."
                        ),
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
                "required": ["name"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> JSONValue:
        name, path, offset, limit = _parse_arguments(arguments)
        if context.skills is None:
            raise ValueError("this runtime has no registered skills")
        skill = context.skills.get(name)
        file_path = _resolve_skill_path(skill.directory, path)
        file_size_bytes = file_path.stat().st_size
        if file_size_bytes > MAX_FILE_SIZE_BYTES:
            raise ValueError(
                "read_skill refused file because its size is "
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
                content = read_text_range(
                    file,
                    offset,
                    limit,
                    content_budget(
                        {
                            "name": skill.identifier,
                            "path": path,
                            "file_size_bytes": file_size_bytes,
                            "content": "",
                        }
                    ),
                )
        except UnicodeDecodeError as error:
            raise ValueError("read_skill requires a UTF-8 text file") from error
        return {
            "name": skill.identifier,
            "path": path,
            "file_size_bytes": file_size_bytes,
            "content": content,
        }


def _parse_arguments(
    arguments: dict[str, JSONValue],
) -> tuple[str, str, int, int]:
    name = arguments.get("name")
    path = arguments.get("path", "SKILL.md")
    offset = arguments.get("offset", 1)
    limit = arguments.get("limit", DEFAULT_READ_LIMIT)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("read_skill requires a non-empty string 'name'")
    if not isinstance(path, str) or not path.strip():
        raise ValueError("read_skill requires a non-empty string 'path'")
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 1
    ):
        raise ValueError(
            "read_skill requires 'offset' to be a positive integer"
        )
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or limit < 1
    ):
        raise ValueError(
            "read_skill requires 'limit' to be a positive integer"
        )
    if not set(arguments) <= {"name", "path", "offset", "limit"}:
        raise ValueError(
            "read_skill accepts only 'name', 'path', 'offset', and 'limit'"
        )
    return name.strip(), path, offset, limit


def _resolve_skill_path(directory: Path, path: str) -> Path:
    if Path(path).is_absolute():
        raise ValueError("read_skill path must be relative to the skill directory")
    resolved = (directory / path).resolve()
    try:
        path_for_comparison(resolved).relative_to(
            path_for_comparison(directory)
        )
    except ValueError as error:
        raise ValueError(
            "read_skill path must stay inside the skill directory"
        ) from error
    if not resolved.is_file():
        raise ValueError(f"skill file does not exist: {path}")
    return resolved
