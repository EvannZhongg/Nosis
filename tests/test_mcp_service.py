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
from unittest.mock import Mock, patch

from agent_core import AgentCancelled, TurnControl
from agent_core.mcp.config import load_mcp_config
from agent_runtime.mcp.client import McpClient, _describe
from agent_runtime.mcp.service import McpService
from tests.mcp_fixtures import running_broker


FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py").resolve())
BRIDGE_SCRIPT = """
import io, json, sys
from pathlib import Path
from unittest.mock import patch
from interfaces.bridge.bridge import Bridge
with patch('interfaces.bridge.bridge.default_config_directory', return_value=Path(sys.argv[1])):
    bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
bridge.open_session({'type': 'open_session', 'workspace': sys.argv[2] if len(sys.argv) > 2 else sys.argv[1], 'session_id': None})
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
    def test_global_requires_stable_stdio_directory_and_preserves_security_identity(self):
        root = Path.cwd()
        settings = config(scope="global", cwd=str(root))
        first = _describe(settings, root, "one", {"TOKEN": "one"})[0]["identity"]
        self.assertEqual(first, _describe(settings, root / "other", "two", {"TOKEN": "one"})[0]["identity"])
        self.assertNotEqual(first, _describe(settings, root, "one", {"TOKEN": "two"})[0]["identity"])
        slower = replace(settings, servers=(replace(settings.servers[0], startup_timeout_seconds=90),))
        self.assertEqual(first, _describe(slower, root, "one", {"TOKEN": "one"})[0]["identity"])
        for cwd in (None, ".", "relative"):
            with self.subTest(cwd=cwd), self.assertRaisesRegex(ValueError, "absolute cwd"):
                _describe(config(scope="global", cwd=cwd), root, "one")

    def test_http_identity_uses_only_effective_transport_environment(self):
        settings = load_mcp_config({"enabled": True, "servers": {"http": {
            "url": "https://example.test/mcp", "scope": "global",
            "headers": {"Authorization": "Bearer first"},
        }}})
        root = Path.cwd()
        first = _describe(settings, root, "one", {"UNRELATED": "one"})[0]["identity"]
        self.assertEqual(first, _describe(settings, root / "other", "two", {"UNRELATED": "two"})[0]["identity"])
        for environment in ({"HTTPS_PROXY": "http://localhost:1234"}, {"SSL_CERT_FILE": "another.pem"}):
            self.assertNotEqual(first, _describe(settings, root, "one", environment)[0]["identity"])
        credential = replace(settings, servers=(replace(settings.servers[0], headers={"Authorization": "Bearer second"}),))
        self.assertNotEqual(first, _describe(credential, root, "one", {})[0]["identity"])

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
    async def test_cancelled_owner_before_start_does_not_cancel_acquire_reap_or_close(self):
        identity = _describe(config(), Path.cwd(), "one")[0]["identity"]

        async def lifecycle(connection):
            connection.ready.set_result(())
            await connection.stop.wait()

        for action in ("acquire", "reap", "close"):
            with self.subTest(action=action), patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
                service = McpService(idle_seconds=0)
                try:
                    waiting = asyncio.create_task(service.acquire(identity, 1, lambda error: None))
                    await asyncio.sleep(0)
                    previous = next(iter(service.resources.values()))
                    self.assertFalse(previous.connection.ready.done())
                    previous.connection.stop.set()
                    previous.task.cancel()
                    waiting.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await waiting
                    self.assertTrue(previous.task.cancelled())
                    if action == "acquire":
                        lease = await service.acquire(identity, 1, lambda error: None)
                        self.assertIsNot(previous, lease.resource)
                        service.release(lease)
                    elif action == "reap":
                        await service.reap()
                        self.assertFalse(service.resources)
                    else:
                        await service.close()
                finally:
                    await service.close()

    async def test_short_waiter_timeout_does_not_bound_shared_initialization(self):
        service = McpService()
        entered, initialize = asyncio.Event(), asyncio.Event()
        connections = []

        async def lifecycle(connection):
            connections.append(connection)
            entered.set()
            await initialize.wait()
            connection.ready.set_result(())
            await connection.stop.wait()

        identity = _describe(config(), Path.cwd(), "one")[0]["identity"]
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                short = asyncio.create_task(service.acquire(identity, 0.05, lambda error: None))
                await entered.wait()
                long = asyncio.create_task(service.acquire(identity, 2, lambda error: None))
                with self.assertRaises(TimeoutError):
                    await short
                self.assertFalse(connections[0].stop.is_set())
                initialize.set()
                lease = await long
                self.assertEqual(len(connections), 1)
                service.release(lease)
            finally:
                initialize.set()
                await service.close()

    async def test_initialization_without_waiters_is_cancelled(self):
        service = McpService()
        stopped = asyncio.Event()

        async def lifecycle(connection):
            try:
                await connection.stop.wait()
            except asyncio.CancelledError:
                pass
            finally:
                stopped.set()

        identity = _describe(config(), Path.cwd(), "one")[0]["identity"]
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                with self.assertRaises(TimeoutError):
                    await service.acquire(identity, 0.05, lambda error: None)
                await asyncio.wait_for(stopped.wait(), 1)
                self.assertTrue(next(iter(service.resources.values())).connection.stop.is_set())
            finally:
                await service.close()

    async def test_default_grace_preserves_resources_after_thirty_seconds(self):
        service = McpService()

        async def lifecycle(connection):
            connection.ready.set_result(())
            await connection.stop.wait()

        identity = _describe(config(), Path.cwd(), "one")[0]["identity"]
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                lease = await service.acquire(identity, 15, lambda error: None)
                service.release(lease)
                lease.resource.idle_since = time.monotonic() - 31
                await service.reap()
                self.assertEqual(len(service.resources), 1)
                lease.resource.idle_since = time.monotonic() - 301
                await service.reap()
                self.assertFalse(service.resources)
            finally:
                await service.close()

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
                with self.assertRaises(TimeoutError):
                    await service.acquire(identity, 0.05, lambda error: None)
                self.assertFalse(first.resource.task.done())
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


class McpBindingTest(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_binding_preserves_previous_leases_and_discards_new_ones(self):
        from agent_runtime.mcp.broker import _Peer

        service = McpService(idle_seconds=0)
        peer = _Peer(service, Mock())
        initializing = asyncio.Event()

        async def lifecycle(connection):
            try:
                if connection.parameters.env.get("WAIT"):
                    initializing.set()
                    await asyncio.Event().wait()
                connection.ready.set_result(())
                await connection.stop.wait()
            except asyncio.CancelledError:
                pass

        settings = load_mcp_config({"enabled": True, "servers": {
            "fast": {"command": sys.executable},
            "slow": {"command": sys.executable, "env": {"WAIT": "yes"}},
        }})
        descriptions = _describe(settings, Path.cwd(), "one")
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                first, = await peer.bind(descriptions[:1])
                updating = asyncio.create_task(peer.bind(descriptions))
                await initializing.wait()
                updating.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await updating
                self.assertEqual(list(peer.leases), [first["lease"]])
                rebound, = await peer.bind(descriptions[:1])
                self.assertEqual(first["lease"], rebound["lease"])
                await peer.bind([])
                self.assertFalse(peer.leases)
                await service.reap()
                self.assertFalse(service.resources)
            finally:
                for lease in peer.leases.values():
                    service.release(lease)
                await service.close()

    async def test_binding_failure_releases_removed_lease_and_retains_healthy_one(self):
        from agent_runtime.mcp.broker import _Peer

        service = McpService(idle_seconds=0)
        peer = _Peer(service, Mock())

        async def lifecycle(connection):
            if connection.parameters.env.get("FAIL"):
                connection.failure = ConnectionError("failed to initialize")
                connection.ready.set_exception(connection.failure)
                return
            connection.ready.set_result(())
            await connection.stop.wait()

        settings = load_mcp_config({"enabled": True, "servers": {
            "first": {"command": sys.executable}, "second": {"command": sys.executable},
        }})
        original = _describe(settings, Path.cwd(), "one")
        changed = replace(settings, servers=(settings.servers[0], replace(settings.servers[1], env={"FAIL": "yes"})))
        with patch("agent_core.mcp.connection.McpConnection.run", lifecycle):
            try:
                first, second = await peer.bind(original)
                removed = peer.leases[second["lease"]]
                healthy, failed = await peer.bind(_describe(changed, Path.cwd(), "one"))
                self.assertEqual(first["lease"], healthy["lease"])
                self.assertIn("error", failed)
                self.assertFalse(removed.active)
                self.assertEqual(list(peer.leases), [first["lease"]])
            finally:
                for lease in peer.leases.values():
                    service.release(lease)
                await service.close()


class McpBrokerTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.broker = self.enterContext(running_broker(self.root))

    def handle(self, settings=None, session="one", workspace=None):
        client = McpClient(self.root)
        self.addCleanup(client.close)
        handle = client.bind(settings or config(), workspace or self.root, session)
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
        first._client.close()
        second._client.close()
        third = self.handle(config(env={"TEST_MCP_LOG": str(log)}))
        self.assertEqual(pid, third.call_tool("fake", "identity", {})["structured_content"]["pid"])
        self.assertEqual(len(log.read_text().splitlines()), 1)

    def test_empty_binding_releases_resources_without_closing_host_transport(self):
        first = self.handle()
        client = first._client
        pid = first.call_tool("fake", "identity", {})["structured_content"]["pid"]
        thread = client._transport._thread
        first.release()
        empty = client.bind(load_mcp_config(None), self.root, "two")
        empty.acquire()
        self.assertEqual(empty.wait_ready(), ())
        time.sleep(2.1)
        empty.release()
        next_view = client.bind(config(), self.root, "two")
        next_view.acquire()
        next_view.wait_ready()
        self.assertIs(thread, client._transport._thread)
        self.assertNotEqual(pid, next_view.call_tool("fake", "identity", {})["structured_content"]["pid"])

    def test_explicit_binding_recovers_disconnected_host_transport(self):
        first = self.handle()
        client = first._client
        old_transport = client._transport
        old_transport._socket.shutdown(socket.SHUT_RDWR)
        wait_for(lambda: old_transport.failed)
        wait_for(lambda: first._leases["fake"] in first._unavailable)
        self.assertTrue(first.matches(config(), self.root, "one"))
        first.release()
        second = client.bind(config(), self.root, "two")
        second.acquire()
        second.wait_ready()
        self.assertIsNot(old_transport, client._transport)
        self.assertEqual(second.call_tool("fake", "echo", {"text": "rebound"})["structured_content"], {"echo": "rebound"})

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
        self.assertIs(first.mcp._client, second.mcp._client)
        self.assertEqual(first.mcp._leases, second.mcp._leases)
        before = first.mcp._descriptions[0]["identity"]["connection"]["env"]
        after = second.mcp._descriptions[0]["identity"]["connection"]["env"]
        changed_names = [name for name in before.keys() | after.keys() if before.get(name) != after.get(name)]
        self.assertEqual(pid, second.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"], changed_names)
        self.assertEqual(plugin_pid, second.mcp.call_tool("example:extra", "identity", {})["structured_content"]["pid"])
        plugin_config["extra"]["env"] = {"TEST_MCP_TOKEN": "changed"}
        path.write_text(json.dumps(plugin_config), encoding="utf-8")
        third = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(second, third)
        self.assertEqual(second.mcp._leases["fake"], third.mcp._leases["fake"])
        self.assertNotEqual(second.mcp._leases["example:extra"], third.mcp._leases["example:extra"])
        self.assertEqual(pid, third.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"])
        self.assertNotEqual(plugin_pid, third.mcp.call_tool("example:extra", "identity", {})["structured_content"]["pid"])

    def test_global_binding_survives_plane_close_and_workspace_session_switch(self):
        import io
        from interfaces.bridge.bridge import Bridge

        bridge_configuration(self.root, self.root / "discovery.jsonl")
        path = self.root / "agent_config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcp"]["servers"]["fake"].update(scope="global", cwd=str(self.root))
        path.write_text(json.dumps(document), encoding="utf-8")
        with patch("interfaces.bridge.bridge.default_config_directory", return_value=self.root):
            bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
        self.addCleanup(bridge.close)
        bridge.open_session({"type": "open_session", "workspace": str(self.root), "session_id": None})
        first = bridge.host.planes.ensure(bridge.host.sessions)
        pid = first.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"]
        thread = bridge.host.mcp._transport._thread
        bridge.host.change_provider("second")
        time.sleep(2.1)  # Longer than this test Broker's one-second resource grace.
        other = self.root / "other"
        other.mkdir()
        bridge.open_session({"type": "open_session", "workspace": str(other), "session_id": None})
        second = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIsNot(first, second)
        self.assertIs(bridge.host.mcp._transport._thread, thread)
        self.assertEqual(first.mcp._leases, second.mcp._leases)
        self.assertEqual(pid, second.mcp.call_tool("fake", "identity", {})["structured_content"]["pid"])

    def test_failed_server_does_not_rebuild_plane_or_interrupt_healthy_server(self):
        import io
        from interfaces.bridge.bridge import Bridge

        bridge_configuration(self.root, self.root / "discovery.jsonl")
        path = self.root / "agent_config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcp"]["servers"].update({
            "bad": {"command": str(self.root / "missing-executable")},
            "healthy": {"command": sys.executable, "args": [FAKE_SERVER]},
        })
        path.write_text(json.dumps(document), encoding="utf-8")
        with patch("interfaces.bridge.bridge.default_config_directory", return_value=self.root):
            bridge = Bridge(io.StringIO(), io.StringIO(), start_scheduler=False)
        self.addCleanup(bridge.close)
        bridge.open_session({"type": "open_session", "workspace": str(self.root), "session_id": None})
        plane = bridge.host.planes.ensure(bridge.host.sessions)
        self.assertIs(plane, bridge.host.planes.ensure(bridge.host.sessions))
        with self.assertRaises(ConnectionError):
            plane.mcp.call_tool("fake", "crash", {})
        wait_for(lambda: plane.mcp._leases["fake"] in plane.mcp._unavailable)
        self.assertIs(plane, bridge.host.planes.ensure(bridge.host.sessions))
        self.assertEqual(plane.mcp.call_tool("healthy", "echo", {"text": "alive"})["structured_content"], {"echo": "alive"})

    def test_one_failed_initializer_keeps_other_servers_available(self):
        settings = load_mcp_config({"enabled": True, "servers": {
            "bad": {"command": str(self.root / "missing-executable")},
            "fake": {"command": sys.executable, "args": [FAKE_SERVER]},
        }})
        statuses = []
        client = McpClient(self.root, statuses.append)
        self.addCleanup(client.close)
        handle = client.bind(settings, self.root, "one")
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

    def test_server_failure_preserves_config_match_and_new_acquire_replaces_resource(self):
        first = self.handle()
        pid = first.call_tool("fake", "identity", {})["structured_content"]["pid"]
        with self.assertRaises(ConnectionError):
            first.call_tool("fake", "crash", {})
        wait_for(lambda: first._leases["fake"] in first._unavailable)
        self.assertTrue(first.matches(config(), self.root, "one"))
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
        other_workspace = self.root / "http-workspace"
        other_workspace.mkdir()
        global_first = self.handle(http(scope="global"))
        global_second = self.handle(http(scope="global"), workspace=other_workspace, session="global-two")
        global_credential = self.handle(http(scope="global", headers={"Authorization": "Bearer other"}))
        self.assertEqual(len(log.read_text().splitlines()), 5)
        self.assertEqual(global_second.call_tool("fake", "echo", {"text": "global"})["structured_content"], {"echo": "global"})
        for handle in (first, second, isolated, credential, global_first, global_second, global_credential):
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
        client = McpClient(other_directory)
        self.addCleanup(client.close)
        clean = client.bind(http(), self.root, "clean", environment=clean_environment)
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

    def test_separate_bridges_share_global_across_workspaces_and_survive_owner_exit(self):
        from concurrent.futures import ThreadPoolExecutor
        log = self.root / "calls.jsonl"
        bridge_configuration(self.root, log)
        path = self.root / "agent_config.json"
        document = json.loads(path.read_text(encoding="utf-8"))
        document["mcp"]["servers"]["fake"].update(scope="global", cwd=str(self.root))
        path.write_text(json.dumps(document), encoding="utf-8")
        workspaces = [self.root / "first-workspace", self.root / "second-workspace"]
        for workspace in workspaces:
            workspace.mkdir()
        processes = [subprocess.Popen(
            [sys.executable, "-c", BRIDGE_SCRIPT, str(self.root), str(workspace)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        ) for workspace in workspaces]
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
        from agent_runtime.mcp.broker import launch

        with tempfile.TemporaryDirectory() as directory, patch(
            "agent_runtime.mcp.broker.subprocess.Popen"
        ) as spawn:
            launch(Path(directory), 300)
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
                [sys.executable, "-c", (
                    "from functools import partial; from agent_runtime.mcp import transport; "
                    "transport._connect = partial(transport._connect, idle_seconds=1)\n"
                    + BRIDGE_SCRIPT
                ), str(root)],
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
                wait_for(lambda: not (root / "mcp" / "broker.json").exists(), timeout=15)
                # Wait for the detached process to close its log handle.
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
