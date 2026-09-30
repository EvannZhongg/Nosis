"""Capture the current computer display and show it to the model."""

from pathlib import Path

from ...execution.authority import WORKSPACE_ACCESS_AUTHORITY, ExecutionScope
from ...media import UnsupportedImageError
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ..paths import resolve_image


class ComputerScreenshotTool(Tool):
    """Capture a Runtime-provided desktop screenshot as model input."""

    name = "computer_screenshot"

    def available(self, context: ToolExecutionContext) -> bool:
        authority = (
            context.execution_router.authority
            if context.execution_router is not None
            else WORKSPACE_ACCESS_AUTHORITY
        )
        return (
            context.screenshot is not None
            and context.vision_input
            and authority.allows(ExecutionScope.HOST)
        )

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Capture the full virtual desktop and inspect it visually. The "
                "returned image uses the same logical-pixel coordinate space as "
                "computer_action: (0, 0) is the image's top-left corner, even when "
                "the OS virtual desktop begins at a negative coordinate. Use this "
                "when you need to observe the desktop or an active app."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label for this screenshot.",
                    }
                },
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        capture = context.screenshot
        if capture is None:
            raise ValueError("computer screenshot capture is unavailable")
        label = arguments.get("label", "computer")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("computer_screenshot label must be a non-empty string")
        path = capture(label.strip())
        if not isinstance(path, Path):
            raise ValueError("screenshot capture returned an invalid path")
        try:
            relative = path.resolve().relative_to(context.workspace.path).as_posix()
            part, info = resolve_image(relative, context)
        except (UnsupportedImageError, OSError, ValueError) as error:
            raise ValueError(f"captured screenshot is not readable: {error}") from error
        return ToolOutput(
            output={
                "path": part.path,
                "mime_type": info.mime_type,
                "width": info.width,
                "height": info.height,
                "size_bytes": info.size_bytes,
            },
            attachments=(part,),
        )
