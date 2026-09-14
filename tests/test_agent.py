import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core.session_paths import workspace_key
from agent_core import (
    Agent,
    AgentConfig,
    AssistantMessageDeltaEvent,
    AssistantMessageEvent,
    ContextManager,
    ContextWindowExceededError,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    ReasoningDeltaEvent,
    Session,
    Tool,
    ToolBatchStartedEvent,
    ToolCall,
    ToolCallEvent,
    ToolCallLimitExceededError,
    ToolConfig,
    ToolDefinition,
    ToolResult,
    ToolResultEvent,
    ToolResultNormalizer,
    Workspace,
    ImagePart,
    TextPart,
    ToolCatalog,
    ToolExecutionContext,
)

REQUEST_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
TOOL_CALL_TIME = datetime(2026, 9, 9, 8, 0, 10, tzinfo=timezone.utc)
TOOL_RESULT_TIME = datetime(2026, 9, 9, 8, 0, 11, tzinfo=timezone.utc)
RESPONSE_TIME = datetime(2026, 9, 9, 8, 1, tzinfo=timezone.utc)
AGENT_CONFIG = AgentConfig(
    max_same_tool_calls=5,
    max_output_tokens=100,
    tools=ToolConfig(enabled=()),
)
TEST_WORKSPACE = Workspace(Path(__file__).parent)


def tool_set(*tools, session=None, workspace=TEST_WORKSPACE, policy=None, **context_fields):
    """Select every given Tool, as a Runtime would from its catalog."""
    context = ToolExecutionContext(
        workspace=workspace,
        session=session if session is not None else Session(),
        **context_fields,
    )
    catalog = ToolCatalog(tools)
    return catalog.select(catalog.names, context, policy=policy)


class MockProvider(LLMProvider):
    def __init__(
        self,
        responses: list[str | LLMResponse],
        input_tokens: int = 1,
        max_context_tokens: int = 1000,
    ) -> None:
        self._responses = iter(responses)
        self._input_tokens = input_tokens
        self._max_context_tokens = max_context_tokens
        self.counted_requests: list[LLMRequest] = []
        self.requests: list[LLMRequest] = []

    @property
    def max_context_tokens(self) -> int:
        return self._max_context_tokens

    def count_input_tokens(self, request: LLMRequest) -> int:
        self.counted_requests.append(request)
        return self._input_tokens

    def stream(
        self,
        request: LLMRequest,
        on_text_delta,
        on_reasoning_delta=None,
    ) -> LLMResponse:
        self.requests.append(request)
        response = next(self._responses)
        if isinstance(response, str):
            response = LLMResponse(content=response)
        if response.reasoning and on_reasoning_delta is not None:
            on_reasoning_delta(response.reasoning)
        if response.content:
            on_text_delta(response.content)
        return response


class EchoTool(Tool):
    name = "echo"

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Echo the provided text.",
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments, context):
        return {"text": arguments["text"]}


def clock(*values: datetime):
    times = iter(values)
    return lambda: next(times)


