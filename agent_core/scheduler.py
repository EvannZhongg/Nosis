"""Durable schedule control-plane models and service.

Schedules are intentionally independent from JobManager/turn execution.  The
service stores only JSON-serialisable definitions and run metadata; execution
is supplied by a callback that builds the normal Agent runtime.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class OneShotTrigger:
    at: datetime
    type: Literal["once"] = "once"


@dataclass(frozen=True)
class CronTrigger:
    expression: str
    timezone: str = "UTC"
    type: Literal["cron"] = "cron"


Trigger = OneShotTrigger | CronTrigger


@dataclass(frozen=True)
class AgentTurnAction:
    prompt: str
    type: Literal["agent_turn"] = "agent_turn"


@dataclass
class Schedule:
    schedule_id: str
    trigger: Trigger
    action: AgentTurnAction
    workspace: str
    origin_session_id: str
    schedule_session_id: str
    enabled: bool = True
    end_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    next_run_at: datetime | None = None


@dataclass
class ScheduledRun:
    run_id: str
    schedule_id: str
    session_id: str
    turn_id: str
    scheduled_for: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    status: str = "scheduled"
    error: str | None = None


def _dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def _trigger_to_dict(trigger: Trigger) -> dict[str, object]:
    value = asdict(trigger)
    value["at"] = _dt(trigger.at) if isinstance(trigger, OneShotTrigger) else None
    if value.get("at") is None and not isinstance(trigger, OneShotTrigger):
        value.pop("at", None)
    return value


def _trigger_from_dict(value: dict[str, object]) -> Trigger:
    if value.get("type") == "once":
        at = _parse_dt(value.get("at"))
        if at is None:
            raise ValueError("once trigger requires at")
        return OneShotTrigger(at)
    if value.get("type") == "cron":
        return CronTrigger(str(value["expression"]), str(value.get("timezone", "UTC")))
    raise ValueError("unknown trigger type")


def _schedule_to_dict(schedule: Schedule) -> dict[str, object]:
    return {
        **asdict(schedule),
        "trigger": _trigger_to_dict(schedule.trigger),
        "action": asdict(schedule.action),
        "created_at": _dt(schedule.created_at),
        "end_at": _dt(schedule.end_at),
        "next_run_at": _dt(schedule.next_run_at),
    }


def _schedule_from_dict(value: dict[str, object]) -> Schedule:
    return Schedule(
        schedule_id=str(value["schedule_id"]),
        trigger=_trigger_from_dict(dict(value["trigger"])),
        action=AgentTurnAction(str(dict(value["action"])["prompt"])),
        workspace=str(value["workspace"]),
        origin_session_id=str(value["origin_session_id"]),
        schedule_session_id=str(value["schedule_session_id"]),
        enabled=bool(value.get("enabled", True)),
        end_at=_parse_dt(value.get("end_at")),
        created_at=_parse_dt(value.get("created_at")) or datetime.now(timezone.utc),
        next_run_at=_parse_dt(value.get("next_run_at")),
    )


class SchedulerService:
    """Persistent schedules and a small in-process due checker."""

    def __init__(self, path: Path | None = None, runner: Callable[[Schedule, ScheduledRun], None] | None = None) -> None:
        self.path = (path or (Path.home() / ".nosis" / "schedule.jsonl")).expanduser().resolve()
        self.runner = runner
        self._schedules: dict[str, Schedule] = {}
        self._runs: dict[str, ScheduledRun] = {}
        self._running: set[str] = set()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._load()

    @property
    def schedules(self) -> list[Schedule]:
        with self._lock:
            return list(self._schedules.values())

    @property
    def runs(self) -> list[ScheduledRun]:
        with self._lock:
            return list(self._runs.values())

    def _append(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _load(self) -> None:
        if not self.path.is_file():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            op = record.get("op")
            if op in {"create", "update"}:
                schedule = _schedule_from_dict(record["schedule"])
                self._schedules[schedule.schedule_id] = schedule
            elif op == "delete":
                self._schedules.pop(str(record["schedule_id"]), None)
            elif op == "run":
                run = _run_from_dict(record["run"])
                self._runs[run.run_id] = run

    def create_schedule(self, *, trigger: Trigger, prompt: str, workspace: str, origin_session_id: str, schedule_session_id: str, end_at: datetime | None = None) -> Schedule:
        if not prompt.strip():
            raise ValueError("prompt must be non-empty")
        if isinstance(trigger, OneShotTrigger) and trigger.at.tzinfo is None:
            trigger = OneShotTrigger(trigger.at.replace(tzinfo=timezone.utc))
        if end_at is not None and end_at.tzinfo is None:
            end_at = end_at.replace(tzinfo=timezone.utc)
        schedule = Schedule(str(uuid4()), trigger, AgentTurnAction(prompt.strip()), str(Path(workspace).expanduser().resolve()), origin_session_id, schedule_session_id, end_at=end_at)
        if isinstance(trigger, OneShotTrigger):
            schedule.next_run_at = trigger.at
        else:
            schedule.next_run_at = self.next_occurrence(schedule, datetime.now(timezone.utc) - timedelta(seconds=1))
        with self._lock:
            self._schedules[schedule.schedule_id] = schedule
            self._append({"op": "create", "schedule": _schedule_to_dict(schedule)})
        return schedule

    def update_schedule(self, schedule_id: str, **changes: object) -> Schedule:
        with self._lock:
            schedule = self._schedules[schedule_id]
            if "prompt" in changes:
                schedule.action = AgentTurnAction(str(changes["prompt"]))
            if "trigger" in changes:
                schedule.trigger = changes["trigger"]  # type: ignore[assignment]
            if "enabled" in changes:
                schedule.enabled = bool(changes["enabled"])
            if "end_at" in changes:
                schedule.end_at = changes["end_at"]  # type: ignore[assignment]
            schedule.next_run_at = self.next_occurrence(schedule, datetime.now(timezone.utc) - timedelta(seconds=1))
            self._append({"op": "update", "schedule": _schedule_to_dict(schedule)})
            return schedule

    def delete_schedule(self, schedule_id: str) -> None:
        with self._lock:
            self._schedules.pop(schedule_id, None)
            self._append({"op": "delete", "schedule_id": schedule_id})

    def next_occurrence(self, schedule: Schedule, after: datetime) -> datetime | None:
        trigger = schedule.trigger
        if isinstance(trigger, OneShotTrigger):
            return trigger.at if trigger.at > after and (schedule.end_at is None or trigger.at <= schedule.end_at) else None
        try:
            zone = ZoneInfo(trigger.timezone)
        except Exception:
            zone = timezone.utc
        cursor = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 * 370):
            if _cron_matches(trigger.expression, cursor):
                candidate = cursor.astimezone(timezone.utc)
                if schedule.end_at is None or candidate <= schedule.end_at:
                    return candidate
            cursor += timedelta(minutes=1)
        return None

    def poll_once(self, now: datetime | None = None) -> list[ScheduledRun]:
        now = now or datetime.now(timezone.utc)
        due: list[tuple[Schedule, datetime]] = []
        with self._lock:
            for schedule in self._schedules.values():
                if schedule.enabled and schedule.next_run_at and schedule.next_run_at <= now:
                    due.append((schedule, schedule.next_run_at))
            for schedule, scheduled_for in due:
                if isinstance(schedule.trigger, CronTrigger) and scheduled_for < now - timedelta(minutes=1):
                    schedule.next_run_at = self.next_occurrence(schedule, now)
                else:
                    schedule.next_run_at = self.next_occurrence(schedule, scheduled_for)
                if isinstance(schedule.trigger, OneShotTrigger):
                    schedule.enabled = False
                self._append({"op": "update", "schedule": _schedule_to_dict(schedule)})
        created: list[ScheduledRun] = []
        for schedule, scheduled_for in due:
            run = ScheduledRun(str(uuid4()), schedule.schedule_id, schedule.schedule_session_id, str(uuid4()), scheduled_for)
            with self._lock:
                if isinstance(schedule.trigger, CronTrigger) and scheduled_for < now - timedelta(minutes=1):
                    run.status, run.error = "skipped", "missed cron occurrence"
                elif schedule.schedule_id in self._running:
                    run.status, run.error = "skipped", "previous scheduled run still running"
                else:
                    self._running.add(schedule.schedule_id)
                    run.status = "running"
                    run.started_at = now
                self._runs[run.run_id] = run
                self._append({"op": "run", "run": _run_to_dict(run)})
            created.append(run)
            if run.status == "running" and self.runner:
                threading.Thread(target=self._execute, args=(schedule, run), daemon=True).start()
        return created

    def _execute(self, schedule: Schedule, run: ScheduledRun) -> None:
        try:
            self.runner(schedule, run)  # type: ignore[misc]
            run.status = "completed"
        except Exception as error:
            run.status, run.error = "failed", str(error)
        finally:
            run.finished_at = datetime.now(timezone.utc)
            with self._lock:
                self._running.discard(schedule.schedule_id)
                self._append({"op": "run", "run": _run_to_dict(run)})

    def start(self, interval: float = 1.0) -> threading.Thread:
        def loop() -> None:
            while not self._stop.is_set():
                self.poll_once()
                self._stop.wait(interval)
        thread = threading.Thread(target=loop, name="nosis-scheduler", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._stop.set()


def _run_to_dict(run: ScheduledRun) -> dict[str, object]:
    return {**asdict(run), **{k: _dt(getattr(run, k)) for k in ("scheduled_for", "started_at", "finished_at")}}


def _run_from_dict(value: dict[str, object]) -> ScheduledRun:
    return ScheduledRun(str(value["run_id"]), str(value["schedule_id"]), str(value["session_id"]), str(value["turn_id"]), _parse_dt(value.get("scheduled_for")) or datetime.now(timezone.utc), _parse_dt(value.get("started_at")), _parse_dt(value.get("finished_at")), str(value.get("status", "scheduled")), value.get("error") if isinstance(value.get("error"), str) else None)


def _cron_matches(expression: str, value: datetime) -> bool:
    fields = expression.split()
    if len(fields) != 5:
        raise ValueError("cron expression must have five fields")
    limits = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    actual = [value.minute, value.hour, value.day, value.month, (value.weekday() + 1) % 7]
    for field, number, (low, high) in zip(fields, actual, limits):
        allowed: set[int] = set()
        for token in field.split(","):
            if token == "*":
                allowed.update(range(low, high + 1)); continue
            if token.startswith("*/"):
                allowed.update(range(low, high + 1, int(token[2:]))); continue
            if "-" in token:
                start, end = token.split("-", 1); allowed.update(range(int(start), int(end) + 1)); continue
            allowed.add(int(token))
        if number not in allowed:
            return False
    return True


__all__ = ["OneShotTrigger", "CronTrigger", "AgentTurnAction", "Schedule", "ScheduledRun", "SchedulerService"]
