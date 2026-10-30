"""Bridge protocol inputs with per-test configuration and scheduler storage."""

import io
import json
import tempfile
import unittest
from pathlib import Path
from typing import TextIO
from unittest.mock import patch

from agent_core.tools.config import TOOL_NAMES
from threading import Lock, Thread
from interfaces.bridge.bridge import Bridge as SessionBridge
from interfaces.bridge.protocol import decode, encode
from agent_runtime.config import default_config_directory
from agent_runtime.services import RuntimeServices


class Bridge(SessionBridge):
    """Feed session-adapter unit tests without a process transport."""

    def __init__(self, stdin, stdout, *, scheduler=None, start_scheduler=True):
        self._stdin = stdin
        self._reader = None
        self._reader_lock = Lock()
        self._test_services = RuntimeServices(
            default_config_directory(), scheduler=scheduler, start_scheduler=start_scheduler,
        )
        super().__init__(services=self._test_services, send=lambda event: stdout.write(encode(event) + "\n"))

    def read_message(self):
        self._start_reader()
        return super().read_message()

    def _register_interaction(self, request_id, turn_id):
        waiter = super()._register_interaction(request_id, turn_id)
        self._start_reader()
        return waiter

    def close(self):
        try:
            super().close()
        finally:
            self._test_services.close()

    def _start_reader(self) -> None:
        with self._reader_lock:
            if self._reader is not None:
                return
            self._reader = Thread(
                target=self._read_stdin,
                name="bridge-input-reader",
                daemon=True,
            )
            self._reader.start()


    def _read_stdin(self) -> None:
        assert self._stdin is not None
        first_message = not self._session_opened
        while True:
            try:
                line = self._stdin.readline()
                if not line:
                    self._route_shutdown()
                    return
                stripped = line.strip()
                if stripped:
                    packet = json.loads(stripped)
                    if isinstance(packet, dict) and packet.get("type") == "shutdown":
                        self.stop()
                        return
                    message = decode(stripped)
                    if first_message:
                        first_message = False
                        if message["type"] == "open_session":
                            self._messages.put(message)
                        else:
                            self.submit(message)
                    else:
                        self.submit(message)
            except ValueError as error:
                self._messages.put(error)
                self._route_shutdown(notify_commands=False)
                return


class BridgeTestCase(unittest.TestCase):
    def make_bridge(
        self,
        lines: list[str] | TextIO,
        config_directory: Path | None = None,
    ) -> tuple[Bridge, io.StringIO]:
        stdin = io.StringIO("".join(f"{line}\n" for line in lines)) if isinstance(lines, list) else lines
        stdout = io.StringIO()
        if config_directory is None:
            config_directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        with patch(
            "tests.bridge_fixtures.default_config_directory",
            return_value=config_directory,
        ):
            bridge = Bridge(stdin, stdout, start_scheduler=False)
        self.addCleanup(bridge.close)
        return bridge, stdout


def emitted(stdout: io.StringIO) -> list[dict]:
    return [
        json.loads(line)
        for line in stdout.getvalue().splitlines()
        if line.strip()
    ]


def open_session_message(
    directory: Path,
    agent_config: dict | None = None,
    provider_config: dict | None = None,
    **extra: object,
) -> dict:
    prompts = directory / "prompts"
    prompts.mkdir(exist_ok=True)
    for filename, content in {
        "Soul.md": "Bridge system prompt for {{workspace}}",
        "SubAgent.md": (
            "Role {{role}}: {{role_description}} in {{workspace}}"
        ),
        "Consolidator.md": "Consolidate the conversation.",
        "GlobalMemory.md": "Reconcile global memory as JSON.",
        "WorkspaceMemory.md": "Reconcile workspace memory as JSON.",
        "SessionTitle.md": "Name this conversation: {{conversation}}",
    }.items():
        path = prompts / filename
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    (directory / "provider_config.json").write_text(
        json.dumps(
            provider_config
            if provider_config is not None
            else {
                "main_agent": {"provider": "first"},
                "subagent": {"provider": ""},
                "providers": {
                    "first": {
                        "model": "openai/first",
                        "max_context_tokens": 1000,
                    },
                    "second": {
                        "model": "openai/second",
                        "max_context_tokens": 1000,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    (directory / "tool_config.json").write_text(
        json.dumps({
            "web_search": {"provider": "duckduckgo"},
            "web_fetch": {"provider": "direct"},
        }), encoding="utf-8",
    )
    resolved_agent_config = {
        "generate_session_title": False,
        "provider": {
            "request_timeout_seconds": 300,
            "max_retries": 2,
        },
        "skills": {
            "global_paths": ["skills"],
            "workspace_paths": [],
        },
        **(agent_config if agent_config is not None else {
            "max_same_tool_calls": 5,
            "output_reserve_tokens": 100,
            "scratch_workspace_root": str(directory / "scratch"),
            "workspace_instruction_files": ["CLAUDE.md", "AGENTS.md"],
            "main_agent": {"tools": {name: False for name in TOOL_NAMES}},
        }),
    }
    (directory / "agent_config.json").write_text(
        json.dumps(
            resolved_agent_config
        ),
        encoding="utf-8",
    )
    return {
        "type": "open_session",
        "workspace": str(directory),
        "session_id": None,
        **extra,
    }
