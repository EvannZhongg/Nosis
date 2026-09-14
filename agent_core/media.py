"""Image inspection, token estimation, and encoding for model requests.

Images travel through the runtime as paths, never as bytes: a
:class:`~agent_core.content.ImagePart` names a file, and only the final
provider request turns it into a data URL.  This module owns the three
operations that need the file itself:

* :func:`probe_image` reads a bounded header prefix to learn the real
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
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path


#: Largest image accepted for inline delivery.  Base64 inflates payloads
#: by 4/3, so this keeps a single attachment under the ~7 MiB that
#: mainstream vision APIs accept for one encoded image.
MAX_IMAGE_BYTES = 5 * 1024 * 1024

#: Bytes read to identify a format and its dimensions.  A JPEG's frame
#: header follows any EXIF thumbnail, which is itself capped at 64 KiB,
#: so this prefix reaches the dimensions of real-world files without
#: loading the whole image.
_PROBE_BYTES = 512 * 1024

SUPPORTED_IMAGE_MIME_TYPES = (
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/webp",
)

#: Fixed-tile pricing: a flat base plus a cost per 512-pixel tile.
_TILE_BASE_TOKENS = 85
_TILE_TOKENS = 170

#: Area pricing: the long edge is clamped, then pixels are charged at a
#: fixed rate.  These are the most expensive published parameters among
#: the providers this runtime reaches, which is the point -- the
#: estimate must not sit below what the provider will actually bill.
_AREA_MAX_EDGE = 1568
_AREA_PIXELS_PER_TOKEN = 750

#: Fallback pixel size for a file whose format is recognized but whose
#: dimensions are not in the probed prefix.  It is priced at the ceiling
#: rather than at some middling guess, because an unknown image that
#: turns out to be large must not be the reason a turn overflows.
_ASSUMED_DIMENSION = _AREA_MAX_EDGE


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

    The format comes from the file's magic bytes rather than its name,
    and the size limit is enforced here so every caller that is about to
    spend context on an image rejects the same set of files.  Pass
    ``max_bytes=None`` when the image is only being displayed rather
    than sent to a model, so a large file stays viewable.

    Results are memoized on the file's identity: counting context calls
    this for every image on every pass of the agent loop, and re-reading
    a header each time would be pure waste.
    """
    if not path.is_file():
        raise UnsupportedImageError(f"image does not exist: {path}")
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
    with _cache_lock:
        cached = _probe_cache.get(key)
        if cached is not None:
            _probe_cache.move_to_end(key)
    if cached is not None:
        if max_bytes is not None and cached.size_bytes > max_bytes:
            raise UnsupportedImageError(
                f"image is {cached.size_bytes} bytes, exceeding the maximum "
                f"of {max_bytes} bytes for inline delivery"
            )
        return cached

    size_bytes = stat.st_size
    if size_bytes == 0:
        raise UnsupportedImageError(f"image file is empty: {path}")
    if max_bytes is not None and size_bytes > max_bytes:
        raise UnsupportedImageError(
            f"image is {size_bytes} bytes, exceeding the maximum of "
            f"{max_bytes} bytes for inline delivery"
        )
    with path.open("rb") as file:
        header = file.read(_PROBE_BYTES)

    probed = _probe_header(header)
    if probed is None:
        raise UnsupportedImageError(
            "file is not a supported image; expected one of "
            + ", ".join(SUPPORTED_IMAGE_MIME_TYPES)
        )
    mime_type, size = probed
    width, height = size or (_ASSUMED_DIMENSION, _ASSUMED_DIMENSION)
    info = ImageInfo(
        mime_type=mime_type,
        width=width,
        height=height,
        size_bytes=size_bytes,
    )
    with _cache_lock:
        _probe_cache[key] = info
        while len(_probe_cache) > _PROBE_CACHE_ENTRIES:
            _probe_cache.popitem(last=False)
    return info


