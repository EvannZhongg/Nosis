"""One frontend-owned Bridge process and its independent session channels."""

import asyncio
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable

from agent_core import Workspace
from interfaces.bridge.event_stream import EventStream
from interfaces.bridge.protocol import PROTOCOL_VERSION


SHUTDOWN_TIMEOUT_SECONDS = 5


class BridgeChannel:
    def __init__(self, owner: "BridgeProcess", channel_id: str) -> None:
        self.owner = owner
        self.channel_id = channel_id
        self._messages: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()
        self._closed = asyncio.Event()
        self._sequence = 0
        self._idle_close: asyncio.Future[bool] | None = None

    def send(self, message: dict[str, object]) -> None:
        self.owner.send({
            "type": "channel_command", "channel_id": self.channel_id, "command": message,
        })

    async def read(self) -> dict[str, object] | None:
        return await self._messages.get()

    def deliver(self, message: dict[str, object]) -> None:
        self._sequence = int(message["event_sequence"])
        self._messages.put_nowait(message)

    def finish(self, error: Exception | None = None) -> None:
        if self._closed.is_set():
            return
        if error is not None:
            stream = EventStream()
            stream.sequence = self._sequence
            self.deliver(stream.publish({
                "type": "fatal",
                "error": {"type": type(error).__name__, "message": str(error), "details": {}},
            }))
        self._closed.set()
        self._finish_idle_close(True)
        self._messages.put_nowait(None)

    def _finish_idle_close(self, released: bool) -> None:
        if self._idle_close is not None:
            self._idle_close.set_result(released)
            self._idle_close = None

    def busy(self) -> None:
        self._finish_idle_close(False)

    async def close(self, *, idle_only: bool = False) -> bool:
        if self._closed.is_set():
            return True
        message = {"type": "close_channel", "channel_id": self.channel_id}
        if idle_only:
            if self._idle_close is None:
                self.owner.send({**message, "idle_only": True})
                self._idle_close = asyncio.get_running_loop().create_future()
            return await asyncio.shield(self._idle_close)
        self.owner.send(message)
        await self._closed.wait()
        return True


class BridgeProcess:
    def __init__(self, process: asyncio.subprocess.Process,
                 on_session: Callable[[BridgeChannel], None] | None = None) -> None:
        self._process = process
        self._read_buffer = bytearray()
        self._channels: dict[str, BridgeChannel] = {}
        self._ready = asyncio.Event()
        self._error: Exception | None = None
        self._reader: asyncio.Task[None] | None = None
        self._closing = False
        self._close_task: asyncio.Task[None] | None = None
        self._on_session = on_session

    @classmethod
    async def spawn(cls, workspace: Workspace, config_directory: Path, *,
                    on_session: Callable[[BridgeChannel], None] | None = None) -> "BridgeProcess":
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "interfaces.bridge",
            "--config-directory", str(config_directory),
            *(("--managed",) if on_session is not None else ()),
            cwd=workspace.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            start_new_session=os.name != "nt",
        )
        bridge = cls(process, on_session)
        bridge._reader = asyncio.create_task(bridge._dispatch())
        try:
            await bridge._ready.wait()
            if bridge._error is not None:
                raise bridge._error
        except BaseException:
            await bridge.close()
            raise
        return bridge

    def open_channel(self, channel_id: str) -> BridgeChannel:
        if self._error is not None:
            raise self._error
        if self._closing:
            raise RuntimeError("Bridge is closing")
        if channel_id in self._channels:
            raise ValueError(f"channel '{channel_id}' is already open")
        channel = BridgeChannel(self, channel_id)
        self._channels[channel_id] = channel
        return channel

    def send(self, message: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            raise RuntimeError("Bridge input is closed")
        stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))

    async def read(self) -> dict[str, object] | None:
        stdout = self._process.stdout
        if stdout is None:
            return None
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._read_buffer[:newline])
                del self._read_buffer[:newline + 1]
                if line.strip():
                    return json.loads(line)
            else:
                chunk = await stdout.read(65536)
                if chunk:
                    self._read_buffer.extend(chunk)
                    continue
                await self._process.wait()
                if self._read_buffer.strip():
                    raise ValueError("Bridge output ended without a final newline")
                return None

    async def _dispatch(self) -> None:
        try:
            while (message := await self.read()) is not None:
                kind = message["type"]
                if kind == "bridge_ready":
                    if message["protocol_version"] != PROTOCOL_VERSION:
                        raise ValueError("Unsupported Bridge protocol version")
                    self._ready.set()
                elif kind == "fatal":
                    raise RuntimeError(str(message["error"]["message"]))
                elif kind == "session_channel" and self._on_session is not None:
                    channel_id = message["channel_id"]
                    if channel_id not in self._channels and not self._closing:
                        self._on_session(self.open_channel(channel_id))
                elif kind == "channel_event":
                    channel = self._channels.get(message["channel_id"])
                    if channel is not None:
                        channel.deliver(message["event"])
                elif kind == "channel_closed":
                    channel = self._channels.pop(message["channel_id"], None)
                    if channel is not None:
                        channel.finish()
                elif kind == "channel_busy":
                    channel = self._channels.get(message["channel_id"])
                    if channel is not None:
                        channel.busy()
                else:
                    raise ValueError(f"Unknown Bridge event: {kind}")
            if not self._closing:
                raise RuntimeError(f"Bridge exited unexpectedly ({self.returncode})")
        except Exception as error:
            self._error = error
        finally:
            self._ready.set()
            for channel in self._channels.values():
                channel.finish(self._error)
            self._channels.clear()
            if self._error is not None and self._close_task is None:
                self._close_task = asyncio.create_task(self._close())

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    @property
    def available(self) -> bool:
        return not self._closing and self._error is None and self.returncode is None

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if not self._closing:
            self._closing = True
            stdin = self._process.stdin
            if stdin is not None and not stdin.is_closing():
                self.send({"type": "shutdown"})
                stdin.close()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=SHUTDOWN_TIMEOUT_SECONDS)
            except (asyncio.TimeoutError, ConnectionResetError):
                self._kill()
                await self._process.wait()
        if self._reader is not None:
            await self._reader

    def _kill(self) -> None:
        if self.returncode is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        else:
            try:
                os.killpg(self._process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
