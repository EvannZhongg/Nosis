import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent_core import AgentCancelled, TurnControl
from agent_core.mcp.config import load_mcp_config
from agent_runtime.mcp_client import McpClientHandle, _describe
from agent_runtime.mcp_service import McpService
from tests.mcp_fixtures import running_broker


FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py").resolve())
BRIDGE_SCRIPT = """
import io, json, sys
from pathlib import Path
from unittest.mock import patch
from interfaces.bridge.bridge import Bridge
with patch('interfaces.bridge.bridge.default_config_directory', return_value=Path(sys.argv[1])):
    bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
bridge.open_session({'type': 'open_session', 'workspace': sys.argv[1], 'session_id': None})
plane = bridge.host.planes.ensure(bridge.host.sessions)
def report():
    print(json.dumps(plane.mcp.call_tool('fake', 'identity', {})['structured_content']), flush=True)
report()
for line in sys.stdin:
    if line.strip() != 'identity':
        break
    report()
bridge.close()
"""


def bridge_configuration(root, log):
    from tests.test_bridge import open_session_message
    from agent_core.tools.config import TOOL_NAMES
    settings = {"enabled": True, "servers": {"fake": {
        "command": sys.executable, "args": [FAKE_SERVER], "env": {"TEST_MCP_LOG": str(log)},
    }}}
    open_session_message(root, agent_config={
        "max_same_tool_calls": 5, "output_reserve_tokens": 100,
        "workspace_instruction_files": [],
        "main_agent": {"tools": {name: False for name in TOOL_NAMES}},
        "memory": {"enabled": False}, "mcp": settings,
    })
    return settings


def read_bridge_state(process):
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(process.stderr.read())
    return json.loads(line)


def config(**server):
    return load_mcp_config({"enabled": True, "servers": {"fake": {
        "command": sys.executable, "args": [FAKE_SERVER], **server,
    }}})


def wait_for(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition did not become true")
        time.sleep(0.02)


class McpIdentityTest(unittest.TestCase):
    def test_environment_snapshot_tracks_managed_credentials_without_library_mutations(self):
        from agent_runtime.settings import EnvironmentReloader

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"MCP_SNAPSHOT_KEY": "inherited"}):
            path = Path(directory) / ".env"
            environment = EnvironmentReloader(path)
            path.write_text("MCP_SNAPSHOT_KEY=managed\n", encoding="utf-8")
            environment.reload()
            os.environ["MCP_LIBRARY_CACHE"] = "internal"
            self.assertEqual(environment.snapshot()["MCP_SNAPSHOT_KEY"], "managed")
            self.assertNotIn("MCP_LIBRARY_CACHE", environment.snapshot())
            path.write_text("", encoding="utf-8")
            environment.reload()
            self.assertEqual(environment.snapshot()["MCP_SNAPSHOT_KEY"], "inherited")

    def test_identity_separates_connection_inputs_but_not_tool_policy(self):
        root = Path.cwd()
        original = config(env={"TEST_MCP_TOKEN": "first"})
        first = _describe(original, root, "one")[0]["identity"]
        other_policy = config(env={"TEST_MCP_TOKEN": "first"}, tools={"enabled": ["echo"], "approval": "never"})
        self.assertEqual(first, _describe(other_policy, root, "two")[0]["identity"])
        for changed, workspace, session in (
            (config(env={"TEST_MCP_TOKEN": "second"}), root, "one"),
            (config(cwd="other", env={"TEST_MCP_TOKEN": "first"}), root, "one"),
            (original, root / "other", "one"),
            (config(scope="session", env={"TEST_MCP_TOKEN": "first"}), root, "two"),
        ):
            self.assertNotEqual(first, _describe(changed, workspace, session)[0]["identity"])
        with patch.dict(os.environ, {"MCP_INHERITED_CREDENTIAL": "changed"}):
            self.assertNotEqual(first, _describe(original, root, "one")[0]["identity"])

    def test_http_identity_includes_headers_and_session_scope(self):
        one = load_mcp_config({"enabled": True, "servers": {"http": {
            "url": "http://localhost/mcp", "headers": {"Authorization": "one"}, "scope": "session",
        }}})
        first = _describe(one, Path.cwd(), "one")[0]["identity"]
        self.assertNotEqual(first, _describe(one, Path.cwd(), "two")[0]["identity"])
        changed = replace(one, servers=(replace(one.servers[0], headers={"Authorization": "two"}),))
        self.assertNotEqual(first, _describe(changed, Path.cwd(), "one")[0]["identity"])


class McpPoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_acquisition_waits_for_idle_subprocess_teardown(self):
        service = McpService(idle_seconds=0)
        closing = asyncio.Event()
        finish_close = asyncio.Event()
        connections = []

        async def lifecycle(connection):
            connections.append(connection)
            connection.ready.set_result(())
            await connection.stop.wait()
            closing.set()
            await finish_close.wait()

        identity = _describe(config(), Path.cwd(), "one")[0]["identity"]
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                first = await service.acquire(identity, 15, lambda error: None)
                service.release(first)
                reaper = asyncio.create_task(service.reap())
                await closing.wait()
                acquire = asyncio.create_task(service.acquire(identity, 15, lambda error: None))
                await asyncio.sleep(0)
                self.assertFalse(acquire.done())
                self.assertEqual(len(connections), 1)
                finish_close.set()
                second = await acquire
                await reaper
                self.assertEqual(len(connections), 2)
                self.assertIn(second.resource.key, service.resources)
                service.release(second)
            finally:
                finish_close.set()
                await service.close()

    async def test_concurrent_acquisition_discovers_once_and_reaps_after_last_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "calls.jsonl"
            service = McpService(idle_seconds=0)
            identity = _describe(config(env={"TEST_MCP_LOG": str(log)}), root, "one")[0]["identity"]
            try:
                first, second = await asyncio.gather(*(
                    service.acquire(identity, 15, lambda error: None) for _ in range(2)
                ))
                self.assertIs(first.resource, second.resource)
                self.assertEqual(len(log.read_text().splitlines()), 1)
                service.release(first)
                await service.reap()
                self.assertEqual(len(service.resources), 1)
                service.release(second)
                await service.reap()
                self.assertFalse(service.resources)
                self.assertIsNone(first.resource.connection.session)
            finally:
                await service.close()

    async def test_cancelling_an_initializer_does_not_cancel_another_acquirer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = McpService(idle_seconds=0)
            identity = _describe(config(env={"TEST_MCP_DELAY_SECONDS": "0.3"}), root, "one")[0]["identity"]
            try:
                first = asyncio.create_task(service.acquire(identity, 15, lambda error: None))
                second = asyncio.create_task(service.acquire(identity, 15, lambda error: None))
                await asyncio.sleep(0.05)
                first.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await first
                lease = await second
                self.assertEqual(lease.resource.references, 1)
                service.release(lease)
                await service.reap()
            finally:
                await service.close()


class McpBrokerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.broker = self.enterContext(running_broker(self.root))

    def handle(self, settings=None, session="one", workspace=None):
        handle = McpClientHandle(self.root, settings or config(), workspace or self.root, session)
        self.addCleanup(handle.release)
        handle.acquire()
        handle.wait_ready()
        return handle

    def test_shared_connection_has_local_visibility_and_approval_and_idle_reuse(self):
        log = self.root / "calls.jsonl"
        first = self.handle(config(env={"TEST_MCP_LOG": str(log)}, tools={"enabled": ["identity", "echo"], "approval": "never"}))
        second = self.handle(config(env={"TEST_MCP_LOG": str(log)}, tools={"enabled": ["identity", "hidden"]}), session="two")
        pid = first.call_tool("fake", "identity", {})["structured_content"]["pid"]
        self.assertEqual(pid, second.call_tool("fake", "identity", {})["structured_content"]["pid"])
        self.assertFalse(first.requires_approval("mcp__fake__identity"))
        self.assertTrue(second.requires_approval("mcp__fake__identity"))
        with self.assertRaisesRegex(ValueError, "not enabled"):
            first.call_tool("fake", "hidden", {})
        first.release()
        second.release()
        third = self.handle(config(env={"TEST_MCP_LOG": str(log)}))
        self.assertEqual(pid, third.call_tool("fake", "identity", {})["structured_content"]["pid"])
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_runtime_rebuilds_reuse_connections_and_plugin_changes_replace_only_affected_resource(self):
        import io
        from interfaces.bridge.bridge import Bridge

        bridge_configuration(self.root, self.root / "discovery.jsonl")
        plugin = self.root / "plugins" / "example"
        plugin.mkdir(parents=True)
        (plugin / "plugin.json").write_text(json.dumps({
            "name": "example", "components": {"mcp": [".mcp.json"]},
        }), encoding="utf-8")
        plugin_config = {"extra": {
            "command": sys.executable, "args": [FAKE_SERVER],
            "tools": {"enabled": ["identity"]},
        }}
        path = plugin / ".mcp.json"
        path.write_text(json.dumps(plugin_config), encoding="utf-8")
        with patch("interfaces.bridge.bridge.default_config_directory", return_value=self.root):
            bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
        self.addCleanup(bridge.close)
        bridge.open_session({"type": "open_session", "workspace": str(self.root), "session_id": None})
        first = bridge.host.planes.ensure(bridge.host.sessions)
        pid = first.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"]
        plugin_pid = first.mcp.call_tool("example:extra", "identity", {})["structured_content"]["pid"]
        bridge.host.change_provider("second")
        second = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(first, second)
        before = first.mcp._descriptions[0]["identity"]["connection"]["env"]
        after = second.mcp._descriptions[0]["identity"]["connection"]["env"]
        changed_names = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
        self.assertEqual(pid, second.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"], changed_names)
        self.assertEqual(plugin_pid, second.mcp.call_tool("example:extra", "identity", {})["structured_content"]["pid"])
        plugin_config["extra"]["env"] = {"TEST_MCP_TOKEN": "changed"}
        path.write_text(json.dumps(plugin_config), encoding="utf-8")
        third = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(second, third)
        self.assertEqual(pid, third.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"])
        self.assertNotEqual(plugin_pid, third.mcp.call_tool("example:extra", "identity", {})["structured_content"]["pid"])

    def test_one_failed_initializer_keeps_other_servers_available(self):
        settings = load_mcp_config({"enabled": True, "servers": {
            "bad": {"command": str(self.root / "missing-executable")},
            "fake": {"command": sys.executable, "args": [FAKE_SERVER]},
        }})
        statuses = []
        handle = McpClientHandle(self.root, settings, self.root, "one", statuses.append)
        self.addCleanup(handle.release)
        handle.acquire()
        handle.wait_ready()
        self.assertTrue(any(status.server == "bad" and status.status == "unavailable" for status in statuses))
        self.assertEqual(handle.call_tool("fake", "echo", {"text": "ready"})["structured_content"], {"echo": "ready"})

    def test_runtime_session_switch_rebinds_session_scoped_handle(self):
        import io
        from interfaces.bridge.bridge import Bridge

        bridge_configuration(self.root, self.root / "discovery.jsonl")
        path = self.root / "agent_config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcp"]["servers"]["fake"]["scope"] = "session"
        path.write_text(json.dumps(document), encoding="utf-8")
        with patch("interfaces.bridge.bridge.default_config_directory", return_value=self.root):
            bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
        self.addCleanup(bridge.close)
        opening = {"type": "open_session", "workspace": str(self.root), "session_id": None}
        bridge.open_session(opening)
        first = bridge.host.planes.ensure(bridge.host.sessions)
        pid = first.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"]
        bridge.open_session(opening)
        second = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(first.mcp, second.mcp)
        self.assertNotEqual(pid, second.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"])

    def test_session_scope_and_credentials_and_workspace_are_isolated(self):
        handles = [
            self.handle(config(scope="session"), session="one"),
            self.handle(config(scope="session"), session="two"),
            self.handle(config(env={"TEST_MCP_TOKEN": "one"})),
            self.handle(config(env={"TEST_MCP_TOKEN": "two"})),
        ]
        other = self.root / "other"
        other.mkdir()
        handles.append(self.handle(workspace=other))
        states = [handle.call_tool("fake", "identity", {})["structured_content"] for handle in handles]
        self.assertEqual(len({state["pid"] for state in states}), len(handles))
        self.assertEqual([state["token"] for state in states[2:4]], ["one", "two"])
        self.assertEqual(Path(states[-1]["cwd"]), other)

    def test_cancel_only_its_request_and_other_server_remains_concurrent(self):
        first = self.handle()
        shared = self.handle(session="two")
        independent = self.handle(config(env={"TEST_MCP_TOKEN": "separate"}))
        control = TurnControl()
        started, release = self.root / "started", self.root / "release"
        errors = []

        def slow():
            try:
                first.call_tool("fake", "wait", {"started": str(started), "release": str(release)}, cancellation=control)
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=slow)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(control.cancel)
        wait_for(started.exists)
        self.assertEqual(independent.call_tool("fake", "echo", {"text": "other"})["structured_content"], {"echo": "other"})
        self.assertEqual(shared.call_tool("fake", "echo", {"text": "shared"})["structured_content"], {"echo": "shared"})
        control.cancel()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], AgentCancelled)
        wait_for(Path(str(started) + ".stopped").exists)
        self.assertEqual(shared.call_tool("fake", "echo", {"text": "after"})["structured_content"], {"echo": "after"})

    def test_closed_transport_invalidates_handle_and_new_acquire_replaces_resource(self):
        first = self.handle()
        pid = first.call_tool("fake", "identity", {})["structured_content"]["pid"]
        with self.assertRaises(ConnectionError):
            first.call_tool("fake", "crash", {})
        wait_for(lambda: not first.matches(config(), self.root, "one"))
        second = self.handle()
        self.assertNotEqual(pid, second.call_tool("fake", "identity", {})["structured_content"]["pid"])

    def test_http_connections_and_discovery_share_but_scope_and_credentials_do_not(self):
        with socket.socket() as port_socket:
            port_socket.bind(("127.0.0.1", 0))
            port = port_socket.getsockname()[1]
        log = self.root / "http-discovery.jsonl"
        process = subprocess.Popen(
            [sys.executable, FAKE_SERVER, str(port)],
            env={**os.environ, "TEST_MCP_LOG": str(log)},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.addCleanup(process.wait, 5)
        self.addCleanup(process.terminate)

        def listening():
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    return True
            except OSError:
                return False

        wait_for(listening)

        def http(**fields):
            return load_mcp_config({"enabled": True, "servers": {"fake": {
                "url": f"http://127.0.0.1:{port}/mcp", **fields,
            }}})

        first = self.handle(http())
        second = self.handle(http(), session="two")
        self.assertEqual(len(log.read_text().splitlines()), 1)
        self.assertEqual(second.call_tool("fake", "echo", {"text": "http"})["structured_content"], {"echo": "http"})
        isolated = self.handle(http(scope="session"), session="two")
        credential = self.handle(http(headers={"Authorization": "Bearer other"}))
        self.assertEqual(len(log.read_text().splitlines()), 3)
        for handle in (first, second, isolated, credential):
            handle.release()
        # The service launcher can have a CA setting absent in another caller.
        # Its process environment must not determine the latter's TLS context.
        other_directory = self.root / "other-broker"
        other_directory.mkdir()
        with patch.dict(os.environ, {"SSL_CERT_FILE": str(self.root / "missing-ca.pem")}):
            other_broker = running_broker(other_directory)
            self.enterContext(other_broker)
        clean_environment = dict(os.environ)
        clean_environment.pop("SSL_CERT_FILE", None)
        clean_environment.pop("SSL_CERT_DIR", None)
        clean = McpClientHandle(other_directory, http(), self.root, "clean", environment=clean_environment)
        self.addCleanup(clean.release)
        clean.acquire()
        clean.wait_ready()
        self.assertEqual(clean.call_tool("fake", "echo", {"text": "clean CA"})["structured_content"], {"echo": "clean CA"})

    def test_timeout_and_release_cancel_only_the_owning_requests(self):
        first = self.handle(config(call_timeout_seconds=1))
        other = self.handle(session="two")
        started, release = self.root / "timed", self.root / "release"
        with self.assertRaises(TimeoutError):
            first.call_tool("fake", "wait", {"started": str(started), "release": str(release)})
        wait_for(Path(str(started) + ".stopped").exists)
        self.assertEqual(other.call_tool("fake", "echo", {"text": "alive"})["structured_content"], {"echo": "alive"})
        errors = []
        started = self.root / "released"

        def slow():
            try:
                first.call_tool("fake", "wait", {"started": str(started), "release": str(release)})
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=slow)
        thread.start()
        wait_for(started.exists)
        first.release()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(errors[0], (ConnectionError, AgentCancelled))
        wait_for(Path(str(started) + ".stopped").exists)
        self.assertEqual(other.call_tool("fake", "echo", {"text": "still alive"})["structured_content"], {"echo": "still alive"})

    def test_separate_client_processes_share_and_exit_without_closing_other_borrowers(self):
        from concurrent.futures import ThreadPoolExecutor
        log = self.root / "calls.jsonl"
        bridge_configuration(self.root, log)
        processes = [subprocess.Popen(
            [sys.executable, "-c", BRIDGE_SCRIPT, str(self.root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for index in range(2)]
        try:
            with ThreadPoolExecutor(2) as executor:
                states = list(executor.map(read_bridge_state, processes))
            self.assertEqual(states[0]["pid"], states[1]["pid"])
            self.assertEqual(len(log.read_text().splitlines()), 1)
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(processes[0].pid)],
                               capture_output=True, check=True)
            else:
                processes[0].kill()
            processes[0].wait(5)
            processes[1].stdin.write("identity\n")
            processes[1].stdin.flush()
            self.assertEqual(states[0]["pid"], read_bridge_state(processes[1])["pid"])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.communicate("\n", timeout=10)
                else:
                    process.communicate(timeout=10)


class McpBootstrapTest(unittest.TestCase):
    @unittest.skipUnless(os.name == "nt", "Windows console creation")
    def test_broker_launch_suppresses_console_without_detached_process(self):
        from agent_runtime.mcp_broker import launch

        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_runtime.mcp_broker.subprocess.Popen"
        ) as spawn:
            launch(Path(directory))
        flags = spawn.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(flags & subprocess.DETACHED_PROCESS)

    @unittest.skipUnless(os.name == "nt", "Windows virtual-environment launcher")
    def test_virtual_environment_interpreter_keeps_windowless_console_state(self):
        probe = subprocess.run(
            [sys.executable, "-c", (
                "import ctypes; "
                "ctypes.windll.kernel32.GetConsoleWindow.restype = ctypes.c_void_p; "
                "print(ctypes.windll.kernel32.GetConsoleWindow())"
            )],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            check=True, timeout=15,
        )
        self.assertEqual(probe.stdout.strip(), "None")

    def test_racing_bridge_startup_and_owner_process_tree_exit(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "discovery.jsonl"
            bridge_configuration(root, log)
            processes = [subprocess.Popen(
                [sys.executable, "-c", BRIDGE_SCRIPT, str(root)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ) for _ in range(2)]
            try:
                with ThreadPoolExecutor(2) as executor:
                    states = list(executor.map(read_bridge_state, processes))
                self.assertEqual(states[0]["pid"], states[1]["pid"])
                self.assertEqual(len(log.read_text().splitlines()), 1)
                if os.name == "nt":
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(processes[0].pid)],
                                   capture_output=True, check=True)
                else:
                    processes[0].kill()
                processes[0].wait(5)
                processes[1].stdin.write("identity\n")
                processes[1].stdin.flush()
                self.assertEqual(read_bridge_state(processes[1])["pid"], states[0]["pid"])
            finally:
                for process in processes:
                    if process.poll() is None:
                        process.communicate("quit\n", timeout=15)
                    else:
                        process.communicate(timeout=15)
                wait_for(lambda: not (root / "mcp" / "broker.json").exists(), timeout=45)
                # The endpoint disappears just before the process exits; on
                # Windows its inherited log handle has to close before the
                # temporary directory can be removed.
                def log_closed() -> bool:
                    try:
                        (root / "mcp" / "broker.log").unlink()
                    except FileNotFoundError:
                        return True
                    except PermissionError:
                        return False
                    return True

                wait_for(log_closed, timeout=45)


if __name__ == "__main__":
    unittest.main()
