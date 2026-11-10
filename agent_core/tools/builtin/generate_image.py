"""Generate or edit images and persist them in the workspace."""

import os
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from ...content import ImagePart
from ...media import MAX_IMAGE_BYTES, image_extension, probe_image_stream
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ...files import bind_file
from ...path_utils import relative_to_root
from ..paths import bind_readable_file


MAX_IMAGES_PER_CALL = 4
MAX_REFERENCE_IMAGES = 4


class GenerateImageTool(Tool):
    name = "generate_image"

    def available(self, context: ToolExecutionContext) -> bool:
        return context.image_generator is not None

    def definition(self, context: ToolExecutionContext) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=(
                "Generate images from a prompt, or edit existing images by "
                "passing their workspace paths as reference_images. Generated "
                "images are saved persistently for inspection. Call present_file "
                "with a saved path to deliver an image to the user."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Detailed generation or edit instructions, including "
                            "subject, composition, style, colors, and constraints."
                        ),
                    },
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": MAX_REFERENCE_IMAGES,
                        "description": (
                            "Optional workspace or Session image paths to edit or "
                            "use as visual references."
                        ),
                    },
                    "aspect_ratio": {
                        "type": "string",
                        "description": (
                            "Optional aspect ratio such as 1:1, 16:9, or 9:16."
                        ),
                    },
                    "image_size": {
                        "type": "string",
                        "description": (
                            "Optional provider size hint such as 1K, 2K, or 4K."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_IMAGES_PER_CALL,
                        "default": 1,
                    },
                },
                "required": ["prompt"],
                "additionalProperties": False,
            },
        )

    def execute(
        self,
        arguments: dict[str, JSONValue],
        context: ToolExecutionContext,
    ) -> ToolOutput:
        if not set(arguments) <= {
            "prompt",
            "reference_images",
            "aspect_ratio",
            "image_size",
            "count",
        }:
            raise ValueError(
                "generate_image accepts only 'prompt', 'reference_images', "
                "'aspect_ratio', 'image_size', and 'count'"
            )
        prompt = arguments.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("generate_image requires a non-empty string 'prompt'")
        reference_images = _reference_images(
            arguments.get("reference_images"), context
        )
        aspect_ratio = _optional_string(arguments, "aspect_ratio")
        image_size = _optional_string(arguments, "image_size")
        count = arguments.get("count", 1)
        if (
            isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or count > MAX_IMAGES_PER_CALL
        ):
            raise ValueError(
                f"generate_image requires 'count' between 1 and {MAX_IMAGES_PER_CALL}"
            )
        generator = context.image_generator
        if generator is None:
            raise ValueError("this runtime has no image generation provider")

        generated = generator.generate(
            prompt=prompt.strip(),
            reference_images=reference_images,
            aspect_ratio=aspect_ratio,
            image_size=image_size,
            count=count,
        )
        if len(generated) != count:
            raise ValueError(
                "image generation provider returned "
                f"{len(generated)} image(s), expected {count}"
            )

        workspace = context.workspace
        attachments = []
        output_images: list[JSONValue] = []
        with ExitStack() as bindings:
            stored = []
            try:
                for image in generated:
                    info = probe_image_stream(BytesIO(image.data), len(image.data))
                    relative = Path(".nosis/attachments") / f"generated-{uuid4().hex}{image_extension(info.mime_type)}"
                    path = workspace.resolve_path(relative.as_posix())
                    file = bindings.enter_context(bind_file(
                        workspace.path, relative_to_root(path, workspace.path), create_parents=True,
                    ))
                    file.write(image.data, replace=False, mode=None)
                    stored.append(file)
                    relative_path = relative.as_posix()
                    attachments.append(ImagePart(
                        path=relative_path, mime_type=info.mime_type,
                        filename=path.name, size_bytes=info.size_bytes,
                    ))
                    item: dict[str, JSONValue] = {
                        "path": relative_path, "mime_type": info.mime_type,
                        "width": info.width, "height": info.height,
                        "size_bytes": info.size_bytes,
                    }
                    if image.revised_prompt is not None:
                        item["revised_prompt"] = image.revised_prompt
                    output_images.append(item)
            except BaseException:
                for file in stored:
                    file.unlink()
                raise

        return ToolOutput(
            output={
                "images": output_images,
                "model": generator.model,
            },
            attachments=tuple(attachments),
        )


def _reference_images(
    value: JSONValue,
    context: ToolExecutionContext,
) -> tuple[bytes, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("generate_image requires 'reference_images' to be an array")
    if len(value) > MAX_REFERENCE_IMAGES:
        raise ValueError(
            "generate_image accepts at most "
            f"{MAX_REFERENCE_IMAGES} reference images"
        )
    resolved = []
    for path in value:
        if not isinstance(path, str) or not path:
            raise ValueError("each reference image path must be a non-empty string")
        with bind_readable_file(path, context) as (file, _display):
            with os.fdopen(file.open_read(), "rb") as stream:
                data = stream.read(MAX_IMAGE_BYTES + 1)
        probe_image_stream(BytesIO(data), len(data))
        resolved.append(data)
    return tuple(resolved)


def _optional_string(arguments: dict[str, JSONValue], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"generate_image requires '{name}' to be a non-empty string")
    return value.strip()
