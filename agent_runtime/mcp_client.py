"""Lightweight synchronous MCP handle with Runtime-local tool policy."""

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeout
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from uuid import uuid4

from agent_core.execution.process import CancellationSignal
from agent_core.mcp.config import McpConfig
from agent_core.mcp.tool import McpTool
from agent_core.tools.base import JSONValue
from agent_core.turn_control import AgentCancelled

from .mcp_wire import MESSAGE_LIMIT, encode


BROKER_STARTUP_SECONDS = 30


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
            connection.update(url=server.url, headers=server.headers, env=effective_env)
        servers.append({
            "server": server.identifier,
            "startup_timeout": server.startup_timeout_seconds,
            "identity": {
                "server": server.identifier,
                "connection": connection,
                "startup_timeout": server.startup_timeout_seconds,
                "workspace": os.path.normcase(str(workspace)),
                "session": session_id if server.scope == "session" else None,
            },
        })
    return servers


@contextmanager
def _startup_lock(directory: Path, closing: threading.Event):
    with (directory / "startup.lock").open("a+b") as handle:
        deadline = time.monotonic() + BROKER_STARTUP_SECONDS
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("timed out waiting for MCP broker startup")
                if closing.wait(0.05):
                    raise AgentCancelled()
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _existing_connection(directory: Path):
    stream = None
    reader = None
    try:
        endpoint = json.loads((directory / "broker.json").read_text(encoding="utf-8"))
        stream = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=2)
        reader = stream.makefile("rb")
        stream.sendall(encode({"token": endpoint["token"]}))
        if json.loads(reader.readline(MESSAGE_LIMIT)).get("authenticated") is not True:
            raise ConnectionError("MCP broker authentication failed")
        stream.settimeout(None)
        return stream, reader
    except (OSError, ValueError, KeyError):
        if reader is not None:
            reader.close()
        if stream is not None:
            stream.close()
        return None


