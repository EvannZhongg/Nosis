"""Perform a controlled computer action and return the new screen."""

from ...execution.authority import WORKSPACE_ACCESS_AUTHORITY, ExecutionScope
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ._desktop import WINDOW_PARAMETER, desktop_output


class ComputerActionTool(Tool):
    name = "computer_action"

    def available(self, context: ToolExecutionContext) -> bool:
        authority = (
            context.execution_router.authority
            if context.execution_router is not None
            else WORKSPACE_ACCESS_AUTHORITY
        )
        return (
            context.computer is not None
            and context.vision_input
            and authority.allows(ExecutionScope.HOST)
        )

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Operate the visible desktop. By default the result includes a new "
                "screenshot. Coordinates are zero-based original-image pixels. For "
                "the full virtual desktop, the Runtime adds its origin, including "
                "negative screen origins. Pass the source screenshot path as observation "
                "to get a best-effort pre-action change signal. screen_changed compares "
                "the frames immediately before and after the action. Pixel changes "
                "are clues about visible state. Re-observe after an observation_error "
                "because the input may already have been sent. Mouse buttons held by "
                "the tool are released at the end of the turn. A display layout, "
                "resolution, or scale change requires a fresh desktop runtime."
                + (
                    " With window, pass the latest observation path for that window. "
                    "Coordinates are relative to the crop's top-left corner. Input is "
                    "rejected if the window moved or if the point/foreground check "
                    "finds a different window. Window checks are sampled, and the "
                    "result reports window_at_point, foreground_before, and foreground_after "
                    "where applicable. Window-scoped scrolling requires the target to be in the foreground."
                    if context.computer_windows else ""
                )
            ),
            parameters={
                "type": "object",
                "properties": {
                    **({"window": WINDOW_PARAMETER} if context.computer_windows else {}),
                    "observation": {
                        "type": "string",
                        "minLength": 1,
                        "description": "Path of the most recent screenshot used as the coordinate reference.",
                    },
                    "screenshot": {
                        "type": "boolean",
                        "default": True,
                        "description": "Capture and attach the screen after the action.",
                    },
                    "action": {
                        "type": "string",
                        "enum": [
                            "click",
                            "move",
                            "drag",
                            "mouse_down",
                            "mouse_up",
                            "type",
                            "key",
                            "hotkey",
                            "scroll",
                            "wait",
                        ],
                        "description": (
                            "Coordinate actions require x and y. Drag additionally "
                            "requires to_x and to_y. Scroll additionally requires a "
                            "non-zero amount. Mouse up releases a button held by this tool. "
                            "Wait optionally accepts seconds."
                        ),
                    },
                    "x": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "X coordinate in the latest screenshot.",
                    },
                    "y": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Y coordinate in the latest screenshot.",
                    },
                    "to_x": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Drag destination X coordinate.",
                    },
                    "to_y": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Drag destination Y coordinate.",
                    },
                    "button": {"type": "string", "enum": ["left", "middle", "right"]},
                    "clicks": {"type": "integer", "minimum": 1, "maximum": 3},
                    "duration": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 10,
                        "description": "Movement or drag duration in seconds.",
                    },
                    "text": {"type": "string"},
                    "key": {
                        "type": "string",
                        "description": (
                            "A key name supported on the current platform, "
                            "such as enter, esc, or tab."
                        ),
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "Key names for a hotkey; Control and Ctrl are "
                            "accepted aliases for ctrl. Use command on macOS and ctrl "
                            "on Windows or Linux."
                        ),
                    },
                    "amount": {
                        "type": "integer",
                        "minimum": -20,
                        "maximum": 20,
                        "description": (
                            "Non-zero logical wheel detents; positive scrolls up and "
                            "negative scrolls down. Scroll requires x and y so the "
                            "target is independent of prior pointer state."
                        ),
                    },
                    "seconds": {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 10,
                        "description": "Wait duration; defaults to one second.",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext) -> ToolOutput:
        computer = context.computer
        if computer is None:
            raise ValueError("computer control is unavailable")
        action = arguments.get("action")
        if not isinstance(action, str) or action not in {
            "click",
            "move",
            "drag",
            "mouse_down",
            "mouse_up",
            "type",
            "key",
            "hotkey",
            "scroll",
            "wait",
        }:
            raise ValueError("computer_action requires a supported action")
        if "window" in arguments and not context.computer_windows:
            raise ValueError("window-scoped computer control is unavailable on this platform")
        return desktop_output(computer(arguments), context, action=action)
