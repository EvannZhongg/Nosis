"""`read_image` and the media message the agent loop injects."""

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from agent_core import (
    Agent,
    ImagePart,
    LLMResponse,
    ProviderCapabilities,
    ReadImageTool,
    Session,
    TextPart,
    ToolCall,
    ToolCatalog,
    ToolExecutionContext,
    Workspace,
)
from agent_core.config import ContextCompressionConfig

from tests.agent_fixtures import AGENT_CONFIG, CONSOLIDATOR_PROMPT, MockProvider
from tests.image_fixtures import jpeg_bytes, png_bytes


class ReadImageToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.workspace = Workspace(self.root)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()
        self.session = Session()

    def context(self, vision_input: bool = True) -> ToolExecutionContext:
        return ToolExecutionContext(
            workspace=self.workspace,
            session=self.session,
            sessions_directory=self.sessions,
            vision_input=vision_input,
        )

    def write_image(self, name: str, width: int = 8, height: int = 6) -> None:
        (self.root / name).write_bytes(png_bytes(width, height))

    def test_unavailable_without_image_input(self) -> None:
        """A text-only model would have the image stripped anyway."""
        self.assertFalse(
            ReadImageTool().available(self.context(vision_input=False))
        )

    def test_available_with_image_input(self) -> None:
        self.assertTrue(ReadImageTool().available(self.context()))

    def test_returns_the_image_as_an_attachment(self) -> None:
        self.write_image("shot.png", 40, 20)

        result = ReadImageTool().execute(
            {"paths": ["shot.png"]}, self.context()
        )

        self.assertEqual(
            result.attachments,
            (
                ImagePart(
                    path="shot.png",
                    mime_type="image/png",
                    filename="shot.png",
                    size_bytes=(self.root / "shot.png").stat().st_size,
                ),
            ),
        )
        self.assertEqual(result.output["images"][0]["width"], 40)
        self.assertEqual(result.output["images"][0]["height"], 20)

    def test_reports_the_media_type_from_the_bytes(self) -> None:
        """An extension is a claim; the provider needs the truth."""
        (self.root / "actually.png").write_bytes(jpeg_bytes(10, 10))

        result = ReadImageTool().execute(
            {"paths": ["actually.png"]}, self.context()
        )

        self.assertEqual(result.attachments[0].mime_type, "image/jpeg")

    def test_rejects_a_path_outside_the_workspace(self) -> None:
        with self.assertRaises(ValueError):
            ReadImageTool().execute(
                {"paths": ["../outside.png"]}, self.context()
            )

    def test_rejects_a_non_image(self) -> None:
        (self.root / "notes.txt").write_text("hello")

        with self.assertRaises(ValueError):
            ReadImageTool().execute({"paths": ["notes.txt"]}, self.context())

    def test_broken_image_is_reported_without_becoming_an_attachment(self) -> None:
        self.write_image("good.png")
        (self.root / "broken.png").write_bytes(png_bytes(40, 25)[:45])
        result = ReadImageTool().execute(
            {"paths": ["broken.png", "good.png"]}, self.context(),
        )
        self.assertEqual([part.path for part in result.attachments], ["good.png"])
        self.assertEqual(result.output["failed"][0]["path"], "broken.png")
        self.assertIn("fully decoded", result.output["failed"][0]["error"])

    def test_multi_path_call_resolves_and_deduplicates_images(self) -> None:
        """Dedup has to follow resolution, not the raw argument text."""
        self.write_image("a.png")
        self.write_image("b.png")
        (self.root / "sub").mkdir()

        for paths in (
            ["a.png", "b.png", "a.png", "./a.png"],
            ["a.png", "b.png", "sub/../a.png"],
        ):
            with self.subTest(paths=paths):
                result = ReadImageTool().execute({"paths": paths}, self.context())

                self.assertEqual([part.path for part in result.attachments], ["a.png", "b.png"])
                self.assertEqual([image["path"] for image in result.output["images"]], ["a.png", "b.png"])

    def test_one_bad_path_does_not_discard_the_others(self) -> None:
        self.write_image("good.png")

        result = ReadImageTool().execute(
            {"paths": ["good.png", "missing.png"]}, self.context()
        )

        self.assertEqual(len(result.attachments), 1)
        self.assertEqual(result.output["failed"][0]["path"], "missing.png")

    def test_rejects_too_many_images(self) -> None:
        with self.assertRaisesRegex(ValueError, "at most"):
            ReadImageTool().execute(
                {"paths": [f"{index}.png" for index in range(9)]},
                self.context(),
            )

    def test_rejects_an_empty_path_list(self) -> None:
        with self.assertRaises(ValueError):
            ReadImageTool().execute({"paths": []}, self.context())


