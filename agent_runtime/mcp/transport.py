"""Host-owned IPC connection and startup of the shared MCP Broker."""

import json
import os
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Callable
from uuid import uuid4

from agent_core.turn_control import AgentCancelled
from .wire import MESSAGE_LIMIT, encode

BROKER_STARTUP_SECONDS = 30


@contextmanager
def _startup_lock(directory: Path, closing: threading.Event, deadline: float):
    with (directory / "startup.lock").open("a+b") as handle:
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


def _existing_connection(directory: Path, *, deadline: float | None = None):
    stream = None
    reader = None
    try:
        endpoint = json.loads((directory / "broker.json").read_text(encoding="utf-8"))
        timeout = 2 if deadline is None else max(0.001, min(2, deadline - time.monotonic()))
        stream = socket.create_connection(("127.0.0.1", endpoint["port"]), timeout=timeout)
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


def _connect(directory: Path, closing: threading.Event, deadline: float, *, idle_seconds: float = 300):
    directory.mkdir(parents=True, exist_ok=True)
    with _startup_lock(directory, closing, deadline):
        if closing.is_set():
            raise AgentCancelled()
        existing = _existing_connection(directory, deadline=deadline)
        if existing is not None:
            return existing
        (directory / "broker.json").unlink(missing_ok=True)
        subprocess.run(
            [sys.executable, "-m", "agent_runtime.mcp.broker", "--launch", str(directory), str(idle_seconds)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            start_new_session=os.name != "nt", check=True, timeout=max(0.001, deadline - time.monotonic()),
        )
        while time.monotonic() < deadline:
            if closing.is_set():
                raise AgentCancelled()
            existing = _existing_connection(directory, deadline=deadline)
            if existing is not None:
                return existing
            closing.wait(0.05)
        raise TimeoutError(f"MCP broker did not start; see {directory / 'broker.log'}")


class McpTransport:
    def __init__(self, directory: Path, on_event: Callable[[dict], None]) -> None:
        self._directory = directory / "mcp"
        self._on_event = on_event
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._pending: dict[str, tuple[dict, Future]] = {}
        self._error: BaseException | None = None

    @property
    def started(self) -> bool:
        return self._thread is not None

    @property
    def failed(self) -> bool:
        return not self._closing.is_set() and self._error is not None

    def request(self, method: str, **parameters) -> tuple[str, Future]:
        request_id = uuid4().hex
        message = {"id": request_id, "method": method, **parameters}
        future: Future = Future()
        with self._lock:
            if self._closing.is_set() or self._error is not None:
                future.set_exception(self._error or ConnectionError("MCP client is closed"))
                return request_id, future
            self._pending[request_id] = (message, future)
            if self._thread is None:
                deadline = time.monotonic() + BROKER_STARTUP_SECONDS
                self._thread = threading.Thread(
                    target=self._run, args=(deadline,), name="nosis-mcp-client", daemon=True,
                )
                self._thread.start()
            if self._socket is not None:
                try:
                    self._socket.sendall(encode(message))
                except OSError as error:
                    self._pending.pop(request_id)
                    future.set_exception(error)
        return request_id, future

    def cancel(self, request_id: str) -> None:
        with self._lock:
            pending = self._pending.pop(request_id, None)
            if pending is None:
                return
            pending[1].cancel()
            if self._socket is not None and not self._closing.is_set():
                try:
                    self._socket.sendall(encode({"method": "cancel", "request": request_id}))
                except OSError:
                    pass

    def _run(self, deadline: float) -> None:
        reader = stream = None
        try:
            stream, reader = _connect(self._directory, self._closing, deadline)
            with self._lock:
                if self._closing.is_set():
                    return
                for message, _ in self._pending.values():
                    stream.sendall(encode(message))
                self._socket = stream
            while line := reader.readline(MESSAGE_LIMIT):
                message = json.loads(line)
                if "event" in message:
                    self._on_event(message)
                    continue
                with self._lock:
                    pending = self._pending.pop(message["id"], None)
                    if pending is not None:
                        future = pending[1]
                        if "error" in message:
                            error = message["error"]
                            cls = {"cancelled": AgentCancelled, "timeout": TimeoutError,
                                   "connection": ConnectionError}.get(error["type"], RuntimeError)
                            future.set_exception(cls(error["message"]))
                        else:
                            future.set_result(message["result"])
            raise ConnectionError("MCP broker disconnected")
        except BaseException as error:
            with self._lock:
                self._error = error
        finally:
            with self._lock:
                self._socket = None
                pending = tuple(self._pending.values())
                self._pending.clear()
                error = self._error or ConnectionError("MCP client is closed")
                for _, future in pending:
                    future.set_exception(error)
            if reader is not None:
                reader.close()
            if stream is not None:
                stream.close()
            if not self._closing.is_set():
                self._on_event({"event": "disconnected", "error": str(error) or type(error).__name__})

    def close(self) -> None:
        self._closing.set()
        with self._lock:
            if self._socket is not None:
                try:
                    self._socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
