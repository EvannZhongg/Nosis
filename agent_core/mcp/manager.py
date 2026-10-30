from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, TYPE_CHECKING

from ..tools import JSONValue, Tool
from ..execution.process import CancellationSignal
from ..turn_control import AgentCancelled
from .config import McpConfig, McpServerConfig
from .tool import McpTool

if TYPE_CHECKING:
    from mcp import ClientSession


# Room for the manager thread and interpreter imports to reach the first
# connection, on top of the slowest server's own startup budget.
_STARTUP_SLACK_SECONDS = 5


@dataclass(frozen=True)
class McpServerStatus:
    server: str
    status: str
    tool_count: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class McpServerInstructions:
    server: str
    instructions: str


@dataclass
class _Request:
    server: str
    tool: str
    arguments: dict[str, JSONValue]
    result: queue.Queue[object]
    cancellation: CancellationSignal | None


class _Stop:
    pass


class McpClientManager:
    def __init__(
        self,
        config: McpConfig,
        workspace: Path,
        on_status: Callable[[McpServerStatus], None] | None = None,
    ) -> None:
        self.config = config
        self._workspace = workspace.resolve()
        self._on_status = on_status
        self._requests: queue.Queue[_Request | _Stop] = queue.Queue()
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task[None] | None = None
        self._startup_error: BaseException | None = None
        self._tools: tuple[Tool, ...] = ()
        self._tool_origins: dict[str, tuple[str, str]] = {}
        self._server_instructions: dict[str, str] = {}
        self._servers = {
            server.identifier: server
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

    def instructions_for(
        self,
        tool_names: Iterable[str],
    ) -> tuple[McpServerInstructions, ...]:
        """Select startup instructions for an Agent's actual MCP tools."""
        selected: set[str] = set()
        for name in tool_names:
            origin = self.tool_identity(name)
            if origin is not None:
                selected.add(origin[0])
        return tuple(
            McpServerInstructions(server, instructions)
            for server, instructions in self._server_instructions.items()
            if server in selected
        )

    def start(self) -> None:
        """Connect while the caller prepares other independent dependencies."""
        if not self._servers:
            return
        if self._thread is not None:
            raise RuntimeError("MCP client manager has already started")
        self._thread = threading.Thread(
            target=self._run_thread,
            name="nosis-mcp",
            daemon=True,
        )
        self._thread.start()

    def wait_ready(self, cancellation: CancellationSignal | None = None) -> tuple[Tool, ...]:
        if not self._servers:
            return ()
        if self._thread is None:
            raise RuntimeError("MCP client manager has not started")
        # Allow one concurrent startup budget plus thread/import overhead.
        budget = max(
            config.startup_timeout_seconds
            for config in self._servers.values()
        )
        deadline = time.monotonic() + budget + _STARTUP_SLACK_SECONDS
        while not self._ready.is_set():
            if cancellation is not None and cancellation.cancelled:
                self.close()
                raise AgentCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.close()
                raise TimeoutError("MCP servers did not finish starting")
            self._ready.wait(min(remaining, 0.05) if cancellation is not None else remaining)
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
        *,
        cancellation: CancellationSignal | None = None,
    ) -> JSONValue:
        if self._closing.is_set():
            raise ConnectionError("MCP client manager has been closed")
        config = self._servers[server]
        result_queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self._requests.put(_Request(server, tool, arguments, result_queue, cancellation))
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
            self._closing.set()
            self._requests.put(_Stop())
            if not self._ready.is_set():
                loop = self._loop
                task = self._task
                if loop is not None and task is not None:
                    try:
                        loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        pass
            thread.join(timeout=5)
        self._thread = None

    def _run_thread(self) -> None:
        try:
            asyncio.run(self._run())
        except BaseException as error:
            if not self._closing.is_set():
                self._startup_error = _unwrap_exception_group(error)
            self._ready.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._task = asyncio.current_task()
        try:
            if not self._closing.is_set():
                await self._serve()
            else:
                self._ready.set()
        finally:
            self._task = None
            self._loop = None

    async def _serve(self) -> None:
        sessions: dict[str, ClientSession] = {}
        tools: list[Tool] = []
        try:
            async with AsyncExitStack() as stack:
                for config in self._servers.values():
                    self._emit(config.identifier, "connecting")

                # Connection and discovery share each server's startup budget;
                # a failed server does not block the other servers.
                results = await asyncio.gather(
                    *(
                        asyncio.wait_for(
                            self._start_server(stack, config),
                            timeout=config.startup_timeout_seconds,
                        )
                        for config in self._servers.values()
                    ),
                    return_exceptions=True,
                )
                for config, outcome in zip(
                    self._servers.values(),
                    results,
                ):
                    if not isinstance(outcome, tuple):
                        self._emit(
                            config.identifier,
                            "unavailable",
                            error=_exception_message(outcome),
                        )
                        continue
                    session, server_tools = outcome
                    origins = {}
                    for tool in server_tools:
                        name = tool.name
                        if name in self._tool_origins or name in origins:
                            self._emit(
                                config.identifier,
                                "unavailable",
                                error=(
                                    f"MCP server '{config.identifier}' produced "
                                    f"duplicate qualified tool name '{name}'"
                                ),
                            )
                            break
                        origins[name] = (
                            config.identifier,
                            tool.remote_name,
                        )
                    else:
                        sessions[config.identifier] = session
                        self._tool_origins.update(origins)
                        tools.extend(server_tools)
                        instructions = session.instructions
                        if instructions and instructions.strip():
                            self._server_instructions[config.identifier] = instructions.strip()
                        self._emit(
                            config.identifier,
                            "ready",
                            len(server_tools),
                        )
                self._tools = tuple(tools)
                self._ready.set()

                while True:
                    request = await asyncio.to_thread(self._requests.get)
                    if isinstance(request, _Stop):
                        break
                    try:
                        value = await self._call(sessions, request)
                    except AgentCancelled:
                        # Do not expose the live server coroutine's traceback
                        # to the thread consuming the cancellation signal.
                        request.result.put(AgentCancelled())
                    except BaseException as error:
                        request.result.put(error)
                    else:
                        request.result.put(value)
        finally:
            for name in sessions:
                self._emit(name, "closed")

    async def _start_server(
        self,
        stack: AsyncExitStack,
        config: McpServerConfig,
    ) -> tuple[ClientSession, tuple[McpTool, ...]]:
        """Connect one server and discover its tools under one budget."""
        from .connection import McpConnection

        connection = McpConnection(config, self._workspace)
        stack.push_async_callback(connection.close)
        try:
            session = await connection.ready
            return session, await self._discover_tools(session, config)
        except BaseException:
            await connection.close()
            raise

    async def _discover_tools(
        self,
        session: ClientSession,
        config: McpServerConfig,
    ) -> tuple[McpTool, ...]:
        from mcp.types import PaginatedRequestParams

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
                discovered.append(
                    McpTool(
                        config.identifier,
                        remote.name,
                        remote.description,
                        remote.input_schema,
                    )
                )
            cursor = result.next_cursor
            if not cursor:
                break
        return tuple(sorted(discovered, key=lambda tool: tool.remote_name))

    async def _call(
        self,
        sessions: dict[str, ClientSession],
        request: _Request,
    ) -> JSONValue:
        config = self._servers[request.server]
        if self._closing.is_set() or (
            request.cancellation is not None and request.cancellation.cancelled
        ):
            raise AgentCancelled
        task = asyncio.create_task(
            sessions[request.server].call_tool(
                request.tool,
                request.arguments,
            ),
        )
        try:
            async with asyncio.timeout(config.call_timeout_seconds):
                while not task.done():
                    if self._closing.is_set() or (
                        request.cancellation is not None and request.cancellation.cancelled
                    ):
                        raise AgentCancelled
                    await asyncio.wait((task,), timeout=0.1)
                result = task.result()
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        data = result.model_dump(mode="json", exclude_none=True)
        if result.is_error:
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
