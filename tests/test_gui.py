import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from agent_core import (
    JsonlSessionStore,
    LLMRequest,
    LLMResponse,
    Message,
    Session,
    Workspace,
)
from interfaces.bridge.settings import SettingsStore

try:
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from interfaces.gui import server
except ModuleNotFoundError:  # pragma: no cover - exercised without [gui]
    TestClient = None


def _symlinks_available() -> bool:
    """Report whether this account may create symlinks.

    Windows needs Developer Mode or administrator rights.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory, "target.txt")
        target.write_text("x", encoding="utf-8")
        try:
            Path(directory, "link.txt").symlink_to(target)
        except OSError:
            return False
    return True


SYMLINKS_AVAILABLE = _symlinks_available()


class FakeBridge:
    """Stands in for the bridge child process.

    Records what the server forwards and emits scripted protocol
    messages in reply, so the relay can be tested without a model. A
    ``None`` in a reply list ends the message stream, which is how a
    real bridge exit reaches the server.
    """

    def __init__(
        self,
        replies: dict[str, list[dict | None]] | None = None,
    ) -> None:
        self.sent: list[dict] = []
        self.cancelled = 0
        self.closed = False
        self._replies = replies or {}
        self._messages: asyncio.Queue[dict | None] = asyncio.Queue()

    def send(self, message: dict) -> None:
        self.sent.append(message)
        for reply in self._replies.get(str(message["type"]), ()):
            self._messages.put_nowait(reply)

    async def read(self) -> dict | None:
        return await self._messages.get()

    def emit(self, *messages: dict | None) -> None:
        for message in messages:
            self._messages.put_nowait(message)

    def cancel_turn(self) -> None:
        self.cancelled += 1
        self._messages.put_nowait(None)

    async def close(self) -> None:
        self.closed = True


@unittest.skipIf(TestClient is None, "Install the gui extra to test the GUI")
class BridgeProcessTest(unittest.TestCase):
    def test_cancel_does_not_interrupt_an_idle_starting_bridge(self) -> None:
        process = SimpleNamespace(returncode=None)
        process.send_signal = Mock()
        bridge = server.BridgeProcess(process)

        bridge.cancel_turn()

        process.send_signal.assert_not_called()

    def test_cancel_routes_to_a_requested_turn(self) -> None:
        stdin = SimpleNamespace(
            is_closing=lambda: False,
            write=Mock(),
        )
        process = SimpleNamespace(returncode=None, stdin=stdin)
        process.send_signal = Mock()
        bridge = server.BridgeProcess(process)

        bridge.send({"type": "user_turn", "turn_id": "t1", "text": "hi"})
        bridge.cancel_turn()

        process.send_signal.assert_not_called()
        self.assertEqual(
            json.loads(stdin.write.call_args.args[0].decode("utf-8")),
            {"type": "cancel", "turn_id": "t1"},
        )

    @unittest.skipIf(os.name == "nt", "POSIX process groups only")
    def test_forced_close_kills_the_bridge_process_group(self) -> None:
        process = SimpleNamespace(returncode=None, pid=123)
        bridge = server.BridgeProcess(process)

        with patch("interfaces.gui.server.os.killpg") as killpg:
            bridge._kill()

        killpg.assert_called_once_with(123, signal.SIGKILL)


@unittest.skipIf(TestClient is None, "Install the gui extra to test the GUI")
class BridgeProcessSpawnTest(unittest.IsolatedAsyncioTestCase):
    async def test_spawn_creates_a_private_process_group(self) -> None:
        process = SimpleNamespace()
        spawn = AsyncMock(return_value=process)

        with patch(
            "interfaces.gui.server.asyncio.create_subprocess_exec",
            spawn,
        ):
            await server.BridgeProcess.spawn(Workspace(Path.cwd()))

        self.assertEqual(
            spawn.call_args.kwargs["creationflags"],
            (
                subprocess.CREATE_NEW_PROCESS_GROUP
                if os.name == "nt"
                else 0
            ),
        )
        self.assertEqual(
            spawn.call_args.kwargs["start_new_session"],
            os.name != "nt",
        )


@unittest.skipIf(TestClient is None, "Install the gui extra to test the GUI")
class ActiveSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_event_replay_window_is_bounded_and_sequences_are_monotonic(self) -> None:
        bridge = FakeBridge()
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        self.addAsyncCleanup(runtime.close)

        bridge.emit(*(
            {
                "type": "assistant_delta",
                "turn_id": "t1",
                "text": str(index),
                "model_call_index": 0,
            }
            for index in range(server.EVENT_REPLAY_LIMIT + 8)
        ))
        await _wait_for_async(
            lambda: runtime.event_sequence == server.EVENT_REPLAY_LIMIT + 8
        )

        self.assertEqual(len(runtime._events), server.EVENT_REPLAY_LIMIT)
        self.assertEqual(runtime._events[0]["event_sequence"], 9)
        self.assertEqual(
            runtime._events[-1]["event_sequence"],
            server.EVENT_REPLAY_LIMIT + 8,
        )

    async def test_runtime_state_replaces_the_cached_attachment_snapshot(self) -> None:
        bridge = FakeBridge()
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        self.addAsyncCleanup(runtime.close)
        approval = {
            "type": "approval_request",
            "turn_id": "t1",
            "request_id": "t1:1",
            "command": "ls",
        }
        bridge.emit(server.runtime_state_message(
            phase="waiting_approval",
            turn_id="t1",
            approval=approval,
            question=None,
            provider="first",
            permission_preset="full_access",
            context_window={"input_tokens": 1},
            jobs=[{"job_id": "j1", "kind": "shell", "status": "running"}],
        ))
        await _wait_for_async(lambda: runtime.event_sequence == 1)

        self.assertEqual(runtime.approval, approval)
        self.assertIsNone(runtime.question)
        self.assertEqual(runtime.permission_preset, "full_access")
        self.assertEqual(list(runtime.jobs), ["j1"])
        self.assertEqual(runtime.context_window, {"input_tokens": 1})

    async def test_plan_updates_replace_the_cached_snapshot(self) -> None:
        bridge = FakeBridge()
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        self.addAsyncCleanup(runtime.close)
        first = {"plan_id": "plan-1", "goal": "Ship", "revision": 1, "steps": []}
        second = {"plan_id": "plan-1", "goal": "Ship", "revision": 2, "steps": []}
        bridge.emit(server.runtime_state_message(
            phase="running",
            turn_id="t1",
            approval=None,
            question=None,
            provider="first",
            permission_preset="ask_for_approval",
            context_window=None,
            jobs=[],
            plan=first,
        ))
        bridge.emit({"type": "plan_updated", "plan": second})
        await _wait_for_async(lambda: runtime.event_sequence == 2)

        self.assertEqual(runtime.plan, second)

    async def test_owner_close_does_not_relabel_the_runtime_as_failed(self) -> None:
        bridge = FakeBridge()
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        runtime.phase = "idle"

        await runtime.close()

        self.assertEqual(runtime.phase, "idle")

    async def test_clean_bridge_exit_does_not_relabel_the_runtime_as_failed(self) -> None:
        bridge = FakeBridge()
        bridge.returncode = 0
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        runtime.phase = "idle"

        bridge.emit(None)
        await _wait_for_async(lambda: runtime.done)

        self.assertEqual(runtime.phase, "idle")

    async def test_bridge_exit_during_a_turn_marks_the_runtime_failed(self) -> None:
        bridge = FakeBridge()
        bridge.returncode = 0
        runtime = server.ActiveSession("s1", "first", "/tmp", [], bridge)
        runtime.phase = "running"

        bridge.emit(None)
        await _wait_for_async(lambda: runtime.done)

        self.assertEqual(runtime.phase, "failed")

@unittest.skipIf(TestClient is None, "Install the gui extra to test the GUI")
class GuiTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "README.md").write_text("Hello Nosis", encoding="utf-8")
        (self.root / "src").mkdir()
        self.store = JsonlSessionStore(self.root / ".nosis" / "sessions")
        self.config = self.root / "config"
        self.config.mkdir()
        (self.config / "provider_config.json").write_text(json.dumps({
            "main_agent": {"provider": "first", "vision_provider": ""},
            "subagent": {"provider": "", "vision_provider": ""},
            "subagent_roles": {},
            "providers": {
                "first": {"model": "openai/first"},
                "second": {"model": "openai/second"},
            },
        }), encoding="utf-8")
        (self.config / "agent_config.json").write_text(json.dumps({
            "max_same_tool_calls": 5,
            "output_reserve_tokens": 100,
            "max_generation_tokens": None,
            "workspace_instruction_files": ["AGENTS.md"],
            "context": {"compression": {"enabled": True, "trigger_ratio": None, "keep_recent_units": 4}},
            "main_agent": {"tools": {"read_file": True}},
            "subagent_roles": {},
            "mcp": {"enabled": False, "servers": {}},
        }), encoding="utf-8")
        (self.config / "skills").mkdir()
        (self.config / "plugins").mkdir()

    def client(self, bridge: FakeBridge | None = None) -> TestClient:
        app = server.create_app(
            Workspace(self.root),
            self.store,
            SettingsStore(self.config),
        )
        client = TestClient(
            app,
            base_url="http://127.0.0.1",
            headers={"host": "127.0.0.1"},
        )
        if bridge is not None:
            async def spawn(cls: object, workspace: Workspace) -> FakeBridge:
                return bridge

            patcher = patch.object(
                server.BridgeProcess,
                "spawn",
                classmethod(spawn),
            )
            patcher.start()
            self.addCleanup(patcher.stop)
        return client

    def test_server_builds_open_session_from_browser_session_choices(self) -> None:
        bridge = FakeBridge(replies={"user_turn": [{"type": "bye"}, None]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "resumed",
                        "attachment_id": "page-1",
                        "provider": "second",
                        "workspace": "/",
                    }
                )
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        # The workspace is not asserted: the server resolves the path the
        # browser sends against the host, so the value it forwards has no
        # host-independent form to compare with.
        opening = bridge.sent[0]
        self.assertEqual(opening["type"], "open_session")
        self.assertEqual(opening["session_id"], "resumed")
        self.assertEqual(opening["provider"], "second")
        self.assertEqual(bridge.sent[1]["text"], "hi")

    def test_rejects_unconfigured_provider_before_spawning(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "provider": "unknown", "attachment_id": "page-1"})
                message = socket.receive_json()

        self.assertEqual(message["type"], "fatal")
        self.assertEqual(bridge.sent, [])

    def test_rejects_an_opening_message_that_is_not_open_session(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                message = socket.receive_json()

        self.assertEqual(message["error"]["type"], "ProtocolError")
        self.assertEqual(bridge.sent, [])

    def test_requires_an_attachment_identity(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1"})
                message = socket.receive_json()

        self.assertEqual(message["error"]["type"], "ProtocolError")
        self.assertIn("attachment_id", message["error"]["message"])
        self.assertEqual(bridge.sent, [])

    def test_runs_multiple_turns_on_the_same_bridge(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {"type": "turn_completed", "turn_id": "done", "usage": None}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "one"}
                )
                self.assertEqual(socket.receive_json()["type"], "turn_completed")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t2", "text": "two"}
                )
                self.assertEqual(socket.receive_json()["type"], "turn_completed")

            self.assertEqual(
                [message["type"] for message in bridge.sent],
                ["open_session", "user_turn", "user_turn"],
            )
            self.assertFalse(bridge.closed)

    def test_a_page_reading_a_running_session_is_sent_every_event(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {
                        "type": "assistant_delta",
                        "turn_id": "t1",
                        "text": "one",
                        "model_call_index": 0,
                    }
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hello"}
                )
                # Reading the delta proves the Runtime holds it for a page.
                self.assertEqual(first.receive_json()["type"], "assistant_delta")

                session = client.get("/api/sessions/s1").json()
                active_sessions = client.get("/api/active-sessions").json()

                self.assertEqual(session["event_sequence"], 0)
                self.assertEqual(active_sessions[0]["event_sequence"], 0)

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "s1",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "takeover": True,
                            "after_event": session["event_sequence"],
                        }
                    )
                    self.assertEqual(second.receive_json()["type"], "session_ready")
                    self.assertEqual(
                        second.receive_json()["type"], "assistant_delta"
                    )
                    state = second.receive_json()

                    # The page now holds both events, so its cursor is the
                    # newest one and a reconnect asks for nothing.
                    self.assertEqual(state["event_sequence"], 2)

                    with client.websocket_connect("/api/session") as third:
                        third.send_json(
                            {
                                "type": "open_session",
                                "session_id": "s1",
                                "attachment_id": "page-3",
                                "attach_only": True,
                                "takeover": True,
                                "after_event": state["event_sequence"],
                            }
                        )
                        self.assertEqual(
                            third.receive_json()["type"], "runtime_state"
                        )

    def test_an_idle_runtime_does_not_resend_the_events_its_journal_holds(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "s1"}],
                "user_turn": [
                    {"type": "turn_completed", "turn_id": "t1", "usage": None}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hello"}
                )
                self.assertEqual(socket.receive_json()["type"], "turn_completed")

            stored = Session("s1")
            stored.begin_turn("t1")
            stored.add_item("user", "hello")
            stored.add_item("assistant", "world")
            stored.finish_turn("completed", "t1")
            self.store.append_events("s1", stored.journal, workspace=self.root)

            session = client.get("/api/sessions/s1").json()
            active_sessions = client.get("/api/active-sessions").json()

            # 'ready' and the completion the journal already holds.
            self.assertEqual(
                [item["content"] for item in session["items"]],
                ["hello", "world"],
            )
            self.assertEqual(session["event_sequence"], 2)
            self.assertEqual(active_sessions[0]["event_sequence"], 2)

            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-2",
                        "attach_only": True,
                        "takeover": True,
                        "after_event": session["event_sequence"],
                    }
                )
                state = second.receive_json()

        self.assertEqual(state["type"], "runtime_state")
        self.assertEqual(state["event_sequence"], 2)

    def test_deleting_a_session_closes_its_idle_runtime_first(self) -> None:
        stored = Session("s1")
        stored.begin_turn("t1")
        stored.add_item("user", "hello")
        stored.finish_turn("completed", "t1")
        self.store.append_events("s1", stored.journal, workspace=self.root)
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})

        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete("/api/sessions/s1")

            self.assertEqual(response.status_code, 200)
            self.assertTrue(bridge.closed)
            self.assertEqual(client.get("/api/active-sessions").json(), [])

    def test_releasing_an_idle_runtime_closes_its_bridge(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete(
                "/api/active-sessions/s1",
                params={"attachment_id": "page-1"},
            )

            self.assertEqual(response.json(), {"released": True})
            self.assertTrue(bridge.closed)
            self.assertEqual(client.get("/api/active-sessions").json(), [])

    def test_stale_model_release_does_not_close_a_replacement_runtime(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-1",
                        "provider": "second",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            response = client.delete(
                "/api/active-sessions/s1",
                params={"provider": "first", "attachment_id": "page-1"},
            )

            self.assertEqual(response.json(), {"released": False})
            self.assertFalse(bridge.closed)

    def test_stale_page_release_does_not_close_a_taken_over_runtime(self) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "s1",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "takeover": True,
                        }
                    )
                    self.assertEqual(first.receive_json()["type"], "attachment_replaced")
                    while True:
                        state = second.receive_json()
                        if state["type"] == "runtime_state":
                            break
                    self.assertEqual(state["phase"], "inactive")

                    response = client.delete(
                        "/api/active-sessions/s1",
                        params={
                            "provider": "first",
                            "attachment_id": "page-1",
                        },
                    )

                    self.assertEqual(response.json(), {"released": False})
                    self.assertFalse(bridge.closed)

    def test_relays_permission_changes_and_restores_runtime_state(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [
                    {
                        "type": "session_ready",
                        "session_id": "s1",
                        "permission_preset": "ask_for_approval",
                    }
                ],
                "permission_set": [
                    {"type": "permission_changed", "preset": "full_access"}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "work"}
                )
                first.send_json(
                    {"type": "permission_set", "preset": "full_access"}
                )
                self.assertEqual(
                    first.receive_json(),
                    {
                        "type": "permission_changed",
                        "preset": "full_access",
                        "event_sequence": 2,
                    },
                )

            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-1",
                        "attach_only": True,
                        "after_event": 2,
                    }
                )
                self.assertEqual(
                    second.receive_json()["permission_preset"],
                    "full_access",
                )
                second.send_json({"type": "cancel", "turn_id": "t1"})

        self.assertEqual(bridge.sent[-1]["type"], "permission_set")

    def test_changes_permission_before_any_turn_without_starting_execution(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [
                    {
                        "type": "session_ready",
                        "session_id": "empty",
                        "permission_preset": "ask_for_approval",
                    },
                    {
                        "type": "runtime_state",
                        "phase": "inactive",
                        "turn_id": None,
                        "approval": None,
                        "question": None,
                        "provider": "first",
                        "permission_preset": "ask_for_approval",
                        "context_window": None,
                        "jobs": [],
                    },
                ],
                "permission_set": [
                    {"type": "permission_changed", "preset": "full_access"}
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "empty",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                self.assertEqual(socket.receive_json()["phase"], "inactive")

                socket.send_json(
                    {"type": "permission_set", "preset": "full_access"}
                )
                self.assertEqual(
                    socket.receive_json()["type"],
                    "permission_changed",
                )

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "permission_set"],
        )

    def test_relays_session_configuration_without_a_user_turn(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [
                    {"type": "session_ready", "session_id": "empty"},
                ],
                "provider_set": [
                    {
                        "type": "provider_changed",
                        "provider": "second",
                        "model": "openai/second",
                    }
                ],
                "workspace_set": [
                    {
                        "type": "workspace_changed",
                        "workspace": str(self.root.resolve()),
                    }
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "empty",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json({"type": "provider_set", "provider": "second"})
                self.assertEqual(
                    socket.receive_json()["type"],
                    "provider_changed",
                )
                socket.send_json(
                    {"type": "workspace_set", "workspace": str(self.root)}
                )
                self.assertEqual(
                    socket.receive_json()["type"],
                    "workspace_changed",
                )

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "provider_set", "workspace_set"],
        )

    def test_reconnect_restores_a_user_question(self) -> None:
        question = {
            "type": "user_question",
            "turn_id": "t1",
            "request_id": "t1:1",
            "question": "Which cache?",
            "options": [{"id": "sqlite", "label": "SQLite"}],
            "allow_free_text": False,
        }
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "shared"}],
                "user_turn": [question],
                "user_question_response": [
                    {"type": "turn_completed", "turn_id": "t1", "usage": None},
                    None,
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {"type": "open_session", "session_id": "shared", "attachment_id": "page-1"}
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "choose"}
                )
                self.assertEqual(first.receive_json()["type"], "user_question")

            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                        "attach_only": True,
                        "after_event": 2,
                    }
                )
                state = second.receive_json()
                self.assertEqual(state["question"], question)
                second.send_json(
                    {
                        "type": "user_question_response",
                        "request_id": "t1:1",
                        "option_id": "sqlite",
                    }
                )
                self.assertEqual(second.receive_json()["type"], "turn_completed")

        self.assertEqual(bridge.sent[-1]["option_id"], "sqlite")

    def test_does_not_forward_unknown_or_lifecycle_messages(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready"}],
                "user_turn": [{"type": "bye"}, None],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1", "attachment_id": "page-1"})
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                # A page must not shut the bridge down or restart it.
                socket.send_json({"type": "shutdown"})
                socket.send_json({"type": "open_session", "session_id": "other", "attachment_id": "page-1"})
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "user_turn"],
        )

    def test_cancel_interrupts_the_bridge_without_being_forwarded(
        self,
    ) -> None:
        bridge = FakeBridge(replies={"open_session": [{"type": "session_ready"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "open_session", "session_id": "s1", "attachment_id": "page-1"})
                self.assertEqual(socket.receive_json()["type"], "session_ready")
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                # The fake ends its stream on cancel, as an exiting
                # bridge would.
                socket.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridge.closed)

        self.assertEqual(bridge.cancelled, 1)
        self.assertEqual(
            [m["type"] for m in bridge.sent],
            ["open_session", "user_turn"],
        )

    def test_reconnects_to_the_same_runtime_and_restores_approval(self) -> None:
        bridge = FakeBridge(
            replies={
                "open_session": [{"type": "session_ready", "session_id": "shared"}],
                "user_turn": [
                    {
                        "type": "approval_request",
                        "turn_id": "t1",
                        "request_id": "t1:1",
                        "command": "ls",
                    }
                ],
                "approval_response": [
                    {"type": "turn_completed", "turn_id": "t1", "usage": None},
                    None,
                ],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "shared", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "list"}
                )
                self.assertEqual(
                    first.receive_json()["type"], "approval_request"
                )

            self.assertFalse(bridge.closed)
            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                        "attach_only": True,
                        "after_event": 2,
                    }
                )
                state = second.receive_json()
                self.assertEqual(state["phase"], "starting")
                self.assertEqual(state["approval"]["request_id"], "t1:1")
                second.send_json(
                    {
                        "type": "approval_response",
                        "request_id": "t1:1",
                        "approved": True,
                    }
                )
                self.assertEqual(
                    second.receive_json()["type"], "turn_completed"
                )

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "user_turn", "approval_response"],
        )
        self.assertTrue(bridge.sent[-1]["approved"])

    def test_background_reconnect_does_not_replace_another_page(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "shared",
                            "attachment_id": "page-2",
                            "attach_only": True,
                        }
                    )
                    replaced = second.receive_json()

                self.assertEqual(
                    replaced,
                    {"type": "attachment_replaced", "phase": "starting"},
                )
                bridge.emit(
                    {
                        "type": "assistant_delta",
                        "turn_id": "t1",
                        "text": "still attached",
                        "model_call_index": 0,
                    }
                )
                self.assertEqual(first.receive_json()["text"], "still attached")
                first.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridge.closed)

    def test_explicit_takeover_replaces_the_previous_page(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                    }
                )
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )

                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {
                            "type": "open_session",
                            "session_id": "shared",
                            "attachment_id": "page-2",
                            "attach_only": True,
                            "after_event": 1,
                            "takeover": True,
                        }
                    )
                    self.assertEqual(
                        first.receive_json(),
                        {"type": "attachment_replaced", "phase": "starting"},
                    )
                    state = second.receive_json()
                    self.assertEqual(state["phase"], "starting")

                    bridge.emit(
                        {
                            "type": "assistant_delta",
                            "turn_id": "t1",
                            "text": "new owner",
                            "model_call_index": 0,
                        }
                    )
                    self.assertEqual(second.receive_json()["text"], "new owner")
                    second.send_json({"type": "cancel", "turn_id": "t1"})
                    _wait_for(client, lambda: bridge.closed)

    def test_replays_events_and_completion_emitted_while_detached(self) -> None:
        bridge = FakeBridge(
            replies={"open_session": [{"type": "session_ready", "session_id": "shared"}]}
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "shared", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")
                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )

            self.assertFalse(bridge.closed)
            bridge.emit(
                {
                    "type": "assistant_delta",
                    "turn_id": "t1",
                    "text": "done",
                    "model_call_index": 0,
                },
                {"type": "turn_completed", "turn_id": "t1", "usage": None},
            )
            _wait_for(
                client,
                lambda: client.get("/api/active-sessions").json()[0]["phase"] == "idle",
            )

            active_sessions = client.get("/api/active-sessions").json()
            self.assertEqual(active_sessions[0]["session_id"], "shared")
            with client.websocket_connect("/api/session") as second:
                second.send_json(
                    {
                        "type": "open_session",
                        "session_id": "shared",
                        "attachment_id": "page-1",
                        "attach_only": True,
                        "after_event": 1,
                    }
                )
                delta = second.receive_json()
                completed = second.receive_json()
                state = second.receive_json()

            self.assertEqual(state["phase"], "idle")
            self.assertEqual(delta["text"], "done")
            self.assertEqual(completed["type"], "turn_completed")
            self.assertFalse(bridge.closed)

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["open_session", "user_turn"],
        )

    def test_replays_a_detached_fatal_then_allows_a_fresh_runtime(self) -> None:
        first = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        second = FakeBridge(replies={"open_session": [{"type": "session_ready", "session_id": "s1"}]})
        bridges = [first, second]

        async def spawn(cls: object, workspace: Workspace) -> FakeBridge:
            return bridges.pop(0)

        with (
            patch.object(
                server.BridgeProcess,
                "spawn",
                classmethod(spawn),
            ),
            self.client() as client,
        ):
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-1"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

            first.emit(
                {
                    "type": "fatal",
                    "error": {
                        "type": "RuntimeError",
                        "message": "broken",
                        "details": {},
                    },
                },
                None,
            )
            _wait_for(client, lambda: first.closed)

            runtime = client.get("/api/active-sessions").json()[0]
            self.assertEqual(runtime["event_sequence"], 1)

            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "s1",
                        "attachment_id": "page-2",
                        "attach_only": True,
                        "takeover": True,
                        "after_event": runtime["event_sequence"],
                    }
                )
                state = socket.receive_json()
                fatal = socket.receive_json()

            self.assertEqual(state["type"], "runtime_state")
            self.assertEqual(state["event_sequence"], 1)
            self.assertEqual(fatal["type"], "fatal")
            self.assertEqual(fatal["event_sequence"], 2)
            _wait_for(client, lambda: client.get("/api/active-sessions").json() == [])

            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "open_session", "session_id": "s1", "attachment_id": "page-3"}
                )
                self.assertEqual(socket.receive_json()["type"], "session_ready")

        self.assertEqual(first.sent[0]["type"], "open_session")
        self.assertEqual(second.sent[0]["type"], "open_session")

    def test_runs_different_sessions_concurrently(self) -> None:
        """Two pages on different sessions must not block each other."""
        bridges: list[FakeBridge] = []

        async def spawn(cls: object, workspace: Workspace) -> FakeBridge:
            # Every connection drives its own agent process, so hand
            # each one a separate fake rather than a shared queue.
            bridge = FakeBridge(replies={"open_session": [{"type": "session_ready"}]})
            bridges.append(bridge)
            return bridge

        with (
            patch.object(
                server.BridgeProcess,
                "spawn",
                classmethod(spawn),
            ),
            self.client() as client,
        ):
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "open_session", "session_id": "one", "attachment_id": "page-1"})
                self.assertEqual(first.receive_json()["type"], "session_ready")

                # The second session stays free even though the first
                # page is still connected and holding its own lock.
                with client.websocket_connect("/api/session") as second:
                    second.send_json({"type": "open_session", "session_id": "two", "attachment_id": "page-2"})
                    self.assertEqual(
                        second.receive_json()["type"],
                        "session_ready",
                    )
                    second.send_json(
                        {"type": "user_turn", "turn_id": "t2", "text": "two"}
                    )
                    second.send_json({"type": "cancel", "turn_id": "t2"})
                    _wait_for(client, lambda: bridges[1].closed)

                first.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "one"}
                )
                first.send_json({"type": "cancel", "turn_id": "t1"})
                _wait_for(client, lambda: bridges[0].closed)

        self.assertEqual(
            [bridge.sent[0]["session_id"] for bridge in bridges],
            ["one", "two"],
        )

    def test_attach_only_does_not_start_a_missing_runtime(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "open_session",
                        "session_id": "missing",
                        "attachment_id": "page-1",
                        "provider": "second",
                        "attach_only": True,
                    }
                )
                state = socket.receive_json()

        self.assertEqual(state["phase"], "inactive")
        self.assertEqual(bridge.sent, [])

    def test_lists_models_without_requiring_keys(self) -> None:
        with self.client() as client:
            self.assertEqual(
                client.get("/api/models").json(),
                {
                    "default": "first",
                    "models": [
                        {"id": "first", "model": "openai/first"},
                        {"id": "second", "model": "openai/second"},
                    ],
                },
            )

    def test_models_are_read_from_current_configuration(self) -> None:
        with self.client() as client:
            document = json.loads((self.config / "provider_config.json").read_text())
            document["main_agent"]["provider"] = "second"
            document["providers"]["third"] = {"model": "openai/third"}
            (self.config / "provider_config.json").write_text(json.dumps(document), encoding="utf-8")

            response = client.get("/api/models").json()

        self.assertEqual(response["default"], "second")
        self.assertIn({"id": "third", "model": "openai/third"}, response["models"])

    def test_settings_api_redacts_and_updates_provider(self) -> None:
        with self.client() as client:
            before = client.get("/api/settings").json()
            response = client.put("/api/settings/providers/third", json={
                "expected_revision": before["revision"],
                "model": "openai/third",
                "url": "https://example.test/v1",
                "max_context_tokens": 2000,
                "api_key": {"action": "set", "value": "secret"},
                "set_default": True,
            })

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["default_provider"], "third")
        self.assertNotIn("secret", json.dumps(body))
        self.assertIn("NOSIS_THIRD_API_KEY=secret", (self.config / ".env").read_text())

    def test_reads_history_written_by_the_tui(self) -> None:
        self.store.bind_workspace("from-tui", self.root)
        session = Session("from-tui")
        session.begin_turn("turn-1")
        session.add_item("user", "TUI 对话")
        session.add_item("assistant", "你好")
        session.finish_turn("completed")
        self.store.append_events("from-tui", session.journal, workspace=self.root)
        with self.client() as client:
            self.assertEqual(
                client.get("/api/sessions").json(),
                [
                    {
                        "workspace": str(self.root.resolve()),
                        "sessions": [
                            {"session_id": "from-tui", "title": "TUI 对话"}
                        ],
                    }
                ],
            )
            data = client.get("/api/sessions/from-tui").json()

        self.assertEqual(data["items"][0]["content"], "TUI 对话")
        self.assertEqual(data["permission_preset"], "ask_for_approval")
        # Only the transcript is exposed, never the request configuration.
        self.assertNotIn("request", data)
        self.assertNotIn("private prompt", json.dumps(data))

    def test_rejects_a_session_id_that_escapes_the_sessions_directory(
        self,
    ) -> None:
        with self.client() as client:
            response = client.get("/api/sessions/..%5Cescape")

        self.assertEqual(response.status_code, 400)

    def test_workspace_listing_rejects_traversal(self) -> None:
        with self.client() as client:
            listing = client.get("/api/workspace").json()
            self.assertEqual(listing["root"], str(self.root.resolve()))
            self.assertIn(
                {"name": "src", "type": "directory"},
                listing["entries"],
            )
            for path in ("..", "/tmp"):
                self.assertEqual(
                    client.get(
                        "/api/workspace",
                        params={"path": path},
                    ).status_code,
                    400,
                )

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_workspace_listing_rejects_external_symlinks(self) -> None:
        (self.root / "outside").symlink_to(
            self.root.parent,
            target_is_directory=True,
        )
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/workspace",
                    params={"path": "outside"},
                ).status_code,
                400,
            )

    def test_serves_a_workspace_image_the_agent_read(self) -> None:
        """``read_image`` may load any image, not only an upload."""
        from tests.test_media import png_bytes

        (self.root / "src" / "diagram.png").write_bytes(png_bytes(8, 8))
        with self.client() as client:
            response = client.get(
                "/api/workspace-image",
                params={"path": "src/diagram.png"},
            )

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"], "image/png")

    def test_the_workspace_image_type_comes_from_the_bytes(self) -> None:
        """A mislabelled extension must not set the served type."""
        from tests.test_media import jpeg_bytes

        (self.root / "lying.png").write_bytes(jpeg_bytes(8, 8))
        with self.client() as client:
            response = client.get(
                "/api/workspace-image", params={"path": "lying.png"}
            )

            self.assertEqual(response.headers["content-type"], "image/jpeg")

    def test_the_workspace_image_route_refuses_non_images(self) -> None:
        """It must not become a way to read arbitrary workspace files."""
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/workspace-image", params={"path": "README.md"}
                ).status_code,
                404,
            )

    def test_the_workspace_image_route_rejects_traversal(self) -> None:
        secret = self.root.parent / "secret.png"
        secret.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        self.addCleanup(secret.unlink)
        with self.client() as client:
            for path in ("../secret.png", "/etc/hosts", "src/../../secret.png"):
                with self.subTest(path=path):
                    self.assertEqual(
                        client.get(
                            "/api/workspace-image", params={"path": path}
                        ).status_code,
                        404,
                    )

    @unittest.skipUnless(
        SYMLINKS_AVAILABLE,
        "creating symlinks needs Developer Mode or administrator rights "
        "on Windows",
    )
    def test_the_workspace_image_route_rejects_external_symlinks(self) -> None:
        from tests.test_media import png_bytes

        outside = self.root.parent / "outside.png"
        outside.write_bytes(png_bytes(8, 8))
        self.addCleanup(outside.unlink)
        (self.root / "link.png").symlink_to(outside)
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/workspace-image", params={"path": "link.png"}
                ).status_code,
                404,
            )

    def test_the_workspace_image_route_404s_an_unreadable_file(self) -> None:
        """An unreadable file is a missing image, not a server fault."""
        from tests.test_media import png_bytes

        path = self.root / "locked.png"
        path.write_bytes(png_bytes(8, 8))
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o644)
        if os.access(path, os.R_OK):
            self.skipTest("this user can read a mode-000 file")
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/workspace-image", params={"path": "locked.png"}
                ).status_code,
                404,
            )

    def test_the_workspace_image_route_serves_a_large_image(self) -> None:
        """The 5 MiB ceiling bounds model delivery, not display."""
        from agent_core.media import MAX_IMAGE_BYTES

        path = self.root / "huge.png"
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1024)
        )
        with self.client() as client:
            self.assertEqual(
                client.get(
                    "/api/workspace-image", params={"path": "huge.png"}
                ).status_code,
                200,
            )

    def test_upload_names_an_attachment_after_its_bytes(self) -> None:
        """A declared media type never decides what an upload is."""
        from tests.test_media import jpeg_bytes

        with self.client() as client:
            data = client.post(
                "/api/attachments",
                files=[("files", ("photo.png", jpeg_bytes(8, 8), "image/png"))],
            ).json()

        attachment = data["attachments"][0]
        self.assertEqual(attachment["mime_type"], "image/jpeg")
        self.assertTrue(attachment["path"].endswith(".jpg"))
        self.assertTrue(
            (self.root / attachment["path"]).is_file(),
            attachment["path"],
        )

    def test_upload_rejects_a_file_that_is_not_an_image(self) -> None:
        with self.client() as client:
            response = client.post(
                "/api/attachments",
                files=[("files", ("notes.png", b"Hello Nosis", "image/png"))],
            )

        self.assertEqual(response.status_code, 415)
        self.assertEqual(
            list((self.root / ".nosis" / "attachments").iterdir()),
            [],
        )

    def test_rejects_foreign_origins_and_hosts(self) -> None:
        with self.client(FakeBridge()) as client:
            with self.assertRaises(WebSocketDisconnect):
                with client.websocket_connect(
                    "/api/session",
                    headers={"origin": "https://other.example"},
                ):
                    pass
            self.assertEqual(
                client.get(
                    "/api/sessions",
                    headers={"host": "other.example"},
                ).status_code,
                400,
            )


class GuiStartupTest(unittest.TestCase):
    @unittest.skipIf(TestClient is None, "Install the gui extra to test the GUI")
    def test_initializes_shared_default_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_directory = root / "nosis"
            with (
                patch.object(
                    server,
                    "default_config_directory",
                    return_value=config_directory,
                ),
                patch("uvicorn.run") as serve,
                patch("builtins.print"),
            ):
                server.main(["--workspace", str(root)])

            self.assertTrue(
                (config_directory / "provider_config.json").is_file()
            )
            self.assertTrue((config_directory / "agent_config.json").is_file())
            with TestClient(
                serve.call_args.args[0],
                base_url="http://127.0.0.1",
            ) as client:
                self.assertEqual(
                    client.get("/api/models").json()["default"],
                    "openai",
                )


async def _wait_for_async(condition, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for the runtime")
        await asyncio.sleep(0.01)


def _wait_for(client, condition, timeout: float = 2.0) -> None:
    """Wait for a condition the server's own tasks produce.

    The test client runs the application's event loop only while a
    request is in flight, so polling from the test thread would never
    schedule those tasks.  Each poll therefore issues a request of its
    own: the real server's loop runs continuously, and this keeps the
    loop turning the same way.
    """

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        client.get("/api/models")
    raise AssertionError("timed out waiting for the relay")


if __name__ == "__main__":
    unittest.main()
