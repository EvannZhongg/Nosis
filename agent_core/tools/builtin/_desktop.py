from dataclasses import asdict

from ...media import UnsupportedImageError
from ..base import JSONValue, ToolOutput
from ..context import DesktopResult, ToolExecutionContext
from ..paths import resolve_image


WINDOW_PARAMETER = {
    "type": "string",
    "minLength": 1,
    "description": "Window selector: hwnd:<decimal> or a unique title/process substring.",
}


def desktop_output(
    result: DesktopResult,
    context: ToolExecutionContext,
    *,
    action: str | None = None,
) -> ToolOutput:
    output: dict[str, JSONValue] = {}
    if action is not None:
        output["action"] = action
        output["observation_changed"] = result.observation_changed
        output["screen_changed"] = result.screen_changed
        if context.computer_windows:
            for key in (
                "window_at_point",
                "window_at_destination",
                "foreground_before",
                "foreground_after",
            ):
                window = getattr(result, key)
                output[key] = asdict(window) if window is not None else None
    if result.window is not None:
        output["window"] = asdict(result.window)
    if result.observation_error is not None:
        output["observation_error"] = result.observation_error
    if result.screenshot is None:
        if action is None:
            raise ValueError("desktop capture returned no screenshot")
        return ToolOutput(output=output)

    try:
        relative = result.screenshot.resolve().relative_to(context.workspace.path).as_posix()
        part, info = resolve_image(relative, context)
    except (UnsupportedImageError, OSError, ValueError) as error:
        if action is None:
            raise ValueError(f"captured screenshot is not readable: {error}") from error
        output["observation_error"] = f"Action ran, but its screenshot is not readable: {error}"
        return ToolOutput(output=output)

    output.update({
        "path" if action is None else "screenshot": part.path,
        "width": info.width,
        "height": info.height,
    })
    if action is None:
        output.update(mime_type=info.mime_type, size_bytes=info.size_bytes)
    if result.origin is not None:
        left, top = result.origin
        output["origin"] = {"x": left, "y": top}
        if result.window is not None:
            output["rect"] = {"left": left, "top": top, "width": info.width, "height": info.height}
    return ToolOutput(output=output, attachments=(part,))
