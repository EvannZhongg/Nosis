"""Image probing, token estimation, and encode memoization."""

import os
import tempfile
import unittest
import warnings
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from tests.image_fixtures import jpeg_bytes, png_bytes

from agent_core import media
from agent_core.media import (
    MAX_IMAGE_BYTES,
    UnsupportedImageError,
    clear_encoded_cache,
    encode_data_url,
    estimate_image_tokens,
    probe_image,
)


def gif_bytes(width: int, height: int) -> bytes:
    with Image.new("RGB", (width, height)) as image, BytesIO() as buffer:
        image.save(buffer, format="GIF")
        return buffer.getvalue()


def webp_bytes(width: int, height: int) -> bytes:
    with Image.new("RGB", (width, height)) as image, BytesIO() as buffer:
        image.save(buffer, format="WEBP")
        return buffer.getvalue()


class ProbeImageTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_reads_dimensions_for_each_image_format(self) -> None:
        for extension, make_bytes, width, height, mime_type in (
            ("png", png_bytes, 40, 25, "image/png"),
            ("jpg", jpeg_bytes, 64, 32, "image/jpeg"),
            ("gif", gif_bytes, 12, 7, "image/gif"),
            ("webp", webp_bytes, 30, 20, "image/webp"),
        ):
            with self.subTest(extension=extension):
                info = probe_image(self.write(f"a.{extension}", make_bytes(width, height)))
                self.assertEqual(info.mime_type, mime_type)
                self.assertEqual((info.width, info.height), (width, height))

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

    def test_rejects_images_with_valid_headers_but_broken_pixels(self) -> None:
        for extension, make_bytes in (
            ("png", png_bytes), ("jpg", jpeg_bytes),
            ("gif", gif_bytes), ("webp", webp_bytes),
        ):
            with self.subTest(extension=extension):
                data = make_bytes(40, 25)
                path = self.write(f"broken.{extension}", data[:len(data) // 2])
                with self.assertRaisesRegex(UnsupportedImageError, "fully decoded"):
                    probe_image(path)

    def test_rejects_a_gif_with_a_broken_later_frame(self) -> None:
        path = self.root / "animated.gif"
        with Image.new("RGB", (40, 25), "red") as first, Image.new("RGB", (40, 25), "blue") as second:
            first.save(path, format="GIF", save_all=True, append_images=[second])
        data = path.read_bytes()
        path.write_bytes(data[:-10])
        with Image.open(path) as image:
            image.load()
        with self.assertRaisesRegex(UnsupportedImageError, "fully decoded"):
            probe_image(path)

    def test_rejects_an_oversized_image(self) -> None:
        path = self.write("big.png", png_bytes(4, 4))
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * MAX_IMAGE_BYTES)

        with self.assertRaisesRegex(UnsupportedImageError, "exceeding"):
            probe_image(path)

    def test_pixel_limit_rejects_images_before_loading_even_without_byte_limit(self) -> None:
        path = self.write("large.png", png_bytes(11, 10))
        for max_bytes in (MAX_IMAGE_BYTES, None):
            for warning_action in ("always", "error"):
                with self.subTest(max_bytes=max_bytes, warning_action=warning_action):
                    with patch.object(Image, "MAX_IMAGE_PIXELS", 100), warnings.catch_warnings(record=True):
                        warnings.simplefilter(warning_action, Image.DecompressionBombWarning)
                        with patch("PIL.PngImagePlugin.PngImageFile.load") as load:
                            with self.assertRaisesRegex(UnsupportedImageError, "pixels"):
                                probe_image(path, max_bytes=max_bytes)
                        load.assert_not_called()

    def test_pixel_limit_accepts_the_boundary(self) -> None:
        path = self.write("boundary.png", png_bytes(10, 10))
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100):
            self.assertEqual(probe_image(path).width, 10)

    def test_pixel_limit_rejects_a_larger_later_gif_frame(self) -> None:
        path = self.root / "animated.gif"
        with Image.new("RGB", (8, 8), "red") as first, Image.new("RGB", (8, 8), "blue") as second:
            first.save(path, format="GIF", save_all=True, append_images=[second], optimize=False)
        # A later frame can expand the canvas beyond the logical screen size.
        data = bytearray(path.read_bytes())
        descriptor = data.rindex(b"\x2c\x00\x00\x00\x00\x08\x00\x08\x00")
        data[descriptor + 5:descriptor + 9] = b"\x0b\x00\x0a\x00"
        path.write_bytes(data)
        with patch.object(Image, "MAX_IMAGE_PIXELS", 100), warnings.catch_warnings(record=True):
            warnings.simplefilter("always", Image.DecompressionBombWarning)
            with self.assertRaisesRegex(UnsupportedImageError, "pixels"):
                probe_image(path)

    def test_the_size_limit_can_be_lifted_for_display(self) -> None:
        """The ceiling bounds what is sent to a model, not what is shown."""
        path = self.write("big.png", b"")
        path.write_bytes(
            png_bytes(4, 4) + b"\x00" * (MAX_IMAGE_BYTES + 1024)
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
            png_bytes(4, 4) + b"\x00" * (MAX_IMAGE_BYTES + 1024)
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

        An image retained in the recent ContextUnit tail is still sent inline,
        so an estimate below what the provider bills can make the runtime ship
        a request the provider rejects.
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


class ImageCacheTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)


