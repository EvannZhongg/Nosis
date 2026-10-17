import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_core import JsonlSessionStore
from agent_core.session_paths import session_log_path
from agent_core.usage import CacheUsage


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


def write_journal(path: Path, records: list[dict[str, object]]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = (
        "\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n"
    ).encode("utf-8")
    path.write_bytes(body)
    return body


def run_migration(root: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--sessions-dir", str(root)],
        capture_output=True, text=True, check=check,
    )


class SessionMigrationTest(unittest.TestCase):
    def test_old_model_usage_migrates_and_session_cache_totals_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "sessions"
            workspace = Path(directory) / "workspace"
            path = session_log_path(root, workspace, "session-1")
            usages = [
                {"input_tokens": 10000, "output_tokens": 10, "total_tokens": 10010},
                {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
                 "cache_read_tokens": 80},
                {"input_tokens": 900, "output_tokens": 10, "total_tokens": 910,
                 "cache_read_tokens": 0, "cache_write_tokens": 200},
                None,
                {"input_tokens": None, "output_tokens": None, "total_tokens": None},
            ]
            records = [event(1, "turn_started", {"status": "running"})]
            records.extend(event(index + 2, "model_completed", {
                "model_call_index": index + 1, "has_tool_calls": False, "usage": usage,
            }) for index, usage in enumerate(usages))
            records.append(event(7, "turn_completed", {"status": "completed"}))
            original = write_journal(path, records)
            with self.assertRaisesRegex(KeyError, "cache_read_tokens"):
                JsonlSessionStore(root).load("session-1", recover=False)

            first = run_migration(root)
            self.assertIn("Migrated 1 Session journal(s), skipped 0", first.stdout)
            self.assertEqual(path.with_name(path.name + ".bak").read_bytes(), original)
            migrated = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(migrated[1]["payload"]["usage"], {
                "input_tokens": 10000, "output_tokens": 10, "total_tokens": 10010,
                "cache_read_tokens": None, "cache_write_tokens": None,
            })
            self.assertEqual(migrated[2]["payload"]["usage"]["cache_read_tokens"], 80)
            self.assertIsNone(migrated[2]["payload"]["usage"]["cache_write_tokens"])
            self.assertEqual(migrated[3], records[3])
            self.assertIsNone(migrated[4]["payload"]["usage"])
            self.assertIsNone(migrated[5]["payload"]["usage"]["cache_read_tokens"])
            self.assertEqual(JsonlSessionStore(root).load("session-1", recover=False).cache_usage,
                             CacheUsage(1000, 80))

            after_first = path.read_bytes()
            second = run_migration(root)
            self.assertIn("Migrated 0 Session journal(s), skipped 1", second.stdout)
            self.assertEqual(path.read_bytes(), after_first)
            self.assertEqual(path.with_name(path.name + ".bak").read_bytes(), original)

    def test_invalid_usage_does_not_change_journal_or_create_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session-1" / "session-1.jsonl"
            original = write_journal(path, [
                event(1, "model_completed", {"usage": {"input_tokens": 100}}),
                event(2, "model_completed", {"usage": []}),
            ])
            result = run_migration(root, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("usage must be an object or null", result.stderr)
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(path.with_name(path.name + ".bak").exists())

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
            original = write_journal(path, records)
            unrelated = root / "artifact.jsonl"
            unrelated.write_text("not a Session Journal\n", encoding="utf-8")

            first = run_migration(root)
            self.assertIn("Migrated 1 Session journal(s), skipped 0", first.stdout)
            self.assertEqual(path.with_name(path.name + ".bak").read_bytes(), original)
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
            second = run_migration(root)
            self.assertIn("Migrated 0 Session journal(s), skipped 1", second.stdout)
            self.assertEqual(path.read_bytes(), after_first)

    def test_up_to_date_journal_is_skipped_without_a_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session-1" / "session-1.jsonl"
            body = write_journal(path, [
                event(1, "job_submitted", {
                    "job_id": "job-1", "kind": "shell", "status": "submitted",
                    "details": {"command": "echo hi"},
                }),
                event(2, "job_completed", {
                    "job_id": "job-1", "kind": "shell", "status": "completed",
                    "elapsed_seconds": 1.0, "result_summary": "hi", "progress": None,
                }),
            ])

            result = run_migration(root)

            self.assertIn("Migrated 0 Session journal(s), skipped 1", result.stdout)
            self.assertEqual(path.read_bytes(), body)
            self.assertFalse(path.with_name(path.name + ".bak").exists())

    def test_rerun_overwrites_a_stale_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session-1" / "session-1.jsonl"
            # A journal written by an older revision of the script can still
            # miss a field this revision knows about, even when its backup
            # already exists.
            body = write_journal(path, [
                event(1, "job_submitted", {
                    "job_id": "job-1", "kind": "shell", "status": "submitted",
                    "details": {},
                }),
                event(2, "job_completed", {
                    "job_id": "job-1", "kind": "shell", "status": "completed",
                    "elapsed_seconds": None, "result_summary": None,
                }),
            ])
            backup = path.with_name(path.name + ".bak")
            backup.write_bytes(b"stale backup\n")

            first = run_migration(root)

            self.assertIn("Migrated 1 Session journal(s), skipped 0", first.stdout)
            self.assertEqual(backup.read_bytes(), body)
            migrated = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertIsNone(migrated[1]["payload"]["progress"])

            after_first = path.read_bytes()
            second = run_migration(root)
            self.assertIn("Migrated 0 Session journal(s), skipped 1", second.stdout)
            self.assertEqual(path.read_bytes(), after_first)
            self.assertEqual(backup.read_bytes(), body)

    def test_invalid_journal_is_not_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "session-1" / "session-1.jsonl"
            path.parent.mkdir()
            path.write_text("{invalid}\n", encoding="utf-8")

            result = run_migration(root, check=False)

            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), "{invalid}\n")
            self.assertFalse(path.with_name(path.name + ".bak").exists())


if __name__ == "__main__":
    unittest.main()