def estimate_image_tokens(width: int, height: int) -> int:
    """Estimate the context cost of one inline image, erring high.

    This number exists to decide when to compress, so the two directions
    of error are not symmetric.  Over-counting compresses a little early.
    Under-counting makes the runtime believe it is inside the budget, so
    it neither compresses nor raises
    :class:`~agent_core.context_manager.ContextWindowExceededError`, and
    the provider rejects the request instead -- surfacing as an opaque
    upstream error rather than the graceful failure the check exists to
    produce.  An image also cannot be compressed away mid-turn: it stays
    inline until the turn ends.  So the estimate is deliberately the
    maximum over the pricing models this runtime talks to, not the
    average.

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


def encode_data_url(path: Path, mime_type: str) -> str:
    """Return ``data:<mime>;base64,<payload>`` for *path*, memoized.

    A turn sends its images on every model call, and a tool-heavy turn
    makes many calls, so encoding on each one would re-read and re-encode
    the same file repeatedly.  The cache key includes the file's
    modification time and size, so an edited image is re-encoded rather
    than served stale.
    """
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size)
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
    with path.open("rb") as file:
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
_encoded_cache: "OrderedDict[tuple[str, int, int], str]" = OrderedDict()
_probe_cache: "OrderedDict[tuple[str, int, int], ImageInfo]" = OrderedDict()

#: Running total of ``_encoded_cache`` value lengths, maintained on
#: insert and eviction so a write never has to re-sum the whole map.
_encoded_bytes = 0


def _probe_header(
    header: bytes,
) -> tuple[str, tuple[int, int] | None] | None:
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", _png_size(header)
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", _jpeg_size(header)
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", _gif_size(header)
    if (
        len(header) >= 12
        and header.startswith(b"RIFF")
        and header[8:12] == b"WEBP"
    ):
        return "image/webp", _webp_size(header)
    return None


def _png_size(header: bytes) -> tuple[int, int] | None:
    # An IHDR chunk always leads a PNG: length, type, then width/height.
    if len(header) < 24 or header[12:16] != b"IHDR":
        return None
    return (
        int.from_bytes(header[16:20], "big"),
        int.from_bytes(header[20:24], "big"),
    )


def _jpeg_size(header: bytes) -> tuple[int, int] | None:
    # Walk the segment chain to the start-of-frame, which carries the
    # dimensions.  Everything before it is metadata of variable length.
    index = 2
    total = len(header)
    while index + 3 < total:
        if header[index] != 0xFF:
            index += 1
            continue
        marker = header[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xD9 or marker == 0xDA:
            return None
        segment_length = int.from_bytes(header[index + 2 : index + 4], "big")
        is_start_of_frame = 0xC0 <= marker <= 0xCF and marker not in (
            0xC4,
            0xC8,
            0xCC,
        )
        if is_start_of_frame:
            if index + 9 > total:
                return None
            return (
                int.from_bytes(header[index + 7 : index + 9], "big"),
                int.from_bytes(header[index + 5 : index + 7], "big"),
            )
        if segment_length < 2:
            return None
        index += 2 + segment_length
    return None


def _gif_size(header: bytes) -> tuple[int, int] | None:
    if len(header) < 10:
        return None
    return (
        int.from_bytes(header[6:8], "little"),
        int.from_bytes(header[8:10], "little"),
    )


def _webp_size(header: bytes) -> tuple[int, int] | None:
    chunk = header[12:16]
    if chunk == b"VP8X" and len(header) >= 30:
        width = int.from_bytes(header[24:27], "little") + 1
        height = int.from_bytes(header[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(header) >= 30:
        # Lossy: a 3-byte frame tag, a sync sequence, then 14-bit sizes.
        if header[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(header[26:28], "little") & 0x3FFF
        height = int.from_bytes(header[28:30], "little") & 0x3FFF
        return width, height
    if chunk == b"VP8L" and len(header) >= 25:
        if header[20] != 0x2F:
            return None
        bits = int.from_bytes(header[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None
