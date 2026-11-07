"""Capture the current computer display and show it to the model."""

from ...execution.authority import WORKSPACE_ACCESS_AUTHORITY, ExecutionScope
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ._desktop import WINDOW_PARAMETER, desktop_output


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
                + (
                    " Pass window to crop the visible desktop to that window. The crop "
                    "can include overlapping windows. Use its path as the observation "
                    "and its hwnd as the target in a window-scoped action. The window "
                    "must fit completely within one display."
                    if context.computer_windows else ""
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Short label for this screenshot.",
                    },
                    **({"window": WINDOW_PARAMETER} if context.computer_windows else {}),
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
        window = arguments.get("window")
        if window is not None and (
            not context.computer_windows or not isinstance(window, str) or not window.strip()
        ):
            raise ValueError("window requires a supported, non-empty window selector")
        return desktop_output(capture(label.strip(), window), context)
