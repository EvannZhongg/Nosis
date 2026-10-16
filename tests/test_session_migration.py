import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_core import JsonlSessionStore
from agent_core.session_paths import session_log_path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "migrate_sessions.py"


def event(seq: int, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "seq": seq,
        "event_id": f"event-{seq}",
        "event_type": event_type,
        "turn_id": "turn-1",
        "timestamp_utc": "2026-10-01T00:00:00Z",
        "tool_call_id": None,
        "payload": payload,
    }


class SessionMigrationTest(unittest.TestCase):
    def test_v1_job_journal_migrates_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions"
            workspace = Path(directory) / "workspace"
            path = session_log_path(root, workspace, "session-1")
            path.parent.mkdir(parents=True)
            records = [
                event(1, "turn_started", {"status": "running"}),
                event(2, "message_appended", {"message": {"role": "user", "content": "旧对话"}}),
                event(3, "job_submitted", {"job_id": "job-1", "kind": "shell", "status": "submitted"}),
                event(4, "job_started", {"job_id": "job-1", "kind": "shell", "status": "running"}),
                event(5, "job_completed", {"job_id": "job-1", "kind": "shell", "status": "completed"}),
                event(6, "turn_completed", {"status": "completed"}),
            ]
            original = ("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n").encode("utf-8")
            path.write_bytes(original)
            unrelated = root / "artifact.jsonl"
            unrelated.write_text("not a Session Journal\n", encoding="utf-8")

            first = subprocess.run(
                [sys.executable, str(SCRIPT), "--sessions-dir", str(root)],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("Migrated 1 Session journal(s).", first.stdout)
            self.assertEqual(path.with_name(path.name + ".v1.bak").read_bytes(), original)
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "not a Session Journal\n")

            migrated = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(migrated[2]["payload"]["details"], {})
            self.assertIsNone(migrated[4]["payload"]["elapsed_seconds"])
            self.assertIsNone(migrated[4]["payload"]["result_summary"])
            self.assertIsNone(migrated[4]["payload"]["progress"])
            self.assertEqual(migrated[1], records[1])
            restored = JsonlSessionStore(root).load("session-1", recover=False)
            self.assertEqual(restored.items[0].content, "旧对话")
            self.assertEqual(restored.jobs["job-1"].status, "completed")

            after_first = path.read_bytes()
            second = subprocess.run(
                [sys.executable, str(SCRIPT), "--sessions-dir", str(root)],
                capture_output=True, text=True, check=True,
            )
            self.assertIn("Migrated 0 Session journal(s).", second.stdout)
            self.assertEqual(path.read_bytes(), after_first)

    def test_invalid_journal_is_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session-1" / "session-1.jsonl"
            path.parent.mkdir()
            path.write_text("{invalid}\n", encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(SCRIPT), "--sessions-dir", str(root)],
                capture_output=True, text=True,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid}\n")
            self.assertFalse(path.with_name(path.name + ".v1.bak").exists())


if __name__ == "__main__":
    unittest.main()
