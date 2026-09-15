import asyncio
import json
import os
import signal
import subprocess
import tempfile
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

    def test_cancel_interrupts_a_requested_turn(self) -> None:
        stdin = SimpleNamespace(
            is_closing=lambda: False,
            write=Mock(),
        )
        process = SimpleNamespace(returncode=None, stdin=stdin)
        process.send_signal = Mock()
        bridge = server.BridgeProcess(process)

        bridge.send({"type": "user_turn", "turn_id": "t1", "text": "hi"})
        bridge.cancel_turn()

        process.send_signal.assert_called_once_with(
            signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT
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
class GuiTest(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "README.md").write_text("Hello Nosis", encoding="utf-8")
        (self.root / "src").mkdir()
        self.provider_config_path = self.root / "provider_config.json"
        self.agent_config_path = self.root / "agent_config.json"
        self.store = JsonlSessionStore(self.root / ".nosis" / "sessions")

    def client(self, bridge: FakeBridge | None = None) -> TestClient:
        app = server.create_app(
            Workspace(self.root),
            self.store,
            self.provider_config_path,
            self.agent_config_path,
            models={"first": "openai/first", "second": "openai/second"},
            default_model="first",
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

    def test_server_builds_start_so_pages_cannot_choose_a_config(self) -> None:
        bridge = FakeBridge(replies={"user_turn": [{"type": "bye"}, None]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {
                        "type": "start",
                        "session_id": "resumed",
                        "provider": "second",
                        # Ignored: the server supplies its own paths.
                        "provider_config_path": "/etc/passwd",
                        "workspace": "/",
                    }
                )
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        self.assertEqual(
            bridge.sent[0],
            {
                "type": "start",
                "workspace": str(self.root.resolve()),
                "session_id": "resumed",
                "provider_config_path": str(self.provider_config_path),
                "agent_config_path": str(self.agent_config_path),
                "provider": "second",
            },
        )
        self.assertEqual(bridge.sent[1]["text"], "hi")

    def test_rejects_unconfigured_provider_before_spawning(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "start", "provider": "unknown"})
                message = socket.receive_json()

        self.assertEqual(message["type"], "fatal")
        self.assertEqual(bridge.sent, [])

    def test_rejects_an_opening_message_that_is_not_start(self) -> None:
        bridge = FakeBridge()
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                message = socket.receive_json()

        self.assertEqual(message["error"]["type"], "ProtocolError")
        self.assertEqual(bridge.sent, [])

    def test_relays_approval_round_trip_in_both_directions(self) -> None:
        bridge = FakeBridge(
            replies={
                "start": [{"type": "ready", "session_id": "s1"}],
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
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "start", "session_id": None})
                self.assertEqual(socket.receive_json()["type"], "ready")

                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "list"}
                )
                request = socket.receive_json()
                self.assertEqual(request["command"], "ls")

                socket.send_json(
                    {
                        "type": "approval_response",
                        "request_id": request["request_id"],
                        "approved": True,
                    }
                )
                self.assertEqual(
                    socket.receive_json()["type"],
                    "turn_completed",
                )

        self.assertTrue(bridge.sent[-1]["approved"])
        self.assertTrue(bridge.closed)

    def test_does_not_forward_unknown_or_lifecycle_messages(self) -> None:
        bridge = FakeBridge(
            replies={
                "start": [{"type": "ready"}],
                "user_turn": [{"type": "bye"}, None],
            }
        )
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "start", "session_id": None})
                self.assertEqual(socket.receive_json()["type"], "ready")
                # A page must not shut the bridge down or restart it.
                socket.send_json({"type": "shutdown"})
                socket.send_json({"type": "start", "session_id": "other"})
                socket.send_json(
                    {"type": "user_turn", "turn_id": "t1", "text": "hi"}
                )
                self.assertEqual(socket.receive_json()["type"], "bye")

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["start", "user_turn"],
        )

    def test_cancel_interrupts_the_bridge_without_being_forwarded(
        self,
    ) -> None:
        bridge = FakeBridge(replies={"start": [{"type": "ready"}]})
        with self.client(bridge) as client:
            with client.websocket_connect("/api/session") as socket:
                socket.send_json({"type": "start", "session_id": None})
                self.assertEqual(socket.receive_json()["type"], "ready")
                # The fake ends its stream on cancel, as an exiting
                # bridge would.
                socket.send_json({"type": "cancel"})
                _wait_for(lambda: bridge.closed)

        self.assertEqual(bridge.cancelled, 1)
        self.assertEqual([m["type"] for m in bridge.sent], ["start"])

    def test_refuses_a_second_page_for_the_same_session(self) -> None:
        """Locks are per session: a second page on it must be turned away."""
        bridge = FakeBridge(replies={"start": [{"type": "ready"}]})
        with (
            patch.object(server, "HANDOVER_TIMEOUT_SECONDS", 0.05),
            self.client(bridge) as client,
        ):
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "start", "session_id": "shared"})
                self.assertEqual(first.receive_json()["type"], "ready")

                # The session id arrives in the opening frame, so the
                # second page must announce it before the server can
                # tell that another page already drives that session.
                with client.websocket_connect("/api/session") as second:
                    second.send_json(
                        {"type": "start", "session_id": "shared"}
                    )
                    message = second.receive_json()

                first.send_json({"type": "cancel"})
                _wait_for(lambda: bridge.closed)

        self.assertEqual(message["error"]["type"], "SessionBusy")

    def test_runs_different_sessions_concurrently(self) -> None:
        """Two pages on different sessions must not block each other."""
        bridges: list[FakeBridge] = []

        async def spawn(cls: object, workspace: Workspace) -> FakeBridge:
            # Every connection drives its own agent process, so hand
            # each one a separate fake rather than a shared queue.
            bridge = FakeBridge(replies={"start": [{"type": "ready"}]})
            bridges.append(bridge)
            return bridge

        with (
            patch.object(server, "HANDOVER_TIMEOUT_SECONDS", 0.05),
            patch.object(
                server.BridgeProcess,
                "spawn",
                classmethod(spawn),
            ),
            self.client() as client,
        ):
            with client.websocket_connect("/api/session") as first:
                first.send_json({"type": "start", "session_id": "one"})
                self.assertEqual(first.receive_json()["type"], "ready")

                # The second session stays free even though the first
                # page is still connected and holding its own lock.
                with client.websocket_connect("/api/session") as second:
                    second.send_json({"type": "start", "session_id": "two"})
                    self.assertEqual(
                        second.receive_json()["type"],
                        "ready",
                    )
                    second.send_json({"type": "cancel"})
                    _wait_for(lambda: bridges[1].closed)

                first.send_json({"type": "cancel"})
                _wait_for(lambda: bridges[0].closed)

        self.assertEqual(
            [bridge.sent[0]["session_id"] for bridge in bridges],
            ["one", "two"],
        )

    def test_accepts_a_reconnect_once_the_previous_page_is_gone(self) -> None:
        """Switching model reconnects; the agent must not look busy."""
        bridge = FakeBridge(replies={"start": [{"type": "ready"}]})
        with self.client(bridge) as client:
            for _ in range(2):
                with client.websocket_connect("/api/session") as socket:
                    socket.send_json({"type": "start", "provider": "second"})
                    self.assertEqual(socket.receive_json()["type"], "ready")
                    socket.send_json({"type": "cancel"})
                    _wait_for(lambda: bridge.closed)
                bridge.closed = False

        self.assertEqual(
            [message["type"] for message in bridge.sent],
            ["start", "start"],
        )

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


def _wait_for(condition, timeout: float = 2.0) -> None:
    """Waits for a relayed message to reach the fake bridge."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("timed out waiting for the relay")


if __name__ == "__main__":
    unittest.main()
