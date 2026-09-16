import json
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core import (
    Agent,
    AgentConfig,
    AssistantMessageDeltaEvent,
    AssistantMessageEvent,
    ContextManager,
    ContextCompressionConfig,
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
    ToolResultEvent,
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
    output_reserve_tokens=100,
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
        input_tokens: int | list[int] = 1,
        max_context_tokens: int = 1000,
        max_output_tokens: int | None = None,
    ) -> None:
        self._responses = iter(responses)
        self._input_tokens = (
            iter(input_tokens) if isinstance(input_tokens, list) else input_tokens
        )
        self._max_context_tokens = max_context_tokens
        self._max_output_tokens = max_output_tokens
        self.counted_requests: list[LLMRequest] = []
        self.requests: list[LLMRequest] = []

    @property
    def max_context_tokens(self) -> int:
        return self._max_context_tokens

    @property
    def max_output_tokens(self) -> int | None:
        return self._max_output_tokens

    def count_input_tokens(self, request: LLMRequest) -> int:
        self.counted_requests.append(request)
        if isinstance(self._input_tokens, int):
            return self._input_tokens
        return next(self._input_tokens)

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
    def test_archive_cursor_advances_by_complete_context_units(self) -> None:
        call = ToolCall("call-old", "echo", {"text": "old"})
        session = Session("session-1")
        session.add_item("user", "old request")
        session.add_item("assistant", None, tool_calls=(call,))
        session.tool_started(call)
        provider = MockProvider(
            ["old turn summary", "new answer"],
            input_tokens=800,
        )
        agent = Agent(
            provider,
            session,
            "You are helpful.",
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
            tool_set(EchoTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("new request")

        self.assertEqual(session.archived_item_cursor, 2)
        self.assertEqual(
            [message.content for message in provider.requests[0].messages
             if message.role == "user"],
            ["old request"],
        )
        self.assertEqual(
            [message.content for message in provider.requests[1].messages
             if message.role == "user"],
            ["new request"],
        )
        self.assertTrue(
            provider.requests[1].system_prompt.endswith(
                "[Archived Context Summary]\nold turn summary"
            )
        )
        self.assertEqual(
            [message.role for message in session.items],
            ["user", "assistant", "user", "assistant"],
        )

    def test_repeated_archives_replace_summary_and_advance_baseline(self) -> None:
        session = Session(
            "session-1",
            items=[Message("user", "first"), Message("assistant", "one")],
        )
        provider = MockProvider(["first summary", "second summary"])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        session.add_item("user", "second")
        context.archive()
        session.add_item("assistant", "two")
        session.add_item("user", "third")
        context.archive()

        self.assertEqual(session.archived_item_cursor, 4)
        self.assertEqual(session.archived_summary, "second summary")
        self.assertTrue(
            provider.requests[1].system_prompt.endswith(
                "[Archived Context Summary]\nfirst summary"
            )
        )
        self.assertEqual(
            [message.content for message in provider.requests[1].messages
             if message.role == "user"],
            ["second"],
        )
        request = context.build_request()
        self.assertTrue(
            request.system_prompt.endswith(
                "[Archived Context Summary]\nsecond summary"
            )
        )
        self.assertNotIn("first summary", request.system_prompt)
        self.assertEqual(
            [message.content for message in request.messages
             if message.role == "user"],
            ["third"],
        )

    def test_projection_repairs_a_tool_chain_after_the_archive_cursor(self) -> None:
        call = ToolCall("call-old", "echo", {"text": "old"})
        session = Session(
            "session-1",
            items=[
                Message("user", "old request"),
                Message("assistant", None, tool_calls=(call,)),
                Message("tool", "old result", tool_call_id=call.id),
                Message("assistant", "old answer"),
                Message("user", "new request"),
            ],
            archived_summary="old summary",
            archived_item_cursor=2,
        )
        context = ContextManager(
            MockProvider([]),
            session,
            "You are helpful.",
            AGENT_CONFIG,
        )
        request = context.build_request()

        self.assertFalse(any(message.role == "tool" for message in request.messages))
        self.assertFalse(any(message.tool_calls for message in request.messages))
        self.assertEqual(
            [message.content for message in request.messages
             if message.role != "system"],
            ["old answer", "new request"],
        )

    def test_archive_keeps_a_multi_tool_media_unit_whole(self) -> None:
        calls = (
            ToolCall("call-a", "read", {}),
            ToolCall("call-b", "read", {}),
        )
        session = Session(
            "session-1",
            items=[
                Message("user", "goal"),
                Message("assistant", "preparing"),
                Message("assistant", None, tool_calls=calls),
                Message("tool", "B", tool_call_id="call-b"),
                Message("tool", "A", tool_call_id="call-a"),
                Message(
                    "user",
                    (ImagePart(path="result.png"),),
                    origin="tool_media",
                ),
            ],
        )
        provider = MockProvider(["summary"])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive()

        self.assertEqual(session.archived_item_cursor, 2)
        retained = [
            message for message in context.build_request().messages
            if message.role != "system"
        ]
        self.assertEqual(
            [message.role for message in retained],
            ["assistant", "tool", "tool", "user"],
        )
        self.assertEqual(retained[0].tool_calls, calls)
        self.assertEqual(
            [message.tool_call_id for message in retained[1:3]],
            ["call-a", "call-b"],
        )
        self.assertTrue(retained[3].is_tool_media)

    def test_archive_compresses_a_multi_tool_media_unit_whole(self) -> None:
        calls = (
            ToolCall("call-a", "read", {}),
            ToolCall("call-b", "read", {}),
        )
        session = Session(
            "session-1",
            items=[
                Message("user", "goal"),
                Message("assistant", None, tool_calls=calls),
                Message("tool", "B", tool_call_id="call-b"),
                Message("tool", "A", tool_call_id="call-a"),
                Message(
                    "user",
                    (ImagePart(path="result.png"),),
                    origin="tool_media",
                ),
                Message("assistant", "next"),
            ],
        )
        provider = MockProvider(["summary"])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive()

        self.assertEqual(session.archived_item_cursor, 5)
        archived = [
            message for message in provider.requests[0].messages
            if message.role != "system"
        ]
        self.assertEqual(
            [message.role for message in archived],
            ["user", "assistant", "tool", "tool", "user"],
        )
        self.assertEqual(archived[1].tool_calls, calls)
        self.assertEqual(
            [message.tool_call_id for message in archived[2:4]],
            ["call-a", "call-b"],
        )
        self.assertEqual(
            archived[4].content,
            "[Image attachment omitted from historical context]",
        )
        self.assertEqual(
            [message.content for message in context.build_request().messages
             if message.role != "system"],
            ["next"],
        )

    def test_new_turn_projects_around_an_incomplete_historical_tool_batch(self):
        call = ToolCall("call-old", "echo", {"text": "old"})
        session = Session("session-1")
        session.add_item("user", "old request")
        session.add_item("assistant", None, tool_calls=(call,))
        session.tool_started(call)
        provider = MockProvider([LLMResponse(content="new answer")])
        agent = Agent(
            provider,
            session,
            "You are helpful.",
            AGENT_CONFIG,
            tool_set(EchoTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("new request")

        messages = provider.requests[0].messages
        self.assertEqual(
            [message.content for message in messages if message.role == "user"],
            ["old request", "new request"],
        )
        self.assertFalse(any(message.tool_calls for message in messages))

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
        self.assertIsNone(provider.requests[0].max_generation_tokens)
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
        self.assertEqual(context.exception.output_reserve_tokens, 100)
        self.assertEqual(context.exception.max_input_tokens, 900)
        self.assertEqual(len(provider.counted_requests), 1)
        self.assertEqual(provider.requests, [])

    def test_rejects_output_reserve_not_smaller_than_context_limit(self) -> None:
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

    def test_provider_output_limit_is_not_sent_without_policy(self) -> None:
        provider = MockProvider(
            ["hello back"],
            input_tokens=200,
            max_context_tokens=1000,
            max_output_tokens=500,
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
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("hello")

        self.assertIsNone(provider.requests[0].max_generation_tokens)

    def test_generation_policy_is_not_reduced_by_remaining_context(self) -> None:
        provider = MockProvider(
            ["hello back"],
            input_tokens=850,
            max_context_tokens=1000,
            max_output_tokens=500,
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                max_generation_tokens=300,
                tools=ToolConfig(enabled=()),
            ),
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("hello")

        self.assertEqual(
            provider.requests[0].max_generation_tokens,
            300,
        )

    def test_provider_output_limit_caps_generation_policy(self) -> None:
        provider = MockProvider(
            ["hello back"],
            input_tokens=200,
            max_context_tokens=1000,
            max_output_tokens=40,
        )
        session = Session(session_id="session-1")
        config = AgentConfig(
            max_same_tool_calls=5,
            output_reserve_tokens=100,
            max_generation_tokens=50,
            tools=ToolConfig(enabled=()),
        )
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=config,
            tools=tool_set(session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE, session=session
            ),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("hello")

        self.assertEqual(
            provider.requests[0].max_generation_tokens,
            40,
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

    def test_keeps_recent_tool_chain_with_original_timestamps(
        self,
    ) -> None:
        tool_call = ToolCall(
            id="call-1",
            name="echo",
            arguments={"text": "hello"},
        )
        provider = MockProvider(
            [
                LLMResponse(
                    content="using echo",
                    reasoning="I need to inspect the requested input first.",
                    tool_calls=(tool_call,),
                ),
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
            [REQUEST_TIME, TOOL_CALL_TIME, TOOL_RESULT_TIME, RESPONSE_TIME],
        )
        self.assertEqual(
            [message.reasoning for message in history[1:5]],
            [None, "I need to inspect the requested input first.", None, None],
        )
        historical_timeline = history[0].content or ""
        self.assertIn("user", historical_timeline)
        self.assertIn("assistant", historical_timeline)
        self.assertIn("assistant step", historical_timeline)
        self.assertIn("tool result", historical_timeline)

    def test_recent_image_is_carried_into_next_request(self) -> None:
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
        self.assertEqual(
            historical.parts,
            (
                TextPart(text="look at this"),
                ImagePart(path=".nosis/attachments/a1.png"),
            ),
        )

    def test_compresses_completed_units_inside_the_active_turn(self) -> None:
        calls = tuple(
            ToolCall(f"call-{name}", "echo", {"text": name})
            for name in ("A", "B", "C")
        )
        provider = MockProvider(
            [
                LLMResponse(content=None, tool_calls=(calls[0],)),
                LLMResponse(content=None, tool_calls=(calls[1],)),
                LLMResponse(content=None, tool_calls=(calls[2],)),
                "active turn checkpoint",
                "done",
            ],
            input_tokens=[100, 100, 100, 800, 100],
        )
        session = Session(session_id="session-1")
        agent = Agent(
            provider=provider,
            session=session,
            system_prompt="You are helpful.",
            config=AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
            tools=tool_set(EchoTool(), session=session),
            context=ToolExecutionContext(
                workspace=TEST_WORKSPACE,
                session=session,
            ),
            now=clock(
                REQUEST_TIME,
                TOOL_CALL_TIME,
                TOOL_RESULT_TIME,
                datetime(2026, 9, 9, 8, 0, 20, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 8, 0, 21, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 8, 0, 30, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 8, 0, 31, tzinfo=timezone.utc),
                RESPONSE_TIME,
            ),
        )

        agent.run("finish the task")

        self.assertEqual(session.archived_item_cursor, 5)
        self.assertEqual(session.archived_summary, "active turn checkpoint")
        consolidation = provider.requests[3]
        self.assertEqual(
            [message.content for message in consolidation.messages
             if message.role == "tool"],
            [
                '{"ok": true, "output": {"text": "A"}}',
                '{"ok": true, "output": {"text": "B"}}',
            ],
        )
        resumed = provider.requests[4]
        self.assertTrue(
            resumed.system_prompt.endswith(
                "[Archived Context Summary]\nactive turn checkpoint"
            )
        )
        retained = [
            message for message in resumed.messages if message.role != "system"
        ]
        self.assertEqual(
            [message.role for message in retained],
            ["assistant", "tool"],
        )
        self.assertEqual(retained[0].tool_calls, (calls[2],))
        self.assertEqual(retained[1].tool_call_id, calls[2].id)

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

    def test_emits_completion_order_but_commits_invocation_order(self) -> None:
        controller = CompletionController(expected=3)
        tool = OrderedCompletionTool(controller)
        calls = tuple(
            ToolCall(
                id=f"call-{label}",
                name="ordered_completion",
                arguments={"label": label},
            )
            for label in ("a", "b", "c")
        )
        session = Session(session_id="session-1")
        provider = MockProvider(
            [
                LLMResponse(content=None, tool_calls=calls),
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

        events = []
        result_events = {
            label: threading.Event() for label in ("a", "b", "c")
        }
        errors = []

        def on_event(event) -> None:
            events.append(event)
            if isinstance(event, ToolResultEvent):
                result_events[event.tool_result.output["label"]].set()

        def run() -> None:
            try:
                agent.run("run three at once", on_event=on_event)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(controller.all_entered.wait(5))

        for label in ("b", "c", "a"):
            controller.release(label)
            self.assertTrue(result_events[label].wait(5))

        thread.join(5)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]

        self.assertEqual(
            [
                event.tool_call.id
                for event in events
                if isinstance(event, ToolCallEvent)
            ],
            ["call-a", "call-b", "call-c"],
        )
        self.assertEqual(
            [
                event.tool_result.tool_call_id
                for event in events
                if isinstance(event, ToolResultEvent)
            ],
            ["call-b", "call-c", "call-a"],
        )
        self.assertEqual(
            [
                message.tool_call_id
                for message in provider.requests[1].messages
                if message.role == "tool"
            ],
            ["call-a", "call-b", "call-c"],
        )
        self.assertEqual(
            [
                message.tool_call_id
                for message in session.items
                if message.role == "tool"
            ],
            ["call-b", "call-c", "call-a"],
        )

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

    def test_cancelled_parallel_batch_journals_other_completed_side_effects(self) -> None:
        barrier = threading.Barrier(2, timeout=5)

        class CancellingTool(EchoTool):
            name = "cancelling"
            concurrent = True

            def execute(self, arguments, context):
                barrier.wait()
                if arguments["text"] == "cancel":
                    raise KeyboardInterrupt()
                return {"text": arguments["text"]}

        calls = (
            ToolCall("call-cancel", "cancelling", {"text": "cancel"}),
            ToolCall("call-write", "cancelling", {"text": "side effect"}),
        )
        session = Session("session-1")
        agent = Agent(
            MockProvider([LLMResponse(None, tool_calls=calls)]),
            session,
            "You are helpful.",
            AGENT_CONFIG,
            tool_set(CancellingTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=lambda: REQUEST_TIME,
        )

        with self.assertRaises(KeyboardInterrupt):
            agent.run("run both")

        self.assertEqual(session.tool_executions["call-cancel"].status, "unknown")
        self.assertEqual(session.tool_executions["call-write"].status, "completed")
        self.assertIn("call-write", [item.tool_call_id for item in session.items])

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


class CompletionController:
    """Lets a test choose the exact order in which concurrent calls finish."""

    def __init__(self, expected: int) -> None:
        self._expected = expected
        self._entered = 0
        self._lock = threading.Lock()
        self._releases: dict[str, threading.Event] = {}
        self.all_entered = threading.Event()

    def complete(self, label: str):
        with self._lock:
            release = self._releases.setdefault(label, threading.Event())
            self._entered += 1
            if self._entered == self._expected:
                self.all_entered.set()
        if not release.wait(5):
            raise TimeoutError(f"call {label} was not released")
        return label

    def release(self, label: str) -> None:
        with self._lock:
            release = self._releases.setdefault(label, threading.Event())
        release.set()


class OrderedCompletionTool(Tool):
    name = "ordered_completion"
    concurrent = True

    def __init__(self, controller: CompletionController) -> None:
        self._controller = controller

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Complete when released by the test.",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        )

    def execute(self, arguments, context):
        return {"label": self._controller.complete(arguments["label"])}


if __name__ == "__main__":
    unittest.main()