def _connect(directory: Path, closing: threading.Event):
    directory.mkdir(parents=True, exist_ok=True)
    with _startup_lock(directory, closing):
        existing = _existing_connection(directory)
        if existing is not None:
            return existing
        (directory / "broker.json").unlink(missing_ok=True)
        subprocess.run(
            [sys.executable, "-m", "agent_runtime.mcp_broker", "--launch", str(directory)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt", check=True, timeout=BROKER_STARTUP_SECONDS,
        )
        deadline = time.monotonic() + BROKER_STARTUP_SECONDS
        while time.monotonic() < deadline:
            if closing.is_set():
                raise AgentCancelled()
            existing = _existing_connection(directory)
            if existing is not None:
                return existing
            closing.wait(0.05)
        raise TimeoutError(f"MCP broker did not start; see {directory / 'broker.log'}")


class McpClientHandle:
    def __init__(
        self, directory: Path, config: McpConfig, workspace: Path, session_id: str,
        on_status: Callable[[McpServerStatus], None] | None = None,
        *, environment: dict[str, str] | None = None,
    ) -> None:
        self._directory = directory / "mcp"
        self._config = config
        self._environment = dict(os.environ if environment is None else environment)
        self._descriptions = _describe(config, workspace, session_id, self._environment)
        self._servers = {server.identifier: server for server in config.servers}
        self._on_status = on_status
        self._tools: tuple[McpTool, ...] = ()
        self._origins: dict[str, tuple[str, str]] = {}
        self._leases: dict[str, str] = {}
        self._pending: dict[str, Future] = {}
        self._pending_lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._ready = threading.Event()
        self._closing = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None
        self._startup_error: BaseException | None = None
        self._failed = False
        self._acquired = False

    def matches(
        self, config: McpConfig, workspace: Path, session_id: str,
        environment: dict[str, str] | None = None,
    ) -> bool:
        return (not self._closing.is_set() and not self._failed
                and self._config == config
                and self._descriptions == _describe(
                    config, workspace, session_id,
                    self._environment if environment is None else environment,
                ))

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(tool.name for tool in self._tools)

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
        if self._acquired or self._closing.is_set():
            raise RuntimeError("MCP handle cannot be acquired twice")
        self._acquired = True
        if not self._descriptions:
            self._ready.set()
            return
        for server in self._descriptions:
            self._emit(server["server"], "connecting")
        self._thread = threading.Thread(target=self._run, name="nosis-mcp-client", daemon=True)
        self._thread.start()

    def wait_ready(self) -> tuple[McpTool, ...]:
        if not self._acquired:
            raise RuntimeError("MCP handle has not been acquired")
        budget = BROKER_STARTUP_SECONDS + max(
            (server["startup_timeout"] for server in self._descriptions), default=0,
        ) + 5
        if not self._ready.wait(budget):
            self.release()
            raise TimeoutError("MCP service did not finish starting")
        if self._startup_error is not None:
            raise self._startup_error
        return self._tools

    def _send(self, message: dict) -> None:
        with self._send_lock:
            if self._socket is None or self._closing.is_set():
                raise ConnectionError("MCP handle is closed")
            self._socket.sendall(encode(message))

    def _run(self) -> None:
        reader = None
        try:
            self._socket, reader = _connect(self._directory, self._closing)
            self._send({"id": "acquire", "method": "acquire", "servers": self._descriptions})
            while line := reader.readline(MESSAGE_LIMIT):
                message = json.loads(line)
                if message.get("event") == "unavailable":
                    self._failed = True
                    for server, lease in self._leases.items():
                        if lease == message["lease"]:
                            self._emit(server, "unavailable", error=message["error"])
                elif message["id"] == "acquire":
                    if "error" in message:
                        raise ConnectionError(message["error"]["message"])
                    self._load_tools(message["result"])
                    self._ready.set()
                else:
                    with self._pending_lock:
                        future = self._pending.pop(message["id"], None)
                    if future is not None:
                        if "error" in message:
                            error = message["error"]
                            if error["type"] == "connection":
                                self._failed = True
                            cls = {"cancelled": AgentCancelled, "timeout": TimeoutError,
                                   "connection": ConnectionError}.get(error["type"], RuntimeError)
                            future.set_exception(cls(error["message"]))
                        else:
                            future.set_result(message["result"])
            if not self._closing.is_set():
                raise ConnectionError("MCP broker disconnected")
        except BaseException as error:
            if not self._closing.is_set():
                self._startup_error = error
                self._failed = True
        finally:
            self._ready.set()
            with self._pending_lock:
                pending = tuple(self._pending.values())
                self._pending.clear()
            for future in pending:
                future.set_exception(ConnectionError("MCP broker disconnected"))
            if reader is not None:
                reader.close()
            if self._socket is not None:
                self._socket.close()

    def _load_tools(self, results: list[dict]) -> None:
        tools = []
        for result in results:
            server = result["server"]
            if "error" in result:
                self._failed = True
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
            self._emit(server, "ready", len(selected))
        self._tools = tuple(tools)

    def call_tool(
        self, server: str, tool: str, arguments: dict[str, JSONValue], *,
        cancellation: CancellationSignal | None = None,
    ) -> JSONValue:
        if (server, tool) not in self._origins.values():
            raise ValueError(f"MCP tool '{server}/{tool}' is not enabled")
        if cancellation is not None and cancellation.cancelled:
            raise AgentCancelled()
        request = uuid4().hex
        future: Future = Future()
        with self._pending_lock:
            self._pending[request] = future
        timeout = self._servers[server].call_timeout_seconds
        deadline = time.monotonic() + timeout
        sent = False
        try:
            self._send({"id": request, "method": "call", "lease": self._leases[server],
                        "tool": tool, "arguments": arguments, "timeout": timeout})
            sent = True
            while True:
                if cancellation is not None and cancellation.cancelled:
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
            with self._pending_lock:
                self._pending.pop(request, None)
            if sent and not future.done():
                try:
                    self._send({"method": "cancel", "request": request})
                except OSError:
                    pass

    def release(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        stream = self._socket
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        for server in self._leases:
            self._emit(server, "closed")

    def _emit(self, server: str, status: str, tool_count=None, error=None) -> None:
        if self._on_status is not None:
            self._on_status(McpServerStatus(server, status, tool_count, error))
