"""Image inspection, token estimation, and encoding for model requests.

Images travel through the runtime as paths, never as bytes: a
:class:`~agent_core.content.ImagePart` names a file, and only the final
provider request turns it into a data URL.  This module owns the three
operations that need the file itself:

* :func:`probe_image` fully decodes every frame to validate the real
  media type and pixel size.  The declared extension is never trusted,
  because a provider that receives ``image/png`` holding JPEG bytes
  rejects the request.
* :func:`estimate_image_tokens` prices an image from its dimensions, so
  counting context never has to encode anything.
* :func:`encode_data_url` produces the base64 payload, memoized on the
  file's identity so one image is encoded once per turn rather than once
  per model call.
"""

import base64
import math
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, Iterator

from PIL import Image, ImageSequence

from .files import bind_file


#: Largest image accepted for inline delivery.  Base64 inflates payloads
#: by 4/3, so this keeps a single attachment under the ~7 MiB that
#: mainstream vision APIs accept for one encoded image.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: Canonical extension per supported image type.  A stored copy of an
#: image is named from the decoded format, never from the name or the
#: media type its sender claimed.
_IMAGE_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

SUPPORTED_IMAGE_MIME_TYPES = tuple(_IMAGE_EXTENSIONS)


def image_extension(mime_type: str) -> str:
    """Return the file extension for a probed image type."""
    return _IMAGE_EXTENSIONS[mime_type]


#: Fixed-tile pricing: a flat base plus a cost per 512-pixel tile.
_TILE_BASE_TOKENS = 85
_TILE_TOKENS = 170

#: Area pricing: the long edge is clamped, then pixels are charged at a
#: fixed rate.  These are the most expensive published parameters among
#: the providers this runtime reaches, which is the point -- the
#: estimate must not sit below what the provider will actually bill.
_AREA_MAX_EDGE = 1568
_AREA_PIXELS_PER_TOKEN = 750


class UnsupportedImageError(ValueError):
    """Raised for a file that is not an image this runtime can send."""


@dataclass(frozen=True)
class ImageInfo:
    mime_type: str
    width: int
    height: int
    size_bytes: int

    @property
    def token_estimate(self) -> int:
        return estimate_image_tokens(self.width, self.height)


def probe_image(path: Path, max_bytes: int | None = MAX_IMAGE_BYTES) -> ImageInfo:
    """Identify *path* as a supported image and measure it.

    The format comes from decoding the file rather than its name,
    and the size limit is enforced here so every caller that is about to
    spend context on an image rejects the same set of files.  Pass
    ``max_bytes=None`` when the image is only being displayed rather
    than sent to a model, so a large file stays viewable.

    Results are memoized on the file's identity: counting context calls
    this for every image on every pass of the agent loop, and decoding
    it each time would be pure waste.
    """
    with _open_image(path) as file:
        return _probe_open_image(file, path, max_bytes)


@contextmanager
def _open_image(path: Path) -> Iterator[BinaryIO]:
    path = path.absolute()
    try:
        with bind_file(Path(path.anchor), Path(*path.parts[1:])) as bound:
            with os.fdopen(bound.open_read(), "rb") as file:
                yield file
    except FileNotFoundError as error:
        raise UnsupportedImageError(f"image does not exist: {path}") from error


