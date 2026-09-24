"""GUI schedule endpoints."""

from fastapi import APIRouter, Body, HTTPException

from agent_core import (
    JsonlSessionStore,
    SchedulerService,
    parse_schedule_update_input,
    trigger_to_dict,
)
from agent_runtime.settings import SettingsStore


def create_schedules_router(
    settings: SettingsStore,
    store: JsonlSessionStore,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/schedules")
    def list_schedules() -> list[dict[str, object]]:
        current = SchedulerService(settings.directory / "schedule.jsonl")
        latest_runs = {}
        for run in current.runs:
            previous = latest_runs.get(run.schedule_id)
            if previous is None or run.scheduled_for > previous.scheduled_for:
                latest_runs[run.schedule_id] = run
        return [
            {
                "schedule_id": item.schedule_id,
                "prompt": item.action.prompt,
                "trigger": trigger_to_dict(item.trigger),
                "workspace": item.workspace,
                "execution_scope": item.execution_scope.value,
                "origin_session_id": item.origin_session_id,
                "schedule_session_id": item.schedule_session_id,
                "session_available": store.has_journal(item.schedule_session_id),
                "enabled": item.enabled,
                "end_at": item.end_at.isoformat() if item.end_at else None,
                "next_run_at": (
                    item.next_run_at.isoformat() if item.next_run_at else None
                ),
                "latest_run": (
                    {
                        "run_id": latest_runs[item.schedule_id].run_id,
                        "status": latest_runs[item.schedule_id].status,
                        "scheduled_for": latest_runs[
                            item.schedule_id
                        ].scheduled_for.isoformat(),
                        "started_at": (
                            latest_runs[item.schedule_id].started_at.isoformat()
                            if latest_runs[item.schedule_id].started_at
                            else None
                        ),
                        "finished_at": (
                            latest_runs[item.schedule_id].finished_at.isoformat()
                            if latest_runs[item.schedule_id].finished_at
                            else None
                        ),
                        "error": latest_runs[item.schedule_id].error,
                    }
                    if item.schedule_id in latest_runs
                    else None
                ),
            }
            for item in current.schedules
        ]

    @router.put("/api/schedules/{schedule_id}")
    def update_schedule(
        schedule_id: str,
        payload: dict[str, object] = Body(...),
    ) -> dict[str, object]:
        try:
            current = SchedulerService(settings.directory / "schedule.jsonl")
            changes = parse_schedule_update_input(payload)
            item = current.update_schedule(schedule_id, **changes)
            return {
                "schedule_id": item.schedule_id,
                "schedule_session_id": item.schedule_session_id,
                "trigger": trigger_to_dict(item.trigger),
                "execution_scope": item.execution_scope.value,
                "enabled": item.enabled,
                "end_at": item.end_at.isoformat() if item.end_at else None,
                "next_run_at": (
                    item.next_run_at.isoformat() if item.next_run_at else None
                ),
            }
        except (KeyError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return router
