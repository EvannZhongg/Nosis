import base64
import json
import unittest
from email.parser import BytesParser
from email.policy import default
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from agent_core.providers import LiteLLMImageGenerator
from agent_core.providers.image_generation import (
    _read_image_url,
)
from agent_core.public_url import require_public_url
from tests.image_fixtures import jpeg_bytes, png_bytes


class LiteLLMImageGeneratorTest(unittest.TestCase):
    def test_generates_and_decodes_base64_images(self) -> None:
        data = png_bytes(8, 6)
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(data).decode("ascii"),
                    url=None,
                    revised_prompt="revised",
                )
            ]
        )
        generator = LiteLLMImageGenerator(
            "openai/gpt-image-2.5-flare",
            base_url="https://example.test/v1",
            api_key="secret",
            default_aspect_ratio="1:1",
            default_image_size="1K",
        )

        with patch(
            "agent_core.providers.image_generation.image_generation",
            return_value=response,
        ) as generate:
            result = generator.generate(prompt="draw")

        self.assertEqual(result[0].data, data)
        self.assertEqual(result[0].revised_prompt, "revised")
        generate.assert_called_once_with(
            prompt="draw",
            model="openai/gpt-image-2.5-flare",
            n=1,
            api_base="https://example.test/v1",
            api_key="secret",
            timeout=600,
            size="1024x1024",
        )

    def test_uses_image_edit_when_references_are_present(self) -> None:
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=base64.b64encode(png_bytes(4, 4)).decode("ascii"),
                    url=None,
                    revised_prompt=None,
                )
            ]
        )
        generator = LiteLLMImageGenerator("openai/gpt-image-2.5-flare")

        with patch(
            "agent_core.providers.image_generation.image_edit",
            return_value=response,
        ) as edit:
            generator.generate(
                prompt="edit",
                reference_images=(png_bytes(2, 3), png_bytes(3, 2)),
                aspect_ratio="4:3",
                image_size="2K",
            )

        edit.assert_called_once_with(
            image=[png_bytes(2, 3), png_bytes(3, 2)],
            prompt="edit",
            size="2048x1536",
            model="openai/gpt-image-2.5-flare",
            n=1,
            api_base=None,
            api_key=None,
            timeout=600,
        )

    def test_rejects_an_empty_provider_response(self) -> None:
        generator = LiteLLMImageGenerator("openai/gpt-image-2.5-flare")

        with patch(
            "agent_core.providers.image_generation.image_generation",
            return_value=SimpleNamespace(data=[]),
        ):
            with self.assertRaisesRegex(ValueError, "expected 1"):
                generator.generate(prompt="draw")

    def test_does_not_follow_provider_image_redirects(self) -> None:
        response = SimpleNamespace(
            data=[
                SimpleNamespace(
                    b64_json=None,
                    url="https://images.example.test/generated.png",
                    revised_prompt=None,
                )
            ]
        )
        stream = unittest.mock.MagicMock()
        stream.__enter__.return_value.is_redirect = True
        generator = LiteLLMImageGenerator("openai/gpt-image-2.5-flare")

        with (
            patch(
                "agent_core.providers.image_generation.image_generation",
                return_value=response,
            ),
            patch(
                "agent_core.providers.image_generation.httpx.stream",
                return_value=stream,
            ) as request,
            patch(
                "agent_core.providers.image_generation.require_public_url"
            ),
        ):
            with self.assertRaisesRegex(ValueError, "redirect"):
                generator.generate(prompt="draw")

        self.assertFalse(request.call_args.kwargs["follow_redirects"])

    def test_openai_size_tiers_and_explicit_dimensions(self) -> None:
        cases = (
            ("1:1", "1K", "1024x1024"),
            ("16:9", "1K", "1280x720"),
            ("16:9", "2K", "2048x1152"),
            ("9:16", "2K", "1152x2048"),
            ("16:9", "4K", "3840x2160"),
            ("2:6", "1K", "480x1440"),
            (None, "2048x2048", "2048x2048"),
            ("16:9", "1536x864", "1536x864"),
            (None, "auto", "auto"),
            ("auto", None, "auto"),
        )
        response = SimpleNamespace(data=[{"b64_json": base64.b64encode(png_bytes(4, 4)).decode()}])
        for model in ("gpt-image-2.5-flare", "openai/gpt-image-2.5-flare", "azure/gpt-image-2"):
            for ratio, size, expected in cases:
                with self.subTest(model=model, ratio=ratio, size=size), patch(
                    "agent_core.providers.image_generation.image_generation", return_value=response,
                ) as generate:
                    LiteLLMImageGenerator(model).generate(
                        prompt="draw", aspect_ratio=ratio, image_size=size,
                    )
                    self.assertEqual(generate.call_args.kwargs["size"], expected)
                    self.assertNotIn("quality", generate.call_args.kwargs)
                    self.assertNotIn("image_config", generate.call_args.kwargs)

    def test_rejects_invalid_openai_dimensions_before_request(self) -> None:
        for ratio, size in (
            ("0:1", "1K"), ("wide", "1K"), ("4:1", "2K"),
            ("1:1", "4K"), (None, "3K"), (None, "0x1024"),
            (None, "1025x1024"), (None, "512x512"),
            (None, "4096x2048"), (None, "2048x512"),
            ("1:1", "1536x864"), ("16:9", "auto"),
        ):
            with self.subTest(ratio=ratio, size=size), patch(
                "agent_core.providers.image_generation.image_generation",
            ) as generate:
                with self.assertRaises(ValueError):
                    LiteLLMImageGenerator("gpt-image-2.5-flare").generate(
                        prompt="draw", aspect_ratio=ratio, image_size=size,
                    )
                generate.assert_not_called()

    def test_call_settings_override_defaults(self) -> None:
        response = SimpleNamespace(data=[{"b64_json": base64.b64encode(png_bytes(4, 4)).decode()}])
        generator = LiteLLMImageGenerator(
            "gpt-image-2.5-flare", default_aspect_ratio="1:1", default_image_size="1K",
        )
        with patch("agent_core.providers.image_generation.image_generation", return_value=response) as generate:
            generator.generate(prompt="draw", aspect_ratio="9:16", image_size="2K")
        self.assertEqual(generate.call_args.kwargs["size"], "1152x2048")

    def test_gpt_image_1_uses_its_supported_native_sizes(self) -> None:
        response = SimpleNamespace(data=[{"b64_json": base64.b64encode(png_bytes(4, 4)).decode()}])
        with patch("agent_core.providers.image_generation.image_generation", return_value=response) as generate:
            generator = LiteLLMImageGenerator("openai/gpt-image-1.5")
            generator.generate(prompt="draw", aspect_ratio="3:2", image_size="1K")
            self.assertEqual(generate.call_args.kwargs["size"], "1536x1024")
            with self.assertRaisesRegex(ValueError, "supported explicit pixel size"):
                generator.generate(prompt="draw", aspect_ratio="3:2", image_size="2K")
            self.assertEqual(generate.call_count, 1)

    def test_native_size_configuration_reaches_http_requests(self) -> None:
        image = png_bytes(4, 4)
        encoded = base64.b64encode(image).decode("ascii")
        for model, base_url, provider in (
            ("gpt-image-2.5-flare", "https://images.example.test/v1", "openai"),
            ("openrouter/google/gemini-2.5-flash-image", "https://openrouter.ai/api/v1", "openrouter"),
            ("gemini/gemini-3-pro-image-preview", "https://generativelanguage.googleapis.com/v1beta", "gemini"),
        ):
            for references in ((), (image, jpeg_bytes(4, 4))):
                requests = []

                def send(client, request, **kwargs):
                    requests.append(request)
                    if provider == "openrouter":
                        body = {"id": "image-test", "object": "chat.completion", "created": 1,
                                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                                    "role": "assistant", "content": "", "images": [{
                                        "type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"},
                                    }],
                                }}]}
                    elif provider == "gemini":
                        body = {"candidates": [{"content": {"parts": [{"inlineData": {
                            "mimeType": "image/png", "data": encoded,
                        }}]}}]}
                    else:
                        body = {"created": 1, "data": [{"b64_json": encoded}]}
                    return httpx.Response(200, request=request, json=body)

                with self.subTest(provider=provider, references=bool(references)), patch(
                    "httpx.Client.send", autospec=True, side_effect=send,
                ):
                    result = LiteLLMImageGenerator(model, base_url=base_url, api_key="test-key").generate(
                        prompt="draw", reference_images=references, aspect_ratio="16:9", image_size="2K",
                    )
                    self.assertEqual(result[0].data, image)
                    self.assertEqual(len(requests), 1)
                    request = requests[0]
                    if provider == "openai" and references:
                        self.assertEqual(request.url.path, "/v1/images/edits")
                        message = BytesParser(policy=default).parsebytes(
                            f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.read(),
                        )
                        fields = {part.get_param("name", header="content-disposition"): part.get_payload(decode=True)
                                  for part in message.iter_parts()}
                        self.assertEqual(fields["size"], b"2048x1152")
                        self.assertNotIn("image_config", fields)
                        files = [part for part in message.iter_parts()
                                 if part.get_param("name", header="content-disposition") == "image[]"]
                        self.assertEqual([part.get_payload(decode=True) for part in files], list(references))
                        self.assertEqual([part.get_content_type() for part in files], ["image/png", "image/jpeg"])
                    else:
                        body = json.loads(request.read())
                        if provider == "openai":
                            self.assertEqual(request.url.path, "/v1/images/generations")
                            self.assertEqual(body["size"], "2048x1152")
                            self.assertNotIn("image_config", body)
                        elif provider == "openrouter":
                            self.assertEqual(request.url.path, "/api/v1/chat/completions")
                            self.assertEqual(body["image_config"], {"aspect_ratio": "16:9", "image_size": "2K"})
                            self.assertEqual(body["modalities"], ["image", "text"])
                            parts = body["messages"][0]["content"]
                            urls = [part["image_url"]["url"] for part in parts if part["type"] == "image_url"]
                            self.assertEqual([base64.b64decode(url.split(",", 1)[1]) for url in urls], list(references))
                            if references:
                                self.assertTrue(urls[1].startswith("data:image/jpeg;base64,"))
                        else:
                            self.assertTrue(request.url.path.endswith(":generateContent"))
                            self.assertEqual(body["generationConfig"]["imageConfig"],
                                             {"aspectRatio": "16:9", "imageSize": "2K"})
                            parts = body["contents"][0]["parts"]
                            self.assertEqual(sum("inlineData" in part for part in parts), len(references))

    def test_rejects_private_image_urls(self) -> None:
        for url in (
            "http://example.com/image.png",
            "https://127.0.0.1/image.png",
            "https://169.254.169.254/latest/meta-data",
            "https://user:password@example.com/image.png",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                require_public_url(
                    url, allowed_schemes=frozenset({"https"})
                )

    def test_rejects_provider_image_redirects(self) -> None:
        redirect = unittest.mock.MagicMock()
        redirect.__enter__.return_value.is_redirect = True
        redirect.__enter__.return_value.headers = {
            "location": "https://cdn.example.test/generated.png"
        }

        with (
            patch(
                "agent_core.providers.image_generation.httpx.stream",
                return_value=redirect,
            ),
            patch(
                "agent_core.providers.image_generation.require_public_url"
            ),
        ):
            with self.assertRaisesRegex(ValueError, "redirect"):
                _read_image_url("https://images.example.test/start", 10)


if __name__ == "__main__":
    unittest.main()
