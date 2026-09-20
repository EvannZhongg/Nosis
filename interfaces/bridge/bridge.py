"""Agent runtime driven over newline-delimited JSON.

The TUI spawns this as a child process and exchanges protocol messages
on stdin/stdout. The class is kept free of process-level setup so tests
can drive it with plain string buffers.
"""

import json
import _thread
from collections import deque
from itertools import count
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import TextIO

from agent_core import (
    Agent,
    AgentEvent,
    AgentCancelled as Cancelled,
    ContextWindowEvent,
    JsonlSessionStore,
    JobManager,
    JobStatusEvent,
    Session,
    CompositeToolPolicy,
    DirectorySkillSource,
    McpConfig,
    McpApprovalPolicy,
    PermissionController,
    PermissionPreset,
    PlanManager,
    ShellApprovalPolicy,
    SkillLoader,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    HostCommandExecutor,
    SandboxedCommandExecutor,
    ToolExecutionContext,
    TurnControl,
    Workspace,
    WorkspaceInstructions,
    ImagePart,
    builtin_catalog,
    load_agent_config,
    merge_mcp_servers,
    message_to_dict,
    probe_image,
    runtime_error_info,
    vision_aware_tool_names,
    skill_aware_tool_names,
    platform_workspace_sandbox_backend,
)
from agent_core.prompting import render_system_prompt
from agent_core.path_utils import path_for_comparison
from agent_core.providers import LiteLLMProvider
from agent_core.mcp.manager import McpClientManager, McpServerStatus
from agent_core.tools import ROLE_TOOL_NAMES

from .config import (
    configured_role_names,
    default_config_directory,
    load_config_with_name,
    load_model_options,
    load_prompt_templates,
    load_vision_config,
)
from .execution_plane import ExecutionPlane
from .instructions import load_workspace_instructions
from .plugins import PluginAgent, PluginManager
from .settings import EnvironmentReloader, SettingsStore, configuration_fingerprint
from .protocol import (
    decode,
    encode,
    event_to_message,
    context_window_to_dict,
    runtime_state_message,
    runtime_failure_to_dict,
    plan_updated_message,
    session_ready_message,
    session_items_message,
    sessions_listed_message,
    settings_snapshot_message,
    settings_update_failed_message,
    usage_to_dict,
)


