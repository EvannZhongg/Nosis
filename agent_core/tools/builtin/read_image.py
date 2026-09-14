"""Put an image from the workspace into the model's own context."""

from ...media import UnsupportedImageError
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ..paths import resolve_image


#: Images a single call may load.  Each one stays inline for the rest of
#: the turn, so an agentic turn that reads directory after directory
#: would otherwise accumulate them without bound.
MAX_IMAGES_PER_CALL = 4


class ReadImageTool(Tool):
    """Load image files so the model can look at them directly.

    This is the counterpart to ``analyze_image``: it exists only for a
    model that accepts image input, and hands over the pixels rather
    than a second model's description of them.
    """

    name = "read_image"

    #: Reading is side-effect free, so a batch may resolve in parallel.
    concurrent = True

    def available(self, context: ToolExecutionContext) -> bool:
        return context.vision_input

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Look at image files yourself. Supports PNG, JPEG, GIF "
                "and WebP up to 5 MiB, at most "
                f"{MAX_IMAGES_PER_CALL} per call. The images are added to "
                "your context for the remainder of the current turn, so "
                "describe anything you need to remember about them in "
                "your reply."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": MAX_IMAGES_PER_CALL,
                        "description": (
                            "Image paths relative to the workspace root."
                        ),
                    },
                },
                "required": ["paths"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        paths = _parse_paths(arguments)
        loaded: list[JSONValue] = []
        failed: list[JSONValue] = []
        attachments = []
        for path in paths:
            try:
                part, info = resolve_image(path, context)
            except (UnsupportedImageError, ValueError) as error:
                # One unreadable path must not discard the images that
                # resolved alongside it; the model is told which failed.
                failed.append({"path": path, "error": str(error)})
                continue
            attachments.append(part)
            loaded.append(
                {
                    "path": part.path,
                    "mime_type": info.mime_type,
                    "width": info.width,
                    "height": info.height,
                    "size_bytes": info.size_bytes,
                }
            )
        if not attachments:
            raise ValueError(
                "read_image could not read any of the requested images: "
                + "; ".join(
                    f"{item['path']}: {item['error']}"  # type: ignore[index]
                    for item in failed
                )
            )
        output: dict[str, JSONValue] = {"images": loaded}
        if failed:
            output["failed"] = failed
        return ToolOutput(output=output, attachments=tuple(attachments))


def _parse_paths(arguments: dict[str, JSONValue]) -> list[str]:
    paths = arguments.get("paths")
    if isinstance(paths, str):
        paths = [paths]
    if not isinstance(paths, list) or not paths:
        raise ValueError("read_image requires a non-empty 'paths' array")
    if len(paths) > MAX_IMAGES_PER_CALL:
        raise ValueError(
            f"read_image accepts at most {MAX_IMAGES_PER_CALL} images per "
            f"call, received {len(paths)}"
        )
    result = []
    for path in paths:
        if not isinstance(path, str) or not path:
            raise ValueError("each read_image path must be a non-empty string")
        result.append(path)
    # An identical path twice would spend the context of two images to
    # show the model one.
    return list(dict.fromkeys(result))
