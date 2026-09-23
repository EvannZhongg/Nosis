"""Generate or edit images and persist them in the workspace."""

import os
import tempfile
from pathlib import Path
from uuid import uuid4

from ...content import ImagePart
from ...media import ImageInfo, image_extension, probe_image
from ...path_utils import path_for_comparison
from ..base import JSONValue, Tool, ToolDefinition, ToolOutput
from ..context import ToolExecutionContext
from ..paths import resolve_readable_path


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
                "images are saved persistently and shown in the conversation."
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

        attachment_root = context.workspace.resolve_path(".nosis/attachments")
        attachment_root.mkdir(parents=True, exist_ok=True)
        stored: list[Path] = []
        attachments = []
        output_images: list[JSONValue] = []
        try:
            for image in generated:
                path, info = _store_image(attachment_root, image.data)
                stored.append(path)
                relative_path = path_for_comparison(path).relative_to(
                    path_for_comparison(context.workspace.path)
                ).as_posix()
                attachments.append(
                    ImagePart(
                        path=relative_path,
                        mime_type=info.mime_type,
                        filename=path.name,
                        size_bytes=info.size_bytes,
                    )
                )
                item: dict[str, JSONValue] = {
                    "path": relative_path,
                    "mime_type": info.mime_type,
                    "width": info.width,
                    "height": info.height,
                    "size_bytes": info.size_bytes,
                }
                if image.revised_prompt is not None:
                    item["revised_prompt"] = image.revised_prompt
                output_images.append(item)
        except BaseException:
            for path in stored:
                path.unlink(missing_ok=True)
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
) -> tuple[Path, ...]:
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
        file_path, _display = resolve_readable_path(path, context)
        probe_image(file_path)
        resolved.append(file_path)
    return tuple(resolved)


def _optional_string(arguments: dict[str, JSONValue], name: str) -> str | None:
    value = arguments.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"generate_image requires '{name}' to be a non-empty string")
    return value.strip()


def _store_image(root: Path, data: bytes) -> tuple[Path, ImageInfo]:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=root,
            prefix=".generated-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        info = probe_image(temporary_path)
        path = root / f"generated-{uuid4().hex}{image_extension(info.mime_type)}"
        os.replace(temporary_path, path)
        temporary_path = None
        return path, info
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