class EncodeDataUrlTest(ImageCacheTestCase):
    def test_rejects_broken_pixels_before_encoding(self) -> None:
        path = self.root / "broken.png"
        path.write_bytes(png_bytes(40, 25)[:45])
        with self.assertRaisesRegex(UnsupportedImageError, "fully decoded"):
            encode_data_url(path)

    def test_produces_a_data_url(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(4, 4))

        url = encode_data_url(path)

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
        first = encode_data_url(path)

        replacement = bytearray(original)
        replacement[-1] ^= 0xFF
        path.write_bytes(bytes(replacement))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))

        self.assertEqual(encode_data_url(path), first)

    def test_an_edited_image_is_encoded_again(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(4, 4))
        first = encode_data_url(path)

        path.write_bytes(png_bytes(8, 8))
        stat = path.stat()
        # Guarantee a distinct mtime on a coarse-grained filesystem.
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        self.assertNotEqual(encode_data_url(path), first)


class ProbeMemoizationTest(ImageCacheTestCase):
    """Counting context probes every image on every agent-loop pass."""

    def test_the_image_is_decoded_once_for_repeated_probes(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(40, 30))
        with patch("PIL.Image.open", wraps=Image.open) as image_open:
            sizes = [
                (probe_image(path).width, probe_image(path).height)
                for _ in range(5)
            ]

        self.assertEqual(image_open.call_count, 1)
        self.assertEqual(set(sizes), {(40, 30)})

    def test_an_edited_image_is_probed_again(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(40, 30))
        self.assertEqual(probe_image(path).width, 40)

        path.write_bytes(png_bytes(11, 12))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        self.assertEqual(probe_image(path).width, 11)

    def test_cached_image_is_revalidated_after_corruption(self) -> None:
        path = self.root / "a.gif"
        path.write_bytes(gif_bytes(40, 25))
        probe_image(path)
        path.write_bytes(path.read_bytes()[:-10])
        with self.assertRaises(UnsupportedImageError):
            probe_image(path)


class EncodedCacheBoundsTest(ImageCacheTestCase):
    """The cache exists to spare work inside a turn, not to grow forever."""

    def test_entries_are_capped(self) -> None:
        for index in range(media._CACHE_MAX_ENTRIES * 2):
            path = self.root / f"i{index}.png"
            path.write_bytes(png_bytes(8, 8))
            encode_data_url(path)

        self.assertLessEqual(
            len(media._encoded_cache), media._CACHE_MAX_ENTRIES
        )

    def test_the_byte_total_stays_exact_across_eviction(self) -> None:
        """A drifting total would evict too eagerly or never at all."""
        for index in range(media._CACHE_MAX_ENTRIES + 5):
            path = self.root / f"i{index}.png"
            path.write_bytes(png_bytes(8, 8))
            encode_data_url(path)

        self.assertEqual(
            media._encoded_bytes,
            sum(len(value) for value in media._encoded_cache.values()),
        )

    def test_re_encoding_the_same_path_does_not_double_count(self) -> None:
        path = self.root / "a.png"
        path.write_bytes(png_bytes(8, 8))
        encode_data_url(path)

        path.write_bytes(png_bytes(16, 16))
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        encode_data_url(path)

        self.assertEqual(
            media._encoded_bytes,
            sum(len(value) for value in media._encoded_cache.values()),
        )


if __name__ == "__main__":
    unittest.main()