class AgentTest(unittest.TestCase):
    def test_calls_provider_and_writes_user_and_assistant_items(self) -> None:
        provider = MockProvider(["hello back"])
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        result = agent.run("hello")

        self.assertEqual(result.response.content, "hello back")
        self.assertEqual(result.request_timestamp_utc, REQUEST_TIME)
        self.assertEqual(result.response_timestamp_utc, RESPONSE_TIME)
        self.assertEqual(result.request, provider.requests[0])
        request_messages = provider.requests[0].messages
        self.assertNotIn("[Conversation Timeline]", provider.requests[0].system_prompt)
        self.assertEqual(provider.requests[0].max_output_tokens, 100)
        self.assertEqual(request_messages[0].role, "system")
        self.assertIn("[Conversation Timeline]", request_messages[0].content)
        self.assertEqual(request_messages[1].role, "user")
        self.assertEqual(request_messages[1].content, "hello")
        self.assertEqual(
            session.items,
            [
                Message(
                    role="user",
                    content="hello",
                    timestamp_utc=REQUEST_TIME,
                ),
                Message(
                    role="assistant",
                    content="hello back",
                    timestamp_utc=RESPONSE_TIME,
                ),
            ],
        )

    def test_rejects_context_before_calling_provider(self) -> None:
        provider = MockProvider(["unused"], input_tokens=901)
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(REQUEST_TIME),
        )

        with self.assertRaises(ContextWindowExceededError) as context:
            agent.run("hello")

        self.assertEqual(context.exception.input_tokens, 901)
        self.assertEqual(context.exception.max_context_tokens, 1000)
        self.assertEqual(context.exception.max_output_tokens, 100)
        self.assertEqual(context.exception.max_input_tokens, 900)
        self.assertEqual(len(provider.counted_requests), 1)
        self.assertEqual(provider.requests, [])

    def test_rejects_output_limit_not_smaller_than_context_limit(self) -> None:
        provider = MockProvider(["unused"], max_context_tokens=100)
        session = Session(session_id="session-1")

        with self.assertRaises(ValueError):
            Agent(
                provider=provider,
                session=session,
                system_prompt="You are helpful.",
                config=AGENT_CONFIG,
                tools=tool_set(session=session),
                context=ToolExecutionContext(
                    workspace=TEST_WORKSPACE, session=session
                ),
            )

    def test_mock_provider_receives_complete_multi_turn_context(self) -> None:
        provider = MockProvider(["first answer", "second answer"])
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(
                datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 8, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 9, 1, tzinfo=timezone.utc),
            ),
        )

        agent.run("first question")
        result = agent.run("second question")

        self.assertEqual(result.response.content, "second answer")
        self.assertEqual(result.request, provider.requests[1])
        self.assertNotIn("[Conversation Timeline]", provider.requests[1].system_prompt)
        history = provider.requests[1].messages
        self.assertEqual(
            [message.role for message in history],
            ["system", "user", "assistant", "system", "user"],
        )
        self.assertEqual(
            [message.content for message in history],
            [
                history[0].content,
                "first question",
                "first answer",
                history[3].content,
                "second question",
            ],
        )
        self.assertIn("[Conversation Timeline]", history[0].content)
        self.assertIn("[Conversation Timeline]", history[3].content)
        self.assertEqual(
            session.items,
            [
                Message(
                    role="user",
                    content="first question",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 0, tzinfo=timezone.utc
                    ),
                ),
                Message(
                    role="assistant",
                    content="first answer",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 1, tzinfo=timezone.utc
                    ),
                ),
                Message(
                    role="user",
                    content="second question",
                    timestamp_utc=datetime(
                        2026, 9, 9, 9, 0, tzinfo=timezone.utc
                    ),
                ),
                Message(
                    role="assistant",
                    content="second answer",
                    timestamp_utc=datetime(
                        2026, 9, 9, 9, 1, tzinfo=timezone.utc
                    ),
                ),
            ],
        )

    def test_executes_tool_calls_and_continues_until_final_response(self) -> None:
        tool_call = ToolCall(
            id="call-1",
            name="echo",
            arguments={"text": "hello"},
        )
        provider = MockProvider(
            [
                LLMResponse(
                    content="I'll use the echo tool.",
                    tool_calls=(tool_call,),
                    reasoning="I need to inspect the requested input first.",
                ),
                LLMResponse(content="tool completed"),
            ]
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            now=clock(
                REQUEST_TIME,
                TOOL_CALL_TIME,
                TOOL_RESULT_TIME,
                RESPONSE_TIME,
            ),
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
        )
        events = []

        result = agent.run("use the echo tool", on_event=events.append)

        self.assertEqual(result.response.content, "tool completed")
        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(result.request, provider.requests[1])
        self.assertEqual(
            provider.requests[0].tools,
            (
                EchoTool().definition(
                    ToolExecutionContext(
                        workspace=TEST_WORKSPACE, session=session
                    )
                ),
            ),
        )
        second_request_messages = provider.requests[1].messages
        self.assertEqual(
            [message.role for message in second_request_messages],
            ["system", "user", "assistant", "tool"],
        )
        self.assertEqual(
            second_request_messages[2],
            Message(
                role="assistant",
                content="I'll use the echo tool.",
                timestamp_utc=TOOL_CALL_TIME,
                tool_calls=(tool_call,),
                reasoning="I need to inspect the requested input first.",
            ),
        )
        self.assertEqual(second_request_messages[3].tool_call_id, "call-1")
        self.assertEqual(
            second_request_messages[2].reasoning,
            "I need to inspect the requested input first.",
        )
        self.assertIn("assistant step (echo)", second_request_messages[0].content)
        self.assertIn("tool result (call-1)", second_request_messages[0].content)
        self.assertEqual(
            json.loads(second_request_messages[3].content),
            {
                "ok": True,
                "output": {"text": "hello"},
            },
        )
        self.assertEqual(
            session.items,
            [
                Message(
                    role="user",
                    content="use the echo tool",
                    timestamp_utc=REQUEST_TIME,
                ),
                Message(
                    role="assistant",
                    content="I'll use the echo tool.",
                    timestamp_utc=TOOL_CALL_TIME,
                    tool_calls=(tool_call,),
                    reasoning="I need to inspect the requested input first.",
                ),
                Message(
                    role="tool",
                    content=second_request_messages[3].content,
                    timestamp_utc=TOOL_RESULT_TIME,
                    tool_call_id="call-1",
                ),
                Message(
                    role="assistant",
                    content="tool completed",
                    timestamp_utc=RESPONSE_TIME,
                ),
            ],
        )
        self.assertEqual(result.items, tuple(session.items))
        self.assertEqual(
            [
                event
                for event in events
                if isinstance(event, AssistantMessageDeltaEvent)
            ],
            [
                AssistantMessageDeltaEvent(
                    text="I'll use the echo tool.",
                    model_call_index=1,
                ),
                AssistantMessageDeltaEvent(
                    text="tool completed",
                    model_call_index=2,
                ),
            ],
        )
        events = [
            event
            for event in events
            if not isinstance(event, AssistantMessageDeltaEvent)
        ]
        self.assertEqual(
            events[0],
            ReasoningDeltaEvent(
                text="I need to inspect the requested input first.",
                model_call_index=1,
            ),
        )
        self.assertEqual(
            events[1],
            AssistantMessageEvent(
                content="I'll use the echo tool.",
                timestamp_utc=TOOL_CALL_TIME,
                model_call_index=1,
            ),
        )
        self.assertEqual(
            events[2],
            ToolBatchStartedEvent(
                model_call_index=1,
                tool_calls=(tool_call,),
            ),
        )
        self.assertEqual(
            events[3],
            ToolCallEvent(
                tool_call=tool_call,
                tool_index=1,
                tool_count=1,
            ),
        )
        self.assertIsInstance(events[4], ToolResultEvent)
        self.assertEqual(events[4].tool_result.name, "echo")
        self.assertEqual(
            events[4].tool_result.output,
            {"text": "hello"},
        )
        self.assertEqual(
            events[5],
            AssistantMessageEvent(
                content="tool completed",
                timestamp_utc=RESPONSE_TIME,
                model_call_index=2,
            ),
        )

    def test_keeps_historical_tool_chain_without_intermediate_timestamps(
        self,
    ) -> None:
        tool_call = ToolCall(
            id="call-1",
            name="echo",
            arguments={"text": "hello"},
        )
        provider = MockProvider(
            [
                LLMResponse(content="using echo", tool_calls=(tool_call,)),
                LLMResponse(content="first answer"),
                LLMResponse(content="second answer"),
            ]
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(
                REQUEST_TIME,
                TOOL_CALL_TIME,
                TOOL_RESULT_TIME,
                RESPONSE_TIME,
                datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 9, 1, tzinfo=timezone.utc),
            ),
        )

        agent.run("first question")
        agent.run("second question")

        history = provider.requests[2].messages
        self.assertEqual(
            [message.role for message in history],
            [
                "system",
                "user",
                "assistant",
                "tool",
                "assistant",
                "system",
                "user",
            ],
        )
        self.assertEqual(history[2].tool_calls, (tool_call,))
        self.assertEqual(history[3].tool_call_id, "call-1")
        self.assertEqual(
            [message.timestamp_utc for message in history[1:5]],
            [REQUEST_TIME, None, None, RESPONSE_TIME],
        )
        self.assertEqual(
            [message.reasoning for message in history[1:5]],
            [None, None, None, None],
        )
        historical_timeline = history[0].content or ""
        self.assertIn("user", historical_timeline)
        self.assertIn("assistant", historical_timeline)
        self.assertNotIn("assistant step", historical_timeline)
        self.assertNotIn("tool result", historical_timeline)

    def test_historical_image_is_not_carried_into_next_request(self) -> None:
        session = Session(session_id="images")
        session.add_item(
            "user",
            "look at this",
            attachments=(ImagePart(path=".nosis/attachments/a1.png"),),
        )
        provider = MockProvider(["ok"])
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="Be helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
        )
        agent.run("follow up")

        self.assertEqual(provider.requests[0].media_root, TEST_WORKSPACE.path)
        historical = provider.requests[0].messages[1]
        self.assertEqual(historical.role, "user")
        self.assertEqual(historical.content, "look at this\n[Image attachment omitted from historical context]")
        self.assertEqual(historical.parts, (TextPart(text=historical.content),))

    def test_context_archive_does_not_send_image_parts(self) -> None:
        session = Session(session_id="archive-images")
        session.add_item(
            "user",
            "summarize this",
            attachments=(ImagePart(path=".nosis/attachments/a1.png"),),
        )
        provider = MockProvider(["summary"])
        context = ContextManager(
            provider=provider,
            session=session,
            system_prompt="Be helpful.",
            config=AGENT_CONFIG,
        )
        context.begin_turn(len(session.items))
        context.archive()

        archived = provider.requests[0].messages
        self.assertTrue(all(not message.parts or all(
            isinstance(part, TextPart) for part in message.parts
        ) for message in archived))

    def test_context_archive_keeps_tools_without_intermediate_timestamps(
        self,
    ) -> None:
        tool_call = ToolCall(
            id="call-1",
            name="echo",
            arguments={"text": "hello"},
        )
        session = Session(
            session_id="session-1",
            items=[
                Message("user", "question", REQUEST_TIME),
                Message(
                    "assistant",
                    None,
                    TOOL_CALL_TIME,
                    tool_calls=(tool_call,),
                    reasoning="Use the tool to inspect the file.",
                ),
                Message(
                    "tool",
                    '{"ok": true}',
                    TOOL_RESULT_TIME,
                    tool_call_id="call-1",
                ),
                Message(
                    "assistant",
                    "answer",
                    RESPONSE_TIME,
                    reasoning="Summarize the tool result.",
                ),
            ],
        )
        provider = MockProvider(["checkpoint"])
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
        )

        agent._context.begin_turn(len(session.items))
        agent._context.archive()

        archived = provider.requests[0].messages
        self.assertEqual(
            [message.role for message in archived],
            ["system", "user", "assistant", "tool", "assistant"],
        )
        self.assertEqual(archived[2].tool_calls, (tool_call,))
        self.assertEqual(archived[3].tool_call_id, "call-1")
        self.assertEqual(
            [message.timestamp_utc for message in archived[1:]],
            [REQUEST_TIME, None, None, RESPONSE_TIME],
        )
        self.assertEqual(
            [message.reasoning for message in archived[1:]],
            [None, None, None, None],
        )
        timeline = archived[0].content or ""
        self.assertNotIn("assistant step", timeline)
        self.assertNotIn("tool result", timeline)

    def test_context_archive_requires_active_turn(self) -> None:
        provider = MockProvider(["checkpoint"])
        context = ContextManager(
            provider=provider,
            session=Session(session_id="session-1"),
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "begin_turn must be called before archiving context",
        ):
            context.archive()

    def test_returns_unknown_tool_error_to_model(self) -> None:
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="missing",
                            arguments={},
                        ),
                    ),
                ),
                LLMResponse(content="cannot use that tool"),
            ]
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(
                REQUEST_TIME,
                TOOL_CALL_TIME,
                TOOL_RESULT_TIME,
                RESPONSE_TIME,
            ),
        )

        agent.run("use a missing tool")

        tool_message = provider.requests[1].messages[-1]
        self.assertEqual(tool_message.role, "tool")
        self.assertEqual(
            json.loads(tool_message.content),
            {
                "ok": False,
                "error": {
                    "type": "tool_not_found",
                    "message": "tool 'missing' is not registered",
                },
            },
        )

    def test_normalizes_large_tool_result_before_model_feedback(self) -> None:
        tool_call = ToolCall(
            id="call-1",
            name="echo",
            arguments={"text": "abcdefghijklmnopqrstuvwxyz"},
        )
        provider = MockProvider(
            [
                LLMResponse(content=None, tool_calls=(tool_call,)),
                LLMResponse(content="done"),
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            workspace = Workspace(Path(directory))
            session = Session(session_id="session-1")
            agent = Agent(
                provider=provider,
                session=session,
                system_prompt="You are helpful.",
                config=AGENT_CONFIG,
                now=clock(
                    REQUEST_TIME,
                    TOOL_CALL_TIME,
                    TOOL_RESULT_TIME,
                    RESPONSE_TIME,
                ),
                tools=tool_set(
                    EchoTool(), session=session, workspace=workspace
                ),
                context=ToolExecutionContext(
                    workspace=workspace, session=session
                ),
                tool_result_normalizer=ToolResultNormalizer(
                    workspace,
                    session.session_id,
                    max_chars=20,
                    preview_chars=12,
                    sessions_directory=workspace.path / ".nosis" / "sessions",
                ),
            )
            events = []

            agent.run("use the echo tool", on_event=events.append)

            tool_message = provider.requests[1].messages[-1]
            feedback = json.loads(tool_message.content)
            artifact_path = (
                f".nosis/sessions/{workspace_key(workspace.path)}"
                "/session-1/call-1.txt"
            )
            self.assertEqual(feedback["artifact_path"], artifact_path)
            self.assertEqual(
                feedback["read_instruction"],
                "Use read_file with path "
                f"'{artifact_path}' to read the complete tool result.",
            )
            full_result = ToolResult(
                tool_call_id="call-1",
                name="echo",
                output={"text": "abcdefghijklmnopqrstuvwxyz"},
            ).to_content()
            self.assertEqual(feedback["size_chars"], len(full_result))
            self.assertEqual(feedback["preview"], full_result[:12])
            self.assertEqual(
                (workspace.path / artifact_path).read_text(
                    encoding="utf-8"
                ),
                full_result,
            )
            result_events = [
                event
                for event in events
                if isinstance(event, ToolResultEvent)
            ]
            self.assertEqual(
                result_events[0].tool_result.output,
                {"text": "abcdefghijklmnopqrstuvwxyz"},
            )

    def test_stops_on_sixth_identical_tool_call(self) -> None:
        responses = [
            LLMResponse(
                content=None,
                tool_calls=(
                    ToolCall(
                        id=f"call-{index}",
                        name="echo",
                        arguments={"text": "hello"},
                    ),
                ),
            )
            for index in range(1, 7)
        ]
        provider = MockProvider(responses)
        session = Session(session_id="session-1")
        timestamps = [
            datetime(2026, 9, 9, 8, 0, index, tzinfo=timezone.utc)
            for index in range(11)
        ]
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(*timestamps),
        )

        with self.assertRaises(ToolCallLimitExceededError) as context:
            agent.run("repeat the echo tool")

        self.assertEqual(context.exception.tool_name, "echo")
        self.assertEqual(context.exception.limit, 5)
        self.assertEqual(len(provider.requests), 6)
        self.assertEqual(
            [item.role for item in session.items],
            ["user", *(["assistant", "tool"] * 5)],
        )

    def test_same_tool_with_different_arguments_resets_repeat_count(
        self,
    ) -> None:
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id=f"echo-{index}",
                            name="echo",
                            arguments={"text": "first"},
                        ),
                    ),
                )
                for index in range(1, 6)
            ]
            + [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="echo-different",
                            name="echo",
                            arguments={"text": "different"},
                        ),
                    ),
                )
            ]
            + [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id=f"echo-{index}",
                            name="echo",
                            arguments={"text": "first"},
                        ),
                    ),
                )
                for index in range(6, 11)
            ]
            + [LLMResponse(content="done")]
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=lambda: REQUEST_TIME,
        )

        result = agent.run("repeat echo with different arguments in between")

        self.assertEqual(result.response.content, "done")
        self.assertEqual(len(provider.requests), 12)

    def test_argument_object_key_order_does_not_reset_repeat_count(
        self,
    ) -> None:
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id=f"call-{index}",
                            name="echo",
                            arguments=arguments,
                        ),
                    ),
                )
                for index, arguments in enumerate(
                    [
                        {"text": "hello", "extra": 1},
                        {"extra": 1, "text": "hello"},
                        {"text": "hello", "extra": 1},
                        {"extra": 1, "text": "hello"},
                        {"text": "hello", "extra": 1},
                        {"extra": 1, "text": "hello"},
                    ],
                    start=1,
                )
            ]
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=lambda: REQUEST_TIME,
        )

        with self.assertRaises(ToolCallLimitExceededError):
            agent.run("repeat the exact same call")


