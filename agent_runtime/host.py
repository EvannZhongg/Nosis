"""Application runtime used directly by Bridge and scheduled workers."""

from concurrent.futures import Future
from dataclasses import replace
from functools import partial
from pathlib import Path
from queue import Queue
from threading import Lock
from typing import Callable

from agent_core import (
    AgentEvent, AttachmentPart, CompositeToolPolicy, ContextWindowEvent,
    McpApprovalPolicy,
    PermissionPreset, SchedulerService, SessionInUseError, ShellApprovalPolicy,
    ComputerApprovalPolicy, Workspace,
    FileWriteApprovalPolicy,
    UnitCompletedEvent,
)

from .callbacks import RuntimeCallbacks
from .config import load_model_options
from .plane_manager import ExecutionPlaneManager
from .session_controller import SessionRuntimeController
from .scheduled_turn import ScheduledTurn
from .services import RuntimeServices
from .turn_runner import TurnResult, TurnRunner


class RuntimeHost:
    def __init__(
        self,
        config_directory: Path,
        *,
        callbacks: RuntimeCallbacks | None = None,
        scheduler: SchedulerService | None = None,
        services: RuntimeServices | None = None,
    ) -> None:
        self.config_directory = config_directory.resolve()
        self.callbacks = callbacks or RuntimeCallbacks()
        runtime_callbacks = replace(self.callbacks, on_event=self.publish_event)
        self._owns_services = services is None
        self.services = services or RuntimeServices(
            self.config_directory, scheduler=scheduler,
        )
        self.environment = self.services.environment
        self.settings = self.services.settings
        self.scheduler = self.services.scheduler
        self.planes = ExecutionPlaneManager(
            self.config_directory, self.environment, self.scheduler,
            runtime_callbacks,
            session_permissions=lambda session_id: self.services.session_permissions(session_id, self.sessions),
        )
        self.sessions = SessionRuntimeController(
            self.config_directory, self.environment, runtime_callbacks,
            CompositeToolPolicy(
                ShellApprovalPolicy(self.callbacks.request_permission),
                FileWriteApprovalPolicy(self.callbacks.request_permission),
                ComputerApprovalPolicy(self.callbacks.request_permission),
                McpApprovalPolicy(
                    self._request_mcp_permission,
                    lambda name: (
                        self.planes.current.mcp.requires_approval(name)
                        if self.planes.current is not None else False
                    ),
                ),
            ),
        )
        self.turns = TurnRunner(runtime_callbacks)
        self._work: Queue[Callable[[], None] | None] = Queue()
        self._work_lock = Lock()
        self._scheduled: set[Future[None]] = set()

    def serve(self) -> None:
        while (work := self._work.get()) is not None:
            work()

    def submit(self, work: Callable[[], None]) -> None:
        with self._work_lock:
            if not self.turns.stopped:
                self._work.put(work)

    def submit_scheduled(self, turn: ScheduledTurn) -> None:
        with self._work_lock:
            stopping = self.turns.stopped
            if not stopping:
                self._scheduled.add(turn.completion)
                self._work.put(partial(self.run_scheduled, turn))
        if stopping:
            turn.completion.cancel()
        else:
            turn.completion.add_done_callback(self._scheduled_done)

    def _scheduled_done(self, completion: Future[None]) -> None:
        with self._work_lock:
            self._scheduled.discard(completion)

    @property
    def busy(self) -> bool:
        with self._work_lock:
            return bool(self._scheduled) or self.turns.busy

    def stop(self) -> None:
        with self._work_lock:
            if not self.turns.stopped:
                self.turns.shutdown()
                self._work.put(None)

    def _require_idle(self) -> None:
        if self.turns.turn_id is not None:
            raise RuntimeError("cannot change runtime during a turn")

    def open_session(self, workspace: Path, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        self._require_idle()
        # Observe an existing writer before opening or mutating session metadata.
        with self.services.session_lock:
            owner = self.services.sessions.get(session_id)
            if owner is not None and owner is not self.sessions:
                raise SessionInUseError(session_id)
            previous = self.sessions.session
            with self.sessions.open_session(workspace, session_id, provider) as resumed:
                self.planes.unbind_session()
            if previous is not None:
                self.services.sessions.pop(previous.session_id)
            self.services.sessions[self.sessions.session.session_id] = self.sessions
        return resumed

    def change_provider(self, provider: str) -> tuple[bool, str]:
        self._require_idle()
        if self.sessions.session is None:
            raise RuntimeError("session is not initialized")
        _, models = load_model_options(self.config_directory / "provider_config.json")
        if provider not in models:
            raise ValueError(f"provider '{provider}' is not configured")
        changed = provider != self.sessions.provider_name
        if changed:
            self.planes.close()
            self.sessions.change_provider(provider)
        return changed, models[provider]

    def change_workspace(self, path: Path) -> bool:
        self._require_idle()
        if self.sessions.session is None:
            raise RuntimeError("session is not initialized")
        workspace = Workspace(path)
        changed = workspace != self.sessions.workspace
        if changed:
            self.planes.close()
            with self.services.session_lock:
                self.sessions.change_workspace(workspace)
        return changed

    def refresh_runtime(self) -> tuple[str, str]:
        self._require_idle()
        try:
            plane = self.planes.ensure(self.sessions, force=True)
        except Exception:
            self.callbacks.on_phase("failed", ())
            raise
        self.callbacks.on_phase("idle", plane.runtime_warnings)
        plane.runtime_warnings = ()
        return plane.provider_name, plane.provider.model_name

    def set_permission_preset(self, preset: PermissionPreset) -> None:
        if self.sessions.permissions is None:
            raise RuntimeError("session is not initialized")
        self.sessions.permissions.set_preset(preset)

    def _request_mcp_permission(self, call) -> bool:
        plane = self.planes.current
        identity = plane.mcp.tool_identity(call.name) if plane is not None else None
        server, tool_name = identity if identity is not None else (None, call.name)
        return self.callbacks.request_permission(
            f"{call.name}({call.arguments})", kind="mcp", server=server, tool_name=tool_name,
        )

    def publish_event(self, event: AgentEvent, turn_id: str) -> None:
        plane = self.planes.current
        if isinstance(event, UnitCompletedEvent):
            if plane is not None and plane.title_task is not None:
                plane.title_task.start(event.start, event.end, turn_id)
            return
        if isinstance(event, ContextWindowEvent) and plane is not None:
            plane.context_window = event.window
        self.callbacks.on_event(event, turn_id)

    def run_turn(
        self, turn_id: str, text: str, *,
        attachments: Callable[[], tuple[AttachmentPart, ...]] = lambda: (),
        input_source: str = "user",
    ) -> TurnResult:
        self.sessions.record_workspace_activity()
        try:
            self.callbacks.on_turn_start(turn_id, text, input_source)
            result = self.turns.run_turn(
                turn_id, text,
                lambda: self.planes.ensure(self.sessions, cancellation=self.turns.control),
                attachments, input_source,
            )
            if input_source == "scheduled" and result.startup_failed:
                session = self.sessions.session
                if session is not None and turn_id not in session.turns:
                    session.begin_turn(turn_id)
                    session.add_item("user", text)
                    session.finish_turn("failed", turn_id, error=result.error)
        finally:
            self.sessions.record_workspace_activity()
        self.callbacks.on_turn_finish(turn_id, result)
        return result

    def run_scheduled(self, turn: ScheduledTurn) -> None:
        if self.turns.stopped:
            turn.completion.cancel()
            return
        if self.sessions.session is None:
            try:
                resumed = self.open_session(turn.workspace, turn.session_id)
            except (SessionInUseError, ValueError, OSError) as error:
                turn.completion.set_exception(error)
                self.callbacks.on_session_open_failed(turn.session_id, error)
                return
            self.callbacks.on_session_opened(resumed)
        try:
            if self.turns.stopped:
                turn.completion.cancel()
                return
            if self.sessions.session.session_id != turn.session_id:
                raise RuntimeError("scheduled turn belongs to another Session Runtime")
            turn.started()
            result = self.run_turn(turn.turn_id, turn.text, input_source="scheduled")
            if result.status == "cancelled":
                turn.completion.cancel()
            elif result.status == "failed":
                turn.completion.set_exception(RuntimeError(result.error.message))
            else:
                turn.completion.set_result(None)
        except Exception as error:
            turn.completion.set_exception(error)

    def close(self) -> None:
        self.stop()
        with self._work_lock:
            pending = tuple(self._scheduled)
        for completion in pending:
            completion.cancel()
        try:
            self.planes.close()
        finally:
            try:
                with self.services.session_lock:
                    if self.sessions.session is not None:
                        session_id = self.sessions.session.session_id
                        if self.services.sessions.get(session_id) is self.sessions:
                            self.services.sessions.pop(session_id)
                    self.sessions.close()
            finally:
                if self._owns_services:
                    self.services.close()
