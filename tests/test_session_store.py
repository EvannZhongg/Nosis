import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_core import JsonlSessionStore, PermissionPreset, Session, ToolCall
from agent_core.session_paths import session_log_path


def persist(store: JsonlSessionStore, workspace: Path, session: Session) -> None:
    store.bind_workspace(session.session_id, workspace)
    store.append_events(session.session_id, session.journal, workspace=workspace)


class JsonlSessionStoreTest(unittest.TestCase):
    def test_appends_incremental_events_without_request_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("session-1")
            session.begin_turn("turn-1")
            session.add_item("user", "你好")
            session.add_item("assistant", "你好！")
            session.finish_turn("completed")
            persist(store, workspace, session)

            path = session_log_path(store.directory, workspace, session.session_id)
            records = [
                json.loads(line)
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([record["seq"] for record in records], [1, 2, 3, 4])
            self.assertTrue(all(record["event_id"] for record in records))
            self.assertTrue(
                all(
                    "request" not in record and "response" not in record
                    for record in records
                )
            )
            self.assertEqual(
                [item.content for item in store.load("session-1").items],
                ["你好", "你好！"],
            )

    def test_lists_sessions_by_workspace_and_first_user_message(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            for session_id, text in (("one", "first"), ("two", "second")):
                session = Session(session_id)
                session.begin_turn("t")
                session.add_item("user", text)
                session.finish_turn("completed")
                persist(store, workspace, session)

            self.assertEqual(
                {item["title"] for item in store.list_sessions()[0]["sessions"]},
                {"first", "second"},
            )

    def test_lists_sessions_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            for session_id in ("older", "newer"):
                session = Session(session_id)
                session.begin_turn("t")
                session.add_item("user", session_id)
                session.finish_turn("completed")
                persist(store, workspace, session)

            older_path = session_log_path(store.directory, workspace, "older")
            newer_path = session_log_path(store.directory, workspace, "newer")
            os.utime(older_path, (100, 100))
            os.utime(newer_path, (200, 200))

            listed = store.list_sessions()[0]["sessions"]
            self.assertEqual(
                [item["session_id"] for item in listed], ["newer", "older"]
            )

    def test_ignores_session_directories_without_a_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            store.bind_workspace("orphan", workspace)
            orphan = next(store.directory.iterdir()) / "orphan"

            self.assertEqual(store.list_sessions(), [])

    def test_stores_permission_preset_in_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            store.bind_workspace("session-1", workspace)

            store.set_permission_preset(
                "session-1", PermissionPreset.FULL_ACCESS, workspace
            )
            session = Session("session-1")
            session.set_permission_preset(PermissionPreset.FULL_ACCESS)
            store.append_events("session-1", session.journal, workspace=workspace)

            metadata = json.loads(
                (
                    session_log_path(store.directory, workspace, "session-1").parent
                    / "session.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["session_id"], "session-1")
            self.assertEqual(metadata["workspace"], str(workspace.resolve()))
            self.assertEqual(metadata["permission_preset"], "full_access")
            self.assertEqual(
                JsonlSessionStore(store.directory).load("session-1").permission_preset,
                PermissionPreset.FULL_ACCESS,
            )

    def test_moves_session_to_new_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first"
            second = root / "second"
            first.mkdir()
            second.mkdir()
            store = JsonlSessionStore(root / "sessions")
            session = Session("session-1")
            session.begin_turn("t")
            session.add_item("user", "hello")
            session.finish_turn("completed")
            persist(store, first, session)
            store.bind_workspace("session-1", second)

            self.assertEqual(store.workspace_for("session-1"), str(second.resolve()))
            self.assertEqual(store.load("session-1").items[0].content, "hello")

    def test_replay_restores_tool_and_turn_states(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("session-1")
            session.begin_turn("turn-1")
            call = ToolCall("call-1", "read", {"path": "README.md"})
            session.add_item("assistant", None, tool_calls=(call,))
            session.tool_started(call)
            session.tool_finished(call, "completed")
            session.add_item("tool", "ok", tool_call_id=call.id)
            session.finish_turn("completed")
            persist(store, workspace, session)

            loaded = store.load("session-1")
            self.assertEqual(loaded.turns["turn-1"].status, "completed")
            self.assertEqual(loaded.tool_executions["call-1"].status, "completed")

    def test_load_missing_and_reject_escape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = JsonlSessionStore(Path(directory) / "sessions")
            self.assertEqual(store.load("missing").items, [])
            with self.assertRaises(ValueError):
                store.load("../escape")

    def test_delete_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("session-1")
            session.begin_turn("t")
            session.finish_turn("completed")
            persist(store, workspace, session)

            self.assertTrue(store.delete_session("session-1"))
            self.assertFalse(store.delete_session("session-1"))


if __name__ == "__main__":
    unittest.main()
