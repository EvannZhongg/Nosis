"""Publish a workspace deliverable through the shared attachment event stream."""

import mimetypes
import shutil
from dataclasses import asdict
from uuid import uuid4

from ...content import FilePart, ImagePart
from ...media import UnsupportedImageError, image_extension, probe_image
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext


class PresentFileTool(Tool):
    name = "present_file"

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Deliver an existing workspace file to the user as a downloadable "
                "attachment. Use this after creating a document, spreadsheet, archive "
                "or other deliverable. This publishes a snapshot, not just a path."
            ),
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Workspace-relative file path."}},
                "required": ["path"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> ToolOutput:
        path = arguments.get("path")
        if not isinstance(path, str) or not path or set(arguments) != {"path"}:
            raise ValueError("present_file requires only a non-empty string 'path'")
        source = context.workspace.resolve_path(path)
        if not source.is_file():
            raise ValueError(f"file does not exist: {path}")
        try:
            info = probe_image(source, max_bytes=None)
        except UnsupportedImageError:
            info = None
        if info is not None:
            info = probe_image(source)
        root = context.workspace.resolve_path(".nosis/attachments")
        root.mkdir(parents=True, exist_ok=True)
        suffix = image_extension(info.mime_type) if info else source.suffix
        target = root / (uuid4().hex + suffix)
        try:
            shutil.copyfile(source, target)
            part_type = ImagePart if info else FilePart
            part = part_type(
                path=f".nosis/attachments/{target.name}", filename=source.name,
                mime_type=info.mime_type if info else mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                size_bytes=target.stat().st_size,
            )
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return ToolOutput(output=asdict(part), attachments=(part,))
