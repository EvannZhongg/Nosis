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

from dotenv import load_dotenv

from agent_core import (
    Agent,
    AgentCancelled as Cancelled,
    JsonlSessionStore,
    JobManager,
    JobStatusEvent,
    Session,
    CompositeToolPolicy,
    McpApprovalPolicy,
    PermissionController,
    PermissionPreset,
    ShellApprovalPolicy,
    SkillRegistry,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    SubprocessCommandExecutor,
    ToolExecutionContext,
    TurnControl,
    Workspace,
    ImagePart,
    builtin_catalog,
    load_agent_config,
    message_to_dict,
    probe_image,
    vision_aware_tool_names,
    skill_aware_tool_names,
)
from agent_core.prompts import load_system_prompt
from agent_core.providers import LiteLLMProvider
from agent_core.mcp.manager import McpClientManager, McpServerStatus

from .config import (
    configured_role_names,
    load_config_with_name,
    load_vision_config,
)
from .protocol import (
    decode,
    encode,
    event_to_message,
    context_window_to_dict,
    ready_message,
    session_items_message,
    sessions_listed_message,
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
        self._interaction_ids = count(1)
        # The interface presents one approval/question at a time even when
        # parallel tools request several interactions concurrently.
        self._interaction_lock = Lock()
        self._turn_id: str | None = None
        self._turn_control: TurnControl | None = None
        self._agent: Agent | None = None
        self._session: Session | None = None
        self._store: JsonlSessionStore | None = None
        self._mcp: McpClientManager | None = None
        self._workspace: Workspace | None = None
        self._permissions: PermissionController | None = None
        self._jobs: JobManager | None = None

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
        first_message = self._agent is None
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
                        if message["type"] == "start":
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
        if message_type == "permission_set":
            if self._permissions is None:
                self._messages.put(message)
            else:
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
        controller = self._permissions
        if controller is None:
            raise RuntimeError("received 'permission_set' before 'start'")
        controller.set_preset(preset)
        self.emit("permission_changed", preset=preset.value)

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
            self.emit(
                "approval_request",
                turn_id=self._turn_id,
                request_id=request_id,
                command=command,
                kind=kind,
                server=server,
                tool_name=tool_name,
            )
            try:
                message = self._wait_for_interaction(waiter)
                return bool(message.get("approved"))
            finally:
                self._unregister_interaction(request_id)

    def request_user_choice(
        self,
        question: str,
        options: list[dict[str, object]],
        allow_free_text: bool,
    ) -> object:
        with self._interaction_lock:
            request_id = f"{self._turn_id}:{next(self._interaction_ids)}"
            waiter = self._register_interaction(request_id)
            self.emit(
                "user_question",
                turn_id=self._turn_id,
                request_id=request_id,
                question=question,
                options=options,
                allow_free_text=allow_free_text,
            )
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

    def request_mcp_permission(self, call) -> bool:
        identity = (
            self._mcp.tool_identity(call.name)
            if self._mcp is not None
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

    def start(self, message: dict[str, object]) -> None:
        config_path = Path(str(message["provider_config_path"])).expanduser().resolve()
        agent_config_path = Path(str(message["agent_config_path"])).expanduser().resolve()
        load_dotenv(config_path.parent / ".env")

        workspace = Workspace(Path(str(message["workspace"])))
        provider = message.get("provider")
        main_provider_name, config = load_config_with_name(
            config_path,
            provider if isinstance(provider, str) and provider else None,
        )
        agent_config = load_agent_config(agent_config_path)
        skills = SkillRegistry.discover(config_path.parent / "skills")

        session_id = message.get("session_id")
        sessions_directory = config_path.parent / "sessions"
        self._store = JsonlSessionStore(sessions_directory)
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
        self._mcp = McpClientManager(
            agent_config.mcp,
            workspace.path,
            on_status=self._emit_mcp_status,
        )
        catalog = catalog.extend(self._mcp.start())

        approval_policy = CompositeToolPolicy(
            ShellApprovalPolicy(self.request_permission),
            McpApprovalPolicy(
                self.request_mcp_permission,
                self._mcp.requires_approval,
            ),
        )
        self._permissions = PermissionController(
            self._session,
            approval_policy,
            lambda preset: self._store.set_permission_preset(
                self._session.session_id,
                preset,
                workspace.path,
            ),
        )
        self._jobs = JobManager(self._session)
        self._jobs.set_update_callback(
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
        )
        context = ToolExecutionContext(
            workspace=workspace,
            session=self._session,
            sessions_directory=sessions_directory,
            command_executor=SubprocessCommandExecutor(workspace.path),
            max_generation_tokens=agent_config.max_generation_tokens,
            vision_input=(
                "image" in main_provider.capabilities.input_modalities
            ),
            vision_provider=vision_provider,
            mcp=self._mcp,
            subagents=subagents,
            jobs=self._jobs,
            skills=skills,
            ask_user=self.request_user_choice,
        )
        self._agent = Agent(
            provider=main_provider,
            session=self._session,
            system_prompt=load_system_prompt(workspace, skills),
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
                    *self._mcp.tool_names,
                ),
                context,
                policy=self._permissions,
            ),
            context=context,
        )

        self.emit(**ready_message(
            session_id=self._session.session_id,
            workspace=str(workspace.path),
            model=config.model,
            resumed=resumed,
            message_count=len(self._session.items),
            permission_preset=self._permissions.preset.value,
            context_window=context_window_to_dict(
                self._agent.context_window()
            ),
            skill_warnings=skills.warnings,
        ))

    def _emit_sessions(self) -> None:
        """Answer ``list_sessions`` with this Workspace's stored Sessions."""
        if self._store is None or self._workspace is None:
            raise RuntimeError("received 'list_sessions' before 'start'")
        self.emit(
            **sessions_listed_message(
                self._store.list_workspace_sessions(self._workspace.path)
            )
        )

    def _emit_session_items(self) -> None:
        """Answer ``load_session`` with the conversation to render."""
        if self._session is None:
            raise RuntimeError("received 'load_session' before 'start'")
        self.emit(
            **session_items_message(
                [message_to_dict(item) for item in self._session.items]
            )
        )

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
    ) -> SubagentRuntime | None:
        """Build the sub-agent runtime, or None when no role is configured."""
        if not agent_config.tools.is_enabled("subagent"):
            return None
        # A provider override for a role that does not exist is a typo, not
        # a silent no-op: the two config files must name the same roles.
        unknown = configured_role_names(config_path) - set(
            agent_config.subagent_roles
        )
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(
                "provider_config.json configures unknown subagent role(s): "
                f"{names}"
            )
        roles = SubagentRoleRegistry(
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
        )
        if not roles:
            return None
        return SubagentRuntime(
            config=agent_config,
            catalog=catalog,
            roles=roles,
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
        if self._agent is None or self._session is None or self._store is None:
            raise RuntimeError("received 'user_turn' before 'start'")
        if self._workspace is None:
            raise RuntimeError("bridge workspace is not initialized")

        self._turn_id = str(message["turn_id"])
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
                "on_event": lambda event: self.emit(
                    **event_to_message(event, self._turn_id or "")
                ),
                "attachments": attachments,
            }
            result = self._agent.run(
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
            return
        except Cancelled:
            self.emit(
                "turn_cancelled",
                turn_id=self._turn_id,
            )
            return
        except Exception as error:
            self.emit(
                "turn_failed",
                turn_id=self._turn_id,
                error=_error_payload(error),
            )
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

    def serve(self) -> None:
        started = False
        while True:
            try:
                message = self.read_message()
            except (json.JSONDecodeError, ValueError) as error:
                self.emit(
                    "fatal",
                    error={"type": "ProtocolError", "message": str(error)},
                )
                raise SystemExit(1)

            if message is None or message["type"] == "shutdown":
                return
            if not started:
                if message["type"] != "start":
                    self.emit(
                        "fatal",
                        error={
                            "type": "ProtocolError",
                            "message": "first message must be 'start'",
                        },
                    )
                    raise SystemExit(1)
                self.start(message)
                started = True
            elif message["type"] == "user_turn":
                self.run_turn(message)
            elif message["type"] == "permission_set":
                self._set_permission_preset(message)
            elif message["type"] == "list_sessions":
                self._emit_sessions()
            elif message["type"] == "load_session":
                self._emit_session_items()

    def close(self) -> None:
        self._route_shutdown(notify_commands=False)
        if self._jobs is not None:
            self._jobs.close()
            self._jobs = None
        if self._mcp is not None:
            self._mcp.close()
            self._mcp = None


def _error_payload(error: Exception) -> dict[str, str]:
    return {"type": type(error).__name__, "message": str(error)}


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
                path=resolved.relative_to(workspace.path).as_posix(),
                mime_type=info.mime_type,
            )
        )
    return tuple(result)
