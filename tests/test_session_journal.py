import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_core import JsonlSessionStore, Message, Session, ToolCall


class SessionJournalTest(unittest.TestCase):
    def test_reused_client_turn_id_does_not_replace_previous_turn(self):
        session = Session("s")
        first = session.begin_turn("turn-1")
        session.finish_turn("completed", first)
        second = session.begin_turn("turn-1")
        session.finish_turn("completed", second)

        self.assertEqual(first, "turn-1")
        self.assertNotEqual(second, first)
        self.assertEqual(len(session.turns), 2)
        self.assertEqual(session.turns[first].status, "completed")
        self.assertEqual(session.turns[second].status, "completed")

    def test_projection_does_not_delete_an_unanswered_tool_call(self):
        session = Session("s")
        session.begin_turn("turn-1")
        session.add_item("user", "run it")
        call = ToolCall("call-1", "danger", {})
        session.add_item("assistant", None, tool_calls=(call,))
        session.tool_started(call)

        self.assertEqual([item.role for item in session.items], ["user", "assistant"])
        self.assertEqual([item.role for item in session.provider_messages()], ["user"])
        self.assertEqual(session.tool_executions["call-1"].status, "started")

    def test_journal_replays_tool_fact_without_request_snapshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.bind_workspace("s", root)
            session = Session("s")
            session.attach_journal_sink(lambda events: store.append_events("s", events, workspace=root))
            session.begin_turn("turn-1")
            session.add_item("user", "hello")
            session.finish_turn("cancelled", "turn-1")

            path = next((root / "sessions").rglob("s.jsonl"))
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all("request" not in record and "response" not in record for record in records))
            loaded = store.load("s")
            self.assertEqual(loaded.turns["turn-1"].status, "cancelled")
            self.assertEqual([event.seq for event in loaded.journal], [1, 2, 3])

    def test_recovery_marks_started_work_unknown_without_replay(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.bind_workspace("s", root)
            session = Session("s")
            session.begin_turn("turn-1")
            call = ToolCall("call-1", "write", {})
            session.tool_started(call)
            store.append_events("s", session.journal, workspace=root)

            resumed = store.load("s", recover=False)
            resumed.attach_journal_sink(
                lambda events: store.append_events("s", events, workspace=root)
            )
            recovered = resumed.recover()

            replayed = store.load("s", recover=False)
            self.assertEqual(replayed.turns["turn-1"].status, "unknown")
            self.assertEqual(replayed.tool_executions["call-1"].status, "unknown")
            self.assertEqual(
                {event.event_type for event in recovered},
                {"turn_recovered", "tool_recovered"},
            )

    def test_completed_tool_survives_later_batch_cancellation(self):
        session = Session("s")
        session.begin_turn("turn-1")
        first = ToolCall("call-1", "write", {"path": "a"})
        second = ToolCall("call-2", "write", {"path": "b"})
        session.add_item("assistant", None, tool_calls=(first, second))
        session.tool_started(first)
        session.tool_started(second)
        session.tool_finished(first, "completed")
        session.add_item("tool", '{"ok":true}', tool_call_id=first.id)

        session.cancel_active_work("turn-1")

        self.assertEqual(session.tool_executions[first.id].status, "completed")
        self.assertEqual(session.tool_executions[second.id].status, "cancelled")
        self.assertEqual(session.turns["turn-1"].status, "cancelled")
        self.assertIn("tool_completed", [event.event_type for event in session.journal])

    def test_parallel_completion_replays_in_call_order_for_provider(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.bind_workspace("s", root)
            session = Session("s")
            session.attach_journal_sink(lambda events: store.append_events("s", events, workspace=root))
            session.begin_turn("turn-1")
            calls = (ToolCall("call-a", "read", {}), ToolCall("call-b", "read", {}))
            session.add_item("assistant", None, tool_calls=calls)
            for call in calls:
                session.tool_started(call)
            for call in reversed(calls):
                session.tool_finished(call, "completed")
                session.add_item("tool", call.id, tool_call_id=call.id)
            session.finish_turn("completed")

            replayed = store.load("s")
            projected = replayed.provider_messages()
            self.assertEqual(
                [item.tool_call_id for item in projected if item.role == "tool"],
                ["call-a", "call-b"],
            )

    def test_replay_ignores_only_a_truncated_final_record(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.bind_workspace("s", root)
            session = Session("s")
            session.begin_turn("turn-1")
            session.add_item("user", "durable")
            store.append_events("s", session.journal, workspace=root)
            path = next((root / "sessions").rglob("s.jsonl"))
            with path.open("a", encoding="utf-8") as file:
                file.write('{"seq":3')

            loaded = store.load("s")

            self.assertEqual([item.content for item in loaded.items], ["durable"])


if __name__ == "__main__":
    unittest.main()
