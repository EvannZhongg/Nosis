"""Desktop screenshots travel through the shared tool-media path."""

import tempfile
import unittest
from pathlib import Path

from datetime import datetime, timezone

from agent_core import (
    Agent,
    AgentConfig,
    ComputerScreenshotTool,
    ImagePart,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    ProviderCapabilities,
    Session,
    ToolCall,
    ToolCatalog,
    ToolConfig,
    ToolExecutionContext,
    Workspace,
)
from agent_core.config import ContextCompressionConfig
from tests.test_media import png_bytes


class ComputerScreenshotToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.workspace = Workspace(self.root)
        self.session = Session()

    def context(self, capture, *, vision=True, vision_provider=None):
        return ToolExecutionContext(
            workspace=self.workspace,
            session=self.session,
            vision_input=vision,
            vision_provider=vision_provider,
            screenshot=capture,
        )

    def capture(self, _label: str) -> Path:
        path = self.root / ".nosis" / "attachments" / "desktop.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(png_bytes(40, 20))
        return path

    def test_returns_a_workspace_relative_image_attachment(self) -> None:
        result = ComputerScreenshotTool().execute(
            {"label": "desktop"}, self.context(self.capture)
        )

        self.assertEqual(
            result.attachments,
            (
                ImagePart(
                    path=".nosis/attachments/desktop.png",
                    mime_type="image/png",
                    filename="desktop.png",
                    size_bytes=(
                        self.root / ".nosis" / "attachments" / "desktop.png"
                    ).stat().st_size,
                ),
            ),
        )
        self.assertEqual(result.output["width"], 40)
        self.assertEqual(result.output["height"], 20)

    def test_is_available_only_for_direct_model_vision(self) -> None:
        tool = ComputerScreenshotTool()
        self.assertTrue(tool.available(self.context(self.capture)))
        self.assertFalse(tool.available(self.context(self.capture, vision=False, vision_provider=object())))
        self.assertFalse(tool.available(self.context(None, vision=False)))

    def test_rejects_a_capture_outside_the_workspace(self) -> None:
        outside = self.root.parent / "outside-screenshot.png"
        outside.write_bytes(png_bytes(4, 4))
        self.addCleanup(outside.unlink, missing_ok=True)

        with self.assertRaisesRegex(ValueError, "not readable"):
            ComputerScreenshotTool().execute(
                {}, self.context(lambda _label: outside)
            )


class _ScreenshotProvider(LLMProvider):
    def __init__(self) -> None:
        self.requests: list[LLMRequest] = []
        self.responses = iter(
            [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            "capture-1",
                            "computer_screenshot",
                            {"label": "desktop"},
                        ),
                    ),
                ),
                LLMResponse(content="I can see the desktop."),
            ]
        )

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(frozenset({"text", "image"}))

    @property
    def max_context_tokens(self) -> int:
        return 100_000

    def count_input_tokens(self, request: LLMRequest) -> int:
        return 10

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        return next(self.responses)


class ComputerScreenshotContextTest(unittest.TestCase):
    def test_screenshot_reaches_the_next_model_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = Workspace(root)
            screenshot = root / ".nosis" / "attachments" / "desktop.png"
            screenshot.parent.mkdir(parents=True)
            screenshot.write_bytes(png_bytes(16, 10))
            session = Session()
            provider = _ScreenshotProvider()
            context = ToolExecutionContext(
                workspace=workspace,
                session=session,
                vision_input=True,
                screenshot=lambda _label: screenshot,
            )
            config = AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(enabled=False),
            )
            agent = Agent(
                provider=provider,
                session=session,
                system_prompt="S",
                consolidator_prompt="C",
                config=config,
                tools=ToolCatalog((ComputerScreenshotTool(),)).select(
                    ("computer_screenshot",), context
                ),
                context=context,
                now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
            )

            agent.run("observe the desktop")

            self.assertGreaterEqual(len(provider.requests), 2)
            final_request = provider.requests[-1]
            images = [
                part
                for message in final_request.messages
                for part in message.parts
                if isinstance(part, ImagePart)
            ]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].path, ".nosis/attachments/desktop.png")


if __name__ == "__main__":
    unittest.main()
