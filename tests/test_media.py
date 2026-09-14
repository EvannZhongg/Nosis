"""Image probing, token estimation, and encode memoization."""

import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from agent_core import media
from agent_core.media import (
    MAX_IMAGE_BYTES,
    UnsupportedImageError,
    clear_encoded_cache,
    encode_data_url,
    estimate_image_tokens,
    probe_image,
)


def png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int, height: int) -> bytes:
    # SOI, an APP0 segment, then the baseline start-of-frame that
    # carries the dimensions.
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 11)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + sof + b"\xff\xd9"


def gif_bytes(width: int, height: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 8


def webp_vp8x_bytes(width: int, height: int) -> bytes:
    payload = (
        b"VP8X"
        + struct.pack("<I", 10)
        + b"\x00\x00\x00\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
    )
    return (
        b"RIFF"
        + struct.pack("<I", 4 + len(payload))
        + b"WEBP"
        + payload
    )


class ProbeImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_reads_png_dimensions(self) -> None:
        info = probe_image(self.write("a.png", png_bytes(40, 25)))

        self.assertEqual(info.mime_type, "image/png")
        self.assertEqual((info.width, info.height), (40, 25))

    def test_reads_jpeg_dimensions(self) -> None:
        info = probe_image(self.write("a.jpg", jpeg_bytes(64, 32)))

        self.assertEqual(info.mime_type, "image/jpeg")
        self.assertEqual((info.width, info.height), (64, 32))

    def test_reads_gif_dimensions(self) -> None:
        info = probe_image(self.write("a.gif", gif_bytes(12, 7)))

        self.assertEqual(info.mime_type, "image/gif")
        self.assertEqual((info.width, info.height), (12, 7))

    def test_reads_webp_dimensions(self) -> None:
        info = probe_image(self.write("a.webp", webp_vp8x_bytes(30, 20)))

        self.assertEqual(info.mime_type, "image/webp")
        self.assertEqual((info.width, info.height), (30, 20))

    def test_the_media_type_comes_from_the_bytes_not_the_name(self) -> None:
        """A provider handed a mislabelled type rejects the request."""
        info = probe_image(self.write("lying.png", jpeg_bytes(10, 10)))

        self.assertEqual(info.mime_type, "image/jpeg")

    def test_rejects_a_non_image(self) -> None:
        with self.assertRaises(UnsupportedImageError):
            probe_image(self.write("a.txt", b"plain text, not an image"))

    def test_rejects_an_empty_file(self) -> None:
        with self.assertRaises(UnsupportedImageError):
            probe_image(self.write("a.png", b""))

    def test_rejects_a_missing_file(self) -> None:
        with self.assertRaises(UnsupportedImageError):
            probe_image(self.root / "absent.png")

    def test_rejects_an_oversized_image(self) -> None:
        path = self.write("big.png", png_bytes(4, 4))
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_IMAGE_BYTES)

        with self.assertRaisesRegex(UnsupportedImageError, "exceeding"):
            probe_image(path)

    def test_the_size_limit_can_be_lifted_for_display(self) -> None:
        """The ceiling bounds what is sent to a model, not what is shown."""
        path = self.write("big.png", b"")
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1024)
        )

        self.assertEqual(
            probe_image(path, max_bytes=None).mime_type, "image/png"
        )

    def test_the_limit_still_applies_after_a_lifted_probe_cached_it(
        self,
    ) -> None:
        """A display probe must not smuggle a huge image past the limit."""
        path = self.write("big.png", b"")
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1024)
        )
        probe_image(path, max_bytes=None)

        with self.assertRaisesRegex(UnsupportedImageError, "exceeding"):
            probe_image(path)