class Bridge:
    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._stdout_lock = Lock()
        self._messages: Queue[dict[str, object] | BaseException | None] = Queue()
        self._reader: Thread | None = None
        self._reader_lock = Lock()
        self._router_lock = Lock()
        self._waiters: dict[str, Queue[dict[str, object] | BaseException]] = {}
        self._queued_turn_ids: set[str] = set()
        self._pending_steers: dict[str, deque[tuple[str, str]]] = {}
        self._pending_cancels: set[str] = set()
        self._input_closed = False
        self._shutdown_requested = False
        self._session_opened = False
        self._interaction_ids = count(1)
        # The interface presents one approval/question at a time even when
        # parallel tools request several interactions concurrently.
        self._interaction_lock = Lock()
        self._turn_id: str | None = None
        self._turn_control: TurnControl | None = None
        self._session: Session | None = None
        self._store: JsonlSessionStore | None = None
        self._sessions_directory: Path | None = None
        self._workspace: Workspace | None = None
        self._permissions: PermissionController | None = None
        self._plan: PlanManager | None = None
        self._config_directory = default_config_directory().resolve()
        self._config_path = self._config_directory / "provider_config.json"
        self._agent_config_path = self._config_directory / "agent_config.json"
        self._environment = EnvironmentReloader(self._config_directory / ".env")
        self._settings = SettingsStore(self._config_directory, self._environment)
        self._provider_name: str | None = None
        self._execution_plane: ExecutionPlane | None = None
        self._approval: dict[str, object] | None = None
        self._question: dict[str, object] | None = None

    def emit(self, type: str, **fields: object) -> None:
        with self._stdout_lock:
            self._stdout.write(encode({"type": type, **fields}) + "\n")

    def read_message(self) -> dict[str, object] | None:
        """Return the next command routed by the sole stdin reader."""
        self._start_reader()
        message = self._messages.get()
        if isinstance(message, BaseException):
            raise message
        return message

    def _start_reader(self) -> None:
        with self._reader_lock:
            if self._reader is not None:
                return
            self._reader = Thread(
                target=self._read_stdin,
                name="bridge-input-reader",
                daemon=True,
            )
            self._reader.start()

    def _read_stdin(self) -> None:
        first_message = not self._session_opened
        while True:
            try:
                line = self._stdin.readline()
                if not line:
                    self._route_shutdown()
                    return
                stripped = line.strip()
                if stripped:
                    message = decode(stripped)
                    if first_message:
                        first_message = False
                        if message["type"] == "open_session":
                            self._messages.put(message)
                        else:
                            self._route_message(message)
                    else:
                        self._route_message(message)
                    if message["type"] == "shutdown":
                        return
            except (json.JSONDecodeError, ValueError) as error:
                self._messages.put(error)
                self._route_shutdown(notify_commands=False)
                return

    def _route_message(self, message: dict[str, object]) -> None:
        message_type = message["type"]
        if message_type in {"approval_response", "user_question_response"}:
            request_id = message.get("request_id")
            if not isinstance(request_id, str):
                return
            with self._router_lock:
                waiter = self._waiters.get(request_id)
                if waiter is not None:
                    waiter.put(message)
            return
        if message_type == "permission_set" and not self._session_opened:
            self._messages.put(message)
            return
        if message_type == "permission_set":
            self._set_permission_preset(message)
            return
        if message_type == "user_steer":
            self._route_steer(message)
            return
        if message_type == "cancel":
            self._route_cancel(message)
            return
        if message_type == "shutdown":
            self._route_shutdown()
            return
        if message_type == "user_turn":
            turn_id = message.get("turn_id")
            if isinstance(turn_id, str):
                with self._router_lock:
                    self._queued_turn_ids.add(turn_id)
        self._messages.put(message)

    def _set_permission_preset(self, message: dict[str, object]) -> None:
        preset = PermissionPreset(str(message.get("preset")))
        if self._permissions is None:
            raise RuntimeError("received 'permission_set' before 'open_session'")
        self._permissions.set_preset(preset)
        self.emit("permission_changed", preset=preset.value)

    def _set_provider(self, message: dict[str, object]) -> None:
        if self._session is None or self._store is None or self._workspace is None:
            raise RuntimeError("received 'provider_set' before 'open_session'")
        if self._turn_id is not None:
            raise RuntimeError("cannot change provider during a turn")
        provider = message.get("provider")
        if not isinstance(provider, str) or not provider:
            raise ValueError("provider must be a non-empty string")
        if self._config_path is None:
            raise RuntimeError("provider configuration is not initialized")
        _, models = load_model_options(self._config_path)
        if provider not in models:
            raise ValueError(f"provider '{provider}' is not configured")
        if provider == self._provider_name:
            self.emit("provider_changed", provider=provider, model=models[provider])
            return
        self._close_execution_plane()
        self._provider_name = provider
        self._store.set_provider(
            self._session.session_id,
            provider,
            self._workspace.path,
        )
        self.emit("provider_changed", provider=provider, model=models[provider])
        self._emit_runtime_state("inactive")

    def _persist_permission_preset(self, preset: PermissionPreset) -> None:
        if self._store is None or self._session is None or self._workspace is None:
            raise RuntimeError("session is not initialized")
        self._store.set_permission_preset(
            self._session.session_id,
            preset,
            self._workspace.path,
        )

    def _set_workspace(self, message: dict[str, object]) -> None:
        if self._session is None or self._store is None:
            raise RuntimeError("received 'workspace_set' before 'open_session'")
        if self._turn_id is not None:
            raise RuntimeError("cannot change workspace during a turn")
        value = message.get("workspace")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("workspace must be a non-empty path")
        workspace = Workspace(Path(value.strip()))
        if self._workspace is not None and workspace.path == self._workspace.path:
            self.emit("workspace_changed", workspace=str(workspace.path))
            return
        self._close_execution_plane()
        self._store.bind_workspace(self._session.session_id, workspace.path)
        self._workspace = workspace
        self._session.workspace = str(workspace.path)
        self._session.attach_journal_sink(
            lambda events: self._store.append_events(
                self._session.session_id,
                events,
                workspace=workspace.path,
            )
        )
        self.emit("workspace_changed", workspace=str(workspace.path))
        self._emit_runtime_state("inactive")

    def _route_steer(self, message: dict[str, object]) -> None:
        steer_id = message.get("steer_id")
        turn_id = message.get("turn_id")
        text = message.get("text")
        if not isinstance(steer_id, str) or not steer_id:
            return
        accepted = False
        if isinstance(text, str) and text.strip():
            with self._router_lock:
                control = self._turn_control
                active_turn_id = self._turn_id
                if control is not None and turn_id == active_turn_id:
                    accepted = control.steer(steer_id, text.strip())
                elif (
                    isinstance(turn_id, str)
                    and turn_id in self._queued_turn_ids
                ):
                    self._pending_steers.setdefault(
                        turn_id, deque()
                    ).append((steer_id, text.strip()))
                    accepted = True
        self.emit(
            "user_steer_received" if accepted else "user_steer_rejected",
            turn_id=turn_id,
            steer_id=steer_id,
            text=text if isinstance(text, str) else "",
        )

    def _route_cancel(self, message: dict[str, object]) -> None:
        turn_id = message.get("turn_id")
        with self._router_lock:
            control = self._turn_control
            if control is not None and turn_id == self._turn_id:
                cancelled = control.cancel()
                waiters = tuple(self._waiters.values()) if cancelled else ()
            else:
                cancelled = False
                waiters = ()
                if (
                    isinstance(turn_id, str)
                    and turn_id in self._queued_turn_ids
                ):
                    self._pending_cancels.add(turn_id)
        for waiter in waiters:
            waiter.put(Cancelled())
        if cancelled:
            _thread.interrupt_main()

    def _route_shutdown(self, *, notify_commands: bool = True) -> None:
        with self._router_lock:
            self._input_closed = True
            self._shutdown_requested = True
            control = self._turn_control
            cancelled = control.cancel() if control is not None else False
            waiters = tuple(self._waiters.values())
        for waiter in waiters:
            waiter.put(Cancelled())
        if cancelled:
            _thread.interrupt_main()
        if notify_commands:
            self._messages.put(None)

    def _register_interaction(
        self, request_id: str
    ) -> Queue[dict[str, object] | BaseException]:
        waiter: Queue[dict[str, object] | BaseException] = Queue()
        with self._router_lock:
            self._waiters[request_id] = waiter
            if (
                self._input_closed
                or (
                    self._turn_control is not None
                    and self._turn_control.cancelled
                )
            ):
                waiter.put(Cancelled())
        self._start_reader()
        return waiter

    def _unregister_interaction(self, request_id: str) -> None:
        with self._router_lock:
            self._waiters.pop(request_id, None)

    @staticmethod
    def _wait_for_interaction(
        waiter: Queue[dict[str, object] | BaseException],
    ) -> dict[str, object]:
        response = waiter.get()
        if isinstance(response, BaseException):
            raise response
        return response

    def request_permission(
        self,
        command: str,
        *,
        kind: str = "shell",
        server: str | None = None,
        tool_name: str | None = None,
    ) -> bool:
        # Serialized so concurrent tool calls queue their prompts instead of
        # racing for each other's answers; the user still answers one at a time.
        with self._interaction_lock:
            request_id = f"{self._turn_id}:{next(self._interaction_ids)}"
            waiter = self._register_interaction(request_id)
            self._approval = {
                "type": "approval_request",
                "turn_id": self._turn_id,
                "request_id": request_id,
                "command": command,
                "kind": kind,
                "server": server,
                "tool_name": tool_name,
            }
            self._question = None
            if self._session is not None:
                self._emit_runtime_state("waiting_approval")
            self.emit(**self._approval)
            try:
                message = self._wait_for_interaction(waiter)
                return bool(message.get("approved"))
            finally:
                self._unregister_interaction(request_id)
                self._approval = None
                if self._session is not None:
                    self._emit_runtime_state("running")

    def request_user_choice(
        self,
        question: str,
        options: list[dict[str, object]],
        allow_free_text: bool,
    ) -> object:
        with self._interaction_lock:
            request_id = f"{self._turn_id}:{next(self._interaction_ids)}"
            waiter = self._register_interaction(request_id)
            self._approval = None
            self._question = {
                "type": "user_question",
                "turn_id": self._turn_id,
                "request_id": request_id,
                "question": question,
                "options": options,
                "allow_free_text": allow_free_text,
            }
            if self._session is not None:
                self._emit_runtime_state("waiting_user")
            self.emit(**self._question)
            option_by_id = {
                str(option["id"]): option for option in options
            }
            try:
                while True:
                    message = self._wait_for_interaction(waiter)
                    option_id = message.get("option_id")
                    text = message.get("text")
                    if isinstance(option_id, str) and option_id in option_by_id:
                        option = option_by_id[option_id]
                        return {
                            "type": "option",
                            "id": option_id,
                            "label": option["label"],
                        }
                    if allow_free_text and isinstance(text, str) and text.strip():
                        return {"type": "text", "text": text.strip()}
            finally:
                self._unregister_interaction(request_id)
                self._question = None
                if self._session is not None:
                    self._emit_runtime_state("running")

    def request_mcp_permission(self, call) -> bool:
        plane = self._execution_plane
        identity = (
            plane.mcp.tool_identity(call.name)
            if plane is not None
            else None
        )
        server, tool_name = (
            identity if identity is not None else (None, call.name)
        )
        return self.request_permission(
            f"{call.name}({call.arguments})",
            kind="mcp",
            server=server,
            tool_name=tool_name,
        )

    def open_session(self, message: dict[str, object]) -> None:
        config_path = self._config_path
        self._environment.reload()

        workspace = Workspace(Path(str(message["workspace"])))
        provider = message.get("provider")
        default_provider, models = load_model_options(config_path)
        session_id = message.get("session_id")
        sessions_directory = self._config_directory / "sessions"
        self._sessions_directory = sessions_directory
        self._store = JsonlSessionStore(sessions_directory)
        stored_provider = (
            self._store.provider_for(str(session_id))
            if isinstance(session_id, str) and session_id
            else None
        )
        main_provider_name = (
            provider
            if isinstance(provider, str) and provider
            else stored_provider or default_provider
        )
        if main_provider_name not in models:
            raise ValueError(f"provider '{main_provider_name}' is not configured")

        resumed = isinstance(session_id, str) and bool(session_id)
        if resumed:
            bound_workspace = self._store.workspace_for(str(session_id))
            if bound_workspace:
                workspace = Workspace(Path(bound_workspace))
        self._session = (
            self._store.load(str(session_id), recover=False)
            if resumed
            else Session()
        )
        self._workspace = workspace
        # Runtime events are persisted as soon as they happen.  There is no
        # Bridge-side transcript checkpoint or tool-call repair buffer.
        self._store.bind_workspace(self._session.session_id, workspace.path)
        self._session.workspace = str(workspace.path)
        self._session.attach_journal_sink(
            lambda events: self._store.append_events(
                self._session.session_id, events, workspace=workspace.path
            )
        )
        self._session.recover()
        self._plan = PlanManager(
            self._session,
            lambda snapshot: self.emit(**plan_updated_message(snapshot)),
        )

        self._provider_name = main_provider_name
        approval_policy = CompositeToolPolicy(
            ShellApprovalPolicy(self.request_permission),
            McpApprovalPolicy(
                self.request_mcp_permission,
                lambda name: (
                    self._execution_plane.mcp.requires_approval(name)
                    if self._execution_plane is not None
                    else False
                ),
            ),
        )
        self._permissions = PermissionController(
            self._session,
            approval_policy,
            self._persist_permission_preset,
        )
        self._session_opened = True

        self.emit(**session_ready_message(
            session_id=self._session.session_id,
            workspace=str(workspace.path),
            provider=main_provider_name,
            model=models[main_provider_name],
            resumed=resumed,
            message_count=len(self._session.items),
            permission_preset=self._session.permission_preset.value,
        ))
        self._emit_runtime_state("inactive")

    def _ensure_execution_plane(self) -> ExecutionPlane:
        if (
            self._session is None
            or self._store is None
            or self._workspace is None
            or self._sessions_directory is None
            or self._provider_name is None
        ):
            raise RuntimeError("received 'user_turn' before 'open_session'")

        self._environment.reload()
        agent_config = load_agent_config(self._agent_config_path)
        config_fingerprint = configuration_fingerprint(
            self._config_directory
        )
        instructions = load_workspace_instructions(
            self._config_directory,
            self._workspace,
            agent_config.workspace_instruction_files,
        )
        plane = self._execution_plane
        if plane is not None:
            if plane.matches(
                workspace=self._workspace,
                provider_name=self._provider_name,
                agent_config=agent_config,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
            ):
                return plane
            self._close_execution_plane()

        self._emit_runtime_state("starting")
        config_path = self._config_path
        workspace = self._workspace
        _, config = load_config_with_name(config_path, self._provider_name)
        plugins = PluginManager.discover(
            self._config_directory / "plugins"
        )
        plugin_mcp_servers, mcp_warnings = (
            plugins.load_mcp_servers()
            if agent_config.mcp.enabled
            else ((), ())
        )
        plugin_agents, agent_warnings = plugins.load_agents()
        mcp_config = McpConfig(
            enabled=agent_config.mcp.enabled,
            servers=merge_mcp_servers(
                (agent_config.mcp.servers, plugin_mcp_servers)
            ),
        )
        mcp = McpClientManager(
            mcp_config,
            workspace.path,
            on_status=self._emit_mcp_status,
        )
        jobs: JobManager | None = None
        workspace_executor: SandboxedCommandExecutor | None = None
        host_executor: HostCommandExecutor | None = None
        try:
            skills = SkillLoader().load(
                (
                    DirectorySkillSource(self._config_directory / "skills"),
                    *plugins.skill_sources(),
                )
            )
            prompts = load_prompt_templates(self._config_directory / "prompts")

            main_provider = LiteLLMProvider(
                model=config.model,
                base_url=config.url,
                api_key=config.key,
                max_context_tokens=config.max_context_tokens,
                media_root=workspace.path,
            )
            vision_provider = self._provider_for(
                load_vision_config(config_path), workspace
            )

            # One catalog of stateless Tool instances is shared by the main
            # Agent and by every sub-agent role.
            catalog = builtin_catalog()
            catalog = catalog.extend(mcp.start())

            if self._permissions is None:
                raise RuntimeError("session permissions are not initialized")
            jobs = JobManager(self._session)
            workspace_executor = SandboxedCommandExecutor(
                workspace.path,
                platform_workspace_sandbox_backend(),
            )
            host_executor = HostCommandExecutor(workspace.path)
            jobs.set_update_callback(
                lambda update: self.emit(
                    **event_to_message(
                        JobStatusEvent(
                            job_id=update.job_id,
                            kind=update.kind,
                            status=update.status,
                        ),
                        update.turn_id,
                    )
                )
            )
            subagents = self._subagent_runtime(
                agent_config,
                catalog,
                config_path,
                workspace,
                self._permissions,
                prompts.subagent,
                prompts.consolidator,
                instructions,
                plugin_agents,
            )
            context = ToolExecutionContext(
                workspace=workspace,
                session=self._session,
                sessions_directory=self._sessions_directory,
                workspace_command_executor=workspace_executor,
                host_command_executor=host_executor,
                max_generation_tokens=agent_config.max_generation_tokens,
                vision_input=(
                    "image" in main_provider.capabilities.input_modalities
                ),
                vision_provider=vision_provider,
                mcp=mcp,
                subagents=subagents,
                jobs=jobs,
                skills=skills,
                plan=self._plan,
                ask_user=self.request_user_choice,
            )
            agent = Agent(
                provider=main_provider,
                session=self._session,
                system_prompt=render_system_prompt(
                    prompts.system,
                    workspace,
                    skills,
                    instructions=instructions,
                ),
                consolidator_prompt=prompts.consolidator,
                config=agent_config,
                tools=catalog.select(
                    (
                        *skill_aware_tool_names(
                            vision_aware_tool_names(
                                agent_config.tools.enabled,
                                main_provider,
                                vision_provider,
                            ),
                            skills,
                        ),
                        "ask_user",
                        "update_plan",
                        *mcp.tool_names,
                    ),
                    context,
                    policy=self._permissions,
                ),
                context=context,
            )
            plane = ExecutionPlane(
                workspace=workspace,
                provider_name=self._provider_name,
                agent_config=agent_config,
                configuration_fingerprint=config_fingerprint,
                instructions=instructions,
                agent=agent,
                workspace_executor=workspace_executor,
                host_executor=host_executor,
                jobs=jobs,
                mcp=mcp,
                context_window=agent.context_window(),
                runtime_warnings=(
                    *plugins.warnings,
                    *mcp_warnings,
                    *agent_warnings,
                    *skills.warnings,
                ),
            )
        except BaseException:
            try:
                if jobs is not None:
                    jobs.close()
            finally:
                try:
                    mcp.close()
                finally:
                    try:
                        if workspace_executor is not None:
                            workspace_executor.close()
                    finally:
                        if host_executor is not None:
                            host_executor.close()
            raise
        self._execution_plane = plane
        return plane

    def _close_execution_plane(self) -> None:
        plane = self._execution_plane
        self._execution_plane = None
        self._approval = None
        self._question = None
        if plane is not None:
            plane.close()

    def _emit_runtime_state(
        self,
        phase: str,
        *,
        runtime_warnings: tuple[str, ...] = (),
    ) -> None:
        plane = self._execution_plane
        jobs = (
            [
                job.to_dict()
                for job in plane.jobs.snapshot()
                if job.status in {"submitted", "running"}
            ]
            if plane is not None
            else []
        )
        self.emit(**runtime_state_message(
            phase=phase,
            turn_id=self._turn_id,
            approval=self._approval,
            question=self._question,
            provider=self._provider_name,
            permission_preset=(
                self._session.permission_preset.value
                if self._session is not None
                else PermissionPreset.ASK_FOR_APPROVAL.value
            ),
            context_window=(
                context_window_to_dict(plane.context_window)
                if plane is not None
                else None
            ),
            jobs=jobs,
            runtime_warnings=runtime_warnings,
            plan=self._plan.snapshot if self._plan is not None else None,
        ))

    def _emit_agent_event(self, event: AgentEvent, turn_id: str) -> None:
        plane = self._execution_plane
        if isinstance(event, ContextWindowEvent) and plane is not None:
            plane.context_window = event.window
        self.emit(**event_to_message(event, turn_id))

    def _emit_sessions(self) -> None:
        """Answer ``list_sessions`` with this Workspace's stored Sessions."""
        if self._store is None or self._workspace is None:
            raise RuntimeError("received 'list_sessions' before 'open_session'")
        self.emit(
            **sessions_listed_message(
                self._store.list_workspace_sessions(self._workspace.path)
            )
        )

    def _emit_session_items(self) -> None:
        """Answer ``load_session`` with the conversation to render."""
        if self._session is None:
            raise RuntimeError("received 'load_session' before 'open_session'")
        self.emit(
            **session_items_message(
                [message_to_dict(item) for item in self._session.items]
            )
        )

    def _settings_request_id(
        self, message: dict[str, object]
    ) -> str | None:
        value = message.get("request_id")
        return value if isinstance(value, str) and value else None

    def _emit_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            self.emit(**settings_snapshot_message(
                self._settings.snapshot(), request_id
            ))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))

    def _save_provider_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        provider = message.get("provider")
        settings = message.get("settings")
        try:
            if not isinstance(provider, str) or not provider:
                raise ValueError("provider must be a non-empty string")
            revision = message.get("expected_revision")
            snapshot = self._settings.save_provider(
                provider,
                settings,
                revision if isinstance(revision, str) else None,
            )
            self._environment.reload()
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))

    def _save_agent_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            revision = message.get("expected_revision")
            snapshot = self._settings.save_agent(
                message.get("settings"),
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))

    def _save_routing_settings(self, message: dict[str, object]) -> None:
        request_id = self._settings_request_id(message)
        try:
            revision = message.get("expected_revision")
            snapshot = self._settings.save_routing(
                message.get("settings"),
                revision if isinstance(revision, str) else None,
            )
            self.emit(**settings_snapshot_message(snapshot, request_id))
        except (OSError, ValueError) as error:
            self.emit(**settings_update_failed_message(error, request_id))

    def _provider_for(
        self,
        config,
        workspace: Workspace,
    ) -> LiteLLMProvider | None:
        if config is None:
            return None
        return LiteLLMProvider(
            model=config.model,
            base_url=config.url,
            api_key=config.key,
            max_context_tokens=config.max_context_tokens,
            media_root=workspace.path,
        )

    def _subagent_runtime(
        self,
        agent_config,
        catalog,
        config_path: Path,
        workspace: Workspace,
        permission_controller: PermissionController,
        subagent_prompt_template: str,
        consolidator_prompt: str,
        workspace_instructions: WorkspaceInstructions,
        plugin_agents: tuple[PluginAgent, ...],
    ) -> SubagentRuntime | None:
        """Build the sub-agent runtime, or None when no role is configured."""
        if not agent_config.tools.is_enabled("subagent"):
            return None
        # A provider override for a role that does not exist is a typo, not
        # a silent no-op: the two config files must name the same roles.
        known_roles = set(agent_config.subagent_roles)
        known_roles.update(agent.name for agent in plugin_agents)
        unknown = configured_role_names(config_path) - known_roles
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(
                "provider_config.json configures unknown subagent role(s): "
                f"{names}"
            )
        roles = [
            SubagentRole(
                name=name,
                description=role.description,
                tools=role.tools.enabled,
                provider=self._provider_for(
                    load_config_with_name(config_path, role=name)[1],
                    workspace,
                ),
                vision_provider=self._provider_for(
                    load_vision_config(config_path, role=name), workspace
                ),
            )
            for name, role in agent_config.subagent_roles.items()
            if role.enabled
        ]
        provider_names = set(load_model_options(config_path)[1])
        for agent in plugin_agents:
            declared_provider = (
                agent.model
                if agent.model not in {None, "inherit"}
                and agent.model in provider_names
                else None
            )
            provider_config = (
                load_config_with_name(config_path, declared_provider)[1]
                if declared_provider is not None
                else load_config_with_name(config_path, role=agent.name)[1]
            )
            roles.append(
                SubagentRole(
                    name=agent.name,
                    description=agent.description,
                    instructions=agent.instructions,
                    tools=(
                        ROLE_TOOL_NAMES
                        if agent.tools is None
                        else agent.tools
                    ),
                    provider=self._provider_for(provider_config, workspace),
                    vision_provider=self._provider_for(
                        load_vision_config(config_path, role=agent.name),
                        workspace,
                    ),
                )
            )
        registry = SubagentRoleRegistry(roles)
        if not registry:
            return None
        return SubagentRuntime(
            config=agent_config,
            catalog=catalog,
            roles=registry,
            subagent_prompt_template=subagent_prompt_template,
            consolidator_prompt=consolidator_prompt,
            workspace_instructions=workspace_instructions,
            tool_policy=permission_controller,
        )

    def _emit_mcp_status(self, status: McpServerStatus) -> None:
        fields: dict[str, object] = {
            "server": status.server,
            "status": status.status,
        }
        if status.tool_count is not None:
            fields["tool_count"] = status.tool_count
        if status.error is not None:
            fields["error"] = status.error
        self.emit("mcp_server_status", **fields)

    def run_turn(self, message: dict[str, object]) -> None:
        self._turn_id = str(message["turn_id"])
        try:
            plane = self._ensure_execution_plane()
        except Exception as error:
            self.emit(
                "turn_failed",
                turn_id=self._turn_id,
                error=runtime_failure_to_dict(runtime_error_info(error)),
            )
            self._turn_id = None
            self._emit_runtime_state("failed")
            return
        if self._session is None or self._store is None:
            raise RuntimeError("execution plane has no session")
        if self._workspace is None:
            raise RuntimeError("bridge workspace is not initialized")

        self._emit_runtime_state(
            "running",
            runtime_warnings=plane.runtime_warnings,
        )
        plane.runtime_warnings = ()
        control = TurnControl()
        with self._router_lock:
            self._turn_control = control
            self._queued_turn_ids.discard(self._turn_id)
            pending_steers = self._pending_steers.pop(
                self._turn_id, deque()
            )
            cancelled = (
                self._turn_id in self._pending_cancels
                or self._shutdown_requested
            )
            self._pending_cancels.discard(self._turn_id)
        for steer_id, text in pending_steers:
            control.steer(steer_id, text)
        if cancelled:
            control.cancel()
        try:
            attachments = _parse_attachments(
                message.get("attachments"), self._workspace
            )
            kwargs = {
                "on_event": lambda event: self._emit_agent_event(
                    event, self._turn_id or ""
                ),
                "attachments": attachments,
            }
            result = plane.agent.run(
                str(message["text"]),
                turn_id=self._turn_id,
                turn_control=control,
                **kwargs,
            )
        except KeyboardInterrupt:
            self.emit(
                "turn_cancelled",
                turn_id=self._turn_id,
            )
            self._turn_id = None
            return
        except Cancelled:
            self.emit(
                "turn_cancelled",
                turn_id=self._turn_id,
            )
            self._turn_id = None
            return
        except Exception as error:
            self.emit(
                "turn_failed",
                turn_id=self._turn_id,
                error=runtime_failure_to_dict(runtime_error_info(error)),
            )
            self._turn_id = None
            return
        finally:
            with self._router_lock:
                if self._turn_control is control:
                    self._turn_control = None
                shutdown_requested = self._shutdown_requested

        if shutdown_requested:
            return
        self.emit(
            "turn_completed",
            turn_id=self._turn_id,
            usage=usage_to_dict(result.response.usage),
        )
        self._turn_id = None

    def serve(self) -> None:
        opened = False
        while True:
            try:
                message = self.read_message()
            except (json.JSONDecodeError, ValueError) as error:
                self.emit(
                    "fatal",
                    error={
                        "type": "ProtocolError",
                        "message": str(error),
                        "details": {},
                    },
                )
                raise SystemExit(1)

            if message is None or message["type"] == "shutdown":
                return
            if not opened:
                if message["type"] != "open_session":
                    self.emit(
                        "fatal",
                        error={
                            "type": "ProtocolError",
                            "message": "first message must be 'open_session'",
                            "details": {},
                        },
                    )
                    raise SystemExit(1)
                self.open_session(message)
                opened = True
            elif message["type"] == "user_turn":
                self.run_turn(message)
            elif message["type"] == "permission_set":
                self._set_permission_preset(message)
            elif message["type"] == "provider_set":
                self._set_provider(message)
            elif message["type"] == "workspace_set":
                self._set_workspace(message)
            elif message["type"] == "list_sessions":
                self._emit_sessions()
            elif message["type"] == "load_session":
                self._emit_session_items()
            elif message["type"] == "settings_get":
                self._emit_settings(message)
            elif message["type"] == "settings_provider_save":
                self._save_provider_settings(message)
            elif message["type"] == "settings_agent_save":
                self._save_agent_settings(message)
            elif message["type"] == "settings_routing_save":
                self._save_routing_settings(message)

    def close(self) -> None:
        self._route_shutdown(notify_commands=False)
        self._close_execution_plane()
def _parse_attachments(value: object, workspace: Workspace) -> tuple[ImagePart, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError("user_turn.attachments must be an array")
    result = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "image":
            raise ValueError("attachments must contain image objects")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise ValueError("image attachment path must be a non-empty string")
        resolved = workspace.resolve_path(path)
        # The media type is taken from the file rather than the client's
        # claim: a provider handed the wrong one rejects the request, and
        # the size limit belongs on every route into the context.
        info = probe_image(resolved)
        result.append(
            ImagePart(
                path=path_for_comparison(resolved).relative_to(
                    path_for_comparison(workspace.path)
                ).as_posix(),
                mime_type=info.mime_type,
            )
        )
    return tuple(result)