def _image_key(file: BinaryIO, path: Path) -> tuple[str, int, int, int, int]:
    info = os.fstat(file.fileno())
    return (str(path), info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


def _probe_open_image(file: BinaryIO, path: Path, max_bytes: int | None) -> ImageInfo:
    key = _image_key(file, path)
    with _cache_lock:
        cached = _probe_cache.get(key)
        if cached is not None:
            _probe_cache.move_to_end(key)
    size = os.fstat(file.fileno()).st_size
    _check_image_size(size, max_bytes)
    if cached is not None:
        return cached
    info = probe_image_stream(file, size, max_bytes=max_bytes)
    with _cache_lock:
        _probe_cache[key] = info
        while len(_probe_cache) > _PROBE_CACHE_ENTRIES:
            _probe_cache.popitem(last=False)
    return info


def _check_image_size(size: int, max_bytes: int | None) -> None:
    if size == 0:
        raise UnsupportedImageError("image file is empty")
    if max_bytes is not None and size > max_bytes:
        raise UnsupportedImageError(
            f"image is {size} bytes, exceeding the maximum of {max_bytes} bytes for inline delivery"
        )


def probe_image_stream(
    file: BinaryIO, size_bytes: int, *, max_bytes: int | None = MAX_IMAGE_BYTES
) -> ImageInfo:
    """Decode an already opened file or an in-memory generated image."""
    _check_image_size(size_bytes, max_bytes)
    try:
        file.seek(0)
        with Image.open(file, formats=("PNG", "JPEG", "GIF", "WEBP")) as image:
            mime_type = Image.MIME[image.format]
            width, height = image.size
            for frame in ImageSequence.Iterator(image):
                pixels = frame.width * frame.height
                if Image.MAX_IMAGE_PIXELS is not None and pixels > Image.MAX_IMAGE_PIXELS:
                    raise Image.DecompressionBombError(
                        f"image is {pixels} pixels, exceeding the maximum of {Image.MAX_IMAGE_PIXELS} pixels"
                    )
                frame.load()
    except (OSError, ValueError, SyntaxError,
            Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise UnsupportedImageError(
            f"image is not a supported image or cannot be fully decoded: {error}"
        ) from error
    return ImageInfo(mime_type, width, height, size_bytes)


def estimate_image_tokens(width: int, height: int) -> int:
    """Estimate the context cost of one inline image, erring high.

    This number exists to decide when to compress, so the two directions
    of error are not symmetric.  Over-counting compresses a little early.
    Under-counting makes the runtime believe it is inside the budget, so
    it neither compresses nor raises
    :class:`~agent_core.context_manager.ContextWindowExceededError`, and
    the provider rejects the request instead -- surfacing as an opaque
    upstream error rather than the graceful failure the check exists to
    produce.  So the estimate is deliberately the maximum over the pricing
    models this runtime talks to, not the average.

    Two families are priced and the larger wins:

    * fixed tiles -- shrink to fit a 2048-pixel square, shrink again
      until the short side is at most 768, then charge per 512-pixel
      tile plus a base cost.  This caps out near 1.4K tokens.
    * area -- shrink so the long edge is at most 1568, then charge by
      pixel area.  This reaches roughly 3.3K tokens, so it dominates for
      any large image.
    """
    width = max(1, width)
    height = max(1, height)
    return max(
        _tiled_image_tokens(width, height),
        _area_image_tokens(width, height),
    )


def _tiled_image_tokens(width: int, height: int) -> int:
    longest = max(width, height)
    if longest > 2048:
        scale = 2048 / longest
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    shortest = min(width, height)
    if shortest > 768:
        scale = 768 / shortest
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    tiles = math.ceil(width / 512) * math.ceil(height / 512)
    return _TILE_BASE_TOKENS + _TILE_TOKENS * tiles


def _area_image_tokens(width: int, height: int) -> int:
    longest = max(width, height)
    if longest > _AREA_MAX_EDGE:
        scale = _AREA_MAX_EDGE / longest
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    return math.ceil(width * height / _AREA_PIXELS_PER_TOKEN)


def fit_image_size(width: int, height: int, max_edge: int | None) -> tuple[int, int]:
    """Fit within a provider's upload dimensions without enlarging images."""
    if max_edge is None or max(width, height) <= max_edge:
        return width, height
    scale = max_edge / max(width, height)
    return max(1, int(width * scale)), max(1, int(height * scale))


def encode_data_url(path: Path, *, max_edge: int | None = None) -> str:
    """Return ``data:<mime>;base64,<payload>`` for *path*, memoized.

    A turn sends its images on every model call, and a tool-heavy turn
    makes many calls, so encoding on each one would re-read and re-encode
    the same file repeatedly.  The cache key includes the file's
    modification time, size and target edge, so edits and a request's
    changing image limit cannot reuse a stale encoding. Resized images
    are encoded in memory; the original file stays unchanged.
    """
    with _open_image(path) as file:
        return _encode_open_image(file, path, max_edge=max_edge)


def _encode_open_image(file: BinaryIO, path: Path, *, max_edge: int | None) -> str:
    info = _probe_open_image(file, path, MAX_IMAGE_BYTES)
    mime_type = info.mime_type
    key = (*_image_key(file, path), mime_type, max_edge)
    with _cache_lock:
        cached = _encoded_cache.get(key)
        if cached is not None:
            # Refresh recency so the working set survives eviction.
            _encoded_cache.move_to_end(key)
            return cached

    # The file is read outside the lock: encoding a few megabytes must
    # not stall a parallel batch of reads. Two threads racing on the
    # same new image both encode it and store equal results, which costs
    # one redundant pass and keeps the lock uncontended.
    size = None
    if max_edge is not None:
        target = fit_image_size(info.width, info.height, max_edge)
        if target != (info.width, info.height):
            size = target
    if size is not None:
        file.seek(0)
        with Image.open(file) as image:
            has_alpha = "A" in image.getbands() or "transparency" in image.info
            with image.convert("RGBA" if has_alpha else "RGB") as pixels:
                resized = pixels.resize(size, Image.Resampling.LANCZOS)
            # JPEG stays compact for photographs; PNG preserves screenshot
            # text and transparency, including palette-based sources.
            image_format = "JPEG" if image.format == "JPEG" else "PNG"
            with BytesIO() as buffer:
                with resized:
                    resized.save(buffer, format=image_format)
                data = buffer.getvalue()
            if len(data) > MAX_IMAGE_BYTES:
                raise UnsupportedImageError(
                    f"resized image is {len(data)} bytes, exceeding the maximum "
                    f"of {MAX_IMAGE_BYTES} bytes for inline delivery"
                )
            payload = base64.b64encode(data).decode("ascii")
        mime_type = "image/jpeg" if image_format == "JPEG" else "image/png"
    else:
        file.seek(0)
        payload = base64.b64encode(file.read()).decode("ascii")
    encoded = f"data:{mime_type};base64,{payload}"

    with _cache_lock:
        previous = _encoded_cache.pop(key, None)
        _encoded_cache[key] = encoded
        global _encoded_bytes
        _encoded_bytes += len(encoded) - (
            len(previous) if previous is not None else 0
        )
        # A single image may exceed the budget; it is kept anyway,
        # because the caller is about to send it.
        while (
            _encoded_bytes > _CACHE_BUDGET_BYTES
            or len(_encoded_cache) > _CACHE_MAX_ENTRIES
        ) and len(_encoded_cache) > 1:
            _, evicted = _encoded_cache.popitem(last=False)
            _encoded_bytes -= len(evicted)
    return encoded


def clear_encoded_cache() -> None:
    """Drop every memoized encoding and probe.  Used by tests."""
    global _encoded_bytes
    with _cache_lock:
        _encoded_cache.clear()
        _probe_cache.clear()
        _encoded_bytes = 0


#: Total encoded bytes retained.  Encodings are held only to spare repeat
#: work inside a turn, so the budget stays small enough that an idle
#: runtime does not pin megabytes of base64.
_CACHE_BUDGET_BYTES = 32 * 1024 * 1024

#: Entry ceiling, so many small images cannot grow the map without bound
#: while staying under the byte budget.
_CACHE_MAX_ENTRIES = 64

#: Probe results are tiny, so many more are kept than encodings.
_PROBE_CACHE_ENTRIES = 512

_cache_lock = threading.Lock()
_encoded_cache: "OrderedDict[tuple[str, int, int, int, int, str, int | None], str]" = OrderedDict()
_probe_cache: "OrderedDict[tuple[str, int, int, int, int], ImageInfo]" = OrderedDict()

#: Running total of ``_encoded_cache`` value lengths, maintained on
#: insert and eviction so a write never has to re-sum the whole map.
_encoded_bytes = 0
