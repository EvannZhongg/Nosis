from datetime import datetime

from ..base import JSONValue, Tool, ToolDefinition
from ..context import ToolExecutionContext
from ...scheduler import CronTrigger, OneShotTrigger, SchedulerService
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
        return ToolDefinition(self.name, "Create a durable future reminder or scheduled Agent task. Supports one-time and recurring plans.", {"type": "object", "properties": {"prompt": {"type": "string", "description": "What Nosis should do when the time arrives."}, "trigger": {"type": "object", "description": "Use once with an ISO-8601 at, or cron with expression and timezone."}, "end_at": {"type": ["string", "null"], "description": "Optional end time for recurring plans."}}, "required": ["prompt", "trigger"], "additionalProperties": False})

    def execute(self, arguments: dict[str, JSONValue], context: ToolExecutionContext):
        trigger = arguments.get("trigger")
        if not isinstance(trigger, dict):
            raise ValueError("trigger must be an object")
        kind = trigger.get("type")
        if kind == "once":
            at = datetime.fromisoformat(str(trigger["at"]))
            parsed = OneShotTrigger(at)
        elif kind == "cron":
            parsed = CronTrigger(str(trigger["expression"]), str(trigger.get("timezone", "UTC")))
        else:
            raise ValueError("trigger.type must be once or cron")
        end_at = arguments.get("end_at")
        end = datetime.fromisoformat(str(end_at)) if isinstance(end_at, str) else None
        schedule_session_id = str(uuid4())
        JsonlSessionStore(context.sessions_directory).bind_workspace(schedule_session_id, context.workspace.path)
        schedule = _service(context).create_schedule(trigger=parsed, prompt=str(arguments["prompt"]), workspace=str(context.workspace.path), origin_session_id=context.session.session_id, schedule_session_id=schedule_session_id, end_at=end)
        return {"schedule_id": schedule.schedule_id, "schedule_session_id": schedule.schedule_session_id, "workspace": schedule.workspace, "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None}


class UpdateScheduledTaskTool(Tool):
    name = "update_scheduled_task"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(self.name, "Explicitly modify a previously created future plan.", {"type": "object", "properties": {"schedule_id": {"type": "string"}, "prompt": {"type": "string", "description": "New instruction for future runs."}, "enabled": {"type": "boolean", "description": "Whether the plan is active."}}, "required": ["schedule_id"], "additionalProperties": False})

    def execute(self, arguments, context):
        schedule = _service(context).update_schedule(str(arguments["schedule_id"]), **{k: arguments[k] for k in ("prompt", "enabled") if k in arguments})
        return {"schedule_id": schedule.schedule_id, "enabled": schedule.enabled, "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None}


class ListScheduledTasksTool(Tool):
    name = "list_scheduled_tasks"

    def available(self, context):
        return isinstance(context.scheduler, SchedulerService)

    def definition(self, context):
        return ToolDefinition(self.name, "List future reminders and scheduled Agent tasks.", {"type": "object", "properties": {}, "additionalProperties": False})

    def execute(self, arguments, context):
        return [{"schedule_id": s.schedule_id, "prompt": s.action.prompt, "workspace": s.workspace, "enabled": s.enabled, "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None} for s in _service(context).schedules]
