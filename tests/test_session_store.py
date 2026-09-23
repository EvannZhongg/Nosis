import json
import os
import tempfile
import unittest
from pathlib import Path

from agent_core import FilePart, ImagePart, JsonlSessionStore, PermissionPreset, Session, ToolCall
from agent_core.session_paths import session_log_path, workspace_directory


def persist(store: JsonlSessionStore, workspace: Path, session: Session) -> None:
    store.bind_workspace(session.session_id, workspace)
    store.append_events(session.session_id, session.journal, workspace=workspace)


class JsonlSessionStoreTest(unittest.TestCase):
    def test_appends_and_replays_journal_events(self) -> None:
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

    def test_lists_session_with_attachments_by_first_user_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("attachment-session")
            session.begin_turn("t")
            session.add_item(
                "user",
                "分析这张图片",
                attachments=(
                    ImagePart(
                        path=".nosis/attachments/image.png",
                        filename="image.png",
                        size_bytes=12,
                    ),
                ),
            )
            session.finish_turn("completed")
            persist(store, workspace, session)

            self.assertEqual(
                store.list_sessions()[0]["sessions"],
                [{"session_id": "attachment-session", "title": "分析这张图片"}],
            )

    def test_names_attachment_only_sessions_from_their_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            single = Session("single-attachment")
            single.begin_turn("t")
            single.add_item(
                "user",
                "",
                attachments=(
                    ImagePart(
                        path=".nosis/attachments/design.png",
                        filename="design.png",
                        size_bytes=12,
                    ),
                ),
            )
            single.finish_turn("completed")
            persist(store, workspace, single)

            multiple = Session("multiple-attachments")
            multiple.begin_turn("t")
            multiple.add_item(
                "user",
                "",
                attachments=(
                    FilePart(
                        path=".nosis/attachments/brief.pdf",
                        filename="brief.pdf",
                        mime_type="application/pdf",
                        size_bytes=24,
                    ),
                    ImagePart(
                        path=".nosis/attachments/reference.png",
                        filename="reference.png",
                        size_bytes=12,
                    ),
                ),
            )
            multiple.finish_turn("completed")
            persist(store, workspace, multiple)

            titles = {
                item["session_id"]: item["title"]
                for item in store.list_sessions()[0]["sessions"]
            }
            self.assertEqual(titles["single-attachment"], "design.png")
            self.assertEqual(
                titles["multiple-attachments"],
                "brief.pdf 等 2 个附件",
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

    def test_lists_only_the_sessions_of_one_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = JsonlSessionStore(root / "sessions")
            for workspace, session_id in (
                (root / "here", "mine"),
                (root / "elsewhere", "theirs"),
            ):
                session = Session(session_id)
                session.begin_turn("t")
                session.add_item("user", session_id)
                session.finish_turn("completed")
                persist(store, workspace, session)

            self.assertEqual(
                store.list_workspace_sessions(root / "here"),
                [{"session_id": "mine", "title": "mine"}],
            )
            self.assertEqual(store.list_workspace_sessions(root / "empty"), [])

    def test_ignores_session_directories_without_a_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            orphan = workspace_directory(store.directory, workspace) / "orphan"
            orphan.mkdir(parents=True)

            self.assertEqual(store.list_sessions(), [])
            self.assertIsNone(store.workspace_for("orphan"))

    def test_workspace_binding_exists_before_the_first_journal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "sessions")

            store.bind_workspace("session-1", workspace)

            self.assertEqual(
                store.workspace_for("session-1"),
                str(workspace.resolve()),
            )
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
            self.assertEqual(metadata, {"permission_preset": "full_access"})
            self.assertEqual(
                JsonlSessionStore(store.directory).load("session-1").permission_preset,
                PermissionPreset.FULL_ACCESS,
            )

    def test_session_without_metadata_uses_its_group_workspace_and_default_permission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "项目 space"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "sessions")
            session = Session("session-1")
            session.add_item("user", "hello")
            store.append_events(
                session.session_id,
                session.journal,
                workspace=workspace,
            )

            loaded = JsonlSessionStore(store.directory).load("session-1")

            self.assertEqual(loaded.workspace, str(workspace.resolve()))
            self.assertEqual(
                loaded.permission_preset,
                PermissionPreset.ASK_FOR_APPROVAL,
            )
            self.assertEqual(
                store.list_sessions()[0]["workspace"],
                str(workspace.resolve()),
            )

    def test_stores_provider_in_session_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "sessions")

            store.set_provider("session-1", "second", workspace)

            reopened = JsonlSessionStore(store.directory)
            self.assertEqual(reopened.provider_for("session-1"), "second")
            self.assertEqual(reopened.list_sessions(), [])

    def test_permission_metadata_can_exist_before_the_first_journal_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "sessions")

            store.set_permission_preset(
                "session-1", PermissionPreset.FULL_ACCESS, workspace
            )

            reopened = JsonlSessionStore(store.directory)
            self.assertEqual(
                reopened.workspace_for("session-1"),
                str(workspace.resolve()),
            )
            self.assertEqual(
                reopened.permission_preset_for("session-1"),
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(reopened.list_sessions(), [])

    def test_permission_journal_alone_is_not_listed_as_a_conversation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            store = JsonlSessionStore(Path(directory) / "sessions")
            session = Session("session-1")
            session.set_permission_preset(PermissionPreset.FULL_ACCESS)
            persist(store, workspace, session)
            store.set_permission_preset(
                session.session_id,
                PermissionPreset.FULL_ACCESS,
                workspace,
            )

            self.assertEqual(store.list_sessions(), [])
            self.assertEqual(store.list_workspace_sessions(workspace), [])
            self.assertEqual(
                store.load("session-1").permission_preset,
                PermissionPreset.FULL_ACCESS,
            )

    def test_ungrouped_store_reports_no_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "subagents"
            store = JsonlSessionStore(root, group_by_workspace=False)
            session = Session("child-1")
            session.add_item("user", "hello")
            store.append_events(session.session_id, session.journal)

            reopened = JsonlSessionStore(root, group_by_workspace=False)
            self.assertIsNone(reopened.workspace_for("child-1"))
            self.assertIsNone(reopened.load("child-1").workspace)
            self.assertEqual(
                [item.content for item in reopened.load("child-1").items],
                ["hello"],
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

            session_directory = session_log_path(
                store.directory, workspace, "session-1"
            ).parent
            workspace_sessions_directory = session_directory.parent
            self.assertTrue(store.delete_session("session-1"))
            self.assertFalse(session_directory.exists())
            self.assertTrue(workspace_sessions_directory.is_dir())
            self.assertFalse(store.delete_session("session-1"))


if __name__ == "__main__":
    unittest.main()
