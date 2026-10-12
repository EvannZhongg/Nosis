import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_core import (
    FilePart,
    ImagePart,
    JsonlSessionStore,
    HostCommandExecutor,
    PermissionPreset,
    PlanSnapshot,
    PlanStep,
    Session,
    ToolCall,
)
from agent_core.session_paths import session_log_path, workspace_directory


def persist(store: JsonlSessionStore, workspace: Path, session: Session) -> None:
    store.bind_workspace(session.session_id, workspace)
    store.append_events(session.session_id, session.journal, workspace=workspace)


class JsonlSessionStoreTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows PowerShell journal protection")
    def test_recursive_powershell_cleanup_cannot_erase_audit_chain(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("active")
            with store.open_journal(session.session_id, workspace) as sink:
                session.attach_journal_sink(sink)
                session.add_item("user", "before cleanup")
                result = HostCommandExecutor(workspace).execute(
                    "Remove-Item -LiteralPath './sessions' -Recurse -Force -ErrorAction Stop",
                    timeout_seconds=10,
                )
                self.assertNotEqual(result.exit_code, 0)
                self.assertFalse(result.timed_out)
                session.add_item("assistant", "cleanup failed")
                self.assertEqual(
                    [item.content for item in store.load("active").items],
                    ["before cleanup", "cleanup failed"],
                )
            session.attach_journal_sink(None)

    def test_open_journal_persists_and_releases_writer_on_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("active")
            with self.assertRaisesRegex(RuntimeError, "turn failed"):
                with store.open_journal(session.session_id, workspace) as sink:
                    session.attach_journal_sink(sink)
                    session.add_item("user", "before failure")
                    self.assertEqual(store.load("active").items[0].content, "before failure")
                    raise RuntimeError("turn failed")
            session.attach_journal_sink(None)
            with store.open_journal(session.session_id, workspace) as sink:
                session.attach_journal_sink(sink)
                session.add_item("assistant", "after failure")
            session.attach_journal_sink(None)
            self.assertEqual(len(store.load("active").items), 2)
            self.assertTrue(store.delete_session("active"))

    @unittest.skipUnless(os.name == "nt", "Windows file sharing test")
    def test_active_journal_survives_same_user_child_mutations(self) -> None:
        mutations = (
            "target.unlink()",
            "target.rename(target.with_suffix('.moved'))",
            "target.write_text('overwritten')",
            "replacement = target.with_suffix('.replacement'); "
            "replacement.write_text('replacement'); replacement.replace(target)",
            "shutil.rmtree(target.parent)",
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                workspace = Path(directory)
                store = JsonlSessionStore(workspace / "sessions")
                session = Session("active")
                path = session_log_path(store.directory, workspace, session.session_id)
                with store.open_journal(session.session_id, workspace) as sink:
                    session.attach_journal_sink(sink)
                    session.add_item("user", "audit before command")
                    before = path.read_bytes()
                    result = subprocess.run(
                        [sys.executable, "-c",
                         "import shutil, sys; from pathlib import Path; "
                         "target = Path(sys.argv[1]); " + mutation, str(path)],
                        capture_output=True, text=True, timeout=15,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("PermissionError", result.stderr)
                    self.assertEqual(path.read_bytes(), before)
                    session.add_item("assistant", "audit after command")
                    self.assertEqual(len(store.load("active").items), 2)
                session.attach_journal_sink(None)
                self.assertTrue(store.delete_session("active"))

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

    def test_forks_the_journal_before_the_edited_turn(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("source")
            session.begin_turn("turn-1")
            session.add_item("user", "first question")
            call = ToolCall("call-1", "read", {"path": "README.md"})
            session.add_item("assistant", None, tool_calls=(call,))
            session.tool_started(call)
            session.tool_finished(call, "completed")
            session.add_item("tool", "first result", tool_call_id=call.id)
            session.finish_turn("completed")
            session.set_archived_summary("summary before edit", 3)
            plan = PlanSnapshot(
                "plan-1",
                "finish",
                1,
                (PlanStep("step-1", "continue", "in_progress"),),
            )
            session.set_plan(plan)
            session.begin_turn("turn-2")
            session.add_item("user", "second question")
            session.set_archived_summary("must not be inherited", 4)
            session.add_item("assistant", "second answer")
            session.finish_turn("completed")
            persist(store, workspace, session)
            store.set_provider("source", "provider-1", workspace)
            store.set_permission_preset(
                "source",
                PermissionPreset.FULL_ACCESS,
                workspace,
            )

            targets = store.editable_user_messages("source")
            self.assertEqual(
                [target["item_index"] for target in targets],
                [3],
            )
            first_anchor = next(
                event.event_id
                for event in session.journal
                if event.event_type == "message_appended"
                and event.turn_id == "turn-1"
            )
            with self.assertRaisesRegex(
                ValueError,
                "session already exists",
            ):
                store.fork_session("source", "source", first_anchor)
            with self.assertRaisesRegex(
                ValueError,
                "editable user message does not exist",
            ):
                store.fork_session("source", "old-fork", first_anchor)
            forked = store.fork_session(
                "source",
                "forked",
                str(targets[0]["anchor_id"]),
            )

            turn_2_start = next(
                index
                for index, event in enumerate(session.journal)
                if event.event_type == "turn_started"
                and event.turn_id == "turn-2"
            )
            self.assertEqual(forked.journal, session.journal[:turn_2_start])
            self.assertEqual(
                [item.content for item in forked.items],
                ["first question", None, "first result"],
            )
            self.assertEqual(forked.archived_summary, "summary before edit")
            self.assertEqual(forked.archived_item_cursor, 3)
            self.assertEqual(forked.compression_count, 1)
            self.assertEqual(forked.plan, plan)
            self.assertEqual(forked.turns["turn-1"].status, "completed")
            self.assertEqual(
                forked.tool_executions["call-1"].status,
                "completed",
            )
            self.assertNotIn("turn-2", forked.turns)
            self.assertEqual(store.workspace_for("forked"), str(workspace.resolve()))
            self.assertEqual(store.provider_for("forked"), "provider-1")
            self.assertEqual(
                store.permission_preset_for("forked"),
                PermissionPreset.FULL_ACCESS,
            )
            summaries = {
                item["session_id"]: item
                for item in store.list_workspace_sessions(workspace)
            }
            self.assertEqual(summaries["forked"]["forked_from"], "source")
            self.assertNotIn("forked_from", summaries["source"])

    def test_forking_the_first_question_starts_with_an_empty_journal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            store = JsonlSessionStore(workspace / "sessions")
            session = Session("source")
            session.begin_turn("turn-1")
            session.add_item("user", "replace me")
            session.add_item("assistant", "old answer")
            session.finish_turn("completed")
            persist(store, workspace, session)
            store.set_permission_preset(
                "source",
                PermissionPreset.FULL_ACCESS,
                workspace,
            )

            target = store.editable_user_messages("source")[0]
            forked = store.fork_session(
                "source",
                "forked",
                str(target["anchor_id"]),
            )

            self.assertEqual(forked.items, [])
            self.assertEqual(forked.journal, [])
            self.assertEqual(
                forked.permission_preset,
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(
                store.load("forked").permission_preset,
                PermissionPreset.FULL_ACCESS,
            )
            self.assertEqual(store.workspace_for("forked"), str(workspace.resolve()))
            self.assertEqual(
                [item["session_id"] for item in store.list_workspace_sessions(workspace)],
                ["source"],
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
