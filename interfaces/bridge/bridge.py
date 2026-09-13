"""Agent runtime driven over newline-delimited JSON.

The TUI spawns this as a child process and exchanges protocol messages
on stdin/stdout. The class is kept free of process-level setup so tests
can drive it with plain string buffers.
"""

import json
from collections import deque
from itertools import count
from pathlib import Path
from typing import TextIO

from dotenv import load_dotenv

from agent_core import (
    Agent,
    JsonlSessionStore,
    Session,
    CompositeToolPolicy,
    McpApprovalPolicy,
    ShellApprovalPolicy,
    SubprocessCommandExecutor,
    SubagentRegistry,
    Workspace,
    ImagePart,
    create_builtin_tools,
    SubagentTool,
    load_agent_config,
)
from agent_core.prompts import load_system_prompt
from agent_core.providers import LiteLLMProvider
from agent_core.mcp.manager import McpClientManager, McpServerStatus

from .config import load_config_with_name, load_vision_config
from .protocol import decode, encode, event_to_message, usage_to_dict


class Cancelled(Exception):
    """Raised to unwind the agent loop when a turn is cancelled."""


class Bridge:
    def __init__(self, stdin: TextIO, stdout: TextIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._deferred: deque[dict[str, object]] = deque()
        self._approval_ids = count(1)
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
        parts = call.name.split("__", 2)
        server = parts[1] if len(parts) == 3 else None
        return self.request_permission(
            f"{call.name}({call.arguments})",
            kind="mcp",
            server=server,
            tool_name=call.name,
        )

    def start(self, message: dict[str, object]) -> None:
        config_path = Path(str(message["provider_config_path"])).expanduser().resolve()
        agent_config_path = Path(str(message["agent_config_path"])).expanduser().resolve()
        load_dotenv(config_path.parent / ".env")

        workspace = Workspace(Path(str(message["workspace"])))
        self._workspace = workspace
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
        self._session = (
            self._store.load(str(session_id)) if resumed else Session()
        )

        main_provider = LiteLLMProvider(
            model=config.model,
            base_url=config.url,
            api_key=config.key,
            max_context_tokens=config.max_context_tokens,
            media_root=workspace.path,
        )
        vision_config = load_vision_config(config_path, main_provider_name)
        vision_provider = (
            LiteLLMProvider(
                model=vision_config.model,
                base_url=vision_config.url,
                api_key=vision_config.key,
                max_context_tokens=vision_config.max_context_tokens,
                media_root=workspace.path,
            )
            if vision_config is not None
            else None
        )

        subagent_registry = SubagentRegistry()
        if agent_config.tools.is_enabled("subagent"):
            # Sub-agents run with an isolated session and their own tool config.
            subagent_provider_name, subagent_provider_config = load_config_with_name(
                config_path,
                provider if isinstance(provider, str) and provider else None,
                subagent=True,
            )
            subagent_provider = LiteLLMProvider(
                model=subagent_provider_config.model,
                base_url=subagent_provider_config.url,
                api_key=subagent_provider_config.key,
                max_context_tokens=subagent_provider_config.max_context_tokens,
                media_root=workspace.path,
            )
            subagent_vision_config = load_vision_config(
                config_path,
                subagent_provider_name,
                agent="subagent",
                inherited=vision_config,
            )
            subagent_vision_provider = (
                LiteLLMProvider(
                    model=subagent_vision_config.model,
                    base_url=subagent_vision_config.url,
                    api_key=subagent_vision_config.key,
                    max_context_tokens=subagent_vision_config.max_context_tokens,
                    media_root=workspace.path,
                )
                if subagent_vision_config is not None
                else None
            )
            child_tools = create_builtin_tools(
                agent_config.subagent_tools,
                workspace,
                SubprocessCommandExecutor(workspace.path),
                shell_timeout_seconds=agent_config.shell_timeout_seconds,
                vision_provider=subagent_vision_provider,
                sessions_directory=sessions_directory,
            )
            subagent_registry.register(
                SubagentTool(
                    provider=subagent_provider,
                    config=agent_config,
                    workspace=workspace,
                    tools=child_tools,
                    parent_session=self._session,
                    parent_session_id=self._session.session_id,
                    sessions_directory=sessions_directory,
                    tool_policy=ShellApprovalPolicy(self.request_permission),
                )
            )
        builtin_tools = create_builtin_tools(
            agent_config.tools,
            workspace,
            SubprocessCommandExecutor(workspace.path),
            shell_timeout_seconds=agent_config.shell_timeout_seconds,
            subagent_registry=subagent_registry,
            vision_provider=vision_provider,
            sessions_directory=sessions_directory,
        )
        self._mcp = McpClientManager(
            agent_config.mcp,
            workspace.path,
            on_status=self._emit_mcp_status,
        )
        mcp_tools = self._mcp.start()
        self._agent = Agent(
            provider=main_provider,
            session=self._session,
            system_prompt=load_system_prompt(workspace),
            config=agent_config,
            workspace=workspace,
            tools=(*builtin_tools, *mcp_tools),
            tool_policy=CompositeToolPolicy(
                ShellApprovalPolicy(self.request_permission),
                McpApprovalPolicy(
                    self.request_mcp_permission,
                    self._mcp.approval_servers,
                ),
            ),
            sessions_directory=sessions_directory,
        )

        self.emit(
            "ready",
            session_id=self._session.session_id,
            workspace=str(workspace.path),
            model=config.model,
            resumed=resumed,
            message_count=len(self._session.items),
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

        self._turn_id = str(message["turn_id"])
        try:
            if self._workspace is None:
                raise RuntimeError("bridge workspace is not initialized")
            attachments = _parse_attachments(message.get("attachments"), self._workspace)
            result = self._agent.run(
                str(message["text"]),
                on_event=lambda event: self.emit(
                    **event_to_message(event, self._turn_id or "")
                ),
                attachments=attachments,
            )
        except (KeyboardInterrupt, Cancelled):
            # Without an AgentRunResult there is nothing to append, so
            # the partial turn stays in memory only.
            self.emit(
                "turn_cancelled",
                turn_id=self._turn_id,
                persisted=False,
            )
            return
        except Exception as error:
            self.emit(
                "turn_failed",
                turn_id=self._turn_id,
                error={
                    "type": type(error).__name__,
                    "message": str(error),
                },
            )
            return

        context_fields = {}
        if self._session.archived_summary is not None:
            context_fields = {
                "archived_summary": self._session.archived_summary,
                "archived_item_count": self._session.archived_item_count,
            }
        self._store.append_turn(
            self._session.session_id,
            result.request,
            result.response,
            result.items,
            **context_fields,
        )
        self.emit(
            "turn_completed",
            turn_id=self._turn_id,
            usage=usage_to_dict(result.response.usage),
        )

    def serve(self) -> None:
        try:
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
        finally:
            if self._mcp is not None:
                self._mcp.close()


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
        mime_type = item.get("mime_type", "image/png")
        if not isinstance(path, str) or not path:
            raise ValueError("image attachment path must be a non-empty string")
        if not isinstance(mime_type, str) or not mime_type:
            raise ValueError("image attachment mime_type must be a non-empty string")
        resolved = workspace.resolve_path(path)
        if not resolved.is_file():
            raise ValueError(f"image attachment does not exist: {path}")
        result.append(
            ImagePart(
                path=resolved.relative_to(workspace.path).as_posix(),
                mime_type=mime_type,
            )
        )
    return tuple(result)
