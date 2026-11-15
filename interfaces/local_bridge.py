"""Authenticated local transport for the resident host's Bridge protocol."""

import asyncio
import json
import os
import secrets
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from threading import Thread
from typing import TextIO
from uuid import uuid4

from agent_core import SessionInUseError, Workspace
from interfaces.bridge.event_stream import EventStream
from interfaces.bridge.protocol import (
    PROTOCOL_VERSION, ProtocolError, decode_bridge_command, encode,
    session_open_failed_message,
)
from interfaces.session_host import ActiveSession, ActiveSessionRegistry


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


@dataclass
class _Attachment:
    runtime: ActiveSession
    attachment_id: str
    queue: asyncio.Queue[dict[str, object] | None]
    task: asyncio.Task[None] | None = None
    cursor: int = 0
    stream: EventStream = field(default_factory=EventStream)

    async def detach(self) -> None:
        await self.runtime.detach(self.queue, self.attachment_id)


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
        channels: dict[str, _Attachment] = {}

        def emit(message: dict[str, object]) -> None:
            writer.write((encode(message) + "\n").encode("utf-8"))

        def event(channel_id: str, message: dict[str, object]) -> None:
            emit({"type": "channel_event", "channel_id": channel_id, "event": message})

        def forward(channel_id: str, attachment: _Attachment, message: dict[str, object]) -> None:
            payload = message.copy()
            payload.pop("event_sequence", None)
            payload.pop("resume_after", None)
            state = payload.pop("runtime", None)
            transcript = payload.pop("transcript", None)
            event(channel_id, attachment.stream.publish(
                payload, state=state, items=transcript["items"] if transcript is not None else None,
            ))

        def snapshot(channel_id: str, attachment: _Attachment, state: dict[str, object],
                     events: list[dict[str, object]], *, resumed: bool) -> None:
            runtime = attachment.runtime
            assert runtime.ready is not None
            checkpoint = runtime.transcript
            forward(channel_id, attachment, {
                **runtime.ready, "resumed": resumed,
                "provider": state["provider"], "workspace": state["workspace"],
                "permission_preset": state["permission_preset"],
                "message_count": len(checkpoint["items"]), "transcript": checkpoint,
            })
            for message in events:
                if message["event_sequence"] > checkpoint["event_sequence"] and message["type"] != "runtime_state":
                    forward(channel_id, attachment, message)
                    if message["type"] in {"fatal", "session_open_failed"}:
                        runtime.mark_failure_delivered()
            forward(channel_id, attachment, state)
            attachment.cursor = runtime.event_sequence

        async def relay(channel_id: str, attachment: _Attachment) -> None:
            try:
                while (message := await attachment.queue.get()) is not None:
                    if message.get("event_sequence", attachment.cursor + 1) <= attachment.cursor:
                        continue
                    forward(channel_id, attachment, message)
                    if message["type"] in {"fatal", "session_open_failed"}:
                        attachment.runtime.mark_failure_delivered()
                    await writer.drain()
                emit({"type": "channel_closed", "channel_id": channel_id})
                await writer.drain()
            except ConnectionError:
                writer.close()
            finally:
                await attachment.detach()
                await self._sessions.remove_if_finished(attachment.runtime)
                channels.pop(channel_id, None)

        async def close_attachment(attachment: _Attachment) -> None:
            assert attachment.task is not None
            attachment.task.cancel()
            await asyncio.gather(attachment.task, return_exceptions=True)
            # A task cancelled before its first instruction cannot run its cleanup.
            await attachment.detach()

        emit({"type": "bridge_ready", "protocol_version": PROTOCOL_VERSION})
        await writer.drain()
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
                    if current is not None:
                        await close_attachment(current)
                        channels.pop(channel_id, None)
                    emit({"type": "channel_closed", "channel_id": channel_id})
                elif current is not None:
                    runtime = current.runtime
                    command = packet["command"]
                    if runtime.owns_attachment(current.attachment_id):
                        if command["type"] == "session_snapshot":
                            events, state = await runtime.snapshot()
                            snapshot(channel_id, current, state, events, resumed=True)
                        else:
                            runtime.send(command)
                else:
                    command = packet["command"]
                    if command["type"] != "open_session":
                        continue
                    session_id = command.get("session_id")
                    attachment_id = f"tui:{uuid4()}"
                    try:
                        workspace = Workspace(Path(command["workspace"]))
                        runtime, _ = await self._sessions.open_or_attach(
                            {**command, "session_id": session_id or str(uuid4())},
                            workspace, attach_only=False,
                        )
                        assert runtime is not None
                        opened = await runtime.attach(0, attachment_id, takeover=False)
                        if opened is None:
                            raise SessionInUseError(runtime.session_id)
                    except (OSError, TypeError, ValueError, SessionInUseError) as error:
                        event(channel_id, EventStream().publish(session_open_failed_message(session_id, error)))
                    else:
                        queue, events, state = opened
                        if runtime.ready is None:
                            for message in events:
                                event(channel_id, message)
                            runtime.mark_failure_delivered()
                            await runtime.detach(queue, attachment_id)
                            await self._sessions.remove_if_finished(runtime)
                        else:
                            attachment = _Attachment(runtime, attachment_id, queue)
                            snapshot(channel_id, attachment, state, events, resumed=bool(session_id))
                            channels[channel_id] = attachment
                            attachment.task = asyncio.create_task(relay(channel_id, attachment))
                await writer.drain()
        except ProtocolError as error:
            emit({"type": "fatal", "error": {"type": "ProtocolError", "message": str(error), "details": {}}})
            await writer.drain()
        finally:
            await asyncio.gather(*(close_attachment(channel) for channel in channels.values()))
