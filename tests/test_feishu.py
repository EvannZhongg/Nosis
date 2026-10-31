import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from interfaces.mobile.config import ChannelConfig
from interfaces.mobile.feishu import FeishuTransport


class FeishuTransportTest(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        return FeishuTransport(ChannelConfig("feishu", "app", "secret", ("owner",)))

    async def test_conversations_run_concurrently_and_preserve_local_order(self):
        transport = self.transport()
        started, release, action = asyncio.Event(), asyncio.Event(), asyncio.Event()
        received = []

        async def message(chat, user, text, message_id, attachments):
            received.append(message_id)
            if message_id == "large":
                started.set()
                await release.wait()

        async def click(*args):
            action.set()

        transport.controller = SimpleNamespace(receive_message=message, receive_action=click)
        for chat, message_id in (("one", "large"), ("one", "next")):
            transport.inbound.put_nowait(dict(type="message", chat_id=chat, user_id="owner", message_id=message_id, text="", attachments=[]))
        transport.inbound.put_nowait(dict(type="action", chat_id="two", user_id="owner", message_id="card", token="token"))
        transport.inbound.put_nowait(None)
        task = asyncio.create_task(transport._dispatch())
        try:
            await asyncio.wait_for(started.wait(), 1)
            await asyncio.wait_for(action.wait(), 1)
            self.assertEqual(received, ["large"])
        finally:
            release.set()
            await task
        self.assertEqual(received, ["large", "next"])

    async def test_dispatch_cancellation_stops_conversation_workers(self):
        transport = self.transport()
        started, stopped = asyncio.Event(), asyncio.Event()

        async def message(*args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        transport.controller = SimpleNamespace(receive_message=message)
        transport.inbound.put_nowait(dict(type="message", chat_id="one", user_id="owner", message_id="large", text="", attachments=[]))
        task = asyncio.create_task(transport._dispatch())
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(stopped.is_set())

    async def test_supervisor_reconnects_and_recovers_after_process_exit(self):
        transport = self.transport()
        transport.controller = SimpleNamespace(recover=AsyncMock())
        transport.reader = asyncio.create_task(asyncio.sleep(0))
        transport.dispatcher = asyncio.create_task(asyncio.sleep(0))
        reconnected = asyncio.Event()

        async def connect():
            transport.reader = asyncio.create_task(asyncio.Event().wait())
            transport.connected = True
            reconnected.set()

        transport._disconnect = AsyncMock()
        transport._connect = AsyncMock(side_effect=connect)
        original_sleep = asyncio.sleep

        async def no_delay(seconds):
            await original_sleep(0)

        with patch("interfaces.mobile.feishu.asyncio.sleep", side_effect=no_delay):
            with self.assertLogs("interfaces.mobile.feishu", level="ERROR"):
                transport.supervisor = asyncio.create_task(transport._supervise())
                await asyncio.wait_for(reconnected.wait(), 1)
                await original_sleep(0)
        self.assertTrue(transport.connected)
        transport.controller.recover.assert_awaited_once()
        reader = transport.reader
        await transport.close()
        self.assertFalse(reader.cancelled())
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)

    async def test_dispatch_keeps_attachment_only_messages_and_their_origin(self):
        transport = self.transport()
        transport.controller = SimpleNamespace(receive_message=AsyncMock())
        attachments = [{"type": "image", "key": "key", "filename": "image"}]
        transport.inbound.put_nowait({"type": "message", "chat_id": "chat", "user_id": "owner", "message_id": "incoming", "text": "", "attachments": attachments})
        transport.inbound.put_nowait(None)
        await transport._dispatch()
        transport.controller.receive_message.assert_awaited_once_with("chat", "owner", "", "incoming", attachments)

    async def test_resource_commands_use_host_paths_and_explicit_media_types(self):
        transport = self.transport()
        transport._request = AsyncMock(return_value={"message_id": "sent"})
        path = Path("staged")
        await transport.download_attachment("incoming", {"type": "image", "key": "key"}, path)
        transport._request.assert_awaited_with("download_attachment", message_id="incoming", key="key", resource_type="image", path=str(path))
        result = await transport.send_attachment("chat", {"type": "file", "filename": "report.pdf"}, path)
        self.assertEqual(result, "sent")
        transport._request.assert_awaited_with("send_attachment", chat_id="chat", attachment_type="file", filename="report.pdf", path=str(path))
