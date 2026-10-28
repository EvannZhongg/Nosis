import json
import threading
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

from agent_core.memory import MemoryContext, MemoryDocument
from agent_core.mcp.manager import McpServerInstructions
from agent_core.plan import PlanSnapshot, PlanStep
from agent_core.prompting import render_system_prompt

from agent_core import (
    AgentRunResult,
    Agent,
    AgentCancelled,
    AgentConfig,
    CacheUsage,
    TokenUsage,
    AssistantMessageDeltaEvent,
    AssistantMessageEvent,
    ModelCompletedEvent,
    UnitCompletedEvent,
    CompressionCompletedEvent,
    ContextManager,
    ContextCompressionConfig,
    ContextWindowEvent,
    ContextWindowExceededError,
    LLMRequest,
    LLMResponse,
    Message,
    ProviderProtocolError,
    ProviderRequestError,
    ProviderStreamError,
    PlanManager,
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
    TurnControl,
    UserSteerAppliedEvent,
    ImagePart,
    TextPart,
    ToolCatalog,
    ToolExecutionContext,
    UpdatePlanTool,
)
from agent_core.context_manager import (
    ARCHIVE_RETRY_COUNT,
    _validated_archive_summary,
)
from tests.agent_fixtures import (
    AGENT_CONFIG, CONSOLIDATOR_PROMPT, TEST_WORKSPACE, EchoTool, MockProvider, make_agent, tool_set,
)

REQUEST_TIME = datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc)
TOOL_CALL_TIME = datetime(2026, 9, 9, 8, 0, 10, tzinfo=timezone.utc)
TOOL_RESULT_TIME = datetime(2026, 9, 9, 8, 0, 11, tzinfo=timezone.utc)
RESPONSE_TIME = datetime(2026, 9, 9, 8, 1, tzinfo=timezone.utc)


class SteeringProvider(MockProvider):
    def __init__(self, responses, turn_control: TurnControl, steer_on_calls: dict[int, str]):
        super().__init__(responses)
        self._turn_control = turn_control
        self._steer_on_calls = steer_on_calls

    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        call_index = len(self.requests) + 1
        response = super().stream(request, on_text_delta, on_reasoning_delta)
        text = self._steer_on_calls.get(call_index)
        if text is not None:
            self._turn_control.steer(f"steer-{call_index}", text)
        return response


class ProtocolFailingProvider(MockProvider):
    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        raise ProviderProtocolError(
            "tool call index and id refer to different calls",
            details={
                "phase": "tool_call_assembly",
                "provider": "deepseek",
                "model": "deepseek/deepseek-flash",
                "reason": "conflicting_identity",
            },
        )


class StreamFailingProvider(MockProvider):
    def stream(self, request, on_text_delta, on_reasoning_delta=None):
        self.requests.append(request)
        raise ProviderStreamError(
            "provider stream ended before the response completed",
            details={
                "phase": "response_stream",
                "reason": "mid_stream_disconnect",
                "attempt_count": 3,
            },
        )


def clock(*values: datetime):
    times = iter(values)
    return lambda: next(times)


