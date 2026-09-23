"""`read_image` and the media message the agent loop injects."""

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core import (
    Agent,
    AgentConfig,
    ImagePart,
    LLMProvider,
    LLMRequest,
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
from agent_core.tools import ToolConfig

from tests.test_media import jpeg_bytes, png_bytes


CONSOLIDATOR_PROMPT = "Consolidate the conversation."


class StubProvider(LLMProvider):
    """Replays a scripted sequence of responses, recording each request."""

    def __init__(self, responses, vision: bool = True) -> None:
        self._responses = list(responses)
        self._vision = vision
        self.requests: list[LLMRequest] = []

    @property
    def capabilities(self) -> ProviderCapabilities:
        modalities = {"text"}
        if self._vision:
            modalities.add("image")
        return ProviderCapabilities(frozenset(modalities))

    @property
    def max_context_tokens(self) -> int:
        return 100_000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return 10

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        return self._responses.pop(0)


def agent_config() -> AgentConfig:
    return AgentConfig(
        max_same_tool_calls=5,
        output_reserve_tokens=100,
        tools=ToolConfig(enabled=()),
        workspace_instruction_files=(),
        context=ContextCompressionConfig(enabled=False),
    )


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

    def test_reads_several_images_in_one_call(self) -> None:
        self.write_image("a.png")
        self.write_image("b.png")

        result = ReadImageTool().execute(
            {"paths": ["a.png", "b.png"]}, self.context()
        )

        self.assertEqual(len(result.attachments), 2)

    def test_a_duplicate_path_is_loaded_once(self) -> None:
        """Sending one image twice would buy nothing for double the cost."""
        self.write_image("a.png")

        result = ReadImageTool().execute(
            {"paths": ["a.png", "a.png"]}, self.context()
        )

        self.assertEqual(len(result.attachments), 1)

    def test_different_spellings_of_one_file_load_once(self) -> None:
        """Dedup has to follow resolution, not the raw argument text."""
        self.write_image("a.png")
        (self.root / "sub").mkdir()

        result = ReadImageTool().execute(
            {"paths": ["a.png", "./a.png", "sub/../a.png"]}, self.context()
        )

        self.assertEqual(len(result.attachments), 1)
        self.assertEqual(len(result.output["images"]), 1)

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

    def run_turn(self, calls, vision: bool = True):
        session = Session()
        provider = StubProvider(
            [
                LLMResponse(content=None, tool_calls=tuple(calls)),
                LLMResponse(content="done"),
            ],
            vision=vision,
        )
        context = ToolExecutionContext(
            workspace=self.workspace,
            session=session,
            sessions_directory=self.sessions,
            vision_input=vision,
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="S",
            consolidator_prompt=CONSOLIDATOR_PROMPT,
            config=agent_config(),
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

    def test_the_image_arrives_in_a_user_message(self) -> None:
        (self.root / "a.png").write_bytes(png_bytes(8, 8))

        session, _ = self.run_turn([self.call("c1", ["a.png"])])

        media = [item for item in session.items if item.is_tool_media]
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0].role, "user")
        self.assertEqual(
            [part for part in media[0].parts if isinstance(part, ImagePart)],
            [
                ImagePart(
                    path="a.png",
                    mime_type="image/png",
                    filename="a.png",
                    size_bytes=(self.root / "a.png").stat().st_size,
                )
            ],
        )

    def test_the_media_message_follows_every_tool_result(self) -> None:
        """A provider rejects a tool call that its next message leaves
        unanswered, so images must not land between two results."""
        (self.root / "a.png").write_bytes(png_bytes(8, 8))
        (self.root / "b.png").write_bytes(png_bytes(8, 8))

        session, _ = self.run_turn(
            [self.call("c1", ["a.png"]), self.call("c2", ["b.png"])]
        )

        roles = [item.role for item in session.items]
        self.assertEqual(
            roles, ["user", "assistant", "tool", "tool", "user", "assistant"]
        )

    def test_one_batch_yields_a_single_media_message(self) -> None:
        (self.root / "a.png").write_bytes(png_bytes(8, 8))
        (self.root / "b.png").write_bytes(png_bytes(8, 8))

        session, _ = self.run_turn(
            [self.call("c1", ["a.png"]), self.call("c2", ["b.png"])]
        )

        media = [item for item in session.items if item.is_tool_media]
        self.assertEqual(len(media), 1)
        self.assertEqual(
            len([p for p in media[0].parts if isinstance(p, ImagePart)]), 2
        )

    def test_the_injected_message_is_marked_as_tool_media(self) -> None:
        """It is not something the person said, and must not look like it."""
        (self.root / "a.png").write_bytes(png_bytes(8, 8))

        session, _ = self.run_turn([self.call("c1", ["a.png"])])

        typed = [item for item in session.items if item.role == "user"]
        self.assertEqual(
            [item.origin for item in typed], ["conversation", "tool_media"]
        )

    def test_no_media_message_when_a_call_loads_nothing(self) -> None:
        session, _ = self.run_turn([self.call("c1", ["absent.png"])])

        self.assertEqual(
            [item for item in session.items if item.is_tool_media], []
        )

    def test_the_media_message_names_the_source(self) -> None:
        """The model must not read its own tool output as a new request."""
        (self.root / "a.png").write_bytes(png_bytes(8, 8))

        session, _ = self.run_turn([self.call("c1", ["a.png"])])

        media = next(item for item in session.items if item.is_tool_media)
        text = "".join(
            part.text for part in media.parts if isinstance(part, TextPart)
        )
        self.assertIn("Tool output", text)
        self.assertIn("a.png", text)

    def test_tool_media_does_not_create_a_turn_context(self) -> None:
        (self.root / "a.png").write_bytes(png_bytes(8, 8))

        _, provider = self.run_turn([self.call("c1", ["a.png"])])

        turn_contexts = [
            message
            for message in provider.requests[-1].messages
            if message.role == "system"
            and str(message.content).startswith("[Turn Context]")
        ]
        self.assertEqual(len(turn_contexts), 1)
        self.assertNotIn("tool images", turn_contexts[0].content or "")

    def test_the_second_model_call_receives_the_image(self) -> None:
        (self.root / "a.png").write_bytes(png_bytes(8, 8))

        _, provider = self.run_turn([self.call("c1", ["a.png"])])

        final = provider.requests[-1]
        images = [
            part
            for message in final.messages
            for part in message.parts
            if isinstance(part, ImagePart)
        ]
        self.assertEqual(len(images), 1)


if __name__ == "__main__":
    unittest.main()
