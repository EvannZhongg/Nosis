import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from interfaces.mobile.config import ChannelConfig
from interfaces.mobile.feishu import FeishuTransport


class FeishuTransportTest(unittest.IsolatedAsyncioTestCase):
    def transport(self):
        return FeishuTransport(ChannelConfig("feishu", "app", "secret", ("owner",)))

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
