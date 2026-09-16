import unittest
import base64
import _thread
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    ImagePart,
    LLMRequest,
    ProviderCapabilities,
    Message,
    TokenUsage,
    ToolCall,
    ToolDefinition,
    TextPart,
)
from agent_core.providers import LiteLLMProvider


def chunk(
    content: object | None = None,
    tool_calls: list[dict] | None = None,
    usage: object | None = None,
) -> object:
    """Build a streamed chunk shaped like a LiteLLM delta."""
    delta = {"content": content, "tool_calls": tool_calls}
    choice = {"delta": delta, "finish_reason": None}
    return type(
        "Chunk",
        (),
        {"choices": [choice], "usage": usage},
    )()


USAGE = type(
    "Usage",
    (),
    {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    },
)()


class LiteLLMProviderTest(unittest.TestCase):
    @patch("agent_core.providers.litellm_provider.completion")
    def test_stream_yields_to_a_main_thread_interrupt_while_waiting(
        self,
        completion_mock,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()

        class BlockingStream:
            def __iter__(self):
                return self

            def __next__(self):
                entered.set()
                release.wait()
                raise StopIteration

        completion_mock.return_value = BlockingStream()
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=1000,
        )

        def interrupt_after_read_starts() -> None:
            entered.wait(1)
            _thread.interrupt_main()

        threading.Thread(
            target=interrupt_after_read_starts,
            daemon=True,
        ).start()
        started = time.monotonic()
        try:
            with self.assertRaises(KeyboardInterrupt):
                provider.stream(
                    LLMRequest(
                        system_prompt="Answer.",
                        messages=(Message(role="user", content="hello"),),
                    ),
                    lambda _text: None,
                )
        finally:
            release.set()

        self.assertLess(time.monotonic() - started, 0.5)

    @patch("agent_core.providers.litellm_provider.completion")
    def test_cancellable_stream_checks_while_waiting_for_a_chunk(
        self,
        completion_mock,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        cancelled = threading.Event()

        class BlockingStream:
            def __iter__(self):
                return self

            def __next__(self):
                entered.set()
                release.wait()
                raise StopIteration

        completion_mock.return_value = BlockingStream()
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=1000,
        )

        def cancel_after_read_starts() -> None:
            entered.wait(1)
            cancelled.set()

        def check_cancelled() -> None:
            if cancelled.is_set():
                raise KeyboardInterrupt

        threading.Thread(target=cancel_after_read_starts, daemon=True).start()
        started = time.monotonic()
        try:
            with self.assertRaises(KeyboardInterrupt):
                provider.stream_cancellable(
                    LLMRequest(
                        system_prompt="Answer.",
                        messages=(Message(role="user", content="hello"),),
                    ),
                    lambda _text: None,
                    None,
                    check_cancelled,
                )
        finally:
            release.set()

        self.assertLess(time.monotonic() - started, 0.5)

    @patch("agent_core.providers.litellm_provider.get_model_info", return_value={"supports_vision": True})
    @patch("agent_core.providers.litellm_provider.completion")
    def test_resolves_relative_image_against_explicit_media_root(
        self,
        completion_mock,
        _model_info_mock,
    ) -> None:
        completion_mock.return_value = iter([chunk(content="ok")])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / ".nosis" / "attachments" / "a1.png"
            image.parent.mkdir(parents=True)
            image.write_bytes(b"image-bytes")
            provider = LiteLLMProvider(
                model="vision/test",
                max_context_tokens=1000,
                media_root=root,
            )
            provider.stream(
                LLMRequest(
                    system_prompt="Answer.",
                    messages=(
                        Message(
                            role="user",
                            content=(
                                TextPart(text="What is this?"),
                                ImagePart(path=".nosis/attachments/a1.png"),
                            ),
                        ),
                    ),
                ),
                lambda _text: None,
            )

        sent = completion_mock.call_args.kwargs["messages"][1]["content"]
        encoded = base64.b64encode(b"image-bytes").decode("ascii")
        self.assertEqual(
            sent[1]["image_url"]["url"],
            f"data:image/png;base64,{encoded}",
        )

    def test_relative_image_without_media_root_is_rejected(self) -> None:
        from agent_core.providers.litellm_provider import _content_to_provider_format

        with self.assertRaisesRegex(ValueError, "media_root"):
            _content_to_provider_format(
                Message(
                    role="user",
                    content=(ImagePart(path=".nosis/attachments/a1.png"),),
                )
            )

    def test_names_the_attachment_and_the_tool_when_it_is_registered(
        self,
    ) -> None:
        """A text-only model is told how to reach the image it cannot see."""
        from agent_core.providers.litellm_provider import (
            _content_to_provider_format,
        )

        content = _content_to_provider_format(
            Message(
                role="user",
                content=(
                    TextPart(text="look at this"),
                    ImagePart(path=".nosis/attachments/a1.png"),
                ),
            ),
            include_images=False,
            can_analyze_images=True,
        )

        self.assertEqual(
            content,
            "look at this\n\n"
            "Attached images (use analyze_image if needed):\n"
            "- .nosis/attachments/a1.png",
        )

    def test_does_not_name_a_tool_the_model_does_not_have(self) -> None:
        """Pointing at an unregistered tool would only invite a failed call."""
        from agent_core.providers.litellm_provider import (
            _content_to_provider_format,
        )

        content = _content_to_provider_format(
            Message(
                role="user",
                content=(
                    TextPart(text="look at this"),
                    ImagePart(path=".nosis/attachments/a1.png"),
                ),
            ),
            include_images=False,
            can_analyze_images=False,
        )

        self.assertNotIn("analyze_image", content)
        # The path is still named, so the model can say what it cannot read.
        self.assertIn(".nosis/attachments/a1.png", content)

    def test_derives_the_hint_from_the_registered_tools(self) -> None:
        """The flag comes from the request's tools, not from a guess."""
        provider = LiteLLMProvider(
            model="text/only",
            max_context_tokens=1000,
            media_root=Path.cwd(),
        )
        message = Message(
            role="user",
            content=(ImagePart(path=".nosis/attachments/a1.png"),),
        )
        analyze = ToolDefinition(
            name="analyze_image",
            description="Analyze an image.",
            parameters={"type": "object", "properties": {}},
        )

        with patch.object(
            LiteLLMProvider,
            "capabilities",
            property(lambda self: ProviderCapabilities(frozenset({"text"}))),
        ):
            from agent_core.providers.litellm_provider import _request_messages

            without = _request_messages(
                LLMRequest(system_prompt="s", messages=(message,)),
                provider,
            )
            with_tool = _request_messages(
                LLMRequest(
                    system_prompt="s",
                    messages=(message,),
                    tools=(analyze,),
                ),
                provider,
            )

        self.assertNotIn("analyze_image", without[1]["content"])
        self.assertIn("use analyze_image", with_tool[1]["content"])

    @patch("agent_core.providers.litellm_provider.get_model_info", return_value={"supports_vision": True})
    @patch("agent_core.providers.litellm_provider.completion")
    def test_missing_image_from_another_workspace_becomes_text_notice(
        self,
        completion_mock,
        _model_info_mock,
    ) -> None:
        completion_mock.return_value = iter([chunk(content="ok")])
        with tempfile.TemporaryDirectory() as directory:
            provider = LiteLLMProvider(
                model="vision/test",
                max_context_tokens=1000,
                media_root=Path(directory),
            )
            provider.stream(
                LLMRequest(
                    system_prompt="Answer.",
                    messages=(
                        Message(
                            role="user",
                            content=(ImagePart(path=".nosis/attachments/from-a.png"),),
                        ),
                    ),
                ),
                lambda _text: None,
            )

        sent = completion_mock.call_args.kwargs["messages"][1]["content"]
        self.assertEqual(sent[0]["type"], "text")
        self.assertIn("unavailable", sent[0]["text"])

    @patch("agent_core.providers.litellm_provider.token_counter")
    @patch("agent_core.providers.litellm_provider.completion")
    def test_passes_configured_model_url_key_and_output_limit(
        self,
        completion_mock,
        token_counter_mock,
    ) -> None:
        completion_mock.return_value = iter(
            [
                chunk(content="resp"),
                chunk(content="onse"),
                chunk(usage=USAGE),
            ]
        )
        provider = LiteLLMProvider(
            model="openai/test-model",
            base_url="https://example.com/v1",
            api_key="secret",
            max_context_tokens=1000,
        )

        request = LLMRequest(
            system_prompt="You are helpful.",
            messages=(Message(role="user", content="hello"),),
            max_generation_tokens=100,
        )
        token_counter_mock.return_value = 12

        input_tokens = provider.count_input_tokens(request)
        deltas: list[str] = []
        response = provider.stream(request, deltas.append)

        self.assertEqual(input_tokens, 12)
        self.assertEqual(deltas, ["resp", "onse"])
        self.assertEqual(response.content, "response")
        self.assertEqual(
            response.usage,
            TokenUsage(
                input_tokens=12,
                output_tokens=5,
                total_tokens=17,
            ),
        )
        token_counter_mock.assert_called_once_with(
            model="openai/test-model",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
            ],
            tools=None,
        )
        completion_mock.assert_called_once_with(
            model="openai/test-model",
            base_url="https://example.com/v1",
            api_key="secret",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "hello"},
            ],
            stream=True,
            stream_options={"include_usage": True},
            max_completion_tokens=100,
        )

    @patch("agent_core.providers.litellm_provider.completion")
    def test_parses_structured_text_content_blocks(self, completion_mock) -> None:
        """Adapters may expose streamed text as OpenAI-style blocks."""
        completion_mock.return_value = iter(
            [
                chunk(content=[{"type": "text", "text": "first "}]),
                chunk(content=[{"type": "text", "text": "second"}]),
            ]
        )
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=1000,
        )

        deltas: list[str] = []
        response = provider.stream(
            LLMRequest(
                system_prompt="Answer.",
                messages=(Message(role="user", content="hello"),),
            ),
            deltas.append,
        )

        self.assertEqual(response.content, "first second")
        self.assertEqual(deltas, ["first ", "second"])
        self.assertNotIn(
            "max_completion_tokens",
            completion_mock.call_args.kwargs,
        )

    @patch("agent_core.providers.litellm_provider.completion")
    def test_serializes_tools_and_parses_tool_calls(
        self,
        completion_mock,
    ) -> None:
        completion_mock.return_value = iter(
            [
                chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "id": "call-1",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path"',
                            },
                        }
                    ]
                ),
                chunk(
                    tool_calls=[
                        {
                            "index": 0,
                            "function": {"arguments": ': "README.md"}'},
                        }
                    ]
                ),
            ]
        )
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=1000,
        )
        tool = ToolDefinition(
            name="read_file",
            description="Read a workspace file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        )

        response = provider.stream(
            LLMRequest(
                system_prompt="You are helpful.",
                messages=(
                    Message(role="user", content="read README.md"),
                    Message(
                        role="assistant",
                        content=None,
                        reasoning="Inspect the file before answering.",
                        tool_calls=(
                            ToolCall(
                                id="previous-call",
                                name="read_file",
                                arguments={"path": "README.md"},
                            ),
                        ),
                    ),
                    Message(
                        role="tool",
                        content='{"ok": true}',
                        tool_call_id="previous-call",
                    ),
                ),
                tools=(tool,),
            ),
            lambda text: None,
        )

        self.assertEqual(
            response.tool_calls,
            (
                ToolCall(
                    id="call-1",
                    name="read_file",
                    arguments={"path": "README.md"},
                ),
            ),
        )
        completion_mock.assert_called_once_with(
            model="openai/test-model",
            base_url=None,
            api_key=None,
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "read README.md"},
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "Inspect the file before answering.",
                    "tool_calls": [
                        {
                            "id": "previous-call",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": '{"path": "README.md"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": '{"ok": true}',
                    "tool_call_id": "previous-call",
                },
            ],
            stream=True,
            stream_options={"include_usage": True},
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a workspace file.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        )

    @patch(
        "agent_core.providers.litellm_provider.token_counter",
        return_value=42,
    )
    def test_counts_tools_as_part_of_input(
        self,
        token_counter_mock,
    ) -> None:
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=1000,
        )
        tool = ToolDefinition(
            name="read_file",
            description="Read a workspace file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        )

        count = provider.count_input_tokens(
            LLMRequest(
                system_prompt="You are helpful.",
                messages=(Message(role="user", content="read it"),),
                tools=(tool,),
            )
        )

        self.assertEqual(count, 42)
        token_counter_mock.assert_called_once_with(
            model="openai/test-model",
            messages=[
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "read it"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "Read a workspace file.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                            },
                        },
                    },
                }
            ],
        )

    @patch("agent_core.providers.litellm_provider.get_model_info")
    def test_uses_litellm_context_limit_when_not_configured(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": 128000,
            "max_output_tokens": 8192,
        }

        provider = LiteLLMProvider(
            model="openai/test-model",
            base_url="https://example.com/v1",
        )

        self.assertEqual(provider.max_context_tokens, 128000)
        self.assertEqual(provider.max_output_tokens, 8192)
        get_model_info_mock.assert_called_once_with(
            model="openai/test-model",
            api_base="https://example.com/v1",
        )

    @patch("agent_core.providers.litellm_provider.get_model_info")
    def test_configured_context_limit_skips_litellm_metadata(
        self,
        get_model_info_mock,
    ) -> None:
        provider = LiteLLMProvider(
            model="openai/test-model",
            max_context_tokens=64000,
        )

        self.assertEqual(provider.max_context_tokens, 64000)
        get_model_info_mock.assert_not_called()

    def test_rejects_invalid_configured_context_limit(self) -> None:
        with self.assertRaises(ValueError):
            LiteLLMProvider(
                model="openai/test-model",
                max_context_tokens=0,
            )

    @patch("agent_core.providers.litellm_provider.get_model_info")
    def test_requires_config_for_model_without_context_metadata(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": None,
            "max_output_tokens": 8192,
        }

        with self.assertRaises(ValueError):
            LiteLLMProvider(model="custom/model")

    @patch("agent_core.providers.litellm_provider.get_model_info")
    def test_requires_config_when_model_metadata_lookup_fails(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.side_effect = RuntimeError("unknown model")

        with self.assertRaisesRegex(
            ValueError,
            "configure 'max_context_tokens'",
        ):
            LiteLLMProvider(model="custom/model")

    @patch("agent_core.providers.litellm_provider.get_model_info")
    def test_unknown_output_limit_is_none(
        self,
        get_model_info_mock,
    ) -> None:
        get_model_info_mock.return_value = {
            "max_input_tokens": 128000,
            "max_output_tokens": None,
            "max_tokens": 8192,
        }

        provider = LiteLLMProvider(model="custom/model")

        self.assertIsNone(provider.max_output_tokens)


if __name__ == "__main__":
    unittest.main()
