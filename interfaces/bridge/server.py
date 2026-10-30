"""Multiplex independent session adapters over one process transport."""

from pathlib import Path
from threading import Lock, Thread
from typing import TextIO

from agent_core import runtime_error_info
from agent_runtime.services import RuntimeServices

from .bridge import Bridge
from .protocol import PROTOCOL_VERSION, ProtocolError, decode_bridge_command, encode, runtime_failure_to_dict


class BridgeServer:
    def __init__(self, stdin: TextIO, stdout: TextIO, *,
                 config_directory: Path) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._write_lock = Lock()
        self._channels_lock = Lock()
        self._channels: dict[str, tuple[Bridge, Thread]] = {}
        self.services = RuntimeServices(config_directory)

    def emit(self, **message: object) -> None:
        with self._write_lock:
            self._stdout.write(encode(message) + "\n")
            self._stdout.flush()

    def serve(self) -> None:
        self.services.warmup()
        self.emit(type="bridge_ready", protocol_version=PROTOCOL_VERSION)
        for line in self._stdin:
            if not line.strip():
                continue
            message = decode_bridge_command(line)
            if message["type"] == "shutdown":
                return
            channel_id = message["channel_id"]
            with self._channels_lock:
                channel = self._channels.get(channel_id)
                if message["type"] == "close_channel":
                    if channel is not None:
                        channel[0].stop()
                    else:
                        self.emit(type="channel_closed", channel_id=channel_id)
                    continue
                command = message["command"]
                if channel is None:
                    if command["type"] != "open_session":
                        raise ProtocolError("first channel command must be 'open_session'")
                    bridge = Bridge(
                        services=self.services,
                        send=lambda event, key=channel_id: self.emit(
                            type="channel_event", channel_id=key, event=event,
                        ),
                    )
                    thread = Thread(
                        target=self._serve_channel, args=(channel_id, bridge),
                        name=f"bridge-session-{channel_id}",
                    )
                    self._channels[channel_id] = bridge, thread
                    thread.start()
                else:
                    bridge = channel[0]
            try:
                bridge.submit(command)
            except Exception as error:
                bridge.emit("fatal", error=runtime_failure_to_dict(runtime_error_info(error)))
                bridge.stop()

    def _serve_channel(self, channel_id: str, bridge: Bridge) -> None:
        try:
            bridge.serve()
        except BaseException as error:
            bridge.emit("fatal", error=runtime_failure_to_dict(runtime_error_info(error)))
        finally:
            try:
                bridge.close()
            finally:
                with self._channels_lock:
                    self._channels.pop(channel_id, None)
                    self.emit(type="channel_closed", channel_id=channel_id)

    def close(self) -> None:
        with self._channels_lock:
            channels = tuple(self._channels.values())
        for bridge, _ in channels:
            bridge.stop()
        for _, thread in channels:
            thread.join()
        self.services.close()
