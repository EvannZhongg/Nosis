import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from agent_core import (
    Session,
    SearchFilesTool,
    Tool,
    ToolBatchExecutor,
    ToolCall,
    ToolCatalog,
    ToolDefinition,
    ToolExecutionContext,
    ToolOutput,
    Workspace,
)


NOW = datetime(2026, 9, 24, 8, 0, tzinfo=timezone.utc)


class CleanupTool(Tool):
    name = "cleanup"

    def __init__(self) -> None:
        self.cleaned: set[str] = set()

    def definition(self, context) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description="Return a result with a cleanup hook.",
            parameters={
                "type": "object",
                "properties": {"label": {"type": "string"}},
                "required": ["label"],
            },
        )

    def execute(self, arguments, context):
        label = arguments["label"]
        return ToolOutput(
            output={"label": label},
            artifact_cleanup=lambda: self.cleaned.add(label),
        )


class RecordingNormalizer:
    def __init__(self) -> None:
        self.calls = []

    def normalize(self, result):
        self.calls.append(result.tool_call_id)
        return f"normalized:{result.tool_call_id}"


class FailingNormalizer:
    def normalize(self, result):
        raise RuntimeError("normalization failed")


class ToolBatchExecutorTest(unittest.TestCase):
    def test_search_timeout_is_journaled_and_returned_as_a_tool_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Session("search-timeout")
            turn_id = session.begin_turn("turn")
            context = ToolExecutionContext(workspace=Workspace(Path(directory)), session=session)
            tool = SearchFilesTool()
            tools = ToolCatalog((tool,)).select((tool.name,), context)
            call = ToolCall("search", tool.name, {"path": ".", "pattern": "x"})

            class Normalizer:
                def normalize(self, result):
                    return result.to_content()

            executor = ToolBatchExecutor(tools, session, Normalizer(), now=lambda: NOW)
            with patch(
                "agent_core.tools.builtin.search_files.time.monotonic",
                side_effect=[0.0, 0.0, 60.0],
            ):
                executor.execute((call,), turn_id=turn_id)

            self.assertEqual(session.tool_executions[call.id].status, "failed")
            events = [event for event in session.journal if event.tool_call_id == call.id]
            self.assertEqual(
                [event.event_type for event in events],
                ["tool_started", "tool_failed", "message_appended"],
            )
            content = json.loads(session.items[-1].content)
            self.assertFalse(content["ok"])
            self.assertEqual(content["error"]["type"], "TimeoutError")
            self.assertIn("60 seconds", content["error"]["message"])

    def test_invalid_arguments_do_not_block_valid_siblings_in_either_execution_mode(self):
        for concurrent in (False, True):
            with self.subTest(concurrent=concurrent):
                session = Session("mixed-batch")
                turn_id = session.begin_turn("turn")
                context = ToolExecutionContext(workspace=Workspace(Path(__file__).parent), session=session)
                tool = CleanupTool()
                tool.concurrent = concurrent
                tools = ToolCatalog((tool,)).select((tool.name,), context)
                invalid = ToolCall.from_raw_arguments("bad", "cleanup", '{"label" "bad"}')
                valid = ToolCall("good", "cleanup", {"label": "good"})
                normalizer = RecordingNormalizer()
                executor = ToolBatchExecutor(tools, session, normalizer, now=lambda: NOW)
                results = []

                executor.execute((invalid, valid), turn_id=turn_id,
                                 on_result=lambda result, _index, _count: results.append(result))

                self.assertEqual(tool.cleaned, {"good"})
                by_id = {result.tool_call_id: result for result in results}
                self.assertEqual(by_id["bad"].error.type, "invalid_tool_arguments")
                self.assertIsNone(by_id["good"].error)
                self.assertEqual(session.tool_executions["bad"].status, "failed")
                self.assertEqual(session.tool_executions["good"].status, "completed")

    def setUp(self) -> None:
        self.session = Session("session-1")
        self.turn_id = self.session.begin_turn("turn-1")
        context = ToolExecutionContext(
            workspace=Workspace(Path(__file__).parent),
            session=self.session,
        )
        self.tool = CleanupTool()
        self.tools = ToolCatalog((self.tool,)).select(
            (self.tool.name,), context
        )

    def test_owns_journal_normalization_callbacks_and_cleanup(self) -> None:
        normalizer = RecordingNormalizer()
        executor = ToolBatchExecutor(
            self.tools,
            self.session,
            normalizer,
            now=lambda: NOW,
        )
        calls = (
            ToolCall("call-a", "cleanup", {"label": "a"}),
            ToolCall("call-b", "cleanup", {"label": "b"}),
        )
        events = []

        executor.execute(
            calls,
            turn_id=self.turn_id,
            on_call=lambda call, index, count: events.append(
                ("call", call.id, index, count)
            ),
            on_result=lambda result, index, count: events.append(
                ("result", result.tool_call_id, index, count)
            ),
        )

        self.assertEqual(normalizer.calls, ["call-a", "call-b"])
        self.assertEqual(
            [(item.tool_call_id, item.content) for item in self.session.items],
            [
                ("call-a", "normalized:call-a"),
                ("call-b", "normalized:call-b"),
            ],
        )
        self.assertEqual(
            events,
            [
                ("call", "call-a", 1, 2),
                ("result", "call-a", 1, 2),
                ("call", "call-b", 2, 2),
                ("result", "call-b", 2, 2),
            ],
        )
        self.assertEqual(self.tool.cleaned, {"a", "b"})
        self.assertEqual(
            {
                execution.status
                for execution in self.session.tool_executions.values()
            },
            {"completed"},
        )

    def test_cleans_result_when_normalization_fails(self) -> None:
        executor = ToolBatchExecutor(
            self.tools,
            self.session,
            FailingNormalizer(),
            now=lambda: NOW,
        )
        call = ToolCall("call-a", "cleanup", {"label": "a"})

        with self.assertRaisesRegex(RuntimeError, "normalization failed"):
            executor.execute((call,), turn_id=self.turn_id)

        self.assertIn("a", self.tool.cleaned)
        self.assertEqual(
            self.session.tool_executions["call-a"].status,
            "completed",
        )
        self.assertEqual(self.session.items, [])


if __name__ == "__main__":
    unittest.main()
