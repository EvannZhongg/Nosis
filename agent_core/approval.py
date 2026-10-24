"""Bounded file-change descriptions shared by approval prompts and history."""

import re
from io import StringIO
from itertools import islice
from typing import TYPE_CHECKING

from .execution.router import WORKSPACE_WRITE_TOOLS

if TYPE_CHECKING:
    from .tools.base import ToolCall


_MAX_DESCRIPTION_CHARS = 2048
_PREVIEW_LINES = 3
_PREVIEW_WIDTH = 120
_MAX_FILE_PATHS = 8
_PATCH_FILE_HEADERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")


def approval_description(call: "ToolCall") -> str:
    if call.name not in WORKSPACE_WRITE_TOOLS:
        return f"{call.name}({call.arguments_json})"

    parts = [call.name]
    arguments = call.arguments
    if call.name == "apply_patch":
        patch = arguments.get("patch")
        if isinstance(patch, str):
            files: list[str] = []
            preview: list[str] = []
            file_count = 0
            for line in StringIO(patch):
                if line.startswith(_PATCH_FILE_HEADERS):
                    file_count += 1
                    if len(files) < _MAX_FILE_PATHS:
                        files.append(_clip(line[4:].rstrip("\r\n"), 160))
                elif line.startswith(("+", "-", " ")) and len(preview) < _PREVIEW_LINES:
                    preview.append(_clip(line.rstrip("\r\n"), _PREVIEW_WIDTH))
            parts.extend([f"Patch: {_text_stats(patch)}, {file_count} files", *files])
            if file_count > len(files):
                parts.append(f"... {file_count - len(files)} more files")
            if preview:
                parts.extend(["Change preview (first 3 lines):", *preview])
        else:
            parts.append("Patch: invalid text")
    else:
        parts.append(f"Path: {_clip(str(arguments.get('path', '')), 160)}")
        if call.name == "write_file":
            parts.append(f"Overwrite: {arguments.get('overwrite', False)}")
            fields = (("Content", "content"),)
        else:
            fields = (("Old text", "old_text"), ("New text", "new_text"))
        for label, key in fields:
            text = arguments.get(key)
            if isinstance(text, str):
                parts.append(f"{label}: {_text_stats(text)}")
                preview = [
                    _clip(line.rstrip("\r\n"), _PREVIEW_WIDTH)
                    for line in islice(StringIO(text, newline=None), _PREVIEW_LINES)
                ]
                if preview:
                    parts.extend(["Preview (first 3 lines):", *preview])
            else:
                parts.append(f"{label}: invalid text")
    return _clip("\n".join(parts), _MAX_DESCRIPTION_CHARS)


def _text_stats(text: str) -> str:
    lines = sum(1 for _ in re.finditer(r"\r\n|\r|\n", text))
    if text and not text.endswith(("\r", "\n")):
        lines += 1
    return f"{len(text.encode('utf-8'))} bytes, {lines} lines"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 3] + "..."
