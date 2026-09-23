import base64
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_core.providers import LiteLLMImageGenerator
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


if __name__ == "__main__":
    unittest.main()
