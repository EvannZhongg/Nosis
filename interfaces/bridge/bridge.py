"""Agent runtime driven over newline-delimited JSON.

The TUI spawns this as a child process and exchanges protocol messages
on stdin/stdout. The class is kept free of process-level setup so tests
can drive it with plain string buffers.
"""

import json
from collections import deque
from itertools import count
from pathlib import Path
from threading import Lock
from typing import TextIO

from dotenv import load_dotenv

from agent_core import (
    Agent,
    AgentCancelled as Cancelled,
    JsonlSessionStore,
    Session,
    CompositeToolPolicy,
    McpApprovalPolicy,
    ShellApprovalPolicy,
    SubagentRole,
    SubagentRoleRegistry,
    SubagentRuntime,
    SubprocessCommandExecutor,
    ToolExecutionContext,
    Workspace,
    ImagePart,
    builtin_catalog,
    load_agent_config,
    probe_image,
    vision_aware_tool_names,
)
from agent_core.prompts import load_system_prompt
from agent_core.providers import LiteLLMProvider
from agent_core.mcp.manager import McpClientManager, McpServerStatus

from .config import (
    configured_role_names,
    load_config_with_name,
    load_vision_config,
)
from .protocol import decode, encode, event_to_message, usage_to_dict


class Bridge:
    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._deferred: deque[dict[str, object]] = deque()
        self._approval_ids = count(1)
        # Parallel tool calls share this one protocol channel. An approval
        # is a read-modify-write on it, so two of them must not interleave:
        # one thread would consume the other's response and both would hang.
        self._approval_lock = Lock()
        self._turn_id: str | None = None
        self._agent: Agent | None = None
        self._session: Session | None = None
        self._store: JsonlSessionStore | None = None
        self._mcp: McpClientManager | None = None
        self._workspace: Workspace | None = None

    def emit(self, type: str, **fields: object) -> None:
        self._stdout.write(encode({"type": type, **fields}) + "\n")

    def read_message(self) -> dict[str, object] | None:
        """Return the next protocol message, or None at end of input.

        Messages deferred by a nested approval read are replayed first,
        in arrival order.
        """
        if self._deferred:
            return self._deferred.popleft()
        return self._read_incoming()

    def _read_incoming(self) -> dict[str, object] | None:
        """Read a fresh message from stdin, skipping blank lines."""
        while True:
            line = self._stdin.readline()
            if not line:
                return None
            stripped = line.strip()
            if stripped:
                return decode(stripped)

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
        with self._approval_lock:
            request_id = f"{self._turn_id}:{next(self._approval_ids)}"
            self.emit(
                "approval_request",
                turn_id=self._turn_id,
                request_id=request_id,
                command=command,
                kind=kind,
                server=server,
                tool_name=tool_name,
            )
            while True:
                # Read fresh input only: replaying the deferred queue here
                # would spin, since non-matching messages go back onto it.
                message = self._read_incoming()
                if message is None or message["type"] == "shutdown":
                    # The UI is gone; never run an unapproved command.
                    raise Cancelled
                if (
                    message["type"] == "approval_response"
                    and message.get("request_id") == request_id
                ):
                    return bool(message.get("approved"))
                self._deferred.append(message)

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

        shell_policy = ShellApprovalPolicy(self.request_permission)
        subagents = self._subagent_runtime(
            agent_config,
            catalog,
            config_path,
            workspace,
            shell_policy,
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
        )
        self._agent = Agent(
            provider=main_provider,
            session=self._session,
            system_prompt=load_system_prompt(workspace),
            config=agent_config,
            tools=catalog.select(
                (
                    *vision_aware_tool_names(
                        agent_config.tools.enabled,
                        main_provider,
                        vision_provider,
                    ),
                    *self._mcp.tool_names,
                ),
                context,
                policy=CompositeToolPolicy(
                    shell_policy,
                    McpApprovalPolicy(
                        self.request_mcp_permission,
                        self._mcp.requires_approval,
                    ),
                ),
            ),
            context=context,
        )

        self.emit(
            "ready",
            session_id=self._session.session_id,
            workspace=str(workspace.path),
            model=config.model,
            resumed=resumed,
            message_count=len(self._session.items),
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
        shell_policy: ShellApprovalPolicy,
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
            tool_policy=shell_policy,
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
                **kwargs,
            )
        except (KeyboardInterrupt, Cancelled):
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

        self.emit(
            "turn_completed",
            turn_id=self._turn_id,
            usage=usage_to_dict(result.response.usage),
        )

    def serve(self) -> None:
        while True:
            try:
                message = self.read_message()
            except (json.JSONDecodeError, ValueError) as error:
                self.emit(
                    "fatal",
                    error={"type": "ProtocolError", "message": str(error)},
                )
                raise SystemExit(1)
            except KeyboardInterrupt:
                continue  # Interrupted while idle: nothing to cancel.

            if message is None or message["type"] == "shutdown":
                return
            if message["type"] == "start":
                self.start(message)
            elif message["type"] == "user_turn":
                self.run_turn(message)

    def close(self) -> None:
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
