"""Route protocol channels to Session Runtime owners in this Bridge."""

from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import TextIO
from uuid import uuid4

from agent_core import SessionInUseError, runtime_error_info
from agent_runtime.services import RuntimeServices
from agent_runtime.session_inbox import ScheduledTurn

from .bridge import Bridge
from .event_stream import EventStream
from .protocol import (
    PROTOCOL_VERSION, ProtocolError, decode_bridge_command, encode,
    runtime_failure_to_dict, session_open_failed_message,
)


@dataclass
class _Session:
    bridge: Bridge
    thread: Thread
    channel_id: str | None
    announced: bool = False


class BridgeServer:
    def __init__(self, stdin: TextIO, stdout: TextIO, *,
                 config_directory: Path, announce_sessions: bool = False) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._write_lock = Lock()
        self._lock = Lock()
        self._sessions: dict[str, _Session] = {}
        self._channels: dict[str, _Session] = {}
        self._announce_sessions = announce_sessions
        self.services = RuntimeServices(config_directory)

    def emit(self, **message: object) -> None:
        with self._write_lock:
            self._stdout.write(encode(message) + "\n")
            self._stdout.flush()

    def serve(self) -> None:
        self.services.warmup()
        self.emit(type="bridge_ready", protocol_version=PROTOCOL_VERSION)
        self.services.start_scheduling(self._submit_scheduled)
        for line in self._stdin:
            if not line.strip():
                continue
            message = decode_bridge_command(line)
            if message["type"] == "shutdown":
                return
            channel_id = message["channel_id"]
            if message["type"] == "close_channel":
                with self._lock:
                    session = self._channels.get(channel_id)
                    if session is not None:
                        host = session.bridge.host
                        if message.get("idle_only") and (host.turns.busy or host.inbox.scheduled):
                            self.emit(type="channel_busy", channel_id=channel_id)
                        else:
                            session.bridge.stop()
                    else:
                        self.emit(type="channel_closed", channel_id=channel_id)
                continue
            self._submit_command(channel_id, message["command"])

    def _submit_command(self, channel_id: str, command: dict[str, object]) -> None:
        with self._lock:
            session = self._channels.get(channel_id)
            if (
                session is not None and session.announced
                and command["type"] == "open_session" and command.get("session_id") == channel_id
            ):
                # The scheduled owner already published (or will publish) its opening.
                return
            if session is None:
                if command["type"] != "open_session":
                    raise ProtocolError("first channel command must be 'open_session'")
                session_id = command.get("session_id") or str(uuid4())
                if not isinstance(session_id, str):
                    raise ProtocolError("'session_id' must be a string or null")
                session = self._sessions.get(session_id)
                if session is not None and session.channel_id is not None:
                    event = session_open_failed_message(session_id, SessionInUseError(session_id))
                    self._emit_channel(channel_id, EventStream().publish(event))
                    self.emit(type="channel_closed", channel_id=channel_id)
                    return
                if session is None:
                    session = self._create_session(session_id, channel_id)
                else:
                    session.channel_id = channel_id
                    self._channels[channel_id] = session
                    session.bridge.attach(lambda event: self._emit_channel(channel_id, event))
                    return
            try:
                session.bridge.submit(command)
            except Exception as error:
                session.bridge.emit("fatal", error=runtime_failure_to_dict(runtime_error_info(error)))
                session.bridge.stop()

    def _submit_scheduled(self, turn: ScheduledTurn) -> None:
        with self._lock:
            session = self._sessions.get(turn.session_id)
            if session is None:
                channel_id = turn.session_id if self._announce_sessions else None
                if channel_id is not None:
                    self.emit(type="session_channel", channel_id=channel_id)
                session = self._create_session(turn.session_id, channel_id)
                session.announced = channel_id is not None
            session.bridge.host.inbox.submit(turn)

    def _create_session(self, session_id: str, channel_id: str | None) -> _Session:
        bridge = Bridge(services=self.services, session_id=session_id)
        thread = Thread(
            target=self._serve_session, args=(session_id, bridge),
            name=f"bridge-session-{session_id}",
        )
        session = _Session(bridge, thread, channel_id)
        self._sessions[session_id] = session
        if channel_id is not None:
            self._channels[channel_id] = session
            bridge.attach(lambda event: self._emit_channel(channel_id, event))
        thread.start()
        return session

    def _emit_channel(self, channel_id: str, event: dict[str, object]) -> None:
        self.emit(type="channel_event", channel_id=channel_id, event=event)

    def _serve_session(self, session_id: str, bridge: Bridge) -> None:
        try:
            bridge.serve()
        except BaseException as error:
            bridge.emit("fatal", error=runtime_failure_to_dict(runtime_error_info(error)))
        finally:
            try:
                bridge.close()
            finally:
                with self._lock:
                    channel_id = self._sessions.pop(session_id).channel_id
                    if channel_id is not None:
                        self._channels.pop(channel_id)
                        self.emit(type="channel_closed", channel_id=channel_id)

    def close(self) -> None:
        self.services.close()
        with self._lock:
            sessions = tuple(self._sessions.values())
            for session in sessions:
                session.bridge.stop()
        for session in sessions:
            session.thread.join()
