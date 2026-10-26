"""Provider limits apply to outgoing pixels, including historical tool media."""

import base64
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from agent_core import ImagePart, LLMRequest, Message, ProviderCapabilities, TextPart
from agent_core.media import (
    MAX_IMAGE_BYTES, UnsupportedImageError, clear_encoded_cache,
    encode_data_url, estimate_image_tokens,
)
from agent_core.providers import LiteLLMProvider, OpenAIProvider
from agent_core.providers.base import _media_tokens, _request_messages
from agent_core.providers.image_limits import image_edge_limit
from agent_core.providers.openai_provider import _responses_input


class ProviderImageLimitsTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        clear_encoded_cache()
        self.addCleanup(clear_encoded_cache)

    def image(self, size, *, name="screenshot.png", mode="RGB", format="PNG"):
        path = self.root / name
        with Image.new(mode, size) as image:
            image.save(path, format=format)
        return ImagePart(path=name, mime_type=Image.MIME[format], filename=name,
                         size_bytes=path.stat().st_size)

    def provider(self, model="deepseek/deepseek-flash", url=None, native=False):
        cls = OpenAIProvider if native else LiteLLMProvider
        provider = cls(model=model, base_url=url, media_root=self.root, max_context_tokens=1000000)
        provider._capabilities = ProviderCapabilities(frozenset({"text", "image"}))
        return provider

    def request(self, part, count=1):
        return LLMRequest("Test", tuple(
            Message("user", (TextPart("Screenshot"), part), origin="tool_media")
            for _ in range(count)
        ))

    def decoded_size(self, url):
        data = base64.b64decode(url.split(",", 1)[1])
        with Image.open(BytesIO(data)) as image:
            image.load()
            return image.size

    def image_urls(self, messages):
        return [part["image_url"]["url"] for message in messages
                if isinstance(message.get("content"), list)
                for part in message["content"] if part["type"] == "image_url"]

    def test_service_rules_and_image_count_boundaries(self):
        for model, url, count, edge in (
            ("deepseek/deepseek-flash", None, 14, 8192),
            ("deepseek/deepseek-flash", None, 15, 4096),
            ("deepseek-flash", "https://api.deepseek.com/v1", 15, 4096),
            ("anthropic/claude-sonnet-4-5", None, 20, 8000),
            ("anthropic/claude-sonnet-4-5", None, 21, 2000),
            ("claude-opus-4-1", "https://api.anthropic.com", 1, 8000),
            ("anthropic/claude-haiku-4-5", None, 1, 8000),
            ("deepseek/deepseek-flash", "https://relay.example/v1", 15, None),
            ("deepseek/deepseek-flash", "https://api.deepseek.com.example/v1", 1, None),
            ("deepseek/future-model", None, 1, None),
            ("openai/gpt-5", "https://api.openai.com/v1", 1, None),
            ("gemini/gemini-2.5-flash", None, 1, None),
            ("dashscope/qwen-plus", None, 1, None),
            ("ollama/llama3.1", None, 1, None),
            ("openai/gpt-5", "https://openrouter.ai/api/v1", 1, None),
        ):
            with self.subTest(model=model, url=url, count=count):
                self.assertEqual(image_edge_limit(model, url, count), edge)

    def test_session_screenshot_is_resized_on_actual_litellm_request(self):
        part = self.image((2538, 8394))
        original = (self.root / part.path).read_bytes()
        response = SimpleNamespace(usage=None, choices=[{
            "delta": {"content": "OK"}, "finish_reason": "stop",
        }])
        with patch("agent_core.providers.litellm_provider.completion", return_value=iter([response])) as call:
            self.provider().stream(self.request(part), lambda _: None)
        messages = call.call_args.kwargs["messages"]
        self.assertEqual(self.decoded_size(self.image_urls(messages)[0]), (2476, 8192))
        notice = messages[1]["content"][1]["text"]
        self.assertIn("original 2538x8394", notice)
        self.assertIn("multiply image x", notice)
        self.assertEqual((self.root / part.path).read_bytes(), original)

    def test_all_historical_images_follow_current_request_limit(self):
        part = self.image((40, 8200))
        for model, count, first_size, next_size in (
            ("deepseek/deepseek-flash", 14, (39, 8192), (19, 4096)),
            ("anthropic/claude-sonnet-4-5", 20, (39, 8000), (9, 2000)),
        ):
            with self.subTest(model=model):
                provider = self.provider(model)
                before = self.image_urls(_request_messages(self.request(part, count), provider))
                after = self.image_urls(_request_messages(self.request(part, count + 1), provider))
                self.assertEqual(len(after), count + 1)
                self.assertTrue(all(self.decoded_size(url) == first_size for url in before))
                self.assertTrue(all(self.decoded_size(url) == next_size for url in after))

    def test_missing_images_do_not_trigger_many_image_limit(self):
        part = self.image((40, 5000))
        request = self.request(part, 14)
        missing = ImagePart(path="missing.png", mime_type="image/png", filename="missing.png", size_bytes=1)
        request = LLMRequest("Test", (*request.messages, Message("user", (missing,))))
        urls = self.image_urls(_request_messages(request, self.provider()))
        self.assertEqual(len(urls), 14)
        self.assertTrue(all(self.decoded_size(url) == (40, 5000) for url in urls))

    def test_native_responses_uses_same_limit_and_sends_png_mime(self):
        part = self.image((50, 8300), name="screen.webp", mode="RGBA", format="WEBP")
        provider = self.provider(url="https://api.deepseek.com", native=True)
        for count, expected in ((14, (49, 8192)), (15, (24, 4096))):
            with self.subTest(count=count):
                inputs = _responses_input(self.request(part, count), provider)
                urls = [p["image_url"] for m in inputs for p in m["content"] if p["type"] == "input_image"]
                self.assertEqual(len(urls), count)
                self.assertTrue(all(url.startswith("data:image/png;base64,") for url in urls))
                self.assertTrue(all(self.decoded_size(url) == expected for url in urls))

    def test_at_boundary_and_unknown_service_preserve_original_bytes(self):
        for size, url in (((50, 8192), None), ((50, 8394), "https://relay.example/v1")):
            with self.subTest(size=size, url=url):
                part = self.image(size, name=f"{size[1]}.png")
                messages = _request_messages(self.request(part), self.provider(url=url))
                data_url = self.image_urls(messages)[0]
                self.assertEqual(base64.b64decode(data_url.split(",", 1)[1]), (self.root / part.path).read_bytes())
                self.assertEqual(len(messages[1]["content"]), 2)

    def test_counting_uses_sent_size_and_notice_without_decoding(self):
        part = self.image((1000, 8200))
        request = self.request(part, 21)
        provider = self.provider("anthropic/claude-sonnet-4-5")
        with patch("PIL.Image.open", side_effect=AssertionError("counting must not decode images")):
            messages = _request_messages(request, provider, encode_media=False)
            tokens = _media_tokens(request, provider)
        self.assertIn("original 1000x8200", messages[1]["content"][1]["text"])
        self.assertEqual(tokens, 21 * estimate_image_tokens(243, 2000))

    def test_cached_resize_preserves_alpha_and_invalidates_after_edit(self):
        part = self.image((100, 9000), mode="RGBA")
        path = self.root / part.path
        first = encode_data_url(path, part.mime_type, max_edge=8192)
        with patch("PIL.Image.open", side_effect=AssertionError("cached image must not be decoded")):
            self.assertEqual(encode_data_url(path, part.mime_type, max_edge=8192), first)
        with Image.open(BytesIO(base64.b64decode(first.split(",", 1)[1]))) as image:
            self.assertEqual(image.mode, "RGBA")
            self.assertEqual(image.getpixel((0, 0))[3], 0)
        self.image((100, 8500), mode="RGBA")
        second = encode_data_url(path, part.mime_type, max_edge=8192)
        self.assertNotEqual(first, second)

    def test_jpeg_resize_stays_jpeg_and_does_not_enlarge_short_edge(self):
        for size, expected in (((2, 9000), (1, 8000)), ((9000, 2), (8000, 1))):
            with self.subTest(size=size):
                part = self.image(size, name=f"photo-{size[0]}.jpg", format="JPEG")
                url = encode_data_url(self.root / part.path, part.mime_type, max_edge=8000)
                self.assertTrue(url.startswith("data:image/jpeg;base64,"))
                self.assertEqual(self.decoded_size(url), expected)

    def test_invalid_images_fail_in_both_counting_and_serialization_for_all_routes(self):
        for payload, error in (
            (b"", "empty"),
            (b"not an image", "not a supported image"),
            (b"x" * (MAX_IMAGE_BYTES + 1), "exceeding"),
        ):
            part = self.image((10, 10))
            (self.root / part.path).write_bytes(payload)
            request = self.request(part)
            for model, url in (
                ("deepseek/deepseek-flash", None),
                ("anthropic/claude-sonnet-4-5", None),
                ("openai/gpt-5", None),
                ("deepseek/deepseek-flash", "https://relay.example/v1"),
            ):
                provider = self.provider(model, url)
                with self.subTest(model=model, url=url, error=error):
                    with self.assertRaisesRegex(UnsupportedImageError, error):
                        _media_tokens(request, provider)
                    for encode in (False, True):
                        with self.assertRaisesRegex(UnsupportedImageError, error):
                            _request_messages(request, provider, encode_media=encode)
                    with patch("agent_core.providers.litellm_provider.token_counter", return_value=0):
                        with self.assertRaisesRegex(UnsupportedImageError, error):
                            provider.count_input_tokens(request)
                    with patch("agent_core.providers.litellm_provider.completion") as call:
                        with self.assertRaisesRegex(UnsupportedImageError, error):
                            provider.stream(request, lambda _: None)
                        call.assert_not_called()

    def test_native_responses_rejects_invalid_image_at_unknown_endpoint(self):
        part = self.image((10, 10))
        (self.root / part.path).write_bytes(b"")
        provider = self.provider(url="https://relay.example/v1", native=True)
        with self.assertRaisesRegex(UnsupportedImageError, "empty"):
            _responses_input(self.request(part), provider)

    def test_unsafe_paths_fail_in_counting_and_serialization(self):
        part = ImagePart(path="../outside.png", mime_type="image/png")
        request = self.request(part)
        for model in ("deepseek/deepseek-flash", "openai/gpt-5"):
            provider = self.provider(model)
            with self.subTest(model=model):
                with self.assertRaisesRegex(ValueError, "within media_root"):
                    _media_tokens(request, provider)
                with self.assertRaisesRegex(ValueError, "within media_root"):
                    _request_messages(request, provider)
