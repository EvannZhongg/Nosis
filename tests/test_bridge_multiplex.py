"""Real stdio process tests with an in-memory model HTTP peer."""

import asyncio
import json
import os
import tempfile
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent_core import IntervalTrigger, JsonlSessionStore, OneShotTrigger, SchedulerService, Workspace
from interfaces.application_host import application_lock
from interfaces.bridge_process import BridgeProcess
from interfaces.local_bridge import ENDPOINT_FILENAME, LocalBridge
from interfaces.session_host import ActiveSessionRegistry
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
        self.server.requests.append((text, [tool["function"]["name"] for tool in body.get("tools", [])]))
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
        # Loopback HTTP peers must not be sent through a developer's system proxy.
        self.enterContext(patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost"}))
        self.temporary = tempfile.TemporaryDirectory(prefix="nosis-multiplex-")
        self.root = Path(self.temporary.name)
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), ModelPeer)
        self.http.entered = Event()
        self.http.release = Event()
        self.http.requests = []
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
            self.assertNotEqual(event["type"], "turn_failed", event)
            if event["type"] == kind:
                return event, events

    async def open(self, session_id):
        channel = self.bridge.open_channel(session_id)
        channel.send({**self.opening, "session_id": session_id})
        ready, _ = await self.until(channel, "session_ready")
        self.assertEqual(ready["protocol_version"], 5)
        await self.until(channel, "runtime_state")
        return channel

    def schedule(self, session_id, prompt, *, trigger=None):
        service = SchedulerService(self.root / "schedule.jsonl")
        now = datetime.now(timezone.utc)
        schedule = service.create_schedule(
            trigger=trigger or OneShotTrigger(now), prompt=prompt,
            workspace=str(self.root), origin_session_id="origin",
            schedule_session_id=session_id,
        )
        return service, schedule

    async def wait_for_run(self, store, schedule, status):
        async with asyncio.timeout(12):
            while True:
                for run in store.runs:
                    if run.schedule_id == schedule.schedule_id and run.status == status:
                        return run
                await asyncio.sleep(0.025)

    async def test_due_schedule_reuses_the_idle_interactive_session(self):
        channel = await self.open("shared")
        channel.send({"type": "user_turn", "turn_id": "warm", "text": "warm"})
        await self.until(channel, "turn_completed")
        store, schedule = self.schedule("shared", "scheduled")
        started, events = await self.until(channel, "scheduled_turn_started")
        self.assertEqual(started["text"], "scheduled")
        done, events = await self.until(channel, "turn_completed")
        run = await self.wait_for_run(store, schedule, "completed")
        self.assertEqual(started["turn_id"], run.turn_id)
        self.assertEqual(done["turn_id"], run.turn_id)
        self.assertFalse(any(event.get("phase") == "starting" for event in events))
        session = JsonlSessionStore(self.root / "sessions").load("shared")
        self.assertEqual([item.content for item in session.items if item.role == "user"], ["warm", "scheduled"])

    async def test_schedule_queues_in_fifo_order_and_skips_overlapping_occurrences(self):
        channel = await self.open("shared")
        channel.send({"type": "user_turn", "turn_id": "first", "text": "blocked"})
        self.assertTrue(await asyncio.to_thread(self.http.entered.wait, 8))
        store, schedule = self.schedule("shared", "scheduled", trigger=IntervalTrigger(
            1, datetime.now(timezone.utc) + timedelta(milliseconds=100),
        ))
        queued = await self.wait_for_run(store, schedule, "queued")
        self.assertIsNone(queued.started_at)
        skipped = await self.wait_for_run(store, schedule, "skipped")
        self.assertIn("queued or running", skipped.error)
        channel.send({"type": "user_turn", "turn_id": "last", "text": "last"})
        self.assertFalse(await channel.close(idle_only=True))
        store.update_schedule(schedule.schedule_id, enabled=False)
        self.http.release.set()
        for turn_id in ("first", queued.turn_id, "last"):
            done, _ = await self.until(channel, "turn_completed")
            self.assertEqual(done["turn_id"], turn_id)
        complete = await self.wait_for_run(store, schedule, "completed")
        self.assertEqual(complete.run_id, queued.run_id)
        self.assertGreater(complete.started_at, complete.scheduled_for)

    async def test_unattended_turn_keeps_approval_and_questions_disabled_on_warm_runtime(self):
        channel = await self.open("shared")
        channel.send({"type": "user_turn", "turn_id": "warm", "text": "warm"})
        await self.until(channel, "turn_completed")
        store, schedule = self.schedule("shared", "approval")
        _, events = await self.until(channel, "turn_completed")
        self.assertFalse(any(event["type"] == "approval_request" for event in events))
        result = next(event for event in events if event["type"] == "tool_result")
        self.assertFalse(result["ok"])
        await self.wait_for_run(store, schedule, "completed")
        channel.send({"type": "user_turn", "turn_id": "next", "text": "next"})
        await self.until(channel, "turn_completed")
        shown = dict(self.http.requests)
        self.assertIn("ask_user", shown["warm"])
        self.assertNotIn("ask_user", shown["approval"])
        self.assertIn("ask_user", shown["next"])

    async def test_stopping_bridge_cancels_the_queued_schedule(self):
        channel = await self.open("shared")
        channel.send({"type": "user_turn", "turn_id": "first", "text": "blocked"})
        self.assertTrue(await asyncio.to_thread(self.http.entered.wait, 8))
        store, schedule = self.schedule("shared", "queued")
        queued = await self.wait_for_run(store, schedule, "queued")
        await self.bridge.close()
        cancelled = await self.wait_for_run(store, schedule, "cancelled")
        self.assertEqual(cancelled.run_id, queued.run_id)
        self.assertIsNone(cancelled.started_at)

    async def test_managed_bridge_cannot_start_over_an_independent_owner(self):
        with application_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "close the standalone Bridge"):
                await BridgeProcess.spawn(Workspace(self.root), self.root, on_session=lambda channel: None)
            channel = await self.open("standalone")
            channel.send({"type": "user_turn", "turn_id": "turn", "text": "still-alive"})
            await self.until(channel, "turn_completed")

    async def test_tui_proxy_attaches_to_the_managed_scheduler_session(self):
        await self.bridge.close()
        workspace = Workspace(self.root)
        store = JsonlSessionStore(self.root / "sessions")
        registry = ActiveSessionRegistry(store, workspace, self.root)
        local = LocalBridge(self.root, registry)
        with application_lock(self.root):
            await registry.start()
            try:
                await local.start()
                endpoint = json.loads((self.root / ENDPOINT_FILENAME).read_text(encoding="utf-8"))
                if os.name == "nt":
                    self.assertTrue(endpoint["address"].startswith(r"\\.\pipe\nosis-"))
                else:
                    self.assertTrue(Path(endpoint["address"]).is_socket())
                schedules, schedule = self.schedule("scheduled", "scheduled-first")
                await self.wait_for_run(schedules, schedule, "completed")
                runtime = await registry.get("scheduled")
                self.assertIsNotNone(runtime)

                terminal = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "interfaces.bridge", "--config-directory", str(self.root),
                    cwd=self.root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                )
                try:
                    async def read():
                        return json.loads(await asyncio.wait_for(terminal.stdout.readline(), 10))

                    self.assertEqual((await read())["type"], "bridge_ready")
                    terminal.stdin.write((json.dumps({
                        "type": "channel_command", "channel_id": "scheduled",
                        "command": {**self.opening, "session_id": "scheduled"},
                    }) + "\n").encode())
                    ready = (await read())["event"]
                    self.assertEqual(ready["type"], "session_ready")
                    self.assertTrue(ready["replayed"])
                    self.assertEqual(ready["event_sequence"], runtime.ready["event_sequence"])
                    self.assertEqual(ready["transcript"]["event_sequence"], runtime.transcript["event_sequence"])
                    self.assertEqual(ready["transcript"]["items"][0]["content"], "scheduled-first")
                    state = (await read())["event"]
                    self.assertEqual(state["phase"], "idle")
                    self.assertIs(await registry.get("scheduled"), runtime)
                    self.assertEqual(state["provider"], "native")
                    terminal.stdin.write((json.dumps({
                        "type": "channel_command", "channel_id": "scheduled",
                        "command": {**self.opening, "session_id": "scheduled"},
                    }) + "\n").encode())
                    duplicate = (await read())["event"]
                    self.assertEqual(duplicate["type"], "session_open_failed")
                    self.assertEqual(duplicate["error"]["type"], "SessionAlreadyOpenError")
                    self.assertIs(await registry.get("scheduled"), runtime)
                    terminal.stdin.write((json.dumps({
                        "type": "channel_command", "channel_id": "scheduled",
                        "command": {"type": "user_turn", "turn_id": "tui", "text": "blocked"},
                    }) + "\n").encode())
                    self.assertTrue(await asyncio.to_thread(self.http.entered.wait, 8))
                    terminal.stdin.write(b'{"type":"close_channel","channel_id":"scheduled","idle_only":true}\n')
                    while (packet := await read())["type"] == "channel_event":
                        self.assertNotEqual(packet["event"]["type"], "fatal")
                    self.assertEqual(packet, {"type": "channel_busy", "channel_id": "scheduled"})
                    self.http.release.set()
                    while (packet := await read())["event"]["type"] != "turn_completed":
                        self.assertNotIn(packet["event"]["type"], {"fatal", "turn_failed", "session_open_failed"})
                    self.assertEqual(packet["event"]["turn_id"], "tui")
                    self.assertEqual(packet["event"]["event_sequence"], runtime.event_sequence)
                    terminal.stdin.write(b'{"type":"close_channel","channel_id":"scheduled"}\n')
                    self.assertEqual(await read(), {"type": "channel_closed", "channel_id": "scheduled"})
                    self.assertIs(await registry.get("scheduled"), runtime)
                    self.assertEqual(store.load("scheduled").turns["tui"].status, "completed")
                    terminal.stdin.write((json.dumps({
                        "type": "channel_command", "channel_id": "scheduled",
                        "command": {**self.opening, "session_id": "scheduled"},
                    }) + "\n").encode())
                    self.assertTrue((await read())["event"]["replayed"])
                    self.assertEqual((await read())["event"]["type"], "runtime_state")
                    terminal.stdin.write(b'{"type":"close_channel","channel_id":"scheduled","idle_only":true}\n')
                    self.assertEqual(await read(), {"type": "channel_closed", "channel_id": "scheduled"})
                    terminal.stdin.write(b'{"type":"channel_command","channel_id":"unopened",'
                                         b'"command":{"type":"user_turn","turn_id":"t","text":"ignored"}}\n')
                    fatal = await read()
                    self.assertEqual(fatal["type"], "fatal")
                    self.assertEqual(fatal["error"]["type"], "ProtocolError")
                    self.assertEqual(await asyncio.wait_for(terminal.stdout.readline(), 10), b"")
                    await asyncio.wait_for(terminal.wait(), 10)
                    self.assertIsNone(await registry.get("scheduled"))
                finally:
                    if terminal.returncode is None:
                        terminal.kill()
                        await terminal.wait()
            finally:
                await local.close()
                await registry.close_all()
            self.assertFalse((self.root / ENDPOINT_FILENAME).exists())

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

    async def test_mismatched_session_id_fails_only_the_contending_channel(self):
        owner = await self.open("shared")
        contender = self.bridge.open_channel("contender")
        contender.send({**self.opening, "session_id": "shared"})
        error, _ = await self.until(contender, "session_open_failed")
        self.assertIn("must match channel_id", error["error"]["message"])
        await contender.close()
        owner.send({"type": "user_turn", "turn_id": "t", "text": "owner"})
        await self.until(owner, "turn_completed")
        await owner.close()
        reopened = await self.open("shared")
        reopened.send({"type": "user_turn", "turn_id": "next", "text": "reopened"})
        await self.until(reopened, "turn_completed")

    async def test_repeated_open_is_rejected_without_waiting_for_the_active_turn(self):
        store, schedule = self.schedule("shared", "scheduled-first")
        await self.wait_for_run(store, schedule, "completed")
        channel = await self.open("shared")
        channel.send({"type": "user_turn", "turn_id": "t", "text": "blocked"})
        self.assertTrue(await asyncio.to_thread(self.http.entered.wait, 8))
        channel.send({**self.opening, "session_id": "shared"})
        error, _ = await self.until(channel, "session_open_failed")
        self.assertEqual(error["error"]["type"], "SessionAlreadyOpenError")
        self.assertIn("session_snapshot", error["error"]["message"])
        self.assertEqual(error["runtime"]["turn_id"], "t")
        self.http.release.set()
        await self.until(channel, "turn_completed")
        channel.send({"type": "session_snapshot"})
        await self.until(channel, "session_ready")
        await self.until(channel, "runtime_state")

    async def test_unopened_session_reports_one_channel_fatal(self):
        channel = self.bridge.open_channel("unopened")
        channel.send({**self.opening, "session_id": "unopened", "workspace": str(self.root / "missing")})
        await self.until(channel, "session_open_failed")
        channel.send({"type": "user_turn", "turn_id": "t", "text": "ignored"})
        self.assertEqual((await asyncio.wait_for(channel.read(), 10))["type"], "user_turn_received")
        failure = await asyncio.wait_for(channel.read(), 10)
        self.assertEqual(failure["type"], "fatal")
        self.assertEqual(failure["error"]["type"], "ProtocolError")
        self.assertIsNone(await asyncio.wait_for(channel.read(), 10))
        self.assertIsNone(self.bridge.returncode)

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
