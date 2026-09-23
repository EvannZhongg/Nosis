from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from agent_core import (
    FilePart,
    ImagePart,
    JsonlSessionStore,
    Message,
    PermissionPreset,
    RuntimeErrorInfo,
    Session,
    ToolCall,
)
from agent_core.projection import project_context_units
from agent_core.content import historical_content


class SessionJournalTest(unittest.TestCase):
    def test_file_attachment_is_journaled_and_replayed(self):
        session = Session("s")
        attachment = FilePart(
            path=".nosis/attachments/report.pdf",
            filename="report.pdf",
            mime_type="application/pdf",
            size_bytes=123,
        )
        session.begin_turn("turn-1")
        session.add_item("user", "summarize", attachments=(attachment,))

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)

        self.assertEqual(replayed.items[0].parts[1], attachment)
        self.assertEqual(
            session.journal[-1].payload["message"]["content"][1],
            {
                "type": "file",
                "path": ".nosis/attachments/report.pdf",
                "filename": "report.pdf",
                "mime_type": "application/pdf",
                "size_bytes": 123,
            },
        )
        self.assertEqual(
            historical_content(replayed.items[0].content),
            "summarize\nAttached files:\n"
            "- report.pdf (.nosis/attachments/report.pdf, application/pdf, "
            "123 bytes)",
        )

    def test_turn_failure_is_structured_and_replayed(self):
        session = Session("s")
        session.begin_turn("turn-1")
        error = RuntimeErrorInfo(
            type="ProviderProtocolError",
            message="invalid streamed arguments",
            details={"phase": "tool_call_assembly", "model_call_index": 2},
        )

        session.finish_turn("failed", error=error)

        self.assertEqual(
            session.journal[-1].payload,
            {
                "status": "failed",
                "error": {
                    "type": "ProviderProtocolError",
                    "message": "invalid streamed arguments",
                    "details": {
                        "phase": "tool_call_assembly",
                        "model_call_index": 2,
                    },
                },
            },
        )
        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)
        self.assertEqual(replayed.turns["turn-1"].error, error)

    def test_permission_preset_is_journaled_and_replayed(self):
        session = Session("s")

        session.set_permission_preset(PermissionPreset.FULL_ACCESS)

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)
        self.assertEqual(replayed.permission_preset, PermissionPreset.FULL_ACCESS)
        self.assertEqual(session.journal[-1].event_type, "permission_preset_changed")
        self.assertEqual(session.journal[-1].payload, {"preset": "full_access"})

    def test_context_unit_keeps_parallel_tool_results_and_media_together(self):
        calls = (
            ToolCall("call-a", "read", {}),
            ToolCall("call-b", "read", {}),
        )
        items = [
            Message("assistant", None, tool_calls=calls),
            Message("tool", "B", tool_call_id="call-b"),
            Message("tool", "A", tool_call_id="call-a"),
            Message(
                "user",
                (ImagePart(path="result.png"),),
                origin="tool_media",
            ),
            Message("assistant", "next"),
        ]

        units = project_context_units(items, start_index=10)

        self.assertEqual([(unit.start, unit.end) for unit in units], [(10, 14), (14, 15)])
        self.assertEqual(
            [message.tool_call_id for message in units[0].messages
             if message.role == "tool"],
            ["call-a", "call-b"],
        )
        self.assertTrue(units[0].messages[-1].is_tool_media)

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
        # An unanswered call exposes no provider messages, but stays a unit.
        units = project_context_units(session.items)
        self.assertEqual([message.role for unit in units for message in unit.messages], ["user"])
        self.assertEqual(session.tool_executions["call-1"].status, "started")

    def test_journal_replays_a_cancelled_turn(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            store.bind_workspace("s", root)
            session = Session("s")
            session.attach_journal_sink(lambda events: store.append_events("s", events, workspace=root))
            session.begin_turn("turn-1")
            session.add_item("user", "hello")
            session.finish_turn("cancelled", "turn-1")

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
            units = project_context_units(replayed.items)
            projected = [message for unit in units for message in unit.messages]
            self.assertEqual(
                [item.tool_call_id for item in projected if item.role == "tool"],
                ["call-a", "call-b"],
            )

    def test_context_archive_replays_the_raw_item_cursor(self):
        session = Session("s")
        session.add_item("user", "first")
        session.add_item("assistant", "answer")
        session.set_archived_summary("summary", 2)

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)

        self.assertEqual(replayed.archived_summary, "summary")
        self.assertEqual(replayed.archived_item_cursor, 2)
        self.assertEqual(replayed.compression_count, 1)
        self.assertEqual(
            session.journal[-1].payload,
            {"summary": "summary", "item_cursor": 2},
        )

    def test_replay_restores_user_anchors_with_turn_membership(self):
        session = Session("s")
        session.begin_turn("turn-1")
        session.add_item("user", "first command")
        session.record_user_interaction(
            "Question: Which target?\nUser answer: api",
            "question_response",
        )
        session.finish_turn("completed")
        session.begin_turn("turn-2")
        session.add_item("user", "second command")

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)

        self.assertEqual(
            [anchor.content for anchor in replayed.user_anchors],
            [
                "first command",
                "Question: Which target?\nUser answer: api",
                "second command",
            ],
        )

    def test_steering_replays_with_its_anchor_source(self):
        session = Session("s")
        session.begin_turn("turn-1")
        session.add_item(
            "user",
            "check tests first",
            user_source="steering",
        )

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)

        self.assertEqual(replayed.user_anchors[0].source, "steering")
        self.assertEqual(replayed.user_anchors[0].content, "check tests first")

    def test_job_result_origin_survives_without_becoming_a_user_anchor(self):
        session = Session("s")
        session.begin_turn("turn-1")
        session.add_item("user", "run it")
        session.add_item(
            "user",
            "[Background job result]",
            origin="job_result",
        )

        replayed = Session("s")
        for event in session.journal:
            replayed.journal.append(event)
            replayed.apply_event(event)

        self.assertEqual(replayed.items[-1].origin, "job_result")
        self.assertEqual(
            [anchor.content for anchor in replayed.user_anchors], ["run it"]
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
