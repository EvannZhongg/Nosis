"""Scheduler adapter that executes through RuntimeHost without a transport."""

from pathlib import Path

from agent_core import (
    ExecutionScope, FULL_ACCESS_AUTHORITY, JsonlSessionStore, PermissionPreset,
    Schedule, ScheduledRun, SchedulerService, Session, WORKSPACE_ONLY_AUTHORITY,
    runtime_error_info,
)

from .host import RuntimeHost


class ScheduledTurnRunner:
    def __init__(self, config_directory: Path, scheduler: SchedulerService) -> None:
        self.config_directory = config_directory
        self.scheduler = scheduler

    def __call__(self, schedule: Schedule, run: ScheduledRun) -> None:
        worker = RuntimeHost(
            self.config_directory,
            scheduler=self.scheduler,
            execution_authority_limit=(
                FULL_ACCESS_AUTHORITY
                if schedule.execution_scope is ExecutionScope.HOST
                else WORKSPACE_ONLY_AUTHORITY
            ),
        )
        try:
            worker.open_session(Path(schedule.workspace), session_id=run.session_id)
            worker.set_permission_preset(
                PermissionPreset.FULL_ACCESS
                if schedule.execution_scope is ExecutionScope.HOST
                else PermissionPreset.WORKSPACE_ACCESS
            )
            result = worker.run_turn(run.turn_id, schedule.action.prompt)
            if result.status != "completed":
                raise RuntimeError(
                    result.error.message if result.error is not None
                    else "scheduled turn did not complete"
                )
        except Exception as error:
            session = worker.sessions.session
            if session is None:
                store = JsonlSessionStore(self.config_directory / "sessions")
                store.bind_workspace(run.session_id, Path(schedule.workspace))
                session = Session(run.session_id, workspace=schedule.workspace)
                session.attach_journal_sink(
                    lambda events: store.append_events(
                        run.session_id, events, workspace=schedule.workspace,
                    )
                )
            if run.turn_id not in session.turns:
                session.begin_turn(run.turn_id)
                session.add_item("user", schedule.action.prompt)
                session.finish_turn("failed", run.turn_id, error=runtime_error_info(error))
            raise
        finally:
            worker.close()
