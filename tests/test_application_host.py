import asyncio
import os
import signal
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

try:
    from fastapi.testclient import TestClient
except ModuleNotFoundError:  # pragma: no cover - exercised without [server]
    TestClient = None

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore
from interfaces import application_host
from interfaces.application_host import ApplicationHost, application_lock


class ApplicationHostCommandTest(unittest.TestCase):
    def test_server_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for arguments, http in (([], True), (["--no-http"], False)):
                with self.subTest(http=http), patch.object(
                    application_host, "run_application_host",
                ) as launch:
                    application_host.main([
                        "--workspace", str(root), "--config-directory", str(root), *arguments,
                    ])
                    launch.assert_called_once_with(root, root, http=http)

    def test_error_interrupt_and_signal_restoration(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch.object(application_host, "run_application_host", side_effect=KeyboardInterrupt):
            application_host.main([])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)
        with patch.object(application_host, "run_application_host", side_effect=RuntimeError("failed")):
            with self.assertRaisesRegex(SystemExit, "Failed to start Nosis server: failed"):
                application_host.main([])
        self.assertIs(signal.getsignal(signal.SIGTERM), previous)

    def test_initializes_configuration_before_serving(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config"
            with (
                patch.object(ApplicationHost, "serve", new_callable=AsyncMock) as serve,
                patch("builtins.print"),
            ):
                application_host.run_application_host(root, config, http=False)
            serve.assert_awaited_once_with(http=False)
            self.assertTrue((config / "provider_config.json").is_file())
            self.assertTrue((config / "im_channels.json").is_file())
            with application_lock(config):
                pass

    def test_duplicate_is_rejected_before_initialization_or_adapter_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                application_lock(root),
                patch.object(application_host, "initialize_config_directory") as initialize,
                patch.object(application_host, "ApplicationHost") as host,
            ):
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    application_host.run_application_host(root, root, http=False)
                initialize.assert_not_called()
                host.assert_not_called()

    def test_headless_process_rejects_duplicate_and_shuts_down_on_signal(self):
        code = """
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch
from interfaces.application_host import main

root = Path(sys.argv[1])
def record(name):
    with (root / "closed").open("a") as file:
        file.write(name + "\\n")
registry = Mock(start=AsyncMock(), close_all=AsyncMock(side_effect=lambda: record("bridge")))
mobile = Mock(start=AsyncMock(side_effect=lambda: print("ready", flush=True)),
              close=AsyncMock(side_effect=lambda: record("im")))
with patch("interfaces.application_host.ActiveSessionRegistry", return_value=registry), \
     patch("interfaces.application_host.MobileService", return_value=mobile):
    main(["--workspace", str(root), "--config-directory", str(root / "config"), "--no-http"])
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code, str(root)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
            )
            try:
                self.assertTrue(process.stdout.readline().startswith(b"Workspace:"))
                self.assertEqual(process.stdout.readline().strip(), b"ready")
                duplicate = subprocess.run([
                    sys.executable, "-m", "interfaces.application_host", "--no-http",
                    "--workspace", str(root), "--config-directory", str(root / "config"),
                ], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=20)
                self.assertNotEqual(duplicate.returncode, 0)
                self.assertIn("already running", duplicate.stderr)
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
                _, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, 0, stderr)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
            self.assertEqual((root / "closed").read_text().splitlines(), ["im", "bridge"])
            with application_lock(root / "config"):
                pass


class ApplicationLockTest(unittest.TestCase):
    def test_config_directories_are_independent_and_lock_is_released_on_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "stop"):
                with application_lock(root / "a"), application_lock(root / "b"):
                    with self.assertRaisesRegex(RuntimeError, "already running"):
                        with application_lock(root / "a"):
                            self.fail("duplicate acquired the lock")
                    raise ValueError("stop")
            with application_lock(root / "a"):
                pass

    def test_cross_process_lock_and_release_after_process_exit(self):
        code = """
