"""Runtime-local MCP tool views over one Host-owned Broker client."""

import os
import shutil
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from agent_core.execution.process import CancellationSignal
from agent_core.mcp.config import McpConfig
from agent_core.mcp.tool import McpTool
from agent_core.tools.base import JSONValue
from agent_core.turn_control import AgentCancelled
from .transport import BROKER_STARTUP_SECONDS, McpTransport


@dataclass(frozen=True)
class McpServerStatus:
    server: str
    status: str
    tool_count: int | None = None
    error: str | None = None


def _describe(
    config: McpConfig, workspace: Path, session_id: str,
    environment: dict[str, str] | None = None,
) -> list[dict]:
    workspace = workspace.resolve()
    inherited = os.environ if environment is None else environment
    servers = []
    for server in config.servers:
        if not config.enabled or not server.enabled:
            continue
        effective_env = {**inherited, **server.env}
        connection: dict = {"transport": server.transport}
        if server.transport == "stdio":
            if server.scope == "global" and (
                server.cwd is None or not Path(server.cwd).is_absolute()
            ):
                raise ValueError(f"global stdio MCP '{server.identifier}' requires an absolute cwd")
            cwd = Path(server.cwd) if server.cwd is not None else workspace
            if not cwd.is_absolute():
                cwd = workspace / cwd
            command = server.command
            if command and ("/" in command or "\\" in command):
                path = Path(command)
                command = str((cwd / path).resolve())
            else:
                command = shutil.which(command or "", path=effective_env.get("PATH")) or command
            connection.update(command=command, args=list(server.args),
                              cwd=str(cwd.resolve()), env=effective_env)
        else:
            # The HTTP transport only consumes proxy and CA environment settings.
            http_env = {key: value for key, value in effective_env.items()
                        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
                        or key in {"SSL_CERT_FILE", "SSL_CERT_DIR"}}
            connection.update(url=server.url, headers=server.headers, env=http_env)
        servers.append({
            "server": server.identifier,
            "startup_timeout": server.startup_timeout_seconds,
            "identity": {
                "server": server.identifier,
                "connection": connection,
                "scope": server.scope,
                "workspace": None if server.scope == "global" else os.path.normcase(str(workspace)),
                "session": session_id if server.scope == "session" else None,
            },
        })
    return servers


class McpClient:
    def __init__(self, directory: Path, on_status: Callable[[McpServerStatus], None] | None = None):
        self._directory = directory
        self.on_status = on_status
        self._transport = McpTransport(directory, self._on_event)
        self._handle: McpClientHandle | None = None

    def bind(
        self, config: McpConfig, workspace: Path, session_id: str, *,
        environment: dict[str, str] | None = None,
    ) -> "McpClientHandle":
        if self._transport.failed:
            self._transport.close()
            self._transport = McpTransport(self._directory, self._on_event)
        return McpClientHandle(self, config, workspace, session_id, environment=environment)

    def _on_event(self, event: dict) -> None:
        if self._handle is not None:
            leases = (tuple(self._handle._leases.values()) if event["event"] == "disconnected"
                      else (event["lease"],))
            for lease in leases:
                self._handle.unavailable(lease, event["error"])

    def close(self) -> None:
        if self._handle is not None:
            self._handle.release()
        self._transport.close()


