"""Authenticated local transport for the resident host's Bridge protocol."""

import asyncio
import json
import os
import secrets
import tempfile
from functools import partial
from pathlib import Path
from threading import Thread
from typing import TextIO
from uuid import uuid4

from agent_core import SessionInUseError, Workspace
from interfaces.bridge.protocol import (
    PROTOCOL_VERSION, ProtocolError, decode_bridge_command, encode,
    check_open_session_id, session_open_failed_message,
)
from interfaces.session_host import ActiveSessionRegistry, SessionAttachment, SessionRunningError


ENDPOINT_FILENAME = "bridge-endpoint.json"


async def _read_frame(reader: asyncio.StreamReader) -> bytes:
    chunks = []
    while True:
        try:
            chunks.append(await reader.readuntil(b"\n"))
            return b"".join(chunks)
        except asyncio.LimitOverrunError as error:
            chunks.append(await reader.readexactly(error.consumed))
        except asyncio.IncompleteReadError as error:
            if chunks or error.partial.strip():
                raise ProtocolError("Bridge input ended without a final newline") from error
            return b""


def proxy_to_resident(directory: Path, stdin: TextIO, stdout: TextIO) -> None:
    """Relay stdio frames over a named pipe or Unix domain socket."""
    asyncio.run(_proxy_resident(directory, stdin, stdout))


async def _connect(address: str) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if os.name != "nt":
        return await asyncio.open_unix_connection(address)
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await loop.create_pipe_connection(lambda: protocol, address)
    return reader, asyncio.StreamWriter(transport, protocol, reader, loop)


async def _proxy_resident(directory: Path, stdin: TextIO, stdout: TextIO) -> None:
    endpoint = directory / ENDPOINT_FILENAME
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 15
    while True:
        try:
            details = json.loads(endpoint.read_text(encoding="utf-8"))
            reader, writer = await asyncio.wait_for(_connect(details["address"]), timeout=1)
            try:
                writer.write(details["token"].encode("ascii") + b"\n")
                await writer.drain()
                ready = await asyncio.wait_for(reader.readline(), timeout=1)
                if json.loads(ready).get("type") != "bridge_ready":
                    raise ValueError("expected the resident Bridge greeting")
            except (OSError, ValueError):
                writer.close()
                await writer.wait_closed()
                raise
            break
        except (OSError, ValueError) as error:
            if loop.time() >= deadline:
                raise RuntimeError(f"unable to connect to the resident Bridge for {directory}") from error
            await asyncio.sleep(0.1)

    inputs: asyncio.Queue[bytes] = asyncio.Queue()

    def read_stdin() -> None:
        try:
            while chunk := os.read(stdin.fileno(), 65536):
                loop.call_soon_threadsafe(inputs.put_nowait, chunk)
            loop.call_soon_threadsafe(inputs.put_nowait, b"")
        except (OSError, RuntimeError):
            # The inherited stdio can close after the resident connection ends.
            return

    async def forward_input() -> None:
        while chunk := await inputs.get():
            writer.write(chunk)
            await writer.drain()
        writer.write(b'{"type":"shutdown"}\n')
        await writer.drain()

    Thread(target=read_stdin, name="local-bridge-input", daemon=True).start()
    forwarding = asyncio.create_task(forward_input())
    try:
        stdout.buffer.write(ready)
        stdout.buffer.flush()
        while chunk := await reader.read(65536):
            stdout.buffer.write(chunk)
            stdout.buffer.flush()
    finally:
        forwarding.cancel()
        await asyncio.gather(forwarding, return_exceptions=True)
        writer.close()
        await writer.wait_closed()


