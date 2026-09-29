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
                "resulting screenshot. Coordinates refer to the latest screenshot."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["click", "move", "type", "key", "hotkey", "scroll"]},
                    "x": {"type": "integer", "minimum": 0},
                    "y": {"type": "integer", "minimum": 0},
                    "button": {"type": "string", "enum": ["left", "middle", "right"]},
                    "clicks": {"type": "integer", "minimum": 1, "maximum": 3},
                    "text": {"type": "string"},
                    "key": {"type": "string"},
                    "keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                    "amount": {"type": "integer", "minimum": -20, "maximum": 20},
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
        if not isinstance(action, str) or action not in {"click", "move", "type", "key", "hotkey", "scroll"}:
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
