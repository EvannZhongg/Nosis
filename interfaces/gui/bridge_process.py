"""Lifecycle wrapper for the GUI's bridge child process."""

import asyncio
import json
import os
import signal
import subprocess
import sys

from agent_core import Workspace
from interfaces.bridge.process import cancel_process


SHUTDOWN_TIMEOUT_SECONDS = 2
TURN_END_MESSAGE_TYPES = frozenset(
    {"turn_completed", "turn_cancelled", "turn_failed"}
)


class BridgeProcess:
    """A bridge child process addressed as a protocol message stream."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self._opened = False
        self._turn_running = False
        self._turn_id: str | None = None
        self._read_buffer = bytearray()

    @classmethod
    async def spawn(cls, workspace: Workspace) -> "BridgeProcess":
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "interfaces.bridge",
            cwd=workspace.path,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Its own process group keeps a cancel interrupt aimed at this
            # child instead of at the console every process shares.
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
            ),
            start_new_session=os.name != "nt",
        )
        return cls(process)

    def send(self, message: dict[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None or stdin.is_closing():
            return
        if message.get("type") == "user_turn":
            self._turn_running = True
            self._turn_id = str(message.get("turn_id"))
        stdin.write(
            (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        )

    async def read(self) -> dict[str, object] | None:
        """Return the next message from the bridge, or None once it ends."""
        stdout = self._process.stdout
        if stdout is None:
            return None
        while True:
            newline = self._read_buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._read_buffer[:newline])
                del self._read_buffer[:newline + 1]
            else:
                # Transcript checkpoints can exceed StreamReader's line limit.
                chunk = await stdout.read(65536)
                if chunk:
                    self._read_buffer.extend(chunk)
                    continue
                await self._process.wait()
                if not self._read_buffer:
                    return None
                line = bytes(self._read_buffer)
                self._read_buffer.clear()
            stripped = line.strip()
            if stripped:
                message = json.loads(stripped)
                message_type = message.get("type")
                if message_type == "session_ready":
                    self._opened = True
                if message_type in TURN_END_MESSAGE_TYPES:
                    self._turn_running = False
                    self._turn_id = None
                return message

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def cancel_turn(self) -> None:
        """Route cancellation to the active turn."""
        if self._turn_running and self._turn_id is not None:
            self.send({"type": "cancel", "turn_id": self._turn_id})

    async def close(self) -> None:
        # Explicit Runtime shutdown routes cancellation first so a running
        # turn can journal what it already produced before the process exits.
        if not self._opened and self._process.returncode is None:
            cancel_process(self._process)
        else:
            self.cancel_turn()
        self.send({"type": "shutdown"})
        if self._process.stdin is not None:
            self._process.stdin.close()
        try:
            await asyncio.wait_for(
                self._process.wait(),
                timeout=SHUTDOWN_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, ConnectionResetError):
            self._kill()
            await self._process.wait()

    def _kill(self) -> None:
        """Kill the bridge together with the processes it started.

        A venv ``python.exe`` is a launcher, so terminating only the process
        that was spawned would leave the real bridge running on the session
        and holding its MCP servers open.
        """
        if self._process.returncode is not None:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(self._process.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return
        try:
            os.killpg(self._process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