class McpClientHandle:
    def __init__(
        self, client: McpClient, config: McpConfig, workspace: Path, session_id: str,
        *, environment: dict[str, str] | None = None,
    ) -> None:
        self._client = client
        self._config = config
        self._environment = dict(os.environ if environment is None else environment)
        self._descriptions = _describe(config, workspace, session_id, self._environment)
        self._servers = {server.identifier: server for server in config.servers}
        self._tools: tuple[McpTool, ...] | None = None
        self._origins: dict[str, tuple[str, str]] = {}
        self._leases: dict[str, str] = {}
        self._unavailable: dict[str, str] = {}
        self._request: tuple[str, Future] | None = None
        self._deadline = 0.0
        self._closed = False

    def matches(
        self, config: McpConfig, workspace: Path, session_id: str,
        environment: dict[str, str] | None = None,
    ) -> bool:
        return (not self._closed and self._config == config
                and self._descriptions == _describe(
                    config, workspace, session_id,
                    self._environment if environment is None else environment,
                ))

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools or ())

    def tool_identity(self, name: str) -> tuple[str, str] | None:
        return self._origins.get(name)

    def requires_approval(self, name: str) -> bool:
        origin = self._origins.get(name)
        if origin is None:
            return False
        server, remote = origin
        required = self._servers[server].tools.require_approval
        return required is None or remote in required

    def acquire(self) -> None:
        if self._request is not None or self._closed:
            raise RuntimeError("MCP handle cannot be acquired twice")
        self._client._handle = self
        for server in self._descriptions:
            self._emit(server["server"], "connecting")
        self._deadline = time.monotonic() + BROKER_STARTUP_SECONDS + max(
            (server["startup_timeout"] for server in self._descriptions), default=0,
        )
        # An unused MCP client must not start a Broker. An existing client still
        # sends an empty binding to release servers removed from configuration.
        if not self._descriptions and not self._client._transport.started:
            ready: Future = Future()
            ready.set_result([])
            self._request = ("", ready)
        else:
            self._request = self._client._transport.request("acquire", servers=self._descriptions)

    def wait_ready(self) -> tuple[McpTool, ...]:
        if self._request is None:
            raise RuntimeError("MCP handle has not been acquired")
        if self._tools is None:
            try:
                results = self._request[1].result(timeout=max(0, self._deadline - time.monotonic()))
            except FutureTimeout:
                if not self._request[1].done():
                    self.release()
                raise
            self._load_tools(results)
        return self._tools

    def _load_tools(self, results: list[dict]) -> None:
        tools = []
        for result in results:
            server = result["server"]
            if "error" in result:
                self._emit(server, "unavailable", error=result["error"])
                continue
            self._leases[server] = result["lease"]
            enabled = self._servers[server].tools.enabled
            selected = []
            for remote in result["tools"]:
                if enabled is not None and remote["name"] not in enabled:
                    continue
                tool = McpTool(server, remote["name"], remote["description"], remote["input_schema"])
                if tool.name in self._origins:
                    raise ValueError(f"duplicate MCP tool name '{tool.name}'")
                self._origins[tool.name] = (server, remote["name"])
                selected.append(tool)
            tools.extend(selected)
            error = self._unavailable.get(result["lease"])
            self._emit(server, "unavailable" if error else "ready", len(selected), error)
        self._tools = tuple(tools)

    def unavailable(self, lease: str, error: str) -> None:
        self._unavailable[lease] = error
        for server, token in self._leases.items():
            if token == lease:
                self._emit(server, "unavailable", error=error)

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, JSONValue], *,
        cancellation: CancellationSignal | None = None,
    ) -> JSONValue:
        if self._closed:
            raise ConnectionError("MCP handle has been released")
        if (server, tool) not in self._origins.values():
            raise ValueError(f"MCP tool '{server}/{tool}' is not enabled")
        if self._leases[server] in self._unavailable:
            raise ConnectionError(self._unavailable[self._leases[server]])
        if cancellation is not None and cancellation.cancelled:
            raise AgentCancelled()
        timeout = self._servers[server].call_timeout_seconds
        deadline = time.monotonic() + timeout
        request, future = self._client._transport.request(
            "call", lease=self._leases[server], tool=tool, arguments=arguments, timeout=timeout,
        )
        try:
            while True:
                if self._closed or (cancellation is not None and cancellation.cancelled):
                    raise AgentCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"MCP tool '{server}/{tool}' did not return in time")
                try:
                    return future.result(timeout=min(0.05, remaining))
                except FutureTimeout:
                    if future.done():
                        return future.result()
        finally:
            if not future.done():
                self._client._transport.cancel(request)

    def release(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._request is not None and not self._request[1].done():
            self._client._transport.cancel(self._request[0])
        for server in self._leases:
            self._emit(server, "closed")

    def _emit(self, server: str, status: str, tool_count=None, error=None) -> None:
        if self._client.on_status is not None:
            self._client.on_status(McpServerStatus(server, status, tool_count, error))
