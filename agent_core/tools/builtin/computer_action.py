"""Perform a controlled computer action and return the new screen."""

from pathlib import Path

from ...execution.authority import WORKSPACE_ACCESS_AUTHORITY, ExecutionScope
from ...media import UnsupportedImageError
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ..paths import resolve_image


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
                "Operate the visible computer interface, then inspect the "
                "resulting screenshot. The screenshot is the bounding rectangle "
                "of the full virtual desktop. Coordinates are zero-based, "
                "non-negative pixels relative to its top-left corner. The Runtime "
                "maps them to OS input coordinates as (input_x, input_y) = "
                "(x + virtual_desktop_left, y + virtual_desktop_top), including "
                "when a monitor is left of or above the primary display. Use "
                "coordinates from the latest screenshot; a display layout, "
                "resolution, or scale change ends computer control with an error. "
                "The type action inserts committed text through the clipboard, "
                "so it is safe with active keyboard input methods."
            ),
            parameters={
                "type": "object",
                "properties": {
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
                            "non-zero amount. Wait optionally accepts seconds."
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
                            "A key name supported by PyAutoGUI on the current platform, "
                            "such as enter, esc, or tab."
                        ),
                    },
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "description": (
                            "PyAutoGUI key names for a hotkey; Control and Ctrl are "
                            "accepted aliases for ctrl. Use command on macOS and ctrl "
                            "on Windows or Linux."
                        ),
                    },
                    "amount": {
                        "type": "integer",
                        "minimum": -20,
                        "maximum": 20,
                        "description": (
                            "Non-zero vertical wheel amount. Scroll requires x and "
                            "y so the target is independent of prior pointer state."
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
        path = computer(arguments)
        if not isinstance(path, Path):
            raise ValueError("computer control returned an invalid screenshot path")
        try:
            relative = path.resolve().relative_to(context.workspace.path).as_posix()
            part, info = resolve_image(relative, context)
        except (UnsupportedImageError, OSError, ValueError) as error:
            raise ValueError(f"action screenshot is not readable: {error}") from error
        return ToolOutput(
            output={"action": action, "screenshot": part.path, "width": info.width, "height": info.height},
            attachments=(part,),
        )
