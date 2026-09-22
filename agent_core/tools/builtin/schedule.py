from uuid import uuid4

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...scheduler import (
    SchedulerService,
    parse_end_at_input,
    parse_schedule_id_input,
    parse_schedule_prompt_input,
    parse_schedule_update_input,
    parse_trigger_input,
    trigger_to_dict,
)
from ...execution import WORKSPACE_ACCESS_AUTHORITY
from ...permissions import PermissionPreset
from ...session_store import JsonlSessionStore


def _service(context: ToolExecutionContext) -> SchedulerService:
    if not isinstance(context.scheduler, SchedulerService):
        raise RuntimeError("scheduler is unavailable")
    return context.scheduler


def _reject_unknown_arguments(
    arguments: dict[str, JSONValue], allowed: set[str]
) -> None:
    unknown = set(arguments) - allowed
    if unknown:
        raise ValueError(f"unsupported argument(s): {', '.join(sorted(unknown))}")


def _trigger_schema() -> dict[str, object]:
    return {
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
    }


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
                    "trigger": _trigger_schema(),
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
        _reject_unknown_arguments(arguments, {"prompt", "trigger", "end_at"})
        prompt = parse_schedule_prompt_input(arguments.get("prompt"))
        parsed = parse_trigger_input(arguments.get("trigger"))
        end = parse_end_at_input(arguments.get("end_at"))
        schedule_session_id = str(uuid4())
        store = JsonlSessionStore(context.sessions_directory)
        store.bind_workspace(schedule_session_id, context.workspace.path)
        store.set_permission_preset(
            schedule_session_id,
            PermissionPreset.from_authority(
                context.session.permission_preset.authority.intersect(
                    WORKSPACE_ACCESS_AUTHORITY
                )
            ),
            context.workspace.path,
        )
        provider = store.provider_for(context.session.session_id)
        if provider is not None:
            store.set_provider(
                schedule_session_id,
                provider,
                context.workspace.path,
            )
        schedule = _service(context).create_schedule(trigger=parsed, prompt=prompt, workspace=str(context.workspace.path), origin_session_id=context.session.session_id, schedule_session_id=schedule_session_id, end_at=end)
        return {"schedule_id": schedule.schedule_id, "schedule_session_id": schedule.schedule_session_id, "workspace": schedule.workspace, "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None}


class UpdateScheduledTaskTool(Tool):
    name = "update_scheduled_task"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(
            self.name,
            "Explicitly modify a previously created future plan while preserving its session and run history.",
            {
                "type": "object",
                "properties": {
                    "schedule_id": {"type": "string"},
                    "prompt": {"type": "string", "description": "New instruction for future runs."},
                    "trigger": _trigger_schema(),
                    "enabled": {"type": "boolean", "description": "Whether the plan is active."},
                    "end_at": {
                        "type": ["string", "null"],
                        "format": "date-time",
                        "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                        "description": "New end time for recurring plans, or null to remove it.",
                    },
                },
                "required": ["schedule_id"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments, context):
        schedule_id = parse_schedule_id_input(arguments.get("schedule_id"))
        changes = parse_schedule_update_input(
            {key: value for key, value in arguments.items() if key != "schedule_id"}
        )
        schedule = _service(context).update_schedule(schedule_id, **changes)
        return {
            "schedule_id": schedule.schedule_id,
            "schedule_session_id": schedule.schedule_session_id,
            "trigger": trigger_to_dict(schedule.trigger),
            "enabled": schedule.enabled,
            "end_at": schedule.end_at.isoformat() if schedule.end_at else None,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }


class ListScheduledTasksTool(Tool):
    name = "list_scheduled_tasks"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(self.name, "List future reminders and scheduled Agent tasks.", {"type": "object", "properties": {}, "additionalProperties": False})

    def execute(self, arguments, context):
        _reject_unknown_arguments(arguments, set())
        return [
            {
                "schedule_id": s.schedule_id,
                "prompt": s.action.prompt,
                "trigger": trigger_to_dict(s.trigger),
                "workspace": s.workspace,
                "enabled": s.enabled,
                "end_at": s.end_at.isoformat() if s.end_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            }
            for s in _service(context).schedules
        ]


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
        _reject_unknown_arguments(arguments, {"schedule_id"})
        schedule_id = parse_schedule_id_input(arguments.get("schedule_id"))
        _service(context).delete_schedule(schedule_id)
        return {"schedule_id": schedule_id, "deleted": True}