class LocalBridge:
    def __init__(self, directory: Path, sessions: ActiveSessionRegistry) -> None:
        self._endpoint = directory / ENDPOINT_FILENAME
        self._sessions = sessions
        self._token = secrets.token_hex(32)
        self._server: asyncio.Server | None = None
        self._pipes = ()
        self._socket_directory: tempfile.TemporaryDirectory | None = None
        self._connections: set[asyncio.Task[None]] = set()

    async def start(self) -> None:
        if os.name == "nt":
            address = r"\\.\pipe\nosis-" + secrets.token_hex(16)
            loop = asyncio.get_running_loop()
            self._pipes = await loop.start_serving_pipe(
                lambda: asyncio.StreamReaderProtocol(asyncio.StreamReader(), self._accept),
                address,
            )
        else:
            self._socket_directory = tempfile.TemporaryDirectory(prefix="nosis-")
            address = str(Path(self._socket_directory.name) / "bridge.sock")
            self._server = await asyncio.start_unix_server(self._accept, address)
        fd, name = tempfile.mkstemp(dir=self._endpoint.parent, prefix=".bridge-endpoint-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump({"address": address, "token": self._token}, file)
            os.replace(name, self._endpoint)
        finally:
            Path(name).unlink(missing_ok=True)

    async def close(self) -> None:
        for pipe in self._pipes:
            pipe.close()
        if self._server is not None:
            self._server.close()
        connections = tuple(self._connections)
        for task in connections:
            task.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()
        if self._socket_directory is not None:
            self._socket_directory.cleanup()
        if self._server is not None or self._pipes:
            self._endpoint.unlink(missing_ok=True)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connection = asyncio.current_task()
        self._connections.add(connection)
        try:
            token = await asyncio.wait_for(reader.readline(), timeout=5)
            if secrets.compare_digest(token, self._token.encode("ascii") + b"\n"):
                await self._serve(reader, writer)
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except (ConnectionError, ValueError, asyncio.TimeoutError):
            pass
        finally:
            self._connections.discard(connection)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        channels: dict[str, tuple[SessionAttachment, asyncio.Task[None]]] = {}

        async def emit(message: dict[str, object]) -> None:
            writer.write((encode(message) + "\n").encode("utf-8"))
            await writer.drain()

        async def event(channel_id: str, message: dict[str, object]) -> None:
            await emit({"type": "channel_event", "channel_id": channel_id, "event": message})

        async def relay(channel_id: str, attachment: SessionAttachment, send) -> None:
            try:
                await attachment.relay(send)
                if channels.pop(channel_id, None) is not None:
                    await emit({"type": "channel_closed", "channel_id": channel_id})
            except ConnectionError:
                writer.close()
            finally:
                await attachment.close()

        async def detach(entry: tuple[SessionAttachment, asyncio.Task[None]]) -> None:
            attachment, task = entry
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # The relay may be cancelled before its first instruction.
            await attachment.close()

        await emit({"type": "bridge_ready", "protocol_version": PROTOCOL_VERSION})
        try:
            while line := await _read_frame(reader):
                if not line.strip():
                    continue
                packet = decode_bridge_command(line.decode("utf-8"))
                if packet["type"] == "shutdown":
                    return
                channel_id = packet["channel_id"]
                current = channels.get(channel_id)
                if packet["type"] == "close_channel":
                    if current is not None and packet.get("idle_only"):
                        try:
                            await self._sessions.release_idle(
                                channel_id, attachment_id=current[0].attachment_id,
                            )
                        except SessionRunningError:
                            await emit({"type": "channel_busy", "channel_id": channel_id})
                            continue
                        if channel_id not in channels:
                            # The live relay already sent the close notification.
                            continue
                    if current is not None:
                        channels.pop(channel_id)
                        await detach(current)
                    await emit({"type": "channel_closed", "channel_id": channel_id})
                    continue

                command = packet["command"]
                send = partial(event, channel_id)
                if command["type"] == "open_session":
                    try:
                        check_open_session_id(channel_id, command)
                    except ValueError as error:
                        cursor = current[0].runtime.event_sequence if current is not None else 0
                        await send({**session_open_failed_message(command.get("session_id"), error),
                                    "event_sequence": cursor})
                        continue
                if current is not None:
                    attachment = current[0]
                    if command["type"] == "session_snapshot":
                        await attachment.snapshot(send)
                    else:
                        attachment.send(command)
                    continue
                if command["type"] != "open_session":
                    continue
                try:
                    workspace = Workspace(Path(command["workspace"]))
                    runtime, created = await self._sessions.open_or_attach(
                        channel_id, command, workspace, attach_only=False,
                    )
                    attachment = await self._sessions.subscribe(runtime, f"tui:{uuid4()}", created=created)
                    if attachment is None:
                        raise SessionInUseError(channel_id)
                except (OSError, TypeError, ValueError, SessionInUseError) as error:
                    await send({**session_open_failed_message(command.get("session_id"), error), "event_sequence": 0})
                    continue
                try:
                    if await attachment.replay(send, checkpoint=True):
                        channels[channel_id] = (attachment, asyncio.create_task(relay(channel_id, attachment, send)))
                        continue
                except BaseException:
                    await attachment.close()
                    raise
                await attachment.close()
        except ProtocolError as error:
            await emit({"type": "fatal", "error": {"type": "ProtocolError", "message": str(error), "details": {}}})
        finally:
            await asyncio.gather(*(detach(entry) for entry in tuple(channels.values())))
