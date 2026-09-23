"""Provider-neutral image generation contracts."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    revised_prompt: str | None = None


class ImageGenerator(Protocol):
    @property
    def model(self) -> str: ...

    def generate(
        self,
        *,
        prompt: str,
        reference_images: tuple[Path, ...] = (),
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int = 1,
    ) -> tuple[GeneratedImage, ...]: ...


__all__ = ["GeneratedImage", "ImageGenerator"]
