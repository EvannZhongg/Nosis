import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from agent_core import JsonlSessionStore, Workspace
from agent_runtime.settings import SettingsStore

from interfaces import application_host
from interfaces.gui import __main__ as gui_main
from interfaces.mobile import __main__ as mobile_main


class ApplicationHostTest(unittest.TestCase):
    def test_gui_and_mobile_entrypoints_use_the_same_host(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(application_host, "run_application_host") as launch:
                with patch.object(application_host, "default_config_directory", return_value=root):
                    gui_main.main(["--workspace", str(root)])
                launch.assert_called_once_with(root, root)
            with patch.object(application_host, "run_application_host") as launch:
                mobile_main.main(["--workspace", str(root), "--config-directory", str(root)])
                launch.assert_called_once_with(root, root)

    def test_entrypoints_share_error_and_interrupt_handling(self):
        for entrypoint in (gui_main, mobile_main):
            with self.subTest(entrypoint=entrypoint.__name__):
                with patch.object(
                    application_host, "run_application_host", side_effect=KeyboardInterrupt
                ):
                    entrypoint.main([])
                with patch.object(
                    application_host, "run_application_host", side_effect=RuntimeError("failed")
                ):
                    with self.assertRaisesRegex(SystemExit, "Failed to start Nosis: failed"):
                        entrypoint.main([])

    def test_host_owns_shared_frontend_lifecycle(self):
        from interfaces.gui import server

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = Mock(start=AsyncMock(), close_all=AsyncMock())
            mobile = Mock(start=AsyncMock(), close=AsyncMock())
            with (
                patch.object(application_host, "ActiveSessionRegistry", return_value=registry),
                patch("interfaces.mobile.service.MobileService", return_value=mobile) as service,
                patch.object(server, "create_app", wraps=server.create_app) as gui_app,
            ):
                store, workspace, settings = (
                    JsonlSessionStore(root / "sessions"),
                    Workspace(root),
                    SettingsStore(root),
                )
                app = application_host.create_app(workspace, store, settings)
                with TestClient(app, base_url="http://127.0.0.1") as client:
                    self.assertEqual(client.get("/api/host").json()["application"], "nosis")
                    registry.start.assert_awaited_once()
                    mobile.start.assert_awaited_once()
                    mobile.close.assert_not_awaited()
                    self.assertIs(gui_app.call_args.args[3], registry)
                    service.assert_called_once_with(settings.directory, registry, store, workspace)
                mobile.close.assert_awaited_once()
                registry.close_all.assert_awaited_once()

    def test_mobile_startup_errors_leave_gui_available_and_release_resources(self):
        for failure in ("invalid_config", "missing_node"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = {
                    "channels": [
                        {
                            "id": "bot",
                            "type": "feishu",
                            "app_id": "app",
                            "app_secret": "secret",
                            "allowed_users": ["ou_owner"],
                        }
                    ]
                }
                (root / "im_channels.json").write_text(
                    "<invalid>" if failure == "invalid_config" else json.dumps(config),
                    encoding="utf-8",
                )
                with (
                    patch.object(
                        application_host.ActiveSessionRegistry, "start", new_callable=AsyncMock
                    ),
                    patch.object(
                        application_host.ActiveSessionRegistry, "close_all", new_callable=AsyncMock
                    ) as close,
                    patch("interfaces.mobile.feishu.shutil.which", return_value=None),
                    patch(
                        "interfaces.mobile.controller.MobileController.close",
                        new_callable=AsyncMock,
                    ) as close_controller,
                    self.assertLogs(application_host.log, level="ERROR") as logs,
                ):
                    app = application_host.create_app(
                        Workspace(root),
                        JsonlSessionStore(root / "sessions"),
                        SettingsStore(root),
                    )
                    with TestClient(app, base_url="http://127.0.0.1") as client:
                        self.assertEqual(client.get("/api/host").status_code, 200)
                    close.assert_awaited_once()
                    if failure == "missing_node":
                        close_controller.assert_awaited_once()
                    self.assertIn("Nosis mobile failed to start", logs.output[0])

    def test_html_on_occupied_port_reports_application_conflict(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>Another application</html>")

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer((application_host.HOST, 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(application_host, "PORT", server.server_port):
                with self.assertRaisesRegex(
                    RuntimeError,
                    f"Port {server.server_port} belongs to another application or Nosis config directory",
                ):
                    application_host.run_application_host(root, root)

    def test_second_frontend_reuses_host_without_starting_another_bridge_or_bot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            identity = {
                "application": "nosis",
                "config_directory": str(root),
                "workspace": str(root),
            }
            requests = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    requests.append(self.path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(identity).encode())

                def log_message(self, *args):
                    pass

            server = ThreadingHTTPServer((application_host.HOST, 0), Handler)
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(thread.join)
            self.addCleanup(server.shutdown)
            output = StringIO()
            with (
                patch.object(application_host, "PORT", server.server_port),
                patch.object(application_host, "create_app") as create_app,
                patch("uvicorn.Server") as uvicorn_server,
                redirect_stdout(output),
            ):
                application_host.run_application_host(root, root)
                application_host.run_application_host(root, root)
                create_app.assert_not_called()
                uvicorn_server.assert_not_called()
            self.assertEqual(requests, ["/api/host", "/api/host"])
            self.assertIn("Using the running Nosis host", output.getvalue())
            identity["config_directory"] = str(root / "another-profile")
            with patch.object(application_host, "PORT", server.server_port):
                with self.assertRaisesRegex(RuntimeError, "another application or Nosis config"):
                    application_host.run_application_host(root, root)
