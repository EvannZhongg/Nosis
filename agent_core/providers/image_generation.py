"""Image generation through LiteLLM's provider adapters."""

import base64
from pathlib import Path

import httpx
from litellm import image_edit, image_generation

from ..image_generation import GeneratedImage
from ..media import MAX_IMAGE_BYTES
from ..public_url import require_public_url


class LiteLLMImageGenerator:
    def __init__(
        self,
        model: str,
        base_url: str | None = None,
        api_key: str | None = None,
        default_aspect_ratio: str | None = None,
        default_image_size: str | None = None,
        timeout_seconds: float = 600,
    ) -> None:
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._default_aspect_ratio = default_aspect_ratio
        self._default_image_size = default_image_size
        self._timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        *,
        prompt: str,
        reference_images: tuple[Path, ...] = (),
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int = 1,
    ) -> tuple[GeneratedImage, ...]:
        image_config = {
            key: value
            for key, value in {
                "aspect_ratio": aspect_ratio or self._default_aspect_ratio,
                "image_size": image_size or self._default_image_size,
            }.items()
            if value is not None
        }
        common = {
            "model": self._model,
            "n": count,
            "api_base": self._base_url,
            "api_key": self._api_key,
            "timeout": self._timeout_seconds,
        }
        if reference_images:
            response = image_edit(
                image=list(reference_images),
                prompt=prompt,
                extra_body=(
                    {"image_config": image_config} if image_config else None
                ),
                **common,
            )
        else:
            response = image_generation(
                prompt=prompt,
                **common,
                **({"image_config": image_config} if image_config else {}),
            )

        images = []
        for item in getattr(response, "data", ()) or ():
            payload = _field(item, "b64_json")
            url = _field(item, "url")
            if isinstance(payload, str) and payload:
                data = _decode_base64(payload)
            elif isinstance(url, str) and url:
                data = _read_image_url(url, self._timeout_seconds)
            else:
                continue
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    f"generated image is {len(data)} bytes, exceeding the "
                    f"maximum of {MAX_IMAGE_BYTES} bytes"
                )
            revised_prompt = _field(item, "revised_prompt")
            images.append(
                GeneratedImage(
                    data=data,
                    revised_prompt=(
                        revised_prompt
                        if isinstance(revised_prompt, str) and revised_prompt
                        else None
                    ),
                )
            )
            if len(images) >= count:
                break
        if len(images) < count:
            raise ValueError(
                "image generation provider returned "
                f"{len(images)} image(s), expected {count}"
            )
        return tuple(images[:count])


def _field(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _decode_base64(value: str) -> bytes:
    payload = (
        value.split(",", 1)[1]
        if value.startswith("data:") and "," in value
        else value
    )
    try:
        return base64.b64decode(payload, validate=True)
    except ValueError as error:
        raise ValueError(
            "image generation provider returned invalid base64 data"
        ) from error


def _read_image_url(url: str, timeout_seconds: float) -> bytes:
    if url.startswith("data:"):
        return _decode_base64(url)
    require_public_url(url, allowed_schemes=frozenset({"https"}))
    with httpx.stream(
        "GET", url, timeout=timeout_seconds, follow_redirects=False
    ) as response:
        if response.is_redirect:
            raise ValueError(
                "image generation provider returned a redirect image URL"
            )
        response.raise_for_status()
        data = bytearray()
        for chunk in response.iter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError(
                    f"generated image exceeds the maximum of {MAX_IMAGE_BYTES} bytes"
                )
    return bytes(data)
__all__ = ["LiteLLMImageGenerator"]
