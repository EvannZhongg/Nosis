"""Application runtime used directly by Bridge and scheduled workers."""

from dataclasses import replace
from pathlib import Path
from typing import Callable

from agent_core import (
    AgentEvent, AttachmentPart, CompositeToolPolicy, ContextWindowEvent,
    ExecutionAuthority, FULL_ACCESS_AUTHORITY, McpApprovalPolicy,
    PermissionPreset, SchedulerService, ShellApprovalPolicy, Workspace,
)

from .callbacks import RuntimeCallbacks
from .config import load_model_options
from .plane_manager import ExecutionPlaneManager
from .session_controller import SessionRuntimeController
from .settings import EnvironmentReloader, SettingsStore
from .turn_runner import TurnResult, TurnRunner


class RuntimeHost:
    def __init__(
        self,
        config_directory: Path,
        *,
        callbacks: RuntimeCallbacks | None = None,
        scheduler: SchedulerService | None = None,
        start_scheduler: bool = True,
        execution_authority_limit: ExecutionAuthority = FULL_ACCESS_AUTHORITY,
    ) -> None:
        self.config_directory = config_directory.resolve()
        self.callbacks = callbacks or RuntimeCallbacks()
        runtime_callbacks = replace(self.callbacks, on_event=self.publish_event)
        self.environment = EnvironmentReloader(self.config_directory / ".env")
        self.settings = SettingsStore(self.config_directory, self.environment)
        self._owns_scheduler = scheduler is None
        self.scheduler = scheduler or SchedulerService(
            self.config_directory / "schedule.jsonl",
        )
        self.planes = ExecutionPlaneManager(
            self.config_directory, self.environment, self.scheduler,
            runtime_callbacks, execution_authority_limit,
        )
        self.sessions = SessionRuntimeController(
            self.config_directory, self.environment, runtime_callbacks,
            CompositeToolPolicy(
                ShellApprovalPolicy(self.callbacks.request_permission),
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
        from .scheduled_runner import ScheduledTurnRunner

        self.scheduler.runner = ScheduledTurnRunner(
            self.config_directory, self.scheduler
        )
        if self._owns_scheduler and start_scheduler:
            self.scheduler.start()

    def _require_idle(self) -> None:
        if self.turns.turn_id is not None:
            raise RuntimeError("cannot change runtime during a turn")

    def open_session(self, workspace: Path, session_id: str | None = None,
                     provider: str | None = None) -> bool:
        self._require_idle()
        self.planes.close()
        return self.sessions.open_session(workspace, session_id, provider)

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
            self.sessions.change_workspace(workspace)
        return changed

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
        if isinstance(event, ContextWindowEvent) and plane is not None:
            plane.context_window = event.window
        self.callbacks.on_event(event, turn_id)

    def run_turn(
        self, turn_id: str, text: str, *,
        attachments: Callable[[], tuple[AttachmentPart, ...]] = lambda: (),
    ) -> TurnResult:
        return self.turns.run_turn(
            turn_id, text, lambda: self.planes.ensure(self.sessions), attachments,
        )

    def close(self) -> None:
        self.turns.shutdown()
        try:
            self.planes.close()
        finally:
            if self._owns_scheduler:
                self.scheduler.close()
