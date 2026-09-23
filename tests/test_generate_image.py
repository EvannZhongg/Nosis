import tempfile
import unittest
from pathlib import Path

from agent_core import (
    GenerateImageTool,
    GeneratedImage,
    Session,
    ToolExecutionContext,
    Workspace,
)
from tests.test_media import png_bytes


class StubImageGenerator:
    model = "test/image-model"

    def __init__(self, images: tuple[GeneratedImage, ...]) -> None:
        self.images = images
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self.images


class GenerateImageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.workspace = Workspace(self.root)
        self.session = Session()

    def context(self, generator=None) -> ToolExecutionContext:
        return ToolExecutionContext(
            workspace=self.workspace,
            session=self.session,
            image_generator=generator,
        )

    def test_is_available_only_with_a_generator(self) -> None:
        tool = GenerateImageTool()

        self.assertFalse(tool.available(self.context()))
        self.assertTrue(
            tool.available(
                self.context(StubImageGenerator((GeneratedImage(b"x"),)))
            )
        )

    def test_persists_generated_images_and_returns_attachments(self) -> None:
        generator = StubImageGenerator(
            (
                GeneratedImage(
                    png_bytes(40, 20),
                    revised_prompt="a revised prompt",
                ),
            )
        )

        result = GenerateImageTool().execute(
            {
                "prompt": "  draw a lighthouse  ",
                "aspect_ratio": "16:9",
                "image_size": "2K",
            },
            self.context(generator),
        )

        self.assertEqual(len(result.attachments), 1)
        attachment = result.attachments[0]
        self.assertTrue(attachment.path.startswith(".nosis/attachments/generated-"))
        self.assertEqual(attachment.mime_type, "image/png")
        self.assertEqual(
            (self.root / attachment.path).read_bytes(),
            png_bytes(40, 20),
        )
        self.assertEqual(result.output["model"], "test/image-model")
        self.assertEqual(result.output["images"][0]["width"], 40)
        self.assertEqual(
            result.output["images"][0]["revised_prompt"],
            "a revised prompt",
        )
        self.assertEqual(generator.calls[0]["prompt"], "draw a lighthouse")
        self.assertEqual(generator.calls[0]["aspect_ratio"], "16:9")
        self.assertEqual(generator.calls[0]["image_size"], "2K")

    def test_resolves_reference_images_before_calling_provider(self) -> None:
        reference = self.root / "reference.png"
        reference.write_bytes(png_bytes(8, 8))
        generator = StubImageGenerator((GeneratedImage(png_bytes(10, 10)),))

        GenerateImageTool().execute(
            {
                "prompt": "edit this",
                "reference_images": ["reference.png"],
            },
            self.context(generator),
        )

        self.assertEqual(
            generator.calls[0]["reference_images"],
            (reference.resolve(),),
        )

    def test_rejects_an_invalid_generated_image_without_leaving_a_file(self) -> None:
        generator = StubImageGenerator((GeneratedImage(b"not an image"),))

        with self.assertRaisesRegex(ValueError, "supported image"):
            GenerateImageTool().execute(
                {"prompt": "draw"}, self.context(generator)
            )

        attachment_root = self.root / ".nosis" / "attachments"
        self.assertEqual(list(attachment_root.iterdir()), [])

    def test_requires_the_requested_number_of_images(self) -> None:
        generator = StubImageGenerator((GeneratedImage(png_bytes(8, 8)),))

        with self.assertRaisesRegex(ValueError, "expected 2"):
            GenerateImageTool().execute(
                {"prompt": "draw", "count": 2}, self.context(generator)
            )


if __name__ == "__main__":
    unittest.main()
