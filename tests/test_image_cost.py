"""Counting and encoding guarantees for image requests.

Two properties matter for cost: counting an image must never encode it,
and sending the same image twice in a turn must not encode it twice.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_core.content import ImagePart, TextPart
from agent_core.llm import LLMRequest
from agent_core.media import clear_encoded_cache, estimate_image_tokens
from agent_core.providers.litellm_provider import (
    LiteLLMProvider,
    _request_messages,
)
from agent_core.session import Message

from tests.test_media import png_bytes


VISION_MODEL = "openai/gpt-4o"
TEXT_MODEL = "openai/text-only"


def capabilities(model, base_url=None):
    from agent_core.llm import ProviderCapabilities

    modalities = {"text"}
    if model == VISION_MODEL:
        modalities.add("image")
    return ProviderCapabilities(frozenset(modalities))


_CAPABILITIES_PATCH = classmethod(
    lambda cls, model, base_url=None: capabilities(model, base_url)
)


class ImageTokenCountingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)
        self.patcher = patch.object(
            LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def provider(self, model: str = VISION_MODEL) -> LiteLLMProvider:
        return LiteLLMProvider(
            model=model,
            max_context_tokens=100_000,
            media_root=self.root,
        )

    def request(self, *parts) -> LLMRequest:
        return LLMRequest(
            system_prompt="S",
            messages=(Message(role="user", content=tuple(parts)),),
            media_root=self.root,
        )

    def write(self, name: str, width: int, height: int) -> ImagePart:
        (self.root / name).write_bytes(png_bytes(width, height))
        return ImagePart(path=name, mime_type="image/png")

    def test_counting_never_encodes_the_image(self) -> None:
        """Encoding to count would re-read the file every loop pass."""
        image = self.write("a.png", 16, 16)

        with patch(
            "agent_core.providers.litellm_provider.encode_data_url"
        ) as encode:
            self.provider().count_input_tokens(self.request(image))

        encode.assert_not_called()

    def test_an_image_costs_its_dimension_estimate(self) -> None:
        image = self.write("a.png", 700, 700)
        provider = self.provider()

        with_image = provider.count_input_tokens(self.request(image))
        without = provider.count_input_tokens(
            self.request(TextPart(text="x"))
        )

        self.assertGreaterEqual(
            with_image - without, estimate_image_tokens(700, 700) - 20
        )

    def test_a_larger_image_costs_more(self) -> None:
        """A flat per-image constant would misprice a wall of screenshots."""
        small = self.write("small.png", 100, 100)
        large = self.write("large.png", 1600, 1600)
        provider = self.provider()

        self.assertLess(
            provider.count_input_tokens(self.request(small)),
            provider.count_input_tokens(self.request(large)),
        )

    def test_base64_length_does_not_inflate_the_count(self) -> None:
        """Counting a data URL as text would overstate it enormously."""
        image = self.write("a.png", 64, 64)
        padded = self.root / "a.png"
        # Same dimensions, far more bytes: a noisy image compresses badly.
        padded.write_bytes(padded.read_bytes() + b"\x00" * 400_000)

        count = self.provider().count_input_tokens(self.request(image))

        self.assertLess(count, 5_000)

    def test_a_text_only_model_pays_nothing_for_an_image(self) -> None:
        """Its images are named, not sent."""
        image = self.write("a.png", 800, 800)
        provider = self.provider(TEXT_MODEL)

        count = provider.count_input_tokens(self.request(image))

        self.assertLess(count, 200)

    def test_an_unreadable_image_is_not_counted(self) -> None:
        missing = ImagePart(path="absent.png", mime_type="image/png")

        count = self.provider().count_input_tokens(self.request(missing))

        self.assertLess(count, 200)


class ImageEncodingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)
        self.patcher = patch.object(
            LiteLLMProvider, "capabilities_for_model", _CAPABILITIES_PATCH
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def provider(self) -> LiteLLMProvider:
        return LiteLLMProvider(
            model=VISION_MODEL,
            max_context_tokens=100_000,
            media_root=self.root,
        )

    def request(self, image: ImagePart) -> LLMRequest:
        return LLMRequest(
            system_prompt="S",
            messages=(Message(role="user", content=(image,)),),
            media_root=self.root,
        )

    def test_sending_produces_a_data_url(self) -> None:
        (self.root / "a.png").write_bytes(png_bytes(8, 8))
        image = ImagePart(path="a.png", mime_type="image/png")

        messages = _request_messages(self.request(image), self.provider())

        content = messages[1]["content"]
        self.assertEqual(content[0]["type"], "image_url")
        self.assertTrue(
            content[0]["image_url"]["url"].startswith(
                "data:image/png;base64,"
            )
        )

    def test_the_file_is_read_once_across_repeated_sends(self) -> None:
        """A tool-heavy turn calls the model many times with one image."""
        (self.root / "a.png").write_bytes(png_bytes(8, 8))
        image = ImagePart(path="a.png", mime_type="image/png")
        provider = self.provider()
        request = self.request(image)

        real_open = Path.open
        reads = []

        def counting_open(self, *args, **kwargs):
            if self.name == "a.png" and "b" in (args[0] if args else "r"):
                reads.append(self.name)
            return real_open(self, *args, **kwargs)

        with patch.object(Path, "open", counting_open):
            for _ in range(4):
                _request_messages(request, provider)

        self.assertEqual(len(reads), 1)

    def test_counting_then_sending_still_sends_the_bytes(self) -> None:
        """The count path must not poison the cache with a placeholder."""
        (self.root / "a.png").write_bytes(png_bytes(8, 8))
        image = ImagePart(path="a.png", mime_type="image/png")
        provider = self.provider()
        request = self.request(image)

        provider.count_input_tokens(request)
        messages = _request_messages(request, provider)

        self.assertEqual(messages[1]["content"][0]["type"], "image_url")

    def test_a_missing_image_degrades_to_a_notice(self) -> None:
        image = ImagePart(path="absent.png", mime_type="image/png")

        messages = _request_messages(self.request(image), self.provider())

        content = messages[1]["content"]
        self.assertEqual(content[0]["type"], "text")
        self.assertIn("unavailable", content[0]["text"])


if __name__ == "__main__":
    unittest.main()
