"""Real stdio process tests with an in-memory model HTTP peer."""

import asyncio
import json
import tempfile
import sys
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

from agent_core import JsonlSessionStore, Workspace
from interfaces.gui.bridge_process import BridgeProcess
from tests.bridge_fixtures import open_session_message


class BridgeTransportTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_fragmented_utf8_checkpoints_keep_the_next_event(self):
        reader = asyncio.StreamReader()
        checkpoint = {"type": "channel_event", "channel_id": "one", "event": {
            "type": "session_ready", "event_sequence": 1,
            "transcript": {"items": [{"role": "user", "content": "续传" * 40000}]},
        }}
        following = {"type": "channel_closed", "channel_id": "one"}
        payload = (json.dumps(checkpoint, ensure_ascii=False) + "\n" + json.dumps(following) + "\n").encode()
        for start in range(0, len(payload), 4093):
            reader.feed_data(payload[start:start + 4093])
        reader.feed_eof()
        bridge = BridgeProcess(SimpleNamespace(stdout=reader, wait=AsyncMock()))
        self.assertEqual(await bridge.read(), checkpoint)
        self.assertEqual(await bridge.read(), following)
        self.assertIsNone(await bridge.read())


class ModelPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body["messages"]
        text = next(message["content"] for message in reversed(messages) if message["role"] == "user")
        if isinstance(text, list):
            text = "".join(part.get("text", "") for part in text)
        if text == "blocked":
            self.server.entered.set()
            self.server.release.wait(15)
        tool_result = next((message for message in reversed(messages) if message["role"] == "tool"), None)
        if text in {"approval", "read-workspace"} and tool_result is None:
            name, arguments = (
                ("read_file", {"path": "marker.txt"}) if text == "read-workspace"
                else ("shell", {"command": "echo should-not-execute"})
            )
            delta = {"tool_calls": [{"index": 0, "id": "call", "type": "function", "function": {
                "name": name, "arguments": json.dumps(arguments),
            }}]}
            finish = "tool_calls"
        else:
            delta = {"content": f"reply:{tool_result['content'] if text == 'read-workspace' else text}"}
            finish = "stop"
        chunks = [
            {"id": "chat", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5",
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "chat", "object": "chat.completion.chunk", "created": 1, "model": "gpt-5",
             "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
        ]
        payload = ("".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks) + "data: [DONE]\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass


class MultiplexBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nosis-multiplex-")
        self.root = Path(self.temporary.name)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), ModelPeer)
        self.http.entered = Event()
        self.http.release = Event()
        self.http_thread = Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()
        self.opening = open_session_message(self.root, provider_config={
            "main_agent": {"provider": "native"},
            "fast_model": {"provider": "native"},
            "subagent": {"provider": "native"},
            "providers": {"native": {
                "model": "gpt-5", "api": "chat_completions", "key": "test-key",
                "url": f"http://127.0.0.1:{self.http.server_port}/v1",
            }},
        })
        path = self.root / "agent_config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config.update(provider={"request_timeout_seconds": 5, "max_retries": 0}, memory={"enabled": False})
        config["main_agent"]["tools"]["shell"] = True
        config["main_agent"]["tools"]["read_file"] = True
        path.write_text(json.dumps(config), encoding="utf-8")
        self.bridge = await BridgeProcess.spawn(Workspace(self.root), self.root)

    async def asyncTearDown(self):
        self.http.release.set()
        await self.bridge.close()
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(2)
        self.temporary.cleanup()

    async def until(self, channel, kind):
        events = []
        while True:
            event = await asyncio.wait_for(channel.read(), 15)
            self.assertIsNotNone(event, events)
            events.append(event)
            self.assertNotEqual(event["type"], "fatal", event)
            if event["type"] == kind:
                return event, events

    async def open(self, key, session_id=None):
        channel = self.bridge.open_channel(key)
        channel.send({**self.opening, "session_id": session_id or key})
        ready, _ = await self.until(channel, "session_ready")
        self.assertEqual(ready["protocol_version"], 3)
        await self.until(channel, "runtime_state")
        return channel

    async def test_concurrent_requests_and_cancellation_are_isolated(self):
        first, second = await asyncio.gather(self.open("one"), self.open("two"))
        first.send({"type": "user_turn", "turn_id": "same", "text": "blocked"})
        self.assertTrue(await asyncio.to_thread(self.http.entered.wait, 10))
        second.send({"type": "user_turn", "turn_id": "same", "text": "second"})
        _, second_events = await self.until(second, "turn_completed")
        self.assertTrue(any(event.get("text") == "reply:second" for event in second_events))
        first.send({"type": "cancel", "turn_id": "same"})
        await self.until(first, "turn_cancelled")
        await first.close()
        self.assertIsNone(self.bridge.returncode)
        second.send({"type": "user_turn", "turn_id": "next", "text": "still-alive"})
        await self.until(second, "turn_completed")
        await second.close()
        store = JsonlSessionStore(self.root / "sessions")
        self.assertEqual(store.load("one").turns["same"].status, "cancelled")
        self.assertEqual(store.load("two").turns["same"].status, "completed")
        self.assertEqual([item.content for item in store.load("two").items if item.role == "user"], ["second", "still-alive"])

    async def test_identical_approval_ids_do_not_cross_session_boundaries(self):
        first, second = await asyncio.gather(self.open("one"), self.open("two"))
        for channel in (first, second):
            channel.send({"type": "user_turn", "turn_id": "same", "text": "approval"})
        a, _ = await self.until(first, "approval_request")
        b, _ = await self.until(second, "approval_request")
        self.assertEqual(a["request_id"], b["request_id"])
        second.send({"type": "approval_response", "request_id": b["request_id"], "approved": False})
        await self.until(second, "turn_completed")
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(first.read(), 0.1)
        first.send({"type": "cancel", "turn_id": "same"})
        await self.until(first, "turn_cancelled")

    async def test_writer_conflict_fails_only_the_contending_channel(self):
        owner = await self.open("owner", "shared")
        contender = self.bridge.open_channel("contender")
        contender.send({**self.opening, "session_id": "shared"})
        error, _ = await self.until(contender, "session_open_failed")
        self.assertEqual(error["error"]["type"], "SessionInUseError")
        await contender.close()
        owner.send({"type": "user_turn", "turn_id": "t", "text": "owner"})
        await self.until(owner, "turn_completed")
        await owner.close()
        reopened = await self.open("owner", "shared")
        reopened.send({"type": "user_turn", "turn_id": "next", "text": "reopened"})
        await self.until(reopened, "turn_completed")

    async def test_process_failure_reaches_every_channel(self):
        first, second = await asyncio.gather(self.open("one"), self.open("two"))
        self.bridge._kill()
        for channel in (first, second):
            event = await asyncio.wait_for(channel.read(), 10)
            self.assertEqual(event["type"], "fatal")
            self.assertEqual(event["event_sequence"], 3)
            self.assertIsNone(await channel.read())
            await channel.close()

    async def test_workspace_relative_tools_use_their_own_session_root(self):
        channels = []
        for name in ("alpha", "beta"):
            workspace = self.root / name
            workspace.mkdir()
            (workspace / "marker.txt").write_text(name, encoding="utf-8")
            channel = self.bridge.open_channel(name)
            channel.send({**self.opening, "session_id": name, "workspace": str(workspace)})
            await self.until(channel, "runtime_state")
            channel.send({"type": "user_turn", "turn_id": "t", "text": "read-workspace"})
            channels.append(channel)
        for name, channel in zip(("alpha", "beta"), channels):
            _, events = await self.until(channel, "turn_completed")
            answer = "".join(event.get("text", "") for event in events if event["type"] == "assistant_delta")
            self.assertIn(name, answer)
            self.assertNotIn("beta" if name == "alpha" else "alpha", answer)

    async def test_channel_fatal_does_not_close_its_peers(self):
        first, second = await asyncio.gather(self.open("one"), self.open("two"))
        first.send({"type": "provider_set", "provider": "not-configured"})
        failed = await asyncio.wait_for(first.read(), 10)
        self.assertEqual(failed["type"], "fatal")
        await first.close()
        second.send({"type": "user_turn", "turn_id": "t", "text": "unaffected"})
        await self.until(second, "turn_completed")
        self.assertIsNone(self.bridge.returncode)

    async def test_cancel_during_mcp_startup_does_not_wait_for_its_timeout(self):
        path = self.root / "agent_config.json"
        config = json.loads(path.read_text(encoding="utf-8"))
        config["mcp"] = {"enabled": True, "servers": {"slow": {
            "command": sys.executable, "args": ["-c", "import time; time.sleep(30)"],
            "startup_timeout_seconds": 30,
        }}}
        path.write_text(json.dumps(config), encoding="utf-8")
        channel = await self.open("slow")
        channel.send({"type": "user_turn", "turn_id": "t", "text": "waiting"})
        await self.until(channel, "mcp_server_status")
        started = time.perf_counter()
        channel.send({"type": "cancel", "turn_id": "t"})
        await self.until(channel, "turn_cancelled")
        await channel.close()
        self.assertLess(time.perf_counter() - started, 5)
        self.assertIsNone(self.bridge.returncode)