class ToolMediaInjectionTest(unittest.TestCase):
    """Images a tool loads reach the model as a separate user message."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.addCleanup(self._directory.cleanup)
        self.workspace = Workspace(self.root)
        self.sessions = self.root / "sessions"
        self.sessions.mkdir()

    def run_turn(self, calls):
        session = Session()
        provider = MockProvider(
            [
                LLMResponse(content=None, tool_calls=tuple(calls)),
                LLMResponse(content="done"),
            ],
            input_tokens=10,
            max_context_tokens=100_000,
            capabilities=ProviderCapabilities(frozenset({"text", "image"})),
        )
        context = ToolExecutionContext(
            workspace=self.workspace,
            session=session,
            sessions_directory=self.sessions,
            vision_input=True,
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="S",
            consolidator_prompt=CONSOLIDATOR_PROMPT,
            config=replace(AGENT_CONFIG, context=ContextCompressionConfig(enabled=False)),
            tools=ToolCatalog((ReadImageTool(),)).select(
                ("read_image",), context
            ),
            context=context,
            now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        agent.run("look at it")
        return session, provider

    def call(self, identifier: str, paths) -> ToolCall:
        return ToolCall(
            id=identifier, name="read_image", arguments={"paths": paths}
        )

    def test_batch_media_follows_all_results_and_reaches_the_next_model_request(self) -> None:
        for name in ("a.png", "b.png"):
            (self.root / name).write_bytes(png_bytes(8, 8))

        session, provider = self.run_turn(
            [self.call("c1", ["a.png"]), self.call("c2", ["b.png"])]
        )

        self.assertEqual([item.role for item in session.items],
                         ["user", "assistant", "tool", "tool", "user", "assistant"])
        self.assertEqual([item.tool_call_id for item in session.items if item.role == "tool"],
                         ["c1", "c2"])
        self.assertEqual([item.origin for item in session.items if item.role == "user"],
                         ["conversation", "tool_media"])
        media, = [item for item in session.items if item.is_tool_media]
        images = [ImagePart(path=name, mime_type="image/png", filename=name,
                            size_bytes=(self.root / name).stat().st_size)
                  for name in ("a.png", "b.png")]
        self.assertEqual([part for part in media.parts if isinstance(part, ImagePart)], images)
        text = "".join(part.text for part in media.parts if isinstance(part, TextPart))
        for source in ("Tool output", "a.png", "b.png"):
            self.assertIn(source, text)
        request = provider.requests[1]
        self.assertEqual([part for message in request.messages for part in message.parts
                          if isinstance(part, ImagePart)], images)
        self.assertEqual([message for message in request.messages if message.is_tool_media], [media])
        turn_contexts = [message for message in request.messages
                         if message.role == "system" and str(message.content).startswith("[Turn Context]")]
        self.assertEqual(len(turn_contexts), 1)

    def test_no_media_message_when_a_call_loads_nothing(self) -> None:
        session, provider = self.run_turn([self.call("c1", ["absent.png"])])

        self.assertEqual([item for item in session.items if item.is_tool_media], [])
        self.assertFalse(any(isinstance(part, ImagePart)
                             for message in provider.requests[1].messages for part in message.parts))


if __name__ == "__main__":
    unittest.main()
