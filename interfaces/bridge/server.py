"""Route protocol channels to Session Runtime owners in this Bridge."""

from dataclasses import dataclass
from pathlib import Path
from threading import Lock, Thread
from typing import TextIO

from agent_core import runtime_error_info
from agent_runtime.services import RuntimeServices
from agent_runtime.scheduled_turn import ScheduledTurn

from .bridge import Bridge
from .event_stream import EventStream
from .protocol import (
    PROTOCOL_VERSION, ProtocolError, decode_bridge_command, encode,
    check_open_session_id, runtime_failure_to_dict, session_open_failed_message,
)


@dataclass
class _Session:
    bridge: Bridge
    thread: Thread


class BridgeServer:
    def __init__(self, stdin: TextIO, stdout: TextIO, *,
                 config_directory: Path, announce_sessions: bool = False) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._write_lock = Lock()
        self._lock = Lock()
        self._sessions: dict[str, _Session] = {}
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
                    session = self._sessions.get(channel_id)
                    if session is not None and session.bridge.attached:
                        if message.get("idle_only") and session.bridge.host.busy:
                            self.emit(type="channel_busy", channel_id=channel_id)
                        else:
                            session.bridge.stop()
                    else:
                        self.emit(type="channel_closed", channel_id=channel_id)
                continue
            self._submit_command(channel_id, message["command"])

    def _submit_command(self, session_id: str, command: dict[str, object]) -> None:
        opening = command["type"] == "open_session"
        if opening:
            try:
                check_open_session_id(session_id, command)
            except ValueError as error:
                event = session_open_failed_message(command.get("session_id"), error)
                with self._lock:
                    session = self._sessions.get(session_id)
                    if session is not None and session.bridge.attached:
                        session.bridge.emit(**event)
                    else:
                        self._emit_channel(session_id, EventStream().publish(event))
                return

        with self._lock:
            session = self._sessions.get(session_id)
            if opening:
                if session is None:
                    session = self._create_session(session_id)
                session.bridge.attach(lambda event: self._emit_channel(session_id, event))
            elif session is None or not session.bridge.attached:
                raise ProtocolError("first channel command must be 'open_session'")
            try:
                session.bridge.submit(command)
            except Exception as error:
                session.bridge.emit("fatal", error=runtime_failure_to_dict(runtime_error_info(error)))
                session.bridge.stop()

    def _submit_scheduled(self, turn: ScheduledTurn) -> None:
        session_id = turn.session_id
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                session = self._create_session(session_id)
                if self._announce_sessions:
                    self.emit(type="session_channel", channel_id=session_id)
                    session.bridge.attach(lambda event: self._emit_channel(session_id, event))
            session.bridge.host.submit_scheduled(turn)

    def _create_session(self, session_id: str) -> _Session:
        bridge = Bridge(services=self.services, session_id=session_id)
        thread = Thread(
            target=self._serve_session, args=(session_id, bridge),
            name=f"bridge-session-{session_id}",
        )
        session = _Session(bridge, thread)
        self._sessions[session_id] = session
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
                    self._sessions.pop(session_id)
                    if bridge.attached:
                        self.emit(type="channel_closed", channel_id=session_id)

    def close(self) -> None:
        self.services.close()
        with self._lock:
            sessions = tuple(self._sessions.values())
            for session in sessions:
                session.bridge.stop()
        for session in sessions:
            session.thread.join()
