import asyncio
import json
import os
import queue
import threading
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx2
from mcp import ClientSession, StdioServerParameters
from mcp.types import PaginatedRequestParams
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from ..tools import JSONValue, Tool
from .config import McpConfig, McpServerConfig
from .tool import McpTool


@dataclass(frozen=True)
class McpServerStatus:
    server: str
    status: str
    tool_count: int | None = None
    error: str | None = None


@dataclass
class _Request:
    server: str
    tool: str
    arguments: dict[str, JSONValue]
    result: queue.Queue[object]


class _Stop:
    pass


class McpClientManager:
    def __init__(
        self,
        config: McpConfig,
        workspace: Path,
        on_status: Callable[[McpServerStatus], None] | None = None,
    ) -> None:
        self._config = config
        self._workspace = workspace
        self._on_status = on_status
        self._requests: queue.Queue[_Request | _Stop] = queue.Queue()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._startup_error: BaseException | None = None
        self._tools: tuple[Tool, ...] = ()
        self._tool_origins: dict[str, tuple[str, str]] = {}
        self._servers = {
            server.name: server
            for server in config.servers
            if config.enabled and server.enabled
        }

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Qualified names of every discovered tool, for catalog selection."""
        return tuple(tool.name for tool in self._tools)

    def requires_approval(self, qualified_name: str) -> bool:
        origin = self._tool_origins.get(qualified_name)
        if origin is None:
            return False
        server, remote_name = origin
        required = self._servers[server].tools.require_approval
        return required is None or remote_name in required

    def tool_identity(
        self,
        qualified_name: str,
    ) -> tuple[str, str] | None:
        return self._tool_origins.get(qualified_name)

    def start(self) -> tuple[Tool, ...]:
        if not self._servers:
            return ()
        if self._thread is not None:
            raise RuntimeError("MCP client manager has already started")
        self._thread = threading.Thread(
            target=self._run_thread,
            name="nosis-mcp",
            daemon=True,
        )
        self._thread.start()
        maximum_timeout = sum(
            config.startup_timeout_seconds
            for config in self._servers.values()
        )
        if not self._ready.wait(maximum_timeout + 1):
            self.close()
            raise TimeoutError("MCP servers did not finish starting")
        if self._startup_error is not None:
            error = self._startup_error
            self.close()
            raise error
        return self._tools

    def call_tool(
        self,
        server: str,
        tool: str,
        arguments: dict[str, JSONValue],
    ) -> JSONValue:
        config = self._servers[server]
        result_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self._requests.put(_Request(server, tool, arguments, result_queue))
        try:
            result = result_queue.get(timeout=config.call_timeout_seconds + 1)
        except queue.Empty as error:
            raise TimeoutError(
                f"MCP tool '{server}/{tool}' did not return in time"
            ) from error
        if isinstance(result, BaseException):
            raise result
        return result  # type: ignore[return-value]

    def close(self) -> None:
        thread = self._thread
        if thread is None:
            return
        if thread.is_alive():
            self._requests.put(_Stop())
            thread.join(timeout=5)
        self._thread = None

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._serve())
        except BaseException as error:
            self._startup_error = _unwrap_exception_group(error)
            self._ready.set()

    async def _serve(self) -> None:
        sessions: dict[str, ClientSession] = {}
        tools: list[Tool] = []
        names: set[str] = set()
        try:
            async with AsyncExitStack() as stack:
                for config in self._servers.values():
                    self._emit(config.name, "connecting")
                    try:
                        session = await asyncio.wait_for(
                            self._connect(stack, config),
                            timeout=config.startup_timeout_seconds,
                        )
                        server_tools = await asyncio.wait_for(
                            self._discover_tools(session, config),
                            timeout=config.startup_timeout_seconds,
                        )
                    except Exception as error:
                        self._emit(
                            config.name,
                            "unavailable",
                            error=_exception_message(error),
                        )
                        continue
                    sessions[config.name] = session
                    for tool in server_tools:
                        name = tool.name
                        if name in names:
                            raise ValueError(
                                f"duplicate MCP tool name '{name}'"
                            )
                        names.add(name)
                        self._tool_origins[name] = (
                            config.name,
                            tool.remote_name,
                        )
                        tools.append(tool)
                    self._emit(config.name, "ready", len(server_tools))
                self._tools = tuple(tools)
                self._ready.set()

                while True:
                    request = await asyncio.to_thread(self._requests.get)
                    if isinstance(request, _Stop):
                        break
                    try:
                        value = await self._call(sessions, request)
                    except BaseException as error:
                        request.result.put(error)
                    else:
                        request.result.put(value)
        finally:
            for name in sessions:
                self._emit(name, "closed")

    async def _connect(
        self,
        stack: AsyncExitStack,
        config: McpServerConfig,
    ) -> ClientSession:
        if config.transport == "stdio":
            cwd = None
            if config.cwd is not None:
                path = Path(config.cwd)
                cwd = str(path if path.is_absolute() else self._workspace / path)
            parameters = StdioServerParameters(
                command=config.command or "",
                args=list(config.args),
                cwd=cwd,
                env={**os.environ, **config.env},
            )
            read, write = await stack.enter_async_context(
                stdio_client(parameters)
            )
        else:
            client = await stack.enter_async_context(
                httpx2.AsyncClient(headers=config.headers)
            )
            streams = await stack.enter_async_context(
                streamable_http_client(config.url or "", http_client=client)
            )
            if len(streams) == 2:
                read, write = streams
            else:
                read, write, _ = streams
        session = await stack.enter_async_context(
            ClientSession(
                read,
                write,
                read_timeout_seconds=float(config.call_timeout_seconds),
            )
        )
        await session.initialize()
        return session

    async def _discover_tools(
        self,
        session: ClientSession,
        config: McpServerConfig,
    ) -> tuple[McpTool, ...]:
        discovered: list[McpTool] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(
                params=PaginatedRequestParams(cursor=cursor)
            )
            for remote in result.tools:
                if (
                    config.tools.enabled is not None
                    and remote.name not in config.tools.enabled
                ):
                    continue
                input_schema = getattr(remote, "input_schema", None)
                if input_schema is None:
                    input_schema = getattr(remote, "inputSchema")
                discovered.append(
                    McpTool(
                        config.name,
                        remote.name,
                        remote.description,
                        input_schema,
                    )
                )
            cursor = getattr(result, "next_cursor", None)
            if cursor is None:
                cursor = getattr(result, "nextCursor", None)
            if not cursor:
                break
        return tuple(discovered)

    async def _call(
        self,
        sessions: dict[str, ClientSession],
        request: _Request,
    ) -> JSONValue:
        config = self._servers[request.server]
        result = await asyncio.wait_for(
            sessions[request.server].call_tool(
                request.tool,
                request.arguments,
            ),
            timeout=config.call_timeout_seconds,
        )
        data = result.model_dump(mode="json", exclude_none=True)
        is_error = getattr(result, "is_error", None)
        if is_error is None:
            is_error = getattr(result, "isError", False)
        if is_error:
            message = _error_message(data)
            raise RuntimeError(
                f"MCP tool '{request.server}/{request.tool}' failed: {message}"
            )
        return data

    def _emit(
        self,
        server: str,
        status: str,
        tool_count: int | None = None,
        error: str | None = None,
    ) -> None:
        if self._on_status is not None:
            self._on_status(McpServerStatus(server, status, tool_count, error))


def _error_message(data: dict[str, Any]) -> str:
    texts = [
        item.get("text")
        for item in data.get("content", [])
        if isinstance(item, dict) and isinstance(item.get("text"), str)
    ]
    return "\n".join(texts) if texts else json.dumps(data, ensure_ascii=False)


def _unwrap_exception_group(error: BaseException) -> BaseException:
    """Expose the actionable MCP error instead of an async task-group wrapper."""
    if isinstance(error, BaseExceptionGroup) and len(error.exceptions) == 1:
        return _unwrap_exception_group(error.exceptions[0])
    return error


def _exception_message(error: BaseException) -> str:
    error = _unwrap_exception_group(error)
    return str(error) or type(error).__name__