class EstimateImageTokensTest(unittest.TestCase):
    def test_a_small_image_costs_one_tile(self) -> None:
        self.assertEqual(estimate_image_tokens(100, 100), 85 + 170)

    def test_cost_grows_with_area(self) -> None:
        small = estimate_image_tokens(256, 256)
        large = estimate_image_tokens(1024, 1024)

        self.assertGreater(large, small)

    def test_an_enormous_image_is_bounded_by_downscaling(self) -> None:
        """Cost must not scale with the raw pixel count."""
        estimate = estimate_image_tokens(20000, 20000)

        self.assertLess(estimate, 5000)

    def test_degenerate_dimensions_do_not_raise(self) -> None:
        self.assertGreater(estimate_image_tokens(0, 0), 0)

    def test_never_under_prices_area_billing(self) -> None:
        """Under-counting defeats the compression guard.

        An image cannot be compressed away mid-turn, so an estimate
        below what the provider bills makes the runtime skip compression
        and ship a request the provider rejects.
        """
        for width, height in [
            (1024, 1024),
            (1568, 1568),
            (2048, 1536),
            (3024, 4032),
            (4096, 4096),
            (16384, 16384),
            (800, 600),
            (513, 2048),
        ]:
            with self.subTest(size=(width, height)):
                scale = min(1.0, 1568 / max(width, height))
                billed = int(width * scale) * int(height * scale) / 750

                self.assertGreaterEqual(
                    estimate_image_tokens(width, height), billed
                )

    def test_a_large_image_is_not_flattened_to_the_tile_ceiling(self) -> None:
        """Fixed tiling alone caps every image near 1.4K tokens."""
        self.assertGreater(estimate_image_tokens(1568, 1568), 3000)


class EncodeDataUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)

    def test_produces_a_data_url(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(4, 4))

        url = encode_data_url(path, "image/png")

        self.assertTrue(url.startswith("data:image/png;base64,"))

    def test_the_second_call_does_not_read_the_file_again(self) -> None:
        """A turn re-sends its images on every model call.

        The content is replaced while mtime and size are restored, so a
        second encode that returns the original proves the bytes were
        served from the cache rather than re-read.
        """
        path = self.root / "a.png"
        original = png_bytes(4, 4)
        path.write_bytes(original)
        stat = path.stat()
        first = encode_data_url(path, "image/png")

        replacement = bytearray(original)
        replacement[-1] ^= 0xFF
        path.write_bytes(bytes(replacement))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        self.assertEqual(encode_data_url(path, "image/png"), first)

    def test_an_edited_image_is_encoded_again(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(4, 4))
        first = encode_data_url(path, "image/png")

        path.write_bytes(png_bytes(8, 8))
        stat = path.stat()
        # Guarantee a distinct mtime on a coarse-grained filesystem.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        self.assertNotEqual(encode_data_url(path, "image/png"), first)


class ProbeMemoizationTest(unittest.TestCase):
    """Counting context probes every image on every agent-loop pass."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)

    def test_the_header_is_read_once_for_repeated_probes(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(40, 30))
        real_open = Path.open
        reads = []

        def counting_open(inner, *args, **kwargs):
            if inner.name == "a.png":
                reads.append(inner.name)
            return real_open(inner, *args, **kwargs)

        with patch.object(Path, "open", counting_open):
            sizes = [
                (probe_image(path).width, probe_image(path).height)
                for _ in range(5)
            ]

        self.assertEqual(len(reads), 1)
        self.assertEqual(set(sizes), {(40, 30)})

    def test_an_edited_image_is_probed_again(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(40, 30))
        self.assertEqual(probe_image(path).width, 40)

        path.write_bytes(png_bytes(11, 12))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        self.assertEqual(probe_image(path).width, 11)


class EncodedCacheBoundsTest(unittest.TestCase):
    """The cache exists to spare work inside a turn, not to grow forever."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)

    def test_entries_are_capped(self) -> None:
        for index in range(media._CACHE_MAX_ENTRIES * 2):
            path = self.root / f"i{index}.png"
            path.write_bytes(png_bytes(8, 8))
            encode_data_url(path, "image/png")

        self.assertLessEqual(
            len(media._encoded_cache), media._CACHE_MAX_ENTRIES
        )

    def test_the_byte_total_stays_exact_across_eviction(self) -> None:
        """A drifting total would evict too eagerly or never at all."""
        for index in range(media._CACHE_MAX_ENTRIES + 5):
            path = self.root / f"i{index}.png"
            path.write_bytes(png_bytes(8, 8))
            encode_data_url(path, "image/png")

        self.assertEqual(
            media._encoded_bytes,
            sum(len(value) for value in media._encoded_cache.values()),
        )

    def test_re_encoding_the_same_path_does_not_double_count(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(8, 8))
        encode_data_url(path, "image/png")

        path.write_bytes(png_bytes(16, 16))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        encode_data_url(path, "image/png")

        self.assertEqual(
            media._encoded_bytes,
            sum(len(value) for value in media._encoded_cache.values()),
        )


if __name__ == "__main__":
    unittest.main()
