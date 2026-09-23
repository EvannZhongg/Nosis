import base64
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.providers import LiteLLMImageGenerator
from agent_core.providers.image_generation import (
    _read_image_url,
)
from agent_core.public_url import require_public_url
from tests.test_media import png_bytes


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
            "openrouter/example/image",
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
            model="openrouter/example/image",
            n=1,
            api_base="https://example.test/v1",
            api_key="secret",
            timeout=600,
            image_config={"aspect_ratio": "1:1", "image_size": "1K"},
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
        generator = LiteLLMImageGenerator("openrouter/example/image")

        with patch(
            "agent_core.providers.image_generation.image_edit",
            return_value=response,
        ) as edit:
            generator.generate(
                prompt="edit",
                reference_images=(Path("a.png"), Path("b.png")),
                aspect_ratio="4:3",
                image_size="2K",
            )

        edit.assert_called_once_with(
            image=[Path("a.png"), Path("b.png")],
            prompt="edit",
            extra_body={
                "image_config": {"aspect_ratio": "4:3", "image_size": "2K"}
            },
            model="openrouter/example/image",
            n=1,
            api_base=None,
            api_key=None,
            timeout=600,
        )

    def test_rejects_an_empty_provider_response(self) -> None:
        generator = LiteLLMImageGenerator("openrouter/example/image")

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
        generator = LiteLLMImageGenerator("openrouter/example/image")

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
