from uuid import uuid4

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...execution.authority import ExecutionAuthority, ExecutionScope
from ...permissions import PermissionPreset
from ...scheduler import (
    SchedulerService,
    parse_end_at_input,
    parse_schedule_id_input,
    parse_schedule_prompt_input,
    parse_schedule_update_input,
    parse_trigger_input,
    trigger_to_dict,
)
from ...session_store import JsonlSessionStore


def _service(context: ToolExecutionContext) -> SchedulerService:
    if not isinstance(context.scheduler, SchedulerService):
        raise RuntimeError("scheduler is unavailable")
    return context.scheduler


def _definition_authority(context: ToolExecutionContext) -> ExecutionAuthority:
    router = context.execution_router
    if router is None:
        raise RuntimeError("scheduled task execution router is unavailable")
    return router.authority


def _execution_authority(context: ToolExecutionContext) -> ExecutionAuthority:
    execution = context.execution
    if execution is None:
        raise RuntimeError("scheduled task execution was not resolved")
    return execution.authority


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
            "type": {"type": "string", "enum": ["once", "interval", "cron"]},
            "at": {
                "type": "string",
                "format": "date-time",
                "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                "description": "Required for once. ISO-8601 date-time with an explicit UTC offset.",
            },
            "seconds": {
                "type": "integer",
                "minimum": 1,
                "description": "Required for interval. Elapsed seconds between runs; missed occurrences are skipped.",
            },
            "start_at": {
                "type": ["string", "null"],
                "format": "date-time",
                "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
                "description": "Optional interval start with an explicit UTC offset.",
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


def _date_time_schema(description: str) -> dict[str, object]:
    return {
        "type": ["string", "null"],
        "format": "date-time",
        "pattern": "(?:Z|[+-][0-9]{2}:[0-9]{2})$",
        "description": description,
    }


class ScheduledTaskTool(Tool):
    name = "scheduled_task"

    def available(self, context):
        return (
            isinstance(context.scheduler, SchedulerService)
            and context.execution_router is not None
        )

    def definition(self, context):
        authority = _definition_authority(context)
        scopes = [ExecutionScope.WORKSPACE.value]
        if authority.allows_unattended(ExecutionScope.HOST):
            scopes.append(ExecutionScope.HOST.value)
        schedule_schema = {
            "type": "object",
            "description": "Required only for create. The complete new plan.",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "What Nosis should do when the time arrives.",
                },
                "trigger": _trigger_schema(),
                "end_at": _date_time_schema(
                    "Optional end time for recurring plans, with an explicit UTC offset."
                ),
                "execution_scope": {
                    "type": "string",
                    "enum": scopes,
                    "default": ExecutionScope.WORKSPACE.value,
                    "description": "Unattended execution boundary. Workspace is the default; Host requires Full Access.",
                },
            },
            "required": ["prompt", "trigger"],
            "additionalProperties": False,
        }
        changes_schema = {
            "type": "object",
            "description": "Required only for update. Supply at least one field; omitted fields stay unchanged.",
            "minProperties": 1,
            "properties": {
                "prompt": {"type": "string"},
                "trigger": _trigger_schema(),
                "enabled": {
                    "type": "boolean",
                    "description": "False pauses the plan; true resumes it.",
                },
                "end_at": _date_time_schema(
                    "New end time with an explicit UTC offset, or null to remove it."
                ),
            },
            "additionalProperties": False,
        }
        return ToolDefinition(
            self.name,
            "Manage durable reminders and scheduled Agent tasks. "
            "Create requires schedule; update requires schedule_id and changes, "
            "preserving the session and run history; list takes only action; "
            "delete requires schedule_id and permanently removes the plan.",
            {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["create", "update", "list", "delete"],
                    },
                    "schedule": schedule_schema,
                    "schedule_id": {
                        "type": "string",
                        "description": "Required only for update or delete.",
                    },
                    "changes": changes_schema,
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        )

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        action = arguments.get("action")
        if action == "create":
            return self._create(arguments, context)
        if action == "update":
            return self._update(arguments, context)
        if action == "list":
            _reject_unknown_arguments(arguments, {"action"})
            return self._list(context)
        if action == "delete":
            _reject_unknown_arguments(arguments, {"action", "schedule_id"})
            schedule_id = parse_schedule_id_input(arguments.get("schedule_id"))
            _service(context).delete_schedule(schedule_id)
            return {"schedule_id": schedule_id, "deleted": True}
        raise ValueError("action must be create, update, list, or delete")

    def _create(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        _reject_unknown_arguments(arguments, {"action", "schedule"})
        data = arguments.get("schedule")
        if not isinstance(data, dict):
            raise ValueError("schedule must be an object")
        _reject_unknown_arguments(
            data, {"prompt", "trigger", "end_at", "execution_scope"}
        )
        prompt = parse_schedule_prompt_input(data.get("prompt"))
        trigger = parse_trigger_input(data.get("trigger"))
        end_at = parse_end_at_input(data.get("end_at"))
        try:
            scope = ExecutionScope(
                data.get("execution_scope", ExecutionScope.WORKSPACE.value)
            )
        except (TypeError, ValueError) as error:
            raise ValueError("execution_scope must be 'workspace' or 'host'") from error
        authority = _execution_authority(context)
        if (
            scope is ExecutionScope.HOST
            and not authority.allows_unattended(ExecutionScope.HOST)
        ):
            raise PermissionError("host scheduled tasks require Full Access")
        schedule_session_id = str(uuid4())
        store = JsonlSessionStore(context.sessions_directory)
        store.bind_workspace(schedule_session_id, context.workspace.path)
        store.set_permission_preset(
            schedule_session_id,
            PermissionPreset.FULL_ACCESS
            if scope is ExecutionScope.HOST
            else PermissionPreset.WORKSPACE_ACCESS,
            context.workspace.path,
        )
        provider = store.provider_for(context.session.session_id)
        if provider is not None:
            store.set_provider(schedule_session_id, provider, context.workspace.path)
        schedule = _service(context).create_schedule(
            trigger=trigger,
            prompt=prompt,
            workspace=str(context.workspace.path),
            origin_session_id=context.session.session_id,
            schedule_session_id=schedule_session_id,
            execution_scope=scope,
            end_at=end_at,
        )
        return {
            "schedule_id": schedule.schedule_id,
            "schedule_session_id": schedule.schedule_session_id,
            "workspace": schedule.workspace,
            "execution_scope": schedule.execution_scope.value,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }

    def _update(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        _reject_unknown_arguments(arguments, {"action", "schedule_id", "changes"})
        schedule_id = parse_schedule_id_input(arguments.get("schedule_id"))
        changes = arguments.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty object")
        parsed = parse_schedule_update_input(changes)
        schedule = _service(context).update_schedule(schedule_id, **parsed)
        return {
            "schedule_id": schedule.schedule_id,
            "schedule_session_id": schedule.schedule_session_id,
            "trigger": trigger_to_dict(schedule.trigger),
            "execution_scope": schedule.execution_scope.value,
            "enabled": schedule.enabled,
            "end_at": schedule.end_at.isoformat() if schedule.end_at else None,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }

    def _list(self, context: ToolExecutionContext):
        return [
            {
                "schedule_id": s.schedule_id,
                "prompt": s.action.prompt,
                "trigger": trigger_to_dict(s.trigger),
                "workspace": s.workspace,
                "execution_scope": s.execution_scope.value,
                "enabled": s.enabled,
                "end_at": s.end_at.isoformat() if s.end_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            }
            for s in _service(context).schedules
        ]