import sys, time
from pathlib import Path
from interfaces.application_host import application_lock
with application_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    time.sleep(60)
"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = subprocess.Popen(
                [sys.executable, "-u", "-c", code, str(root)],
                cwd=Path(__file__).resolve().parents[1],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(process.stdout.readline().strip(), b"locked")
                with self.assertRaisesRegex(RuntimeError, "already running"):
                    with application_lock(root):
                        self.fail("another process acquired the lock")
            finally:
                process.kill()
                process.communicate(timeout=10)
            self.assertTrue((root / "server.lock").is_file())
            with application_lock(root):
                pass

    def test_headless_host_does_not_import_http_dependencies(self):
        code = """
import asyncio, builtins, tempfile
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

original_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name.split(".")[0] in {"fastapi", "starlette", "uvicorn", "websockets", "wsproto"}:
        raise AssertionError("HTTP dependency imported: " + name)
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded_import

from interfaces.application_host import ApplicationHost
from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore

async def check(root):
    ready = asyncio.Event()
    mobile = Mock(start=AsyncMock(side_effect=ready.set), close=AsyncMock())
    registry = Mock(start=AsyncMock(), close_all=AsyncMock())
    with patch("interfaces.application_host.ActiveSessionRegistry", return_value=registry), \
         patch("interfaces.application_host.MobileService", return_value=mobile):
        host = ApplicationHost(Workspace(root), JsonlSessionStore(root / "sessions"), SettingsStore(root))
        task = asyncio.create_task(host.serve(http=False))
        await asyncio.wait_for(ready.wait(), 2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        registry.start.assert_awaited_once()
        registry.close_all.assert_awaited_once()
        mobile.close.assert_awaited_once()

with tempfile.TemporaryDirectory() as directory:
    asyncio.run(check(Path(directory)))
"""
        result = subprocess.run(
            [sys.executable, "-c", code], cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ApplicationHostLifecycleTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = Mock(start=AsyncMock(), close_all=AsyncMock())
        self.mobile = Mock(start=AsyncMock(), close=AsyncMock())
        self.enterContext(patch.object(application_host, "ActiveSessionRegistry", return_value=self.registry))
        self.service = self.enterContext(patch.object(application_host, "MobileService", return_value=self.mobile))
        self.host = ApplicationHost(
            Workspace(self.root), JsonlSessionStore(self.root / "sessions"), SettingsStore(self.root),
        )

    async def stop(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_parallel_adapter_startup_and_shutdown_before_bridge(self):
        http_ready, mobile_starting = asyncio.Event(), asyncio.Event()
        order = []

        async def mobile_start():
            mobile_starting.set()
            await asyncio.Future()

        async def serve_http():
            http_ready.set()
            try:
                await asyncio.Future()
            finally:
                order.append("http")

        self.mobile.start.side_effect = mobile_start
        self.mobile.close.side_effect = lambda: order.append("im")
        self.registry.close_all.side_effect = lambda: order.append("bridge")
        with patch.object(self.host, "_serve_http", side_effect=serve_http):
            task = asyncio.create_task(self.host.serve())
            self.addAsyncCleanup(self.stop, task)
            await asyncio.wait_for(http_ready.wait(), 2)
            await asyncio.wait_for(mobile_starting.wait(), 2)
            self.registry.start.assert_awaited_once()
            self.assertFalse(task.done())
            await self.stop(task)

        self.mobile.close.assert_awaited_once()
        self.registry.close_all.assert_awaited_once()
        self.assertCountEqual(order[:-1], ["http", "im"])
        self.assertEqual(order[-1], "bridge")
        self.service.assert_called_once_with(
            self.host.settings.directory, self.host.active_sessions,
            self.host.store, self.host.workspace,
        )

    async def test_im_failure_leaves_http_running_and_releases_mobile_resources(self):
        failed, ready = asyncio.Event(), asyncio.Event()
        self.mobile.start.side_effect = RuntimeError("transport unavailable")

        async def serve_http():
            ready.set()
            await asyncio.Future()

        with (
            patch.object(self.host, "_serve_http", side_effect=serve_http),
            patch.object(application_host.log, "error", side_effect=lambda *args: failed.set()) as log,
        ):
            task = asyncio.create_task(self.host.serve())
            self.addAsyncCleanup(self.stop, task)
            await asyncio.wait_for(ready.wait(), 2)
            await asyncio.wait_for(failed.wait(), 2)
            self.assertFalse(task.done())
            self.mobile.close.assert_awaited_once()
            self.registry.close_all.assert_not_awaited()
            self.assertEqual(log.call_args.args[1], "IM")
            await self.stop(task)
        self.registry.close_all.assert_awaited_once()

    async def test_headless_failure_is_reported_and_releases_bridge(self):
        self.mobile.start.side_effect = ValueError("invalid channels")
        with self.assertLogs(application_host.log, level="ERROR"):
            with self.assertRaisesRegex(ValueError, "invalid channels"):
                await self.host.serve(http=False)
        self.mobile.close.assert_awaited_once()
        self.registry.close_all.assert_awaited_once()

    async def test_bridge_startup_failure_releases_resources_without_starting_adapters(self):
        self.registry.start.side_effect = RuntimeError("bridge unavailable")
        with self.assertRaisesRegex(RuntimeError, "bridge unavailable"):
            await self.host.serve(http=False)
        self.service.assert_not_called()
        self.registry.close_all.assert_awaited_once()


@unittest.skipIf(TestClient is None, "Install the server extra to test HTTP")
class HttpAdapterTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from interfaces.gui import server

        self.server = server
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.registry = Mock(start=AsyncMock(), close_all=AsyncMock())
        self.store = JsonlSessionStore(self.root / "sessions")
        self.workspace = Workspace(self.root)
        self.settings = SettingsStore(self.root)

    async def test_fastapi_lifespan_does_not_own_shared_resources(self):
        app = self.server.create_app(self.workspace, self.store, self.settings, self.registry)
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(client.get("/api/sessions").json(), [])
        self.registry.start.assert_not_awaited()
        self.registry.close_all.assert_not_awaited()

    async def test_occupied_http_port_does_not_stop_im(self):
        ready, failed = asyncio.Event(), asyncio.Event()
        mobile = Mock(start=AsyncMock(side_effect=ready.set), close=AsyncMock())
        with (
            socket.socket() as listener,
            patch.object(application_host, "ActiveSessionRegistry", return_value=self.registry),
            patch.object(application_host, "MobileService", return_value=mobile),
            patch.object(application_host.log, "error", side_effect=lambda *args: failed.set()) as log,
        ):
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            host = ApplicationHost(self.workspace, self.store, self.settings)
            with patch.object(self.server, "PORT", listener.getsockname()[1]):
                task = asyncio.create_task(host.serve())
                try:
                    await asyncio.wait_for(ready.wait(), 2)
                    await asyncio.wait_for(failed.wait(), 2)
                    self.assertEqual(log.call_args.args[1], "HTTP")
                    self.assertFalse(task.done())
                    mobile.close.assert_not_awaited()
                    self.registry.close_all.assert_not_awaited()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        mobile.close.assert_awaited_once()
        self.registry.close_all.assert_awaited_once()

    async def test_http_cancellation_gracefully_closes_listener_without_closing_sessions(self):
        import httpx

        instances = []
        server_type = self.server.HostedServer

        def create_server(config):
            instance = server_type(config)
            instances.append(instance)
            return instance

        with (
            patch.object(self.server, "PORT", 0),
            patch.object(self.server, "HostedServer", side_effect=create_server),
            patch("builtins.print"),
        ):
            task = asyncio.create_task(self.server.serve_http(
                self.workspace, self.store, self.settings, self.registry,
            ))
            try:
                async def wait_started():
                    while not instances or not instances[0].started:
                        if task.done():
                            await task
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(wait_started(), 5)
                port = instances[0].servers[0].sockets[0].getsockname()[1]
                response = await asyncio.to_thread(
                    httpx.get, f"http://127.0.0.1:{port}/api/sessions", trust_env=False,
                )
                self.assertEqual(response.status_code, 200)
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
        self.registry.close_all.assert_not_awaited()
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", port))

    def test_reports_missing_websocket_backend(self):
        with patch.object(self.server.importlib, "import_module", side_effect=ModuleNotFoundError):
            with self.assertRaisesRegex(RuntimeError, "uv sync --extra server"):
                self.server.require_websocket_backend()
