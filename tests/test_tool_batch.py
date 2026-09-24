import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core import (
    Session,
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