class ConcurrentToolBatchTest(unittest.TestCase):
    """A batch runs in parallel only when every tool declares it is safe."""

    def test_runs_a_concurrent_tool_batch_on_separate_threads(self) -> None:
        tool = BlockingTool(concurrent=True, expected=3)
        session = Session(session_id="session-1")
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=tuple(
                        ToolCall(
                            id=f"call-{index}",
                            name="blocking",
                            arguments={"index": index},
                        )
                        for index in range(3)
                    ),
                ),
                LLMResponse(content="done"),
            ]
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(tool, session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=lambda: REQUEST_TIME,
        )

        agent.run("run three at once")

        # All three were inside execute() simultaneously, which only the
        # thread pool allows; a sequential run would deadlock on the barrier.
        self.assertEqual(tool.peak_concurrency, 3)

    def test_runs_a_non_concurrent_tool_batch_sequentially(self) -> None:
        tool = BlockingTool(concurrent=False, expected=1)
        session = Session(session_id="session-1")
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=tuple(
                        ToolCall(
                            id=f"call-{index}",
                            name="blocking",
                            arguments={"index": index},
                        )
                        for index in range(3)
                    ),
                ),
                LLMResponse(content="done"),
            ]
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(tool, session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=lambda: REQUEST_TIME,
        )

        agent.run("run three in order")

        self.assertEqual(tool.peak_concurrency, 1)

    def test_a_mixed_batch_stays_sequential(self) -> None:
        """One non-concurrent call keeps the whole batch on one thread."""
        blocking = BlockingTool(concurrent=True, expected=1)
        session = Session(session_id="session-1")
        provider = MockProvider(
            [
                LLMResponse(
                    content=None,
                    tool_calls=(
                        ToolCall(
                            id="call-1",
                            name="blocking",
                            arguments={"index": 0},
                        ),
                        ToolCall(
                            id="call-2",
                            name="echo",
                            arguments={"text": "hi"},
                        ),
                    ),
                ),
                LLMResponse(content="done"),
            ]
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AGENT_CONFIG,
            tools=tool_set(blocking, EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=lambda: REQUEST_TIME,
        )

        agent.run("one of each")

        self.assertEqual(blocking.peak_concurrency, 1)


class BlockingTool(Tool):
    """Records how many calls are inside execute() at the same time.

    ``expected`` callers must arrive before any of them returns, so a
    parallel batch is observable and a sequential one cannot fake it.
    """

    name = "blocking"

    def __init__(self, *, concurrent: bool, expected: int) -> None:
        self.concurrent = concurrent
        self._barrier = threading.Barrier(expected, timeout=5)
        self._lock = threading.Lock()
        self._active = 0
        self.peak_concurrency = 0

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Block until the batch has arrived.",
            parameters={
                "type": "object",
                "properties": {"index": {"type": "integer"}},
                "required": ["index"],
            },
        )

    def execute(self, arguments, context):
        with self._lock:
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)
        self._barrier.wait()
        with self._lock:
            self._active -= 1
        return {"index": arguments["index"]}


if __name__ == "__main__":
    unittest.main()
