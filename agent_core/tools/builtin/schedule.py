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


def _parse_permission_preset(value: object, context: ToolExecutionContext) -> PermissionPreset:
    try:
        preset = PermissionPreset(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "permission_preset must be 'ask_for_approval', 'workspace_access', or 'full_access'"
        ) from error
    if (
        preset is PermissionPreset.FULL_ACCESS
        and not _execution_authority(context).allows_unattended(ExecutionScope.HOST)
    ):
        raise PermissionError("Full Access scheduled tasks require Full Access")
    return preset


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
        presets = [
            PermissionPreset.ASK_FOR_APPROVAL.value,
            PermissionPreset.WORKSPACE_ACCESS.value,
        ]
        if authority.allows_unattended(ExecutionScope.HOST):
            presets.append(PermissionPreset.FULL_ACCESS.value)
        permission_schema = {
            "type": "string",
            "enum": presets,
            "description": (
                "Permission preset of the task's session, shared with interactive use "
                "and future scheduled runs. Full Access requires the caller to have Full Access. "
                "Unattended runs cannot grant interactive approvals."
            ),
        }
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
                "permission_preset": {
                    **permission_schema,
                    "description": permission_schema["description"] + " If omitted, inherits the caller session's current permission preset.",
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
                "permission_preset": permission_schema,
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
            data, {"prompt", "trigger", "end_at", "permission_preset"}
        )
        prompt = parse_schedule_prompt_input(data.get("prompt"))
        trigger = parse_trigger_input(data.get("trigger"))
        end_at = parse_end_at_input(data.get("end_at"))
        preset = _parse_permission_preset(
            data.get("permission_preset", context.session.permission_preset.value), context,
        )
        schedule_session_id = str(uuid4())
        store = JsonlSessionStore(context.sessions_directory)
        store.bind_workspace(schedule_session_id, context.workspace.path)
        store.set_permission_preset(
            schedule_session_id,
            preset,
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
            end_at=end_at,
        )
        return {
            "schedule_id": schedule.schedule_id,
            "schedule_session_id": schedule.schedule_session_id,
            "workspace": schedule.workspace,
            "permission_preset": preset.value,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }

    def _update(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        _reject_unknown_arguments(arguments, {"action", "schedule_id", "changes"})
        schedule_id = parse_schedule_id_input(arguments.get("schedule_id"))
        changes = arguments.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ValueError("changes must be a non-empty object")
        parsed = parse_schedule_update_input({
            key: value for key, value in changes.items() if key != "permission_preset"
        })
        preset = (
            _parse_permission_preset(changes["permission_preset"], context)
            if "permission_preset" in changes else None
        )
        service = _service(context)
        if preset is None:
            schedule = service.update_schedule(schedule_id, **parsed)
        else:
            schedule = next((item for item in service.schedules if item.schedule_id == schedule_id), None)
            if schedule is None:
                raise ValueError("scheduled task not found")
            if context.session_permissions is None:
                raise RuntimeError("session permission control is unavailable")
            # Acquire the Session writer before applying either part of the update.
            with context.session_permissions(schedule.schedule_session_id) as permissions:
                if parsed:
                    schedule = service.update_schedule(schedule_id, **parsed)
                permissions.set_preset(preset)
        store = JsonlSessionStore(context.sessions_directory)
        return {
            "schedule_id": schedule.schedule_id,
            "schedule_session_id": schedule.schedule_session_id,
            "trigger": trigger_to_dict(schedule.trigger),
            "permission_preset": store.permission_preset_for(schedule.schedule_session_id).value,
            "enabled": schedule.enabled,
            "end_at": schedule.end_at.isoformat() if schedule.end_at else None,
            "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
        }

    def _list(self, context: ToolExecutionContext):
        store = JsonlSessionStore(context.sessions_directory)
        return [
            {
                "schedule_id": s.schedule_id,
                "prompt": s.action.prompt,
                "trigger": trigger_to_dict(s.trigger),
                "workspace": s.workspace,
                "permission_preset": store.permission_preset_for(s.schedule_session_id).value,
                "enabled": s.enabled,
                "end_at": s.end_at.isoformat() if s.end_at else None,
                "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
            }
            for s in _service(context).schedules
        ]
