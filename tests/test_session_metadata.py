import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_core import JsonlSessionStore, PermissionPreset, Session
from agent_core import session_store as session_store_module
from agent_core.session_paths import session_directory, workspace_key
from agent_core.time_utils import format_utc


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_metadata.py"
FIELDS = {"session_id", "workspace_id", "created", "updated"}


class SessionMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.store = JsonlSessionStore(self.root / "sessions")
        self.clock = self.enterContext(patch.object(session_store_module, "datetime"))
        self.now = datetime(2030, 1, 1, tzinfo=timezone.utc)
        self.clock.now.return_value = self.now

    def metadata_path(self, session_id: str) -> Path:
        return session_directory(self.store.directory, self.root, session_id) / "session.json"

    def metadata(self, session_id: str) -> dict:
        return json.loads(self.metadata_path(session_id).read_text(encoding="utf-8"))

    def advance(self) -> None:
        self.now += timedelta(seconds=1)
        self.clock.now.return_value = self.now

    def test_all_creation_paths_populate_identity_and_times(self) -> None:
        session = Session("appended")
        session.add_item("user", "hello")
        self.store.bind_workspace("bound", self.root)
        with self.store.open_journal("opened", self.root):
            pass
        self.store.append_events("appended", session.journal, self.root)
        self.store.set_provider("provider", "main", self.root)
        self.store.set_permission_preset("permission", PermissionPreset.FULL_ACCESS, self.root)
        for session_id in ("bound", "opened", "appended", "provider", "permission"):
            with self.subTest(session_id=session_id):
                metadata = self.metadata(session_id)
                self.assertEqual({key: metadata[key] for key in FIELDS}, {
                    "session_id": session_id, "workspace_id": workspace_key(self.root),
                    "created": format_utc(self.now), "updated": format_utc(self.now),
                })

    def test_only_explicit_activity_refreshes_time_and_preserves_existing_fields(self) -> None:
        self.store.bind_workspace("session", self.root)
        self.store.set_provider("session", "main", self.root)
        before = self.metadata("session")
        self.advance()
        with self.store.open_journal("session") as sink:
            session = Session("session")
            session.attach_journal_sink(sink)
            session.begin_turn("turn")
            session.add_item("user", "hello")
            session.finish_turn("completed")
        self.store.set_title("session", "Manual title", "manual")
        self.store.set_permission_preset("session", PermissionPreset.FULL_ACCESS, self.root)
        self.assertEqual(self.metadata("session")["updated"], before["updated"])
        projected = self.metadata("session")
        self.store.record_activity("session", self.root)
        self.assertEqual(self.metadata("session"), {**projected, "updated": format_utc(self.now)})
        self.assertEqual(self.metadata("session")["created"], before["created"])
        self.store.load("session")
        self.store.list_sessions()
        self.assertEqual(self.metadata("session")["updated"], format_utc(self.now))

    def test_fork_has_its_own_identity_and_creation_time(self) -> None:
        session = Session("source")
        session.begin_turn("turn")
        session.add_item("user", "hello")
        session.finish_turn("completed")
        self.store.append_events("source", session.journal, self.root)
        before = self.metadata("source")
        self.advance()
        anchor = self.store.editable_user_messages("source")[0]["anchor_id"]
        self.store.fork_session("source", "fork", anchor)
        fork = self.metadata("fork")
        self.assertEqual(fork["session_id"], "fork")
        self.assertEqual(fork["workspace_id"], before["workspace_id"])
        self.assertEqual(fork["created"], format_utc(self.now))
        self.assertEqual(fork["updated"], fork["created"])
        self.assertEqual(fork["forked_from"], "source")
        self.assertEqual(self.metadata("source"), before)

    def test_workspace_move_updates_parent_and_child_identity_without_resetting_times(self) -> None:
        self.store.bind_workspace("parent", self.root)
        parent_path = self.metadata_path("parent")
        child_store = JsonlSessionStore(parent_path.parent / "subagents", group_by_workspace=False)
        with child_store.open_journal("child", self.root):
            pass
        child_path = child_store.directory / "child" / "session.json"
        child_before = json.loads(child_path.read_text(encoding="utf-8"))
        parent_before = self.metadata("parent")
        self.advance()
        other = self.root / "other"
        self.store.bind_workspace("parent", other)
        moved = session_directory(self.store.directory, other, "parent")
        for path, before in (
            (moved / "session.json", parent_before),
            (moved / "subagents" / "child" / "session.json", child_before),
        ):
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {
                **before, "workspace_id": workspace_key(other),
            })

    def test_existing_records_are_not_backfilled_at_runtime(self) -> None:
        self.store.bind_workspace("old", self.root)
        path = self.metadata_path("old")
        old = {key: value for key, value in self.metadata("old").items() if key not in FIELDS}
        path.write_text(json.dumps(old), encoding="utf-8")
        with self.assertRaisesRegex(KeyError, "backfill_metadata.py"):
            self.store.record_activity("old", self.root)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), old)

    def test_combined_migration_preserves_fields_journals_and_original_workspace_times(self) -> None:
        directory = self.metadata_path("old").parent
        directory.mkdir(parents=True)
        existing = {
            "title": "保留标题", "title_source": "manual", "has_activity": True,
            "provider": "main", "permission_preset": "full_access", "extra": {"keep": 1},
        }
        path = directory / "session.json"
        path.write_text(json.dumps(existing), encoding="utf-8")
        journal = directory / "old.jsonl"
        journal.write_text('{"timestamp_utc":"2026-01-01T00:00:00Z"}\n', encoding="utf-8")
        modified = datetime(2026, 2, 1, tzinfo=timezone.utc).timestamp()
        for file in (path, journal):
            os.utime(file, (modified, modified))
        child_path = directory / "subagents" / "child" / "session.json"
        child_path.parent.mkdir(parents=True)
        child_existing = {"title": "child", "created": "2026-01-02T00:00:00Z"}
        child_path.write_text(json.dumps(child_existing), encoding="utf-8")
        artifact = directory / "artifacts" / "session.json"
        artifact.parent.mkdir()
        artifact.write_text("invalid artifact", encoding="utf-8")
        original = journal.read_bytes(), journal.stat().st_mtime_ns

        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Updated metadata for 1 Workspace(s).", result.stdout)
        self.assertIn("Updated metadata for 2 Session(s).", result.stdout)
        metadata = self.metadata("old")
        self.assertEqual({key: metadata[key] for key in existing}, existing)
        self.assertEqual(metadata["session_id"], "old")
        self.assertEqual(metadata["workspace_id"], workspace_key(self.root))
        self.assertEqual(metadata["created"], "2026-01-01T00:00:00Z")
        self.assertEqual(metadata["updated"], "2026-02-01T00:00:00Z")
        child = json.loads(child_path.read_text(encoding="utf-8"))
        self.assertEqual(child["workspace_id"], workspace_key(self.root))
        self.assertEqual(child["created"], child_existing["created"])
        workspace_path = directory.parent / "workspace.json"
        workspace = json.loads(workspace_path.read_text(encoding="utf-8"))
        self.assertEqual(workspace["updated"], "2026-02-01T00:00:00Z")
        self.assertEqual((journal.read_bytes(), journal.stat().st_mtime_ns), original)
        self.assertEqual(artifact.read_text(encoding="utf-8"), "invalid artifact")
        snapshots = {file: (file.read_bytes(), file.stat().st_mtime_ns) for file in (path, child_path, workspace_path)}
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Updated metadata for 0 Workspace(s).", result.stdout)
        self.assertIn("Updated metadata for 0 Session(s).", result.stdout)
        for file, snapshot in snapshots.items():
            self.assertEqual((file.read_bytes(), file.stat().st_mtime_ns), snapshot)