class AgentTest(unittest.TestCase):
    def test_invalid_plan_arguments_are_returned_and_corrected_in_the_same_turn(self):
        raw = '{"goal":"finish","steps":[{"id":"1","title":"work","status" "completed"}]}'
        invalid = ToolCall.from_raw_arguments("bad-plan", "update_plan", raw)
        valid = ToolCall("good-plan", "update_plan", {
            "goal": "finish",
            "steps": [{"id": "1", "title": "work", "status": "completed"}],
        })
        sibling = ToolCall("echo", "echo", {"text": "still runs"})
        provider = MockProvider([
            LLMResponse(None, tool_calls=(invalid, sibling)),
            LLMResponse(None, tool_calls=(valid,)),
            "done",
        ])
        session = Session("invalid-plan")
        context = ToolExecutionContext(
            workspace=TEST_WORKSPACE, session=session, plan=PlanManager(session),
        )
        tools = ToolCatalog((UpdatePlanTool(), EchoTool())).select(
            ("update_plan", "echo"), context,
        )
        agent = Agent(provider, session, "system", CONSOLIDATOR_PROMPT,
                      AGENT_CONFIG, tools, context)
        events = []

        result = agent.run("finish", on_event=events.append, turn_id="turn-1")

        self.assertEqual(result.content, "done")
        self.assertEqual(session.turns["turn-1"].status, "completed")
        self.assertEqual(session.plan.revision, 1)
        self.assertEqual(session.plan.steps[0].status, "completed")
        repair_request = provider.requests[1]
        failed_call = next(item for item in repair_request.messages if item.tool_calls)
        self.assertEqual(failed_call.tool_calls[0], invalid)
        failure = next(item for item in repair_request.messages if item.tool_call_id == invalid.id)
        self.assertEqual(json.loads(failure.content), {
            "ok": False,
            "error": {"type": "invalid_tool_arguments", "message": invalid.argument_error.message},
        })
        sibling_result = next(item for item in repair_request.messages if item.tool_call_id == sibling.id)
        self.assertEqual(json.loads(sibling_result.content)["output"], {"text": "still runs"})
        self.assertEqual(session.tool_executions[invalid.id].arguments, raw)
        self.assertEqual(session.tool_executions[invalid.id].status, "failed")
        self.assertEqual(session.tool_executions[valid.id].status, "completed")
        self.assertEqual([event.tool_result.error is not None for event in events
                          if isinstance(event, ToolResultEvent)], [True, False, False])

    def test_repeated_invalid_arguments_still_obey_the_tool_call_limit(self):
        provider = MockProvider([
            LLMResponse(None, tool_calls=(ToolCall.from_raw_arguments(str(i), "echo", '{"text"'),))
            for i in range(3)
        ])
        session = Session("invalid-repeat")
        agent = Agent(provider, session, "system", CONSOLIDATOR_PROMPT,
                      replace(AGENT_CONFIG, max_same_tool_calls=2),
                      tool_set(EchoTool(), session=session),
                      ToolExecutionContext(workspace=TEST_WORKSPACE, session=session))

        with self.assertRaises(ToolCallLimitExceededError):
            agent.run("work", turn_id="turn-1")

        self.assertEqual(len([item for item in session.items if item.role == "tool"]), 2)
        self.assertEqual(session.turns["turn-1"].status, "failed")

    def test_steering_during_compression_resets_tool_repetition(self) -> None:
        control = TurnControl()
        call = ToolCall("call", "echo", {"text": "again"})
        provider = SteeringProvider(
            [LLMResponse(content=None, tool_calls=(call,)),
             LLMResponse(content="- [ephemeral] Compression completed."),
             LLMResponse(content=None, tool_calls=(call,)),
             LLMResponse(content="done")],
            control, {2: "try once more"},
        )
        provider._input_tokens = iter([10, 4, 1, 800, 1, 1, 1])
        session = Session("compression-steer")
        agent = Agent(
            provider, session, "system", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, max_same_tool_calls=1,
                    context=ContextCompressionConfig(keep_recent_units=1)),
            tool_set(EchoTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
        )

        result = agent.run("work", turn_control=control)

        self.assertEqual(result.content, "done")
        self.assertEqual(session.compression_count, 1)
        self.assertEqual(len([item for item in session.items if item.role == "tool"]), 2)
        self.assertTrue(any(item.content == "try once more" for item in session.items))

    def test_large_context_default_compression_threshold_is_capped(self) -> None:
        provider = MockProvider([], max_context_tokens=1_048_576)
        context = ContextManager(
            provider,
            Session("session-1"),
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=8_192,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
            ),
        )

        window = context.window(0)

        self.assertEqual(window.max_input_tokens, 1_040_384)
        self.assertEqual(window.compression_threshold, 262_144)
        self.assertIsNone(window.compression_summary)

    def test_system_prompt_tokens_exclude_runtime_context_and_request_framing(self) -> None:
        prompt = render_system_prompt(
            "You are helpful. Workspace: {{workspace}}", TEST_WORKSPACE,
            mcp_instructions=(McpServerInstructions("docs", "Search the docs."),),
        )
        for system_prompt in ("", prompt):
            with self.subTest(system_prompt=system_prompt):
                provider = MockProvider([])
                session = Session("session-1")
                session.set_archived_summary("Keep the task.", 0)
                session.plan = PlanSnapshot(
                    "plan-1", "Finish the task", 1,
                    (PlanStep("step-1", "Work", "pending"),),
                )
                with patch.object(provider, "count_input_tokens", side_effect=lambda request:
                                  7 + len(request.system_prompt.split())) as count:
                    context = ContextManager(
                        provider, session, system_prompt, CONSOLIDATOR_PROMPT,
                        AGENT_CONFIG,
                        memory_context=MemoryContext(
                            MemoryDocument(facts=("A remembered fact.",)), MemoryDocument(),
                        ),
                    )
                    request = context.build_request(tool_set(EchoTool(), session=session).definitions)
                    self.assertTrue(request.messages)
                    self.assertTrue(request.tools)
                    self.assertEqual(context.window(120).system_prompt_tokens, len(system_prompt.split()))
                    session.set_archived_summary("A different summary.", 0)
                    self.assertEqual(context.window(240).system_prompt_tokens, len(system_prompt.split()))
                    self.assertEqual(count.call_count, 2)
                    self.assertEqual(count.call_args_list[0].args[0], LLMRequest(system_prompt, ()))
                    self.assertEqual(count.call_args_list[1].args[0], LLMRequest("", ()))

    def test_system_prompt_tokens_reach_initial_snapshot_and_turn_events(self) -> None:
        provider = MockProvider(["done"], input_tokens=[43, 7, 120, 160, 180])
        agent = make_agent(provider, Session("session-1"))
        self.assertEqual(agent.context_window().system_prompt_tokens, 36)
        events = []
        agent.run("hello", events.append)
        windows = [event.window for event in events if isinstance(event, ContextWindowEvent)]
        self.assertEqual([window.input_tokens for window in windows], [160, 180])
        self.assertEqual([window.system_prompt_tokens for window in windows], [36, 36])

    def test_context_window_includes_current_compression_summary(self) -> None:
        provider = MockProvider([], max_context_tokens=1_000)
        session = Session("session-1")
        session.set_archived_summary("Keep the active task and its constraints.", 0)
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
            ),
        )

        self.assertEqual(
            context.window(0).compression_summary,
            "Keep the active task and its constraints.",
        )

    def test_applies_steering_after_a_tool_batch_commits(self) -> None:
        control = TurnControl()
        call = ToolCall("call-1", "echo", {"text": "hello"})
        provider = SteeringProvider(
            [LLMResponse(content=None, tool_calls=(call,)), LLMResponse(content="done")],
            control,
            {1: "check tests first"},
        )
        session = Session("session-1")
        events = []
        agent = Agent(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
            tool_set(EchoTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=clock(REQUEST_TIME, TOOL_CALL_TIME, TOOL_RESULT_TIME, RESPONSE_TIME, RESPONSE_TIME),
        )

        agent.run("work", on_event=events.append, turn_control=control)

        self.assertEqual(
            [message.role for message in session.items],
            ["user", "assistant", "tool", "user", "assistant"],
        )
        self.assertEqual(session.items[3].content, "check tests first")
        self.assertEqual(
            [message.content for message in provider.requests[1].messages if message.role == "user"],
            ["work", "check tests first"],
        )
        self.assertEqual(
            [event.text for event in events if isinstance(event, UserSteerAppliedEvent)],
            ["check tests first"],
        )

    def test_steering_before_return_reopens_the_turn(self) -> None:
        control = TurnControl()
        provider = SteeringProvider(
            [
                LLMResponse(content="first answer"),
                LLMResponse(content="second answer"),
                LLMResponse(content="revised answer"),
            ],
            control,
            {1: "also explain why", 2: "keep it concise"},
        )
        session = Session("session-1")
        agent = Agent(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
            tool_set(session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=clock(
                REQUEST_TIME,
                RESPONSE_TIME,
                RESPONSE_TIME,
                RESPONSE_TIME,
                RESPONSE_TIME,
                RESPONSE_TIME,
            ),
        )

        result = agent.run("answer", turn_control=control)

        self.assertEqual(result.content, "revised answer")
        self.assertEqual(
            [message.content for message in session.items],
            [
                "answer",
                "first answer",
                "also explain why",
                "second answer",
                "keep it concise",
                "revised answer",
            ],
        )
        self.assertFalse(control.steer("late", "too late"))
        self.assertFalse(control.cancel())

    def test_archive_cursor_advances_by_complete_context_units(self) -> None:
        call = ToolCall("call-old", "echo", {"text": "old"})
        session = Session("session-1")
        session.add_item("user", "old request")
        session.add_item("assistant", None, tool_calls=(call,))
        session.tool_started(call)
        provider = MockProvider(
            ["- [ephemeral] Old turn summary.", "new answer"],
            input_tokens=800,
        )
        agent = Agent(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
            tool_set(EchoTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        agent.run("new request")

        self.assertEqual(session.archived_item_cursor, 2)
        compression_record = provider.requests[0].messages[0].content or ""
        self.assertIn("old request", compression_record)
        self.assertEqual(
            [message.content for message in provider.requests[1].messages
             if message.role == "user"],
            ["new request"],
        )
        self.assertEqual(provider.requests[1].system_prompt, "You are helpful.")
        self.assertIn(
            "[Archived Context Summary]\n- [ephemeral] Old turn summary.",
            provider.requests[1].messages[0].content,
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
        provider = MockProvider([
            "- [durable] First summary.",
            "- [durable] Second summary.",
        ])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        session.add_item("user", "second")
        context.archive(context.build_request())
        self.assertEqual(
            provider.requests[0].system_prompt,
            "You are helpful.",
        )
        session.add_item("assistant", "two")
        session.add_item("user", "third")
        context.archive(context.build_request())

        self.assertEqual(session.archived_item_cursor, 4)
        self.assertEqual(
            session.archived_summary,
            "- [durable] Second summary.",
        )
        self.assertEqual(session.compression_count, 2)
        self.assertEqual(provider.requests[1].system_prompt, "You are helpful.")
        self.assertIn(
            "[Archived Context Summary]\n- [durable] First summary.",
            provider.requests[1].messages[0].content,
        )
        self.assertIn(Message("user", "second"), provider.requests[1].messages[:-1])
        self.assertEqual(provider.requests[1].messages[-1].content, CONSOLIDATOR_PROMPT)
        request = context.build_request()
        self.assertEqual(request.system_prompt, "You are helpful.")
        self.assertIn(
            "[Archived Context Summary]\n- [durable] Second summary.",
            request.messages[0].content,
        )
        self.assertNotIn("First summary.", request.messages[0].content)
        self.assertEqual(
            [message.content for message in request.messages
             if message.role == "user"],
            ["third"],
        )

    def test_archive_preserves_the_original_request_prefix_and_tools(self) -> None:
        call = ToolCall("original-call", "echo", {"text": "result"})
        session = Session("prefix")
        session.begin_turn("past")
        session.add_item("user", "original constraint", REQUEST_TIME)
        session.add_item("assistant", "prior answer")
        session.finish_turn("completed")
        session.set_archived_summary("- [durable] Earlier checkpoint.", 2)
        session.begin_turn("current")
        session.add_item("user", "complete the task", TOOL_CALL_TIME)
        session.add_item(
            "assistant", None, tool_calls=(call,), reasoning="original reasoning",
            provider_data={"openai_responses": [{"type": "reasoning", "encrypted_content": "opaque"}]},
        )
        session.add_item("tool", "original result", tool_call_id=call.id)
        session.add_item("assistant", "retained answer")
        session.add_item("user", "retained user instruction", RESPONSE_TIME)
        session.plan = PlanSnapshot(
            "plan", "Retained dynamic plan", 1,
            (PlanStep("later", "Pending work", "pending"),),
        )
        provider = MockProvider(["- [ephemeral] Checkpoint."])
        system_prompt = render_system_prompt(
            "Original system", TEST_WORKSPACE,
            mcp_instructions=(McpServerInstructions(
                "demo", "MCP guide remains outside the checkpoint.",
            ),),
        )
        context = ContextManager(
            provider, session, system_prompt, CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=2)),
            media_root=TEST_WORKSPACE.path,
            memory_context=MemoryContext(
                MemoryDocument(facts=("Persistent memory fact.",)), MemoryDocument(),
            ),
        )
        tools = (
            ToolDefinition("echo", "Echo text", {"type": "object"}),
            ToolDefinition("other", "Another tool", {"type": "object"}),
        )
        original = context.build_request(tools)
        context.archive(original)
        compressed = provider.requests[0]
        self.assertEqual(compressed.system_prompt, original.system_prompt)
        self.assertEqual(context.build_request(tools).system_prompt, system_prompt)
        self.assertIn("MCP guide remains outside the checkpoint.", compressed.system_prompt)
        self.assertFalse(any(
            "MCP guide remains outside the checkpoint." in str(message.content)
            for message in compressed.messages
        ))
        self.assertIs(compressed.tools, original.tools)
        self.assertEqual(compressed.media_root, original.media_root)
        self.assertEqual(compressed.messages[:-1], original.messages[:6])
        self.assertTrue(all(a is b for a, b in zip(compressed.messages[:-1], original.messages)))
        self.assertEqual(compressed.messages[-1], Message("user", CONSOLIDATOR_PROMPT))
        self.assertIs(compressed.messages[-3], session.items[3])
        self.assertIs(compressed.messages[-2], session.items[4])
        self.assertIn("[Archived Context Summary]", compressed.messages[0].content)
        self.assertIn("Persistent memory fact.", compressed.messages[0].content)
        self.assertIn("[Lossless User Anchors]", compressed.messages[1].content)
        self.assertIn("[Current Plan]", original.messages[-1].content)
        self.assertNotIn("[Current Plan]", str(compressed.messages))
        self.assertNotIn("retained user instruction", str(compressed.messages))
        self.assertEqual(session.archived_item_cursor, 5)
        self.assertEqual(session.items[3].reasoning, "original reasoning")
        from agent_core.providers.base import _request_messages, _request_tools
        self.assertEqual(_request_messages(compressed)[:-1], _request_messages(original)[:7])
        self.assertEqual(_request_tools(compressed), _request_tools(original))

    def test_archive_does_not_cut_at_an_earlier_equal_message(self) -> None:
        session = Session(items=[
            Message("user", "goal"), Message("assistant", "same"),
            Message("assistant", "same"), Message("user", "retained"),
        ])
        provider = MockProvider(["- [ephemeral] Checkpoint."])
        context = ContextManager(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        context.archive(context.build_request())
        self.assertEqual(provider.requests[0].messages[:-1], tuple(session.items[:3]))
        self.assertEqual(session.archived_item_cursor, 3)

    def test_archive_checks_main_window_including_the_compression_instruction(self) -> None:
        session = Session(items=[Message("user", "old"), Message("assistant", "retained")])
        provider = MockProvider([], input_tokens=901)
        context = ContextManager(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        with self.assertRaises(ContextWindowExceededError):
            context.archive(context.build_request())
        self.assertEqual(provider.requests, [])
        self.assertEqual(session.archived_item_cursor, 0)

    def test_archive_rejects_a_missing_or_copied_boundary_without_mutating_history(self) -> None:
        for copied in (False, True):
            with self.subTest(copied=copied):
                session = Session(items=[
                    Message("user", "already archived"), Message("assistant", "old answer"),
                    Message("user", "next request"), Message("assistant", "next answer"),
                    Message("user", "retained"),
                ])
                session.set_archived_summary("- [durable] Existing checkpoint.", 2)
                provider = MockProvider([])
                context = ContextManager(
                    provider, session, "System", CONSOLIDATOR_PROMPT,
                    replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
                )
                request = context.build_request()
                invalid = replace(request, messages=(
                    tuple(replace(message) for message in request.messages) if copied else ()
                ))
                items, journal = tuple(session.items), tuple(session.journal)
                with self.assertRaisesRegex(ValueError, "archive boundary.*supplied request"):
                    context.archive(invalid)
                self.assertEqual(provider.requests, [])
                self.assertEqual(tuple(session.items), items)
                self.assertEqual(tuple(session.journal), journal)
                self.assertEqual(session.archived_summary, "- [durable] Existing checkpoint.")
                self.assertEqual(session.archived_item_cursor, 2)
                self.assertEqual(session.compression_count, 1)
                self.assertIsNone(session.cache_usage)

    def test_archive_checks_main_window_again_for_a_repair(self) -> None:
        session = Session(items=[Message("user", "old"), Message("assistant", "retained")])
        provider = MockProvider(["invalid checkpoint"], input_tokens=[10, 4, 899, 901])
        context = ContextManager(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        with self.assertRaises(ContextWindowExceededError):
            context.archive(context.build_request())
        self.assertEqual(len(provider.requests), 1)
        self.assertIsNone(session.archived_summary)
        self.assertEqual(session.archived_item_cursor, 0)

    def test_archive_does_nothing_without_archivable_units(self) -> None:
        session = Session(items=[Message("user", "retained")])
        provider = MockProvider([])
        context = ContextManager(provider, session, "System", CONSOLIDATOR_PROMPT, AGENT_CONFIG)
        context.archive(context.build_request())
        self.assertEqual(provider.requests, [])
        self.assertEqual(session.compression_count, 0)

    def test_compression_never_executes_returned_tool_calls(self) -> None:
        session = Session(items=[Message("user", "old"), Message("assistant", "answer")])
        provider = MockProvider([
            LLMResponse(content=None, tool_calls=(ToolCall("forbidden", "echo", {"text": "bad"}),)),
            "- [ephemeral] Checkpoint.", "done",
        ], input_tokens=800)
        tools = tool_set(EchoTool(), session=session)
        agent = Agent(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
            tools, ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
        )
        with patch.object(tools, "execute") as execute:
            result = agent.run("new request")
        execute.assert_not_called()
        self.assertEqual(result.content, "done")
        self.assertEqual(session.compression_count, 1)
        self.assertEqual(len(provider.requests[0].tools), 1)
        self.assertEqual(provider.requests[0].messages, provider.requests[1].messages[:-1])
        self.assertIn("must not contain tool calls", provider.requests[1].messages[-1].content)

    def test_empty_compression_error_is_recorded_as_null(self) -> None:
        class FailingProvider(MockProvider):
            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                raise RuntimeError()

        calls = []
        session = Session("compression-error")
        session.add_item("user", "old")
        session.add_item("assistant", "answer")
        context = ContextManager(
            FailingProvider([]), session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        with self.assertRaises(RuntimeError):
            context.archive(context.build_request(), on_call=calls.append)
        self.assertEqual(calls[0].status, "failed")
        self.assertIsNone(calls[0].error)

    def test_compression_reports_each_attempt_after_journaling_usage(self) -> None:
        session = Session(items=[Message("user", "old"), Message("assistant", "answer")])
        provider = MockProvider([
            LLMResponse(content="invalid checkpoint", usage=TokenUsage(800, 10, 810, 600, None)),
            LLMResponse(content="- [durable] Checkpoint.", usage=TokenUsage(800, 20, 820, 700, 10)),
            LLMResponse(content="done", usage=TokenUsage(200, 30, 230, 50, None)),
        ], input_tokens=800)
        events = []
        agent = Agent(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
            tool_set(session=session), ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
        )

        def on_event(event):
            if isinstance(event, CompressionCompletedEvent):
                self.assertEqual(session.journal[-1].event_type, "compression_completed")
                self.assertEqual(event.cache_usage, session.cache_usage)
            events.append(event)

        result = agent.run("new request", on_event)

        completions = [event for event in events if isinstance(event, CompressionCompletedEvent)]
        calls = [event.payload for event in session.journal if event.event_type == "compression_completed"]
        self.assertEqual([(call["attempt"], call["status"]) for call in calls],
                         [(1, "invalid_response"), (2, "completed")])
        self.assertEqual([event.cache_usage for event in completions],
                         [CacheUsage(800, 600), CacheUsage(1600, 1300)])
        self.assertEqual(session.cache_usage, CacheUsage(1800, 1350))
        self.assertEqual([event.model_call_index for event in events if isinstance(event, ModelCompletedEvent)], [1])
        self.assertEqual(result.usage, TokenUsage(200, 30, 230, 50, None))
        self.assertEqual(session.compression_count, 1)

    def test_exhausted_compression_still_reports_and_retains_billed_usage(self) -> None:
        session = Session(items=[Message("user", "old"), Message("assistant", "answer")])
        provider = MockProvider([
            LLMResponse(content="invalid checkpoint", usage=TokenUsage(800, 10, 810, 600, None)),
        ] * (ARCHIVE_RETRY_COUNT + 1), input_tokens=800)
        events = []
        agent = Agent(
            provider, session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
            tool_set(session=session), ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
        )
        with self.assertRaisesRegex(ValueError, "invalid checkpoint after"):
            agent.run("new request", events.append)
        completions = [event for event in events if isinstance(event, CompressionCompletedEvent)]
        self.assertEqual(len(completions), ARCHIVE_RETRY_COUNT + 1)
        self.assertEqual(completions[-1].cache_usage, CacheUsage(4800, 3600))
        self.assertEqual(session.cache_usage, CacheUsage(4800, 3600))
        self.assertEqual(session.compression_count, 0)
        self.assertEqual(session.journal[-1].event_type, "turn_failed")

    def test_compression_does_not_retry_provider_validation_errors(self) -> None:
        class FailingProvider(MockProvider):
            def stream(self, request, on_text_delta, on_reasoning_delta=None):
                raise ValueError("invalid provider configuration")

        calls = []
        session = Session("compression-provider-error")
        session.add_item("user", "old")
        session.add_item("assistant", "answer")
        context = ContextManager(
            FailingProvider([]), session, "System", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        with self.assertRaisesRegex(ValueError, "invalid provider configuration"):
            context.archive(context.build_request(), on_call=calls.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].status, "failed")
        self.assertEqual(session.archived_item_cursor, 0)

    def test_archive_retries_invalid_shape_before_committing(self) -> None:
        session = Session(
            "session-1",
            items=[Message("user", "first"), Message("assistant", "one")],
        )
        provider = MockProvider([
            "I should produce a checkpoint.",
            (
                "- [permanent] Keep the user's stable preference.\n"
                "- [durable] Preserve the project decision.\n"
                "- [ephemeral] Continue the active task.\n"
                "- [correction] Use the latest corrected value."
            ),
        ])
        auxiliary_calls = []
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )
        session.add_item("user", "second")

        context.archive(context.build_request(), on_call=auxiliary_calls.append)

        self.assertEqual(len(provider.requests), 2)
        self.assertEqual(
            [(call.task, call.attempt, call.status) for call in auxiliary_calls],
            [("compression", 1, "invalid_response"), ("compression", 2, "completed")],
        )
        self.assertEqual(
            provider.requests[0].messages,
            provider.requests[1].messages[:-1],
        )
        self.assertEqual(
            provider.requests[0].system_prompt,
            "You are helpful.",
        )
        self.assertIn(
            "The previous checkpoint was rejected:",
            provider.requests[1].messages[-1].content,
        )
        self.assertIn(
            "checkpoint lines must use '- [mark] fact'",
            provider.requests[1].messages[-1].content,
        )
        self.assertEqual(
            session.archived_summary,
            (
                "- [permanent] Keep the user's stable preference.\n"
                "- [durable] Preserve the project decision.\n"
                "- [ephemeral] Continue the active task.\n"
                "- [correction] Use the latest corrected value."
            ),
        )
        self.assertEqual(session.archived_item_cursor, 2)
        self.assertEqual(session.compression_count, 1)

    def test_archive_exhausts_shape_retries_without_committing(self) -> None:
        session = Session(
            "session-1",
            items=[Message("user", "first"), Message("assistant", "one")],
        )
        provider = MockProvider(
            ["- [unknown] Invalid mark."] * (ARCHIVE_RETRY_COUNT + 1)
        )
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )
        session.add_item("user", "second")

        with self.assertRaisesRegex(
            ValueError,
            rf"invalid checkpoint after {ARCHIVE_RETRY_COUNT + 1} attempts",
        ):
            context.archive(context.build_request())

        self.assertEqual(len(provider.requests), ARCHIVE_RETRY_COUNT + 1)
        self.assertIsNone(session.archived_summary)
        self.assertEqual(session.archived_item_cursor, 0)
        self.assertEqual(session.compression_count, 0)

    def test_archive_validation_allows_only_harmless_outer_formatting(self) -> None:
        self.assertEqual(
            _validated_archive_summary(LLMResponse(
                content=(
                    " \n```markdown\n\n"
                    " - [durable] Keep the project decision. \n\n"
                    " - [ephemeral] Continue the active task. \n"
                    "``` \n"
                ),
            )),
            (
                "- [durable] Keep the project decision.\n"
                "- [ephemeral] Continue the active task."
            ),
        )
        self.assertEqual(
            _validated_archive_summary(LLMResponse(
                content="```text\n(nothing)\n```",
            )),
            "(nothing)",
        )
        with self.assertRaisesRegex(ValueError, "checkpoint lines"):
            _validated_archive_summary(LLMResponse(
                content="Summary:\n- [durable] Keep the project decision.",
            ))

    def test_archive_accepts_nothing_checkpoint(self) -> None:
        session = Session(
            "session-1",
            items=[Message("user", "first"), Message("assistant", "one")],
        )
        provider = MockProvider(["(nothing)"])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )
        session.add_item("user", "second")

        context.archive(context.build_request())

        self.assertEqual(session.archived_summary, "(nothing)")
        self.assertEqual(len(provider.requests), 1)

    def test_archive_preserves_reasoning_in_original_messages(self) -> None:
        session = Session(
            "session-1",
            items=[
                Message("user", "first"),
                Message(
                    "assistant",
                    "answer",
                    reasoning="private reasoning",
                ),
                Message("user", "second"),
            ],
        )
        provider = MockProvider(["- [ephemeral] Summary."])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive(context.build_request())

        archived = provider.requests[0].messages
        self.assertEqual(archived[:-1], tuple(session.items[:2]))
        self.assertIs(archived[1], session.items[1])
        self.assertEqual(archived[1].reasoning, "private reasoning")
        self.assertEqual(archived[-1], Message("user", CONSOLIDATOR_PROMPT))

    def test_context_keeps_current_and_previous_turn_user_anchors(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.add_item("user", "first command", REQUEST_TIME)
        session.add_item("assistant", "first answer", RESPONSE_TIME)
        session.record_user_interaction(
            "Question: Which target?\nUser answer: api",
            "question_response",
        )
        session.finish_turn("completed")
        session.begin_turn("turn-2")
        session.add_item("user", "second command", REQUEST_TIME)
        session.add_item("assistant", "working", TOOL_CALL_TIME)
        session.add_item("user", "keep it concise", TOOL_RESULT_TIME)
        session.set_archived_summary("lossy state", 5)
        context = ContextManager(
            MockProvider([]),
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
        )

        request = context.build_request()

        stable_context = request.messages[0].content or ""
        self.assertIn("[Archived Context Summary]\nlossy state", stable_context)
        anchors = request.messages[1].content or ""
        self.assertIn("[Lossless User Anchors]", anchors)
        self.assertIn("historical turn, user_input, turn_id=turn-1", anchors)
        self.assertIn("first command", anchors)
        self.assertIn("Which target?", anchors)
        self.assertIn("User answer: api", anchors)
        self.assertNotIn("current turn", anchors)
        self.assertEqual(
            [
                message.content for message in request.messages
                if message.role == "user"
                and message.content in {"second command", "keep it concise"}
            ],
            ["second command", "keep it concise"],
        )

    def test_context_places_mid_tool_interaction_after_the_tool_result(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.add_item("user", "deploy it")
        call = ToolCall("call-1", "ask_user", {})
        session.add_item("assistant", None, tool_calls=(call,))
        session.record_user_interaction(
            "Question: Which target?\nUser answer: api",
            "question_response",
        )
        session.add_item("tool", '{"answer":"api"}', tool_call_id=call.id)
        context = ContextManager(
            MockProvider([]),
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
        )

        request = context.build_request()

        self.assertEqual(
            [(message.role, message.origin) for message in request.messages],
            [
                ("user", "conversation"),
                ("assistant", "conversation"),
                ("tool", "conversation"),
                ("user", "user_interaction"),
            ],
        )

    def test_context_does_not_duplicate_user_anchor_still_in_recent_tail(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.add_item("user", "first command")
        session.add_item("assistant", "first answer")
        session.finish_turn("completed")
        session.begin_turn("turn-2")
        session.add_item("user", "second command")
        session.set_archived_summary("lossy state", 2)
        context = ContextManager(
            MockProvider([]),
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
        )

        request = context.build_request()

        anchors = next(
            message.content or "" for message in request.messages
            if message.role == "user"
            and isinstance(message.content, str)
            and "[Lossless User Anchors]" in message.content
        )
        self.assertIn("first command", anchors)
        self.assertNotIn("second command", anchors)
        self.assertEqual(
            [message.content for message in request.messages
             if message.role == "user" and message.content == "second command"],
            ["second command"],
        )

    def test_context_stops_extending_user_anchors_at_the_token_budget(self) -> None:
        session = Session("session-1")
        for turn_id, command in (
            ("turn-1", "old command"),
            ("turn-2", "previous command"),
        ):
            session.begin_turn(turn_id)
            session.add_item("user", command)
            session.add_item("assistant", "done")
            session.finish_turn("completed")
        session.begin_turn("turn-3")
        session.add_item("user", "current command")
        session.set_archived_summary("lossy state", len(session.items))
        context = ContextManager(
            MockProvider([], input_tokens=2001),
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
        )

        request = context.build_request()

        rendered = "\n".join(
            message.content for message in request.messages
            if isinstance(message.content, str)
        )
        self.assertNotIn("old command", rendered)
        anchors = next(
            message.content or "" for message in request.messages
            if message.role == "user"
            and isinstance(message.content, str)
            and "[Lossless User Anchors]" in message.content
        )
        self.assertIn("previous command", anchors)
        self.assertNotIn("current command", anchors)
        self.assertEqual(
            [message.content for message in request.messages
             if message.role == "user" and message.content == "current command"],
            ["current command"],
        )

    def test_stable_context_and_pinned_user_remain_a_cache_prefix(self) -> None:
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.add_item("user", "finish the migration", REQUEST_TIME)
        session.set_archived_summary("- [ephemeral] Migration is active.", 1)
        context = ContextManager(
            MockProvider([]),
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AGENT_CONFIG,
        )

        first = context.build_request()
        session.add_item("assistant", "working", TOOL_CALL_TIME)
        second = context.build_request()

        self.assertEqual(first.messages, second.messages[:len(first.messages)])
        self.assertIn("[Archived Context Summary]", first.messages[0].content)
        self.assertEqual(first.messages[-1].role, "user")
        self.assertEqual(first.messages[-1].content, "finish the migration")
        self.assertIs(first.messages[-1], session.items[0])

    def test_repeated_active_turn_archives_reinject_the_user_command(self) -> None:
        first_call = ToolCall("call-1", "echo", {"text": "first"})
        second_call = ToolCall("call-2", "echo", {"text": "second"})
        session = Session("session-1")
        session.begin_turn("turn-1")
        session.add_item("user", "complete the migration")
        session.add_item("assistant", None, tool_calls=(first_call,))
        session.add_item("tool", "first result", tool_call_id=first_call.id)
        session.add_item("assistant", "continuing")
        provider = MockProvider([
            "- [ephemeral] First checkpoint.",
            "- [ephemeral] Second checkpoint.",
        ])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive(context.build_request())
        session.add_item("assistant", None, tool_calls=(second_call,))
        session.add_item("tool", "second result", tool_call_id=second_call.id)
        session.add_item("assistant", "almost done")
        context.archive(context.build_request())

        second_messages = provider.requests[1].messages
        self.assertIs(second_messages[1], session.items[0])
        self.assertEqual(sum(message is session.items[0] for message in second_messages), 1)
        self.assertIn(Message("tool", "second result", tool_call_id=second_call.id), second_messages)
        self.assertEqual(second_messages[-1], Message("user", CONSOLIDATOR_PROMPT))

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
            CONSOLIDATOR_PROMPT,
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
        provider = MockProvider(["- [ephemeral] Summary."])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive(context.build_request())

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
                Message(
                    "assistant",
                    None,
                    timestamp_utc=TOOL_CALL_TIME,
                    tool_calls=calls,
                    reasoning="private tool planning",
                ),
                Message(
                    "tool",
                    "B",
                    timestamp_utc=datetime(
                        2026, 9, 9, 8, 0, 12, tzinfo=timezone.utc
                    ),
                    tool_call_id="call-b",
                ),
                Message(
                    "tool",
                    "A",
                    timestamp_utc=TOOL_RESULT_TIME,
                    tool_call_id="call-a",
                ),
                Message(
                    "user",
                    (ImagePart(path="result.png"),),
                    origin="tool_media",
                ),
                Message("assistant", "next"),
            ],
        )
        provider = MockProvider(["- [ephemeral] Summary."])
        context = ContextManager(
            provider,
            session,
            "You are helpful.",
            CONSOLIDATOR_PROMPT,
            AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
        )

        context.archive(context.build_request())

        self.assertEqual(session.archived_item_cursor, 5)
        archived = provider.requests[0].messages
        self.assertEqual([message.role for message in archived], ["user", "assistant", "tool", "tool", "user", "user"])
        self.assertIs(archived[0], session.items[0])
        self.assertIs(archived[1], session.items[1])
        self.assertEqual(archived[1].tool_calls, calls)
        self.assertEqual(archived[1].timestamp_utc, TOOL_CALL_TIME)
        self.assertEqual(archived[1].reasoning, "private tool planning")
        self.assertIs(archived[2], session.items[3])
        self.assertIs(archived[3], session.items[2])
        self.assertIs(archived[4], session.items[4])
        self.assertEqual(archived[-1], Message("user", CONSOLIDATOR_PROMPT))
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
            CONSOLIDATOR_PROMPT,
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
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(REQUEST_TIME, RESPONSE_TIME),
        )

        result = agent.run("hello")

        self.assertEqual(result.content, "hello back")
        request_messages = provider.requests[0].messages
        self.assertNotIn("[Turn Context]", provider.requests[0].system_prompt)
        self.assertIsNone(provider.requests[0].max_generation_tokens)
        self.assertEqual(request_messages[0].role, "system")
        self.assertEqual(
            request_messages[0].content,
            "[Turn Context]\n"
            "User message received at "
            f"{REQUEST_TIME.astimezone().isoformat(timespec='seconds')}.",
        )
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

    def test_persists_structured_provider_error_with_model_call_index(self) -> None:
        provider = ProtocolFailingProvider([])
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(REQUEST_TIME),
        )

        with self.assertRaises(ProviderProtocolError):
            agent.run("hello", turn_id="turn-1")

        error = session.turns["turn-1"].error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.type, "ProviderProtocolError")
        self.assertEqual(error.details["phase"], "tool_call_assembly")
        self.assertEqual(error.details["model_call_index"], 1)

    def test_persists_http_error_with_model_call_index(self) -> None:
        provider = MockProvider([])
        session = Session(session_id="session-1")
        agent = make_agent(provider=provider, session=session, now=clock(REQUEST_TIME))
        failure = ProviderRequestError(
            "chat_completions request failed: HTTP 400: Unsupported image",
            details={"phase": "provider_request", "reason": "http_error", "status_code": 400},
        )
        with patch.object(provider, "stream", side_effect=failure):
            with self.assertRaises(ProviderRequestError):
                agent.run("hello", turn_id="turn-1")
        error = session.turns["turn-1"].error
        self.assertEqual(error.type, "ProviderRequestError")
        self.assertIn("Unsupported image", error.message)
        self.assertEqual(error.details["status_code"], 400)
        self.assertEqual(error.details["model_call_index"], 1)
        self.assertEqual(session.journal[-1].payload["error"]["details"], error.details)

    def test_persists_stream_error_with_model_call_index(self) -> None:
        provider = StreamFailingProvider([])
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(REQUEST_TIME),
        )

        with self.assertRaises(ProviderStreamError):
            agent.run("hello", turn_id="turn-1")

        error = session.turns["turn-1"].error
        self.assertIsNotNone(error)
        assert error is not None
        self.assertEqual(error.type, "ProviderStreamError")
        self.assertEqual(error.details["phase"], "response_stream")
        self.assertEqual(error.details["attempt_count"], 3)
        self.assertEqual(error.details["model_call_index"], 1)

    def test_rejects_context_before_calling_provider(self) -> None:
        provider = MockProvider(["unused"], input_tokens=901)
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(REQUEST_TIME),
        )

        with self.assertRaises(ContextWindowExceededError) as context:
            agent.run("hello")

        self.assertEqual(context.exception.input_tokens, 901)
        self.assertEqual(context.exception.max_context_tokens, 1000)
        self.assertEqual(context.exception.output_reserve_tokens, 100)
        self.assertEqual(context.exception.max_input_tokens, 900)
        self.assertEqual(len(provider.counted_requests), 3)
        self.assertEqual(provider.requests, [])

    def test_rejects_output_reserve_not_smaller_than_context_limit(self) -> None:
        provider = MockProvider(["unused"], max_context_tokens=100)
        session = Session(session_id="session-1")

        with self.assertRaises(ValueError):
            make_agent(
                provider=provider,
                session=session,
            )

    def test_provider_output_limit_is_not_sent_without_policy(self) -> None:
        provider = MockProvider(
            ["hello back"],
            input_tokens=200,
            max_context_tokens=1000,
            max_output_tokens=500,
        )
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
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
        agent = make_agent(
            provider=provider,
            session=session,
            config=AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                max_generation_tokens=300,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
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
            workspace_instruction_files=(),
        )
        agent = make_agent(
            provider=provider,
            session=session,
            config=config,
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
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(
                datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 8, 1, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 9, 9, 1, tzinfo=timezone.utc),
            ),
        )

        agent.run("first question")
        result = agent.run("second question")

        self.assertEqual(result.content, "second answer")
        self.assertNotIn("[Turn Context]", provider.requests[1].system_prompt)
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
        self.assertEqual(
            history[0].content,
            "[Turn Context]\n"
            "User message received at "
            f"{datetime(2026, 9, 9, 8, 0, tzinfo=timezone.utc).astimezone().isoformat(timespec='seconds')}.",
        )
        self.assertEqual(
            history[3].content,
            "[Turn Context]\n"
            "User message received at "
            f"{datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc).astimezone().isoformat(timespec='seconds')}.",
        )
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
                    provider_data={"native_state": "tool-round"},
                    usage=TokenUsage(1000, 20, 1020, 800, 100),
                ),
                LLMResponse(content="tool completed", provider_data={"native_state": "final"},
                            usage=TokenUsage(1100, 30, 1130, 1000, None)),
            ], max_context_tokens=4000,
        )
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
            now=clock(
                REQUEST_TIME,
                TOOL_CALL_TIME,
                TOOL_RESULT_TIME,
                RESPONSE_TIME,
            ),
            tools=(EchoTool(),),
        )
        events = []

        result = agent.run("use the echo tool", on_event=events.append)

        self.assertEqual(
            result,
            AgentRunResult("tool completed", TokenUsage(1100, 30, 1130, 1000, None)),
        )
        completed = [event for event in events if isinstance(event, ModelCompletedEvent)]
        self.assertEqual(
            [event for event in events if isinstance(event, UnitCompletedEvent)],
            [UnitCompletedEvent(1, 3), UnitCompletedEvent(3, 4)],
        )
        self.assertEqual(completed, [
            ModelCompletedEvent(1, TokenUsage(1000, 20, 1020, 800, 100), CacheUsage(1000, 800)),
            ModelCompletedEvent(2, TokenUsage(1100, 30, 1130, 1000, None), CacheUsage(2100, 1800)),
        ])
        self.assertLess(events.index(completed[0]), next(
            index for index, event in enumerate(events) if isinstance(event, ToolBatchStartedEvent)
        ))
        calls = [event for event in session.journal if event.event_type == "model_completed"]
        self.assertEqual([{key: value for key, value in event.payload.items() if key != "input_token_anchor"}
                          for event in calls], [
            {"model_call_index": 1, "has_tool_calls": True, "usage": {
                "input_tokens": 1000, "output_tokens": 20, "total_tokens": 1020,
                "cache_read_tokens": 800, "cache_write_tokens": 100,
            }},
            {"model_call_index": 2, "has_tool_calls": False, "usage": {
                "input_tokens": 1100, "output_tokens": 30, "total_tokens": 1130,
                "cache_read_tokens": 1000, "cache_write_tokens": None,
            }},
        ])
        self.assertEqual(len(provider.requests), 2)
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
            second_request_messages[:len(provider.requests[0].messages)],
            provider.requests[0].messages,
        )
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
                provider_data={"native_state": "tool-round"},
            ),
        )
        self.assertEqual(second_request_messages[3].tool_call_id, "call-1")
        self.assertEqual(
            second_request_messages[2].reasoning,
            "I need to inspect the requested input first.",
        )
        self.assertEqual(
            second_request_messages[0].content,
            "[Turn Context]\n"
            "User message received at "
            f"{REQUEST_TIME.astimezone().isoformat(timespec='seconds')}.",
        )
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
                    provider_data={"native_state": "tool-round"},
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
                    provider_data={"native_state": "final"},
                ),
            ],
        )
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
            if not isinstance(
                event,
                (AssistantMessageDeltaEvent, ContextWindowEvent, ModelCompletedEvent, UnitCompletedEvent),
            )
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

    def test_keeps_recent_tool_chain_with_fixed_turn_context(
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(EchoTool(),),
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
        self.assertEqual(
            history[0].content,
            "[Turn Context]\n"
            "User message received at "
            f"{REQUEST_TIME.astimezone().isoformat(timespec='seconds')}.",
        )
        self.assertNotIn("assistant", history[0].content or "")
        self.assertNotIn("tool", history[0].content or "")

    def test_recent_image_is_carried_into_next_request(self) -> None:
        session = Session(session_id="images")
        session.add_item(
            "user",
            "look at this",
            REQUEST_TIME,
            attachments=(
                ImagePart(
                    path=".nosis/attachments/a1.png",
                    mime_type="image/png",
                    filename="a1.png",
                    size_bytes=123,
                ),
            ),
        )
        provider = MockProvider(["ok"])
        agent = make_agent(
            provider=provider,
            session=session,
            system_prompt="Be helpful.",
        )
        agent.run("follow up")

        self.assertEqual(provider.requests[0].media_root, TEST_WORKSPACE.path)
        historical = provider.requests[0].messages[1]
        self.assertEqual(historical.role, "user")
        self.assertEqual(
            historical.parts,
            (
                TextPart(text="look at this"),
                ImagePart(
                    path=".nosis/attachments/a1.png",
                    mime_type="image/png",
                    filename="a1.png",
                    size_bytes=123,
                ),
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
                "- [ephemeral] Active turn checkpoint.",
                "done",
            ],
            input_tokens=[10, 4, 100, 100, 100, 800, 100, 100, 120],
        )
        session = Session(session_id="session-1")
        agent = make_agent(
            provider=provider,
            session=session,
            config=AgentConfig(
                max_same_tool_calls=5,
                output_reserve_tokens=100,
                tools=ToolConfig(enabled=()),
                workspace_instruction_files=(),
                context=ContextCompressionConfig(keep_recent_units=1),
            ),
            tools=(EchoTool(),),
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
        self.assertEqual(
            session.archived_summary,
            "- [ephemeral] Active turn checkpoint.",
        )
        consolidation = provider.requests[3]
        self.assertEqual(
            [message.role for message in consolidation.messages],
            ["system", "user", "assistant", "tool", "assistant", "tool", "user"],
        )
        self.assertEqual(consolidation.messages[1].content, "finish the task")
        self.assertEqual(consolidation.messages[2].tool_calls, (calls[0],))
        self.assertEqual(consolidation.messages[4].tool_calls, (calls[1],))
        self.assertEqual(consolidation.messages[3].content, '{"ok": true, "output": {"text": "A"}}')
        self.assertEqual(consolidation.messages[5].content, '{"ok": true, "output": {"text": "B"}}')
        self.assertEqual(consolidation.messages[-1], Message("user", CONSOLIDATOR_PROMPT))
        resumed = provider.requests[4]
        self.assertEqual(resumed.system_prompt, "You are helpful.")
        self.assertIn(
            "[Archived Context Summary]\n- [ephemeral] Active turn checkpoint.",
            resumed.messages[0].content,
        )
        current_user = [
            message for message in resumed.messages
            if message.role == "user" and message.content == "finish the task"
        ]
        self.assertEqual(len(current_user), 1)
        self.assertFalse(any(
            message.role == "user"
            and isinstance(message.content, str)
            and "[Lossless User Anchors]" in message.content
            and "finish the task" in message.content
            for message in resumed.messages
        ))
        retained = [
            message for message in resumed.messages
            if message.role not in {"system", "user"}
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(EchoTool(),),
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(EchoTool(),),
            now=lambda: REQUEST_TIME,
        )

        result = agent.run("repeat echo with different arguments in between")

        self.assertEqual(result.content, "done")
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(EchoTool(),),
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(tool,),
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
        agent = make_agent(
            provider=provider,
            session=session,
            tools=(blocking, EchoTool()),
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
            CONSOLIDATOR_PROMPT,
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

class CooperativeCancellationTest(unittest.TestCase):
    def test_cancel_after_one_tool_prevents_the_next_side_effect(self) -> None:
        control = TurnControl()
        executed = []

        class CancellingTool(EchoTool):
            def execute(self, arguments, context):
                executed.append(arguments["text"])
                control.cancel()
                return arguments["text"]

        calls = tuple(ToolCall(name, "echo", {"text": name}) for name in ("first", "second"))
        session = Session()
        agent = Agent(
            MockProvider([LLMResponse(None, tool_calls=calls)]), session,
            "system", CONSOLIDATOR_PROMPT, AGENT_CONFIG,
            tool_set(CancellingTool(), session=session),
            ToolExecutionContext(workspace=TEST_WORKSPACE, session=session),
        )
        with self.assertRaises(AgentCancelled):
            agent.run("run both", turn_control=control)
        self.assertEqual(executed, ["first"])
        self.assertEqual(session.tool_executions["first"].status, "completed")
        self.assertEqual(session.tool_executions["second"].status, "cancelled")
        self.assertIn("first", [item.tool_call_id for item in session.items])

    def test_cancel_during_approval_prevents_tool_execution(self) -> None:
        control = TurnControl()
        executed = []

        class Policy:
            def authorize(self, call, context):
                control.cancel()

        class RecordingTool(EchoTool):
            def execute(self, arguments, context):
                executed.append(arguments)
                return "done"

        tools = tool_set(RecordingTool(), policy=Policy())
        with self.assertRaises(AgentCancelled):
            tools.execute(ToolCall("call", "echo", {"text": "work"}), turn_control=control)
        self.assertEqual(executed, [])

    def test_compression_observes_cancellation_before_committing_a_summary(self) -> None:
        control = TurnControl()
        auxiliary_calls = []

        class CancellingProvider(MockProvider):
            def stream(self, *args):
                control.cancel()
                return LLMResponse("- [ephemeral] Summary.")

        session = Session()
        session.add_item("user", "old question")
        session.add_item("assistant", "old answer")
        context = ContextManager(
            CancellingProvider([]), session, "system", CONSOLIDATOR_PROMPT,
            replace(AGENT_CONFIG, context=ContextCompressionConfig(keep_recent_units=1)),
        )
        with self.assertRaises(AgentCancelled):
            context.archive(context.build_request(), control.raise_if_cancelled, auxiliary_calls.append)
        self.assertEqual(session.archived_item_cursor, 0)
        self.assertIsNone(session.archived_summary)
        self.assertEqual(
            [(call.task, call.status) for call in auxiliary_calls],
            [("compression", "cancelled")],
        )


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
