import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from agent_core import JsonlSessionStore, PermissionPreset, Session
from agent_core.session_paths import session_directory, workspace_directory, workspace_key
from agent_core.time_utils import format_utc
from agent_core import workspace_metadata as workspace_metadata_module


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "backfill_workspaces.py"


class WorkspaceMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.workspace = self.root / "项目 space"
        self.workspace.mkdir()
        self.store = JsonlSessionStore(self.root / "sessions")
        self.metadata_path = workspace_directory(
            self.store.directory, self.workspace,
        ) / "workspace.json"
        self.clock = self.enterContext(patch.object(workspace_metadata_module, "datetime"))
        self.now = datetime(2026, 11, 21, 0, 0, tzinfo=timezone.utc)
        self.clock.now.return_value = self.now

    def read_metadata(self) -> dict[str, str]:
        return json.loads(self.metadata_path.read_text(encoding="utf-8"))

    def advance(self) -> None:
        self.now += timedelta(seconds=1)
        self.clock.now.return_value = self.now

    def test_all_session_creation_paths_register_the_workspace(self) -> None:
        session = Session("appended")
        session.add_item("user", "hello")
        actions = (
            lambda: self.store.bind_workspace("bound", self.workspace),
            lambda: self.enterContext(self.store.open_journal("opened", self.workspace)),
            lambda: self.store.append_events("appended", session.journal, self.workspace),
            lambda: self.store.set_provider("provider", "main", self.workspace),
            lambda: self.store.set_permission_preset("permission", PermissionPreset.FULL_ACCESS, self.workspace),
        )
        for index, action in enumerate(actions):
            with self.subTest(index=index):
                self.store = JsonlSessionStore(self.root / f"sessions-{index}")
                self.metadata_path = workspace_directory(self.store.directory, self.workspace) / "workspace.json"
                action()
                self.assertEqual(self.read_metadata(), {
                    "workspace_id": workspace_key(self.workspace),
                    "path": str(self.workspace), "name": self.workspace.name,
                    "created": format_utc(self.now), "updated": format_utc(self.now),
                })

    def test_session_persistence_and_reads_do_not_refresh_existing_workspace(self) -> None:
        self.store.bind_workspace("first", self.workspace)
        before = self.metadata_path.read_bytes(), self.metadata_path.stat().st_mtime_ns
        with (
            patch.object(workspace_metadata_module, "_locked_workspace", side_effect=AssertionError("must not lock workspace")),
            patch.object(workspace_metadata_module, "_read_workspace_metadata", side_effect=AssertionError("must not read workspace metadata")),
        ):
            self.advance()
            self.store.bind_workspace("second", self.workspace)
            session = Session("first")
            with self.store.open_journal("first") as sink:
                session.attach_journal_sink(sink)
                session.begin_turn("turn")
                for index in range(10):
                    session.add_item("user", f"hello {index}")
                session.set_permission_preset(PermissionPreset.FULL_ACCESS)
                session.finish_turn("completed")
            self.advance()
            self.store.append_events("second", session.journal)
            self.store.set_provider("first", "main", self.workspace)
            self.store.set_permission_preset("second", PermissionPreset.FULL_ACCESS, self.workspace)
            self.store.set_title("first", "Renamed", "manual")
            self.store.load("first")
            self.store.workspace_for("first")
            self.store.list_sessions()
            self.store.list_workspace_sessions(self.workspace)
            self.store.search_messages("hello", limit=10, per_session_limit=2)
        self.assertEqual((self.metadata_path.read_bytes(), self.metadata_path.stat().st_mtime_ns), before)

    def test_explicit_activity_preserves_creation_time(self) -> None:
        self.store.bind_workspace("first", self.workspace)
        created = self.read_metadata()["created"]
        self.advance()
        workspace_metadata_module.touch_workspace_metadata(self.metadata_path.parent)
        self.assertEqual(self.read_metadata()["created"], created)
        self.assertEqual(self.read_metadata()["updated"], format_utc(self.now))

    def test_root_workspace_uses_its_anchor_as_name(self) -> None:
        workspace = Path(Path.cwd().anchor)
        self.assertEqual(workspace.name, "")
        self.store.bind_workspace("root", workspace)
        path = workspace_directory(self.store.directory, workspace) / "workspace.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["name"], workspace.anchor)
        self.assertEqual(metadata["path"], str(workspace))

    def test_backfill_repairs_empty_root_name_without_changing_other_metadata(self) -> None:
        workspace = Path(Path.cwd().anchor)
        self.store.bind_workspace("root", workspace)
        path = workspace_directory(self.store.directory, workspace) / "workspace.json"
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["name"] = ""
        path.write_text(json.dumps(metadata), encoding="utf-8")
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)],
            capture_output=True, text=True, check=True,
        )
        self.assertIn("Updated metadata for 1 Workspace(s).", result.stdout)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {**metadata, "name": workspace.anchor},
        )
        before = path.read_bytes(), path.stat().st_mtime_ns
        subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)],
            capture_output=True, text=True, check=True,
        )
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_workspace_activity_is_isolated_and_move_registers_the_target(self) -> None:
        self.store.bind_workspace("moved", self.workspace)
        original = self.metadata_path.read_bytes()
        other = self.root / "other"
        self.advance()
        self.store.bind_workspace("moved", other)
        self.assertEqual(self.metadata_path.read_bytes(), original)
        target = workspace_directory(self.store.directory, other) / "workspace.json"
        self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["created"], format_utc(self.now))

    def test_concurrent_session_creation_shares_one_workspace_record(self) -> None:
        def bind(index: int) -> None:
            JsonlSessionStore(self.store.directory).bind_workspace(str(index), self.workspace)

        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(bind, range(8)))
        self.assertEqual(self.read_metadata()["created"], format_utc(self.now))
        for index in range(8):
            self.assertEqual(self.store.workspace_for(str(index)), str(self.workspace))

    def test_existing_workspace_without_metadata_is_not_migrated_at_runtime(self) -> None:
        self.metadata_path.parent.mkdir(parents=True)
        with self.assertRaisesRegex(FileNotFoundError, "backfill_workspaces.py"):
            self.store.bind_workspace("new", self.workspace)
        self.assertFalse(self.metadata_path.exists())

    def test_ungrouped_child_journals_do_not_create_workspace_metadata(self) -> None:
        child = JsonlSessionStore(self.root / "subagents", group_by_workspace=False)
        with child.open_journal("child") as sink:
            session = Session("child")
            session.attach_journal_sink(sink)
            session.add_item("user", "hello")
        self.assertEqual(list(self.root.rglob("workspace.json")), [])

    def test_backfill_handles_sessions_without_events(self) -> None:
        for index in range(2):
            session = session_directory(self.store.directory, self.workspace, str(index))
            session.mkdir(parents=True)
            path = session / ("session.json" if index == 0 else f"{index}.jsonl")
            path.write_text("{}" if index == 0 else "", encoding="utf-8")
        subprocess.run(
            [sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)],
            capture_output=True, text=True, check=True,
        )
        metadata = self.read_metadata()
        self.assertEqual(metadata["path"], str(self.workspace))
        self.assertLessEqual(metadata["created"], metadata["updated"])

    def test_backfill_uses_history_times_and_preserves_existing_metadata(self) -> None:
        for index in range(2):
            session = session_directory(self.store.directory, self.workspace, str(index))
            session.mkdir(parents=True)
            journal = session / f"{index}.jsonl"
            journal.write_text(json.dumps({"timestamp_utc": f"2026-10-0{index + 1}T00:00:00Z"}) + "\n", encoding="utf-8")
            stamp = datetime(2026, 10, index + 3, tzinfo=timezone.utc).timestamp()
            os.utime(journal, (stamp, stamp))
        ignored = self.store.directory / ".locks" / "workspaces"
        ignored.mkdir(parents=True)
        (ignored / "session.json").write_text("{}", encoding="utf-8")
        artifact = session / "artifacts" / "other.jsonl"
        artifact.parent.mkdir()
        artifact.write_text("invalid journal", encoding="utf-8")
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in self.store.directory.rglob("*.jsonl")}

        result = subprocess.run([sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)], capture_output=True, text=True, check=True)
        self.assertIn("Updated metadata for 1 Workspace(s).", result.stdout)
        self.assertEqual(self.read_metadata()["created"], "2026-10-01T00:00:00Z")
        self.assertEqual(self.read_metadata()["updated"], "2026-10-04T00:00:00Z")
        for path, original in before.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), original)
        original = self.metadata_path.read_bytes(), self.metadata_path.stat().st_mtime_ns
        result = subprocess.run([sys.executable, str(SCRIPT), "--sessions-dir", str(self.store.directory)], capture_output=True, text=True, check=True)
        self.assertIn("Updated metadata for 0 Workspace(s).", result.stdout)
        self.assertEqual((self.metadata_path.read_bytes(), self.metadata_path.stat().st_mtime_ns), original)
        self.advance()
        self.store.bind_workspace("new", self.workspace)
        self.assertEqual(self.read_metadata()["created"], "2026-10-01T00:00:00Z")
