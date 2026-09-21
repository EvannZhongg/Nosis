from datetime import datetime

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...scheduler import CronTrigger, IntervalTrigger, OneShotTrigger, SchedulerService
from ...session_store import JsonlSessionStore
from uuid import uuid4


def _service(context: ToolExecutionContext) -> SchedulerService:
    if not isinstance(context.scheduler, SchedulerService):
        raise RuntimeError("scheduler is unavailable")
    return context.scheduler


class CreateScheduledTaskTool(Tool):
    name = "create_scheduled_task"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(
            self.name,
            "Create a durable future reminder or scheduled Agent task. Supports one-time and recurring plans.",
            {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What Nosis should do when the time arrives.",
                    },
                    "trigger": {
                        "type": "object",
                        "description": "When the task should run.",
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": ["once", "interval", "cron"],
                            },
                            "at": {
                                "type": "string",
                                "format": "date-time",
                                "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                                "description": "Required for once. ISO-8601 date-time with an explicit UTC offset, such as 2099-01-01T09:00:00+08:00.",
                            },
                            "seconds": {
                                "type": "integer",
                                "minimum": 1,
                                "description": "Required for interval. Elapsed seconds between runs; for example 30, 90, or 5400. Polls arriving more than one interval period late skip missed occurrences and retain the next boundary.",
                            },
                            "start_at": {
                                "type": ["string", "null"],
                                "format": "date-time",
                                "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                                "description": "Optional interval start, with an explicit UTC offset.",
                            },
                            "expression": {
                                "type": "string",
                                "description": "Required for cron. Five-field cron expression with minute precision.",
                            },
                            "timezone": {
                                "type": "string",
                                "description": "IANA timezone name, such as Asia/Shanghai. Defaults to UTC.",
                            },
                        },
                        "required": ["type"],
                        "additionalProperties": False,
                    },
                    "end_at": {
                        "type": ["string", "null"],
                        "format": "date-time",
                        "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                        "description": "Optional end time for recurring plans, with an explicit UTC offset.",
                    },
                },
                "required": ["prompt", "trigger"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        trigger = arguments.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("trigger must be an object")
        kind = trigger.get("type")
        if kind == "once":
            at_value = trigger.get("at")
            if not isinstance(at_value, str) or not at_value:
                raise ValueError(
                    'Error: once trigger requires \'at\'. Retry with at="<ISO-8601 with UTC offset>".'
                )
            at = datetime.fromisoformat(at_value)
            parsed = OneShotTrigger(at)
        elif kind == "cron":
            expression = trigger.get("expression")
            if not isinstance(expression, str) or not expression:
                raise ValueError(
                    'Error: cron trigger requires \'expression\'. Retry with expression="<five-field cron>".'
                )
            parsed = CronTrigger(expression, str(trigger.get("timezone", "UTC")))
        elif kind == "interval":
            start_at = trigger.get("start_at")
            if start_at is not None and not isinstance(start_at, str):
                raise ValueError("interval trigger start_at must be an ISO-8601 string or null")
            parsed = IntervalTrigger(
                trigger.get("seconds"),  # type: ignore[arg-type]
                datetime.fromisoformat(start_at) if start_at is not None else None,
            )
        else:
            raise ValueError("trigger.type must be once, cron, or interval")
        end_at = arguments.get("end_at")
        end = datetime.fromisoformat(str(end_at)) if isinstance(end_at, str) else None
        schedule_session_id = str(uuid4())
        store = JsonlSessionStore(context.sessions_directory)
        store.bind_workspace(schedule_session_id, context.workspace.path)
        store.set_permission_preset(
            schedule_session_id,
            context.session.permission_preset,
            context.workspace.path,
        )
        provider = store.provider_for(context.session.session_id)
        if provider is not None:
            store.set_provider(
                schedule_session_id,
                provider,
                context.workspace.path,
            )
        schedule = _service(context).create_schedule(trigger=parsed, prompt=str(arguments["prompt"]), workspace=str(context.workspace.path), origin_session_id=context.session.session_id, schedule_session_id=schedule_session_id, end_at=end)
        return {"schedule_id": schedule.schedule_id, "schedule_session_id": schedule.schedule_session_id, "workspace": schedule.workspace, "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None}


class UpdateScheduledTaskTool(Tool):
    name = "update_scheduled_task"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(self.name, "Explicitly modify a previously created future plan.", {"type": "object", "properties": {"schedule_id": {"type": "string"}, "prompt": {"type": "string", "description": "New instruction for future runs."}, "enabled": {"type": "boolean", "description": "Whether the plan is active."}}, "required": ["schedule_id"], "additionalProperties": False})

    def execute(self, arguments, context):
        try:
            schedule = _service(context).update_schedule(str(arguments["schedule_id"]), **{k: arguments[k] for k in ("prompt", "enabled") if k in arguments})
        except KeyError as error:
            raise ValueError("scheduled task not found") from error
        return {"schedule_id": schedule.schedule_id, "enabled": schedule.enabled, "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None}


class ListScheduledTasksTool(Tool):
    name = "list_scheduled_tasks"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(self.name, "List future reminders and scheduled Agent tasks.", {"type": "object", "properties": {}, "additionalProperties": False})

    def execute(self, arguments, context):
        return [{"schedule_id": s.schedule_id, "prompt": s.action.prompt, "workspace": s.workspace, "enabled": s.enabled, "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None} for s in _service(context).schedules]


class DeleteScheduledTaskTool(Tool):
    name = "delete_scheduled_task"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(
            self.name,
            "Permanently delete a future reminder or scheduled Agent task.",
            {
                "type": "object",
                "properties": {"schedule_id": {"type": "string"}},
                "required": ["schedule_id"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments, context):
        schedule_id = str(arguments["schedule_id"])
        try:
            _service(context).delete_schedule(schedule_id)
        except KeyError as error:
            raise ValueError("scheduled task not found") from error
        return {"schedule_id": schedule_id, "deleted": True}
