"""Durable schedule control-plane models and service.

Schedules are intentionally independent from JobManager/turn execution.  The
service stores only JSON-serialisable definitions and run metadata; execution
is supplied by a callback that builds the normal Agent runtime.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Callable, Literal, TypedDict
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .execution.authority import ExecutionScope


@dataclass(frozen=True)
class OneShotTrigger:
    at: datetime
    type: Literal["once"] = "once"

    def __post_init__(self) -> None:
        _require_aware(self.at, "once trigger at")


@dataclass(frozen=True)
class CronTrigger:
    expression: str
    timezone: str = "UTC"
    type: Literal["cron"] = "cron"

    def __post_init__(self) -> None:
        _timezone(self.timezone)


@dataclass(frozen=True)
class IntervalTrigger:
    """A fixed elapsed-time duration between successive runs."""

    seconds: int
    start_at: datetime | None = None
    type: Literal["interval"] = "interval"

    def __post_init__(self) -> None:
        if isinstance(self.seconds, bool) or not isinstance(self.seconds, int) or self.seconds < 1:
            raise ValueError("interval seconds must be a positive integer")
        if self.start_at is not None:
            _require_aware(self.start_at, "interval trigger start_at")


Trigger = OneShotTrigger | CronTrigger | IntervalTrigger


class ScheduleUpdateInput(TypedDict, total=False):
    prompt: str
    trigger: Trigger
    enabled: bool
    end_at: datetime | None


class _Unset:
    pass


_UNSET = _Unset()


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
    execution_scope: ExecutionScope
    enabled: bool = True
    end_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    next_run_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_scope, ExecutionScope):
            raise ValueError("schedule execution_scope must be workspace or host")
        _require_aware(self.created_at, "schedule created_at")
        if self.next_run_at is not None:
            _require_aware(self.next_run_at, "schedule next_run_at")


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


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")


def parse_schedule_prompt_input(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("prompt must be a non-empty string")
    return value.strip()


def parse_schedule_id_input(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("schedule_id must be a non-empty string")
    return value


def parse_schedule_enabled_input(value: object) -> bool:
    if not isinstance(value, bool):
        raise ValueError("enabled must be a boolean")
    return value


def _timezone(name: str) -> tzinfo:
    if name == "UTC":
        return timezone.utc
    if not isinstance(name, str) or not name:
        raise ValueError(f"unknown IANA timezone: {name}")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as error:
        raise ValueError(f"unknown IANA timezone: {name}") from error


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "datetime value")
    return parsed


def trigger_to_dict(trigger: Trigger) -> dict[str, object]:
    if isinstance(trigger, OneShotTrigger):
        return {"type": "once", "at": _dt(trigger.at)}
    if isinstance(trigger, CronTrigger):
        return {"type": "cron", "expression": trigger.expression, "timezone": trigger.timezone}
    return {"type": "interval", "seconds": trigger.seconds, "start_at": _dt(trigger.start_at)}


def parse_end_at_input(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("end_at must be an ISO-8601 string or null")
    parsed = datetime.fromisoformat(value)
    _require_aware(parsed, "schedule end_at")
    return parsed


def parse_trigger_input(value: object) -> Trigger:
    if not isinstance(value, dict):
        raise ValueError("trigger must be an object")
    kind = value.get("type")
    if kind == "once":
        unknown = set(value) - {"type", "at"}
        if unknown:
            raise ValueError(
                f"once trigger has unsupported field(s): {', '.join(sorted(unknown))}"
            )
        at = value.get("at")
        if not isinstance(at, str) or not at:
            raise ValueError(
                'Error: once trigger requires \'at\'. Retry with at="<ISO-8601 with UTC offset>".'
            )
        return OneShotTrigger(datetime.fromisoformat(at))
    if kind == "cron":
        unknown = set(value) - {"type", "expression", "timezone"}
        if unknown:
            raise ValueError(
                f"cron trigger has unsupported field(s): {', '.join(sorted(unknown))}"
            )
        expression = value.get("expression")
        if not isinstance(expression, str) or not expression:
            raise ValueError(
                'Error: cron trigger requires \'expression\'. Retry with expression="<five-field cron>".'
            )
        timezone_name = value.get("timezone", "UTC")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ValueError("cron trigger timezone must be an IANA timezone name")
        return CronTrigger(expression, timezone_name)
    if kind == "interval":
        unknown = set(value) - {"type", "seconds", "start_at"}
        if unknown:
            raise ValueError(
                f"interval trigger has unsupported field(s): {', '.join(sorted(unknown))}"
            )
        seconds = value.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 1:
            raise ValueError(
                "Error: interval trigger requires positive integer 'seconds'. Retry with seconds=<positive integer>."
            )
        start_at = value.get("start_at")
        if start_at is not None and (not isinstance(start_at, str) or not start_at):
            raise ValueError(
                "interval trigger start_at must be an ISO-8601 string or null"
            )
        return IntervalTrigger(
            seconds,
            datetime.fromisoformat(start_at) if start_at is not None else None,
        )
    raise ValueError("trigger.type must be once, cron, or interval")


def parse_schedule_update_input(value: object) -> ScheduleUpdateInput:
    if not isinstance(value, dict):
        raise ValueError("schedule update must be an object")
    unknown = set(value) - {"prompt", "trigger", "enabled", "end_at"}
    if unknown:
        names = ", ".join(sorted(str(name) for name in unknown))
        raise ValueError(f"unsupported schedule update field(s): {names}")
    changes: ScheduleUpdateInput = {}
    if "prompt" in value:
        changes["prompt"] = parse_schedule_prompt_input(value["prompt"])
    if "trigger" in value:
        changes["trigger"] = parse_trigger_input(value["trigger"])
    if "enabled" in value:
        changes["enabled"] = parse_schedule_enabled_input(value["enabled"])
    if "end_at" in value:
        changes["end_at"] = parse_end_at_input(value["end_at"])
    return changes


def _trigger_from_dict(value: dict[str, object]) -> Trigger:
    if value.get("type") == "once":
        at = _parse_dt(value.get("at"))
        if at is None:
            raise ValueError("once trigger requires at")
        return OneShotTrigger(at)
    if value.get("type") == "cron":
        return CronTrigger(str(value["expression"]), str(value.get("timezone", "UTC")))
    if value.get("type") == "interval":
        return IntervalTrigger(
            value.get("seconds"), _parse_dt(value.get("start_at"))  # type: ignore[arg-type]
        )
    raise ValueError("unknown trigger type")


def _schedule_to_dict(schedule: Schedule) -> dict[str, object]:
    return {
        **asdict(schedule),
        "trigger": trigger_to_dict(schedule.trigger),
        "action": asdict(schedule.action),
        "execution_scope": schedule.execution_scope.value,
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
        execution_scope=ExecutionScope(str(value["execution_scope"])),
        enabled=bool(value.get("enabled", True)),
        end_at=_parse_dt(value.get("end_at")),
        created_at=_parse_dt(value.get("created_at")) or datetime.now(timezone.utc),
        next_run_at=_parse_dt(value.get("next_run_at")),
    )


def _next_occurrence(
    trigger: Trigger, end_at: datetime | None, after: datetime
) -> datetime | None:
    if isinstance(trigger, OneShotTrigger):
        candidate = trigger.at if trigger.at > after else None
    elif isinstance(trigger, IntervalTrigger):
        delta = timedelta(seconds=trigger.seconds)
        candidate = trigger.start_at or after + delta
        if candidate <= after:
            candidate += ((after - candidate) // delta + 1) * delta
    else:
        zone = _timezone(trigger.timezone)
        cursor = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)
        candidate = None
        for _ in range(60 * 24 * 370):
            if _cron_matches(trigger.expression, cursor):
                candidate = cursor.astimezone(timezone.utc)
                break
            cursor += timedelta(minutes=1)
    if candidate is None or (end_at is not None and candidate > end_at):
        return None
    return candidate


def _missed(trigger: Trigger, scheduled_for: datetime, now: datetime) -> str | None:
    if isinstance(trigger, CronTrigger) and scheduled_for < now - timedelta(minutes=1):
        return "missed cron occurrence"
    if isinstance(trigger, IntervalTrigger):
        delta = timedelta(seconds=trigger.seconds)
        # Permit normal poll jitter at the exact one-period boundary.
        if now - scheduled_for > delta + timedelta(milliseconds=100):
            return "missed interval occurrence"
    return None


def _next_after_missed_interval(
    trigger: IntervalTrigger, scheduled_for: datetime, now: datetime
) -> datetime:
    delta = timedelta(seconds=trigger.seconds)
    return scheduled_for + ((now - scheduled_for) // delta + 1) * delta


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
        self._leader_lock = None
        self._reload()

    @property
    def schedules(self) -> list[Schedule]:
        with self._lock:
            self._reload()
            return list(self._schedules.values())

    @property
    def runs(self) -> list[ScheduledRun]:
        with self._lock:
            self._reload()
            return list(self._runs.values())

    def _append(self, record: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()

    def _reload(self) -> None:
        schedules: dict[str, Schedule] = {}
        runs: dict[str, ScheduledRun] = {}
        if not self.path.is_file():
            self._schedules = schedules
            self._runs = runs
            return
        text = self.path.read_text(encoding="utf-8")
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                if index == len(lines) - 1 and not text.endswith("\n"):
                    break
                raise
            op = record.get("op")
            if op in {"create", "update"}:
                schedule = _schedule_from_dict(record["schedule"])
                schedules[schedule.schedule_id] = schedule
            elif op == "delete":
                schedules.pop(str(record["schedule_id"]), None)
            elif op == "run":
                run = _run_from_dict(record["run"])
                runs[run.run_id] = run
        self._schedules = schedules
        self._runs = runs

    def create_schedule(self, *, trigger: Trigger, prompt: str, workspace: str, origin_session_id: str, schedule_session_id: str, execution_scope: ExecutionScope = ExecutionScope.WORKSPACE, end_at: datetime | None = None) -> Schedule:
        normalized_prompt = parse_schedule_prompt_input(prompt)
        if end_at is not None:
            _require_aware(end_at, "schedule end_at")
        now = datetime.now(timezone.utc)
        if isinstance(trigger, IntervalTrigger):
            next_run_at = _next_occurrence(trigger, end_at, now)
        else:
            next_run_at = _next_occurrence(trigger, end_at, now - timedelta(seconds=1))
        schedule = Schedule(
            str(uuid4()),
            trigger,
            AgentTurnAction(normalized_prompt),
            str(Path(workspace).expanduser().resolve()),
            origin_session_id,
            schedule_session_id,
            execution_scope,
            end_at=end_at,
            next_run_at=next_run_at,
        )
        with self._lock:
            self._reload()
            self._schedules[schedule.schedule_id] = schedule
            self._append({"op": "create", "schedule": _schedule_to_dict(schedule)})
        return schedule

    def update_schedule(
        self,
        schedule_id: str,
        *,
        prompt: str | _Unset = _UNSET,
        trigger: Trigger | _Unset = _UNSET,
        enabled: bool | _Unset = _UNSET,
        end_at: datetime | None | _Unset = _UNSET,
    ) -> Schedule:
        schedule_id = parse_schedule_id_input(schedule_id)
        with self._lock:
            self._reload()
            schedule = self._schedules.get(schedule_id)
            if schedule is None:
                raise ValueError("scheduled task not found")
            action = schedule.action
            updated_trigger = schedule.trigger
            updated_enabled = schedule.enabled
            updated_end_at = schedule.end_at
            if not isinstance(prompt, _Unset):
                action = AgentTurnAction(parse_schedule_prompt_input(prompt))
            if not isinstance(trigger, _Unset):
                if not isinstance(trigger, (OneShotTrigger, CronTrigger, IntervalTrigger)):
                    raise ValueError("trigger must be a supported trigger")
                updated_trigger = trigger
            if not isinstance(enabled, _Unset):
                updated_enabled = parse_schedule_enabled_input(enabled)
            if not isinstance(end_at, _Unset):
                if end_at is not None and not isinstance(end_at, datetime):
                    raise ValueError("end_at must be a datetime or null")
                if end_at is not None:
                    _require_aware(end_at, "schedule end_at")
                updated_end_at = end_at
            now = datetime.now(timezone.utc)
            if isinstance(updated_trigger, IntervalTrigger):
                next_run_at = _next_occurrence(updated_trigger, updated_end_at, now)
            else:
                next_run_at = _next_occurrence(
                    updated_trigger, updated_end_at, now - timedelta(seconds=1)
                )
            updated = replace(
                schedule,
                action=action,
                trigger=updated_trigger,
                enabled=updated_enabled,
                end_at=updated_end_at,
                next_run_at=next_run_at,
            )
            self._schedules[schedule_id] = updated
            self._append({"op": "update", "schedule": _schedule_to_dict(updated)})
            return updated

    def delete_schedule(self, schedule_id: str) -> None:
        schedule_id = parse_schedule_id_input(schedule_id)
        with self._lock:
            self._reload()
            schedule = self._schedules.get(schedule_id)
            if schedule is None:
                raise ValueError("scheduled task not found")
            self._schedules.pop(schedule_id)
            self._append({"op": "delete", "schedule_id": schedule_id})

    def next_occurrence(self, schedule: Schedule, after: datetime) -> datetime | None:
        _require_aware(after, "next occurrence after")
        return _next_occurrence(schedule.trigger, schedule.end_at, after)

    def poll_once(self, now: datetime | None = None) -> list[ScheduledRun]:
        now = now or datetime.now(timezone.utc)
        _require_aware(now, "scheduler poll time")
        due: list[tuple[Schedule, datetime, str | None]] = []
        with self._lock:
            self._reload()
            for schedule in self._schedules.values():
                if schedule.enabled and schedule.next_run_at and schedule.next_run_at <= now:
                    due.append(
                        (
                            schedule,
                            schedule.next_run_at,
                            _missed(schedule.trigger, schedule.next_run_at, now),
                        )
                    )
            for schedule, scheduled_for, missed in due:
                if missed:
                    if isinstance(schedule.trigger, IntervalTrigger):
                        candidate = _next_after_missed_interval(
                            schedule.trigger, scheduled_for, now
                        )
                        schedule.next_run_at = (
                            candidate
                            if schedule.end_at is None or candidate <= schedule.end_at
                            else None
                        )
                    else:
                        schedule.next_run_at = self.next_occurrence(schedule, now)
                else:
                    schedule.next_run_at = self.next_occurrence(schedule, scheduled_for)
                if isinstance(schedule.trigger, OneShotTrigger):
                    schedule.enabled = False
                self._append({"op": "update", "schedule": _schedule_to_dict(schedule)})
        created: list[ScheduledRun] = []
        for schedule, scheduled_for, missed_reason in due:
            run = ScheduledRun(str(uuid4()), schedule.schedule_id, schedule.schedule_session_id, str(uuid4()), scheduled_for)
            with self._lock:
                if missed_reason:
                    run.status, run.error = "skipped", missed_reason
                elif schedule.schedule_id in self._running:
                    run.status, run.error = "skipped", "previous scheduled run still running"
                elif self.runner is None:
                    run.status = "failed"
                    run.error = "scheduled task runner is unavailable"
                    run.started_at = now
                    run.finished_at = now
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
                self._runs[run.run_id] = run
                self._append({"op": "run", "run": _run_to_dict(run)})

    def start(self, interval: float = 1.0) -> threading.Thread:
        def loop() -> None:
            try:
                while not self._stop.is_set():
                    if self._leader_lock is not None or self._acquire_leader_lock():
                        self.poll_once()
                    self._stop.wait(interval)
            finally:
                self._release_leader_lock()
        thread = threading.Thread(target=loop, name="nosis-scheduler", daemon=True)
        thread.start()
        return thread

    def close(self) -> None:
        self._stop.set()

    def _acquire_leader_lock(self) -> bool:
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        try:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = lock_path.open("a+b")
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if "handle" in locals():
                handle.close()
            return False
        self._leader_lock = handle
        return True

    def _release_leader_lock(self) -> None:
        handle = self._leader_lock
        self._leader_lock = None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


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


__all__ = ["OneShotTrigger", "CronTrigger", "IntervalTrigger", "AgentTurnAction", "Schedule", "ScheduleUpdateInput", "ScheduledRun", "SchedulerService", "parse_end_at_input", "parse_schedule_enabled_input", "parse_schedule_id_input", "parse_schedule_prompt_input", "parse_schedule_update_input", "parse_trigger_input", "trigger_to_dict"]
