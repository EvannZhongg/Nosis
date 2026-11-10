"""Publish a readable file through the shared attachment event stream."""

import mimetypes
import os
from pathlib import Path
from dataclasses import asdict
from uuid import uuid4

from ...content import FilePart, ImagePart
from ...media import UnsupportedImageError, image_extension, probe_image_stream
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ..paths import bind_readable_file
from ...files import bind_file


class PresentFileTool(Tool):
    name = "present_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Deliver an existing workspace file or Session artifact as a downloadable "
                "attachment. Use this after creating a document, spreadsheet, archive "
                "or other deliverable. This publishes a snapshot, not just a path."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {
                    "type": "string",
                    "description": "Workspace-relative file path or .nosis/sessions/... Session artifact path.",
                }},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> ToolOutput:
        path = arguments.get("path")
        if not isinstance(path, str) or not path or set(arguments) != {"path"}:
            raise ValueError("present_file requires only a non-empty string 'path'")
        workspace = context.workspace
        with bind_readable_file(path, context) as (bound, _display):
            name = bound.name
            with os.fdopen(bound.open_read(), "rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                try:
                    info = probe_image_stream(stream, size, max_bytes=None)
                except UnsupportedImageError:
                    info = None
                suffix = image_extension(info.mime_type) if info else Path(name).suffix
                relative = Path(".nosis/attachments") / (uuid4().hex + suffix)
                with bind_file(workspace.path, relative, create_parents=True) as output:
                    stream.seek(0)
                    output.write(stream, replace=False, mode=None)
                part_type = ImagePart if info else FilePart
                part = part_type(
                    path=relative.as_posix(), filename=name,
                    mime_type=info.mime_type if info else mimetypes.guess_type(name)[0] or "application/octet-stream",
                    size_bytes=size,
                )
        return ToolOutput(output=asdict(part), attachments=(part,), present_to_user=True)
