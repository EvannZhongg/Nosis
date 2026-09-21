import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_core.scheduler import OneShotTrigger, SchedulerService


class SchedulerServiceTest(unittest.TestCase):
    def test_due_schedule_without_a_runner_fails_instead_of_staying_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            now = datetime.now(timezone.utc)
            service.create_schedule(
                trigger=OneShotTrigger(now),
                prompt="scheduled prompt",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )

            run = service.poll_once(now)[0]

            self.assertEqual(run.status, "failed")
            self.assertEqual(run.error, "scheduled task runner is unavailable")
            self.assertIsNotNone(run.finished_at)

    def test_multiple_services_observe_new_schedules_but_only_one_polls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "schedule.jsonl"
            first = SchedulerService(path, runner=lambda schedule, run: None)
            second = SchedulerService(path, runner=lambda schedule, run: None)
            first_thread = first.start(interval=0.01)
            second_thread = second.start(interval=0.01)
            self.addCleanup(first.close)
            self.addCleanup(second.close)
            now = datetime.now(timezone.utc)

            second.create_schedule(
                trigger=OneShotTrigger(now),
                prompt="scheduled prompt",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )

            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                runs = first.runs
                if runs:
                    break
                time.sleep(0.01)
            first.close()
            second.close()
            first_thread.join(1)
            second_thread.join(1)

            self.assertEqual(len(first.runs), 1)


if __name__ == "__main__":
    unittest.main()
