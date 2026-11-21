"""Image generation through LiteLLM's provider adapters."""

import base64
import math
import re
from fractions import Fraction
from io import BytesIO

import httpx
from litellm import completion, get_llm_provider, image_edit, image_generation

from ..image_generation import GeneratedImage
from ..media import MAX_IMAGE_BYTES, probe_image_stream
from ..public_url import require_public_url
from ._utils import get_field


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
        reference_images: tuple[bytes, ...] = (),
        aspect_ratio: str | None = None,
        image_size: str | None = None,
        count: int = 1,
    ) -> tuple[GeneratedImage, ...]:
        model, provider, _, _ = get_llm_provider(model=self._model)
        parameters = _image_parameters(
            provider, model,
            aspect_ratio or self._default_aspect_ratio,
            image_size or self._default_image_size,
        )
        common = {
            "model": self._model,
            "n": count,
            "api_base": self._base_url,
            "api_key": self._api_key,
            "timeout": self._timeout_seconds,
        }
        if provider == "openrouter":
            # LiteLLM's image_edit adapter drops extra_body for JSON providers.
            # Use the underlying chat API so image_config reaches OpenRouter.
            content: list[dict[str, object]] = []
            for data in reference_images:
                info = probe_image_stream(BytesIO(data), len(data))
                payload = base64.b64encode(data).decode("ascii")
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{info.mime_type};base64,{payload}"},
                    }
                )
            content.append({"type": "text", "text": prompt})
            response = completion(
                messages=[{"role": "user", "content": content}],
                modalities=["image", "text"],
                **parameters,
                **common,
            )
            response_images = [
                {"url": get_field(get_field(item, "image_url"), "url")}
                for choice in response.choices
                for item in get_field(get_field(choice, "message"), "images") or ()
            ]
        elif reference_images:
            response = image_edit(
                image=list(reference_images),
                prompt=prompt,
                **parameters,
                **common,
            )
            response_images = getattr(response, "data", ()) or ()
        else:
            response = image_generation(
                prompt=prompt,
                **common,
                **parameters,
            )
            response_images = getattr(response, "data", ()) or ()

        images = []
        for item in response_images:
            payload = get_field(item, "b64_json")
            url = get_field(item, "url")
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
            revised_prompt = get_field(item, "revised_prompt")
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


def _without_none(**values: str | None) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}


def _image_parameters(
    provider: str,
    model: str,
    aspect_ratio: str | None,
    image_size: str | None,
) -> dict[str, object]:
    if aspect_ratio is None and image_size is None:
        return {}
    if provider == "openrouter":
        return {
            "image_config": _without_none(
                aspect_ratio=aspect_ratio, image_size=image_size,
            )
        }
    if provider == "gemini" or (
        provider == "vertex_ai" and model.startswith("gemini")
    ):
        return {
            "imageConfig": _without_none(
                aspectRatio=aspect_ratio, imageSize=image_size,
            )
        }
    if provider not in {"openai", "azure"}:
        raise ValueError(f"image size mapping is not supported for provider '{provider}'")
    return {"size": _openai_image_size(model, aspect_ratio, image_size)}


def _openai_image_size(
    model: str,
    aspect_ratio: str | None,
    image_size: str | None,
) -> str:
    ratio = None
    if aspect_ratio is not None and aspect_ratio != "auto":
        if not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", aspect_ratio):
            raise ValueError("OpenAI aspect_ratio must be a positive ratio such as 16:9")
        ratio = Fraction(*map(int, aspect_ratio.split(":")))
    if image_size == "auto":
        if ratio is not None:
            raise ValueError("OpenAI image_size='auto' cannot specify an aspect_ratio")
        return "auto"
    if image_size is None and aspect_ratio == "auto":
        return "auto"
    if image_size and re.fullmatch(r"[1-9][0-9]*x[1-9][0-9]*", image_size):
        width, height = map(int, image_size.split("x"))
        if ratio is not None and Fraction(width, height) != ratio:
            raise ValueError("OpenAI image_size dimensions must match aspect_ratio")
    else:
        edges = {"1K": 1024, "2K": 2048, "4K": 3840}
        if image_size is not None and image_size not in edges:
            raise ValueError("OpenAI image_size must be 1K, 2K, 4K, auto, or WIDTHxHEIGHT")
        ratio = ratio or Fraction(1)
        if not Fraction(1, 3) <= ratio <= 3:
            raise ValueError("OpenAI aspect_ratio must be between 1:3 and 3:1")
        if model.startswith("gpt-image-1"):
            sizes = {
                Fraction(1): (1024, 1024),
                Fraction(3, 2): (1536, 1024),
                Fraction(2, 3): (1024, 1536),
            }
            if image_size not in {None, "1K"} or ratio not in sizes:
                raise ValueError(f"{model} requires a supported explicit pixel size")
            width, height = sizes[ratio]
        else:
            # Preserve the exact ratio on the API's 16px grid. A 1K hint
            # grows only as needed to meet the API's minimum pixel count.
            unit_width, unit_height = ratio.numerator * 16, ratio.denominator * 16
            scale = max(
                edges[image_size or "1K"] // max(unit_width, unit_height),
                math.ceil(math.sqrt(655_360 / (unit_width * unit_height))),
            )
            width, height = unit_width * scale, unit_height * scale
    if model.startswith("gpt-image-2") and (
        width % 16 or height % 16 or max(width, height) > 3840
        or not Fraction(1, 3) <= Fraction(width, height) <= 3
        or not 655_360 <= width * height <= 8_294_400
    ):
        raise ValueError(
            f"OpenAI size {width}x{height} must have edges divisible by 16, "
            "at most 3840px per edge, ratio between 1:3 and 3:1, and "
            "655360–8294400 pixels; choose a smaller tier or explicit dimensions"
        )
    return f"{width}x{height}"


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
