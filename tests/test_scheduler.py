import tempfile
import time
import unittest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from agent_core.scheduler import (
    AgentTurnAction,
    CronTrigger,
    IntervalTrigger,
    OneShotTrigger,
    Schedule,
    SchedulerService,
    _schedule_to_dict,
    parse_schedule_update_input,
)


class SchedulerServiceTest(unittest.TestCase):
    def test_parse_schedule_update_input_validates_the_complete_payload(self) -> None:
        changes = parse_schedule_update_input(
            {
                "prompt": "  updated prompt  ",
                "trigger": {"type": "interval", "seconds": 60},
                "enabled": False,
                "end_at": "2099-12-31T23:59:00+00:00",
            }
        )

        self.assertEqual(changes["prompt"], "updated prompt")
        self.assertEqual(changes["trigger"], IntervalTrigger(60))
        self.assertIs(changes["enabled"], False)
        self.assertEqual(
            changes["end_at"],
            datetime(2099, 12, 31, 23, 59, tzinfo=timezone.utc),
        )
        with self.assertRaisesRegex(
            ValueError, "unsupported schedule update field"
        ):
            parse_schedule_update_input({"unknown": True})

    def test_update_rejects_unknown_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            schedule = service.create_schedule(
                trigger=IntervalTrigger(60),
                prompt="scheduled prompt",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )

            with self.assertRaises(TypeError):
                service.update_schedule(schedule.schedule_id, unknown=True)

    def test_update_normalizes_prompt_and_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            schedule = service.create_schedule(
                trigger=IntervalTrigger(60),
                prompt="scheduled prompt",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )

            updated = service.update_schedule(
                schedule.schedule_id, prompt="  updated prompt  "
            )
            self.assertEqual(updated.action.prompt, "updated prompt")
            with self.assertRaisesRegex(ValueError, "prompt must be"):
                service.update_schedule(schedule.schedule_id, prompt=" ")
            with self.assertRaisesRegex(ValueError, "enabled must be"):
                service.update_schedule(schedule.schedule_id, enabled=1)
            with self.assertRaisesRegex(ValueError, "schedule_id must be"):
                service.update_schedule(123, prompt="updated")  # type: ignore[arg-type]

    def test_create_normalizes_prompt_and_rejects_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")

            schedule = service.create_schedule(
                trigger=IntervalTrigger(60),
                prompt="  scheduled prompt  ",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )
            self.assertEqual(schedule.action.prompt, "scheduled prompt")
            with self.assertRaisesRegex(ValueError, "prompt must be"):
                service.create_schedule(
                    trigger=IntervalTrigger(60),
                    prompt=" ",
                    workspace=directory,
                    origin_session_id="origin",
                    schedule_session_id="other-session",
                )

    def test_cron_rejects_unknown_timezone_instead_of_falling_back_to_utc(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown IANA timezone"):
            CronTrigger("0 9 * * *", "Asia/Shangai")

    def test_absolute_times_require_an_explicit_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone offset"):
            OneShotTrigger(datetime(2099, 1, 1))

    def test_interval_supports_elapsed_seconds_and_minutes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            start = datetime(2099, 1, 1, tzinfo=timezone.utc)
            for seconds in (30, 90, 90 * 60):
                schedule = service.create_schedule(
                    trigger=IntervalTrigger(seconds, start),
                    prompt="scheduled prompt",
                    workspace=directory,
                    origin_session_id="origin",
                    schedule_session_id="scheduled-session",
                )
                self.assertEqual(service.next_occurrence(schedule, start), start + timedelta(seconds=seconds))

    def test_cron_field_steps_are_anchored_to_each_hour(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            schedule = Schedule(
                schedule_id="cron",
                trigger=CronTrigger("*/7 * * * *", "UTC"),
                action=AgentTurnAction("scheduled prompt"),
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )
            after = datetime(2099, 1, 1, 12, 1, tzinfo=timezone.utc)
            self.assertEqual(
                service.next_occurrence(schedule, after),
                datetime(2099, 1, 1, 12, 7, tzinfo=timezone.utc),
            )
            self.assertEqual(
                service.next_occurrence(
                    schedule, datetime(2099, 1, 1, 12, 55, tzinfo=timezone.utc)
                ),
                datetime(2099, 1, 1, 12, 56, tzinfo=timezone.utc),
            )
            self.assertEqual(
                service.next_occurrence(
                    schedule, datetime(2099, 1, 1, 12, 56, tzinfo=timezone.utc)
                ),
                datetime(2099, 1, 1, 13, 0, tzinfo=timezone.utc),
            )

    def test_utc_cron_does_not_require_tzdata(self) -> None:
        with patch("agent_core.scheduler.ZoneInfo", side_effect=ZoneInfoNotFoundError("tzdata unavailable")):
            trigger = CronTrigger("0 9 * * *")
            self.assertEqual(trigger.timezone, "UTC")

    def test_invalid_timezone_names_have_stable_errors(self) -> None:
        for name in ("", "../UTC"):
            with self.assertRaisesRegex(ValueError, "unknown IANA timezone"):
                CronTrigger("0 9 * * *", name)

    def test_missed_interval_skips_to_next_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(
                Path(directory) / "schedule.jsonl", runner=lambda schedule, run: None
            )
            start = datetime(2099, 1, 1, tzinfo=timezone.utc)
            schedule = service.create_schedule(
                trigger=IntervalTrigger(1, start),
                prompt="scheduled prompt",
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )
            schedule.next_run_at = start
            service._append({"op": "update", "schedule": _schedule_to_dict(schedule)})
            now = start + timedelta(seconds=12)
            runs = service.poll_once(now)
            self.assertEqual(len(runs), 1)
            self.assertEqual(runs[0].status, "skipped")
            self.assertEqual(runs[0].error, "missed interval occurrence")
            self.assertEqual(service.schedules[0].next_run_at, start + timedelta(seconds=13))

    def test_cron_next_occurrence_uses_declared_timezone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = SchedulerService(Path(directory) / "schedule.jsonl")
            schedule = Schedule(
                schedule_id="cron",
                trigger=CronTrigger("0 9 * * *", "Asia/Shanghai"),
                action=AgentTurnAction("scheduled prompt"),
                workspace=directory,
                origin_session_id="origin",
                schedule_session_id="scheduled-session",
            )

            self.assertEqual(
                service.next_occurrence(
                    schedule, datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc)
                ),
                datetime(2099, 1, 1, 1, 0, tzinfo=timezone.utc),
            )

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
